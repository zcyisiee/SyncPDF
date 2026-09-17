"""``bdt serve`` 草稿编译：隔离副本、原子发布、失败不破坏上一版、防抖（W09）。

编译链**不 mock**：隔离副本创建、草稿物化、源 PDF 重指、argv 构造、子进程调度、取消、
发布、清理全部走真代码；只有最后那一层 ``bdt run`` 被换成 stub 脚本（argv 层面），
因为真 build 需要 LaTeX + 完整 IR（分钟级），而本轮验收点是 serve 层的隔离/发布/回滚/
防抖/并发语义。真 build 的那一条在 ``tmp/`` 里的真实 workdir 副本上做冒烟（见 W09 报告）。

W12 的版本归档（发布成功后归档 + 失败/取消不归档 + ``trigger``）也在这里覆盖：归档钩子
长在 ``settle_compile`` 的成功分支上，用同一套 stub 才验得动。

stub 脚本用 ``control/mode`` 控制四种结局：

- ``ok``：写 PDF + ``ok=true`` 信封 + exit 0；
- ``quality_fail``：写 PDF + ``ok=false``（``check_needs_fix``）+ exit 1 —— 这就是真实
  ``bdt run --from apply`` 在质量门禁不过时的样子，build 其实成功了；
- ``build_fail``：不写 PDF + ``ok=false``（``tool_exception``）+ exit 1；
- ``sleep``：长睡（验取消 / 活动期间编辑只读）。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import pickle
import shutil
import signal
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="serve 需要 web extra: fastapi")

from babeldoc_tools.serve import compile as compile_mod  # noqa: E402
from babeldoc_tools.serve.app import create_app  # noqa: E402
from babeldoc_tools.serve.compile import compile_state_path  # noqa: E402
from babeldoc_tools.serve.compile import read_compile_state  # noqa: E402
from babeldoc_tools.serve.draft import DraftStore  # noqa: E402
from babeldoc_tools.serve.jobs import JobRecord  # noqa: E402
from babeldoc_tools.serve.jobs import pid_alive  # noqa: E402
from babeldoc_tools.serve.jobs import utc_now  # noqa: E402
from babeldoc_tools.serve.schemas import API_PREFIX as API  # noqa: E402
from babeldoc_tools.serve.store import STATE_DIR  # noqa: E402
from babeldoc_tools.serve.store import DocumentStore  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PID = "P05-002"
#: 真 workdir 里的产物名：stub 写进副本 ``output/``，发布后同名出现在真 workdir。
PDF_NAME = "paper.no_watermark.zh.mono.pdf"
PDF_OK = b"PDF-ok\n"
PDF_QUALITY = b"PDF-quality\n"

#: 编译 stub（argv 层面替换 ``bdt run``；不联网、不编译，只模拟收尾行为）。
COMPILE_STUB = """#!/bin/sh
# W09 编译测试 stub：$1 = control 目录，$2 = 隔离副本目录（也是子进程 cwd）。
set -eu
ctl="$1"
wd="$2"
mode="$(cat "$ctl/mode")"
emit() { printf '%s\\n' "$1"; }
mkdir -p "$wd/output"
case "$mode" in
  ok)
    printf 'PDF-ok\\n' > "$wd/output/paper.no_watermark.zh.mono.pdf"
    emit '{"ok": true, "data": {"stages": [{"stage": "apply", "status": "ok"}, {"stage": "build", "status": "ok"}]}}'
    exit 0
    ;;
  quality_fail)
    printf 'PDF-quality\\n' > "$wd/output/paper.no_watermark.zh.mono.pdf"
    emit '{"ok": false, "error": {"code": "check_needs_fix", "message": "check verdict=needs_fix"}, "data": {"stages": [{"stage": "apply", "status": "ok"}, {"stage": "build", "status": "ok"}, {"stage": "check", "status": "ok"}]}}'
    exit 1
    ;;
  build_fail)
    emit '{"ok": false, "error": {"code": "tool_exception", "message": "build 期间崩了"}, "data": {"stages": [{"stage": "apply", "status": "ok"}, {"stage": "build", "status": "failed"}]}}'
    exit 1
    ;;
  build_ok_no_pdf)
    emit '{"ok": true, "data": {"stages": [{"stage": "build", "status": "ok"}]}}'
    exit 0
    ;;
  no_envelope)
    printf 'PDF-noenvelope\\n' > "$wd/output/paper.no_watermark.zh.mono.pdf"
    printf '这不是 JSON\\n'
    exit 1
    ;;
  sleep)
    sleep 300
    ;;
  *)
    printf 'unknown mode %s\\n' "$mode" >&2
    exit 2
    ;;
esac
"""

#: 轮询步长 / 默认上限（所有等待都是固定步数封顶，不会挂死）。
POLL_SECONDS = 0.1
WAIT_SECONDS = 30.0


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _make_workdir(root: Path, did: str) -> Path:
    """最小可编译 workdir：agent/ 全套 + 源 PDF + 供解析的 state.pkl。"""
    workdir = root / did
    agent = workdir / "agent"
    agent.mkdir(parents=True)
    (agent / "translated.md").write_text(
        f"<!--MD_HEADER-->\n\n<!-- id={PID} label=text -->\n原译文\n", encoding="utf-8"
    )
    (agent / "anchors.json").write_text(
        json.dumps({"rows": [{"id": PID, "layout_label": "text"}]}), encoding="utf-8"
    )
    (agent / "document.md").write_text(f"<!--{PID}-->\nsource\n", encoding="utf-8")
    (agent / "run_state.json").write_text("{}\n", encoding="utf-8")
    (workdir / "input.pdf").write_bytes(b"%PDF-1.4 stub source\n")
    # state.pkl 只需要是 dict（prepare 会读 temp_pdf_path/pdf_path 再重新 pickle）。
    (agent / "state.pkl").write_bytes(
        pickle.dumps(
            {
                "pdf_path": "paper.pdf",
                "temp_pdf_path": str(workdir / "input.pdf"),
                "lang_in": "en",
                "lang_out": "zh",
            }
        )
    )
    (workdir / "output").mkdir()
    return workdir


@pytest.fixture
def root(tmp_path: Path) -> Path:
    base = tmp_path / "root"
    _make_workdir(base, "alpha")
    _make_workdir(base, "beta")
    return base


@pytest.fixture
def control(tmp_path: Path) -> Path:
    """stub 的 control 目录（``mode`` 文件决定这一次编译的结局）。"""
    path = tmp_path / "control"
    path.mkdir()
    return path


def set_mode(control: Path, mode: str) -> None:
    (control / "mode").write_text(mode, encoding="utf-8")


@pytest.fixture
def stub(tmp_path: Path, control: Path, monkeypatch) -> Path:
    """把 compile 的 argv 换成 stub 脚本（其余服务端代码一行不改）。"""
    script = tmp_path / "compile-stub.sh"
    script.write_text(COMPILE_STUB, encoding="utf-8")
    script.chmod(0o755)
    set_mode(control, "ok")

    def fake_argv(isolated: Path) -> list[str]:
        return [str(script), str(control), str(isolated)]

    monkeypatch.setattr(compile_mod, "build_compile_argv", fake_argv)
    # 防抖默认拉长：绝大多数用例自己显式 POST compile（防抖用例自己改短）。
    monkeypatch.setattr(compile_mod, "DEBOUNCE_SECONDS", 60.0)
    return script


def _shutdown(root: Path, *, timeout: float = 20.0) -> None:
    """测试收尾：SIGKILL 掉还活着的 job 进程组（stub 可能还在睡 300s）。"""
    groups = []
    for snapshot in sorted((root / STATE_DIR / "jobs").glob("j_*.json")):
        record = json.loads(snapshot.read_text(encoding="utf-8"))
        pgid = record.get("pgid")
        if record.get("status") == "running" and pgid and pid_alive(pgid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGKILL)
            groups.append(pgid)
    deadline = time.monotonic() + timeout
    while groups and time.monotonic() < deadline:
        groups = [pgid for pgid in groups if pid_alive(pgid)]
        if groups:
            time.sleep(0.05)


@pytest.fixture
def client(root: Path, stub: Path):
    # stub 必须在 app 建起来之前装好（argv 替换是全局 monkeypatch）；
    # 显式依赖它，而不是靠 import 顺序。
    assert stub.is_file()
    with TestClient(create_app(DocumentStore.for_root(root))) as test_client:
        yield test_client
        _shutdown(root)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def patch_draft(client, target: str = "改后的译文", *, base: int, layout=None) -> dict:
    entry: dict = {"target": target}
    if layout is not None:
        entry["layout"] = layout
    response = client.patch(
        f"{API}/documents/alpha/draft",
        json={"base_revision": base, "paragraphs": {PID: entry}},
    )
    assert response.status_code == 200, response.text
    return response.json()


def post_compile(client, did: str = "alpha", **body) -> tuple[int, dict]:
    response = client.post(f"{API}/documents/{did}/jobs", json={"action": "compile", **body})
    return response.status_code, response.json()


def start_compile(client, did: str = "alpha", **body) -> str:
    status, payload = post_compile(client, did, **body)
    assert status == 202, payload
    return payload["job_id"]


def get_job(client, job_id: str) -> dict:
    response = client.get(f"{API}/jobs/{job_id}")
    assert response.status_code == 200, response.text
    return response.json()


def wait_job(
    client, job_id: str, statuses: set[str], *, timeout: float = WAIT_SECONDS
) -> dict:
    """轮询 ``GET /jobs/{jid}`` 直到落进 ``statuses``；超时带诊断失败。"""
    record = get_job(client, job_id)
    for _ in range(max(1, int(timeout / POLL_SECONDS))):
        if record["status"] in statuses:
            return record
        time.sleep(POLL_SECONDS)
        record = get_job(client, job_id)
    raise AssertionError(
        f"job 停在 {record['status']}（期望 {sorted(statuses)}）："
        f"{json.dumps(record, ensure_ascii=False)[:1200]}"
    )


def wait_compile_status(
    client, did: str = "alpha", statuses: set[str] = frozenset({"ok"}), *, timeout=WAIT_SECONDS
) -> dict:
    """轮询详情端点的 ``compile`` 字段直到落进 ``statuses``。"""
    body = client.get(f"{API}/documents/{did}").json()
    for _ in range(max(1, int(timeout / POLL_SECONDS))):
        if body["compile"]["status"] in statuses:
            return body
        time.sleep(POLL_SECONDS)
        body = client.get(f"{API}/documents/{did}").json()
    raise AssertionError(
        f"compile.status 停在 {body['compile']['status']}（期望 {sorted(statuses)}）"
    )


def workdir(root: Path, did: str = "alpha") -> Path:
    return root / did


def output_pdf(root: Path, did: str = "alpha") -> Path:
    return workdir(root, did) / "output" / PDF_NAME


def isolated_dir(root: Path, job_id: str, did: str = "alpha") -> Path:
    return workdir(root, did) / STATE_DIR / f"compile-{job_id}"


def compile_jobs(client, did: str = "alpha") -> list[dict]:
    response = client.get(f"{API}/documents/{did}/jobs")
    assert response.status_code == 200, response.text
    return [job for job in response.json() if job["action"] == "compile"]


def detail(client, did: str = "alpha") -> dict:
    response = client.get(f"{API}/documents/{did}")
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# 成功路径：隔离副本 → 发布 → 清理
# --------------------------------------------------------------------------- #
def test_compile_publishes_new_pdf_and_records_revision(client, root):
    patch_draft(client, base=0, layout={"scale_cap": 0.9})
    job_id = start_compile(client)
    record = wait_job(client, job_id, {"succeeded"})

    assert record["action"] == "compile"
    assert record["from_stage"] == "apply"
    assert record["profile"] is None  # compile 不需要 provider
    assert record["run_id"] is None  # 归档随隔离副本删掉，不报虚幻的 run
    assert output_pdf(root).read_bytes() == PDF_OK

    state = read_compile_state(workdir(root))
    assert state["status"] == "ok"
    assert state["revision"] == 1  # 与捕获的草稿 revision 一致
    assert state["artifact"]["name"] == PDF_NAME
    assert state["artifact"]["revision"] == 1
    assert state["artifact"]["size"] == len(PDF_OK)
    # 隔离目录删除（失败也删，见后面的用例）
    assert not isolated_dir(root, job_id).exists()
    # 真 workdir 的 agent/ 没被编译过程污染（草稿只写进副本）
    assert not (workdir(root) / "agent" / "layout_overrides.json").exists()


def test_compile_publishes_even_when_quality_gate_fails(client, root, control):
    """build 成功但 check/review 门禁不过（真实 ``--from apply`` 的样子）→ 仍然发布。"""
    set_mode(control, "quality_fail")
    job_id = start_compile(client)
    record = wait_job(client, job_id, {"succeeded"})

    # 门禁结论留在信封里（诊断用），但 job 不算失败：编译成功 ≠ 质量通过
    assert "check_needs_fix" in (record["envelope"] or "")
    assert record["exit_code"] == 1
    assert output_pdf(root).read_bytes() == PDF_QUALITY
    assert read_compile_state(workdir(root))["status"] == "ok"
    # 质量门禁失败不得把 pipeline_ok 拉绿
    assert detail(client)["quality"]["pipeline_ok"] is False


def test_compile_materialises_draft_into_the_copy_only(client, root):
    """草稿物化只发生在副本里：真 workdir 的 translated.md / 覆盖文件不受影响。"""
    before = (workdir(root) / "agent" / "translated.md").read_text(encoding="utf-8")
    patch_draft(client, "草稿译文", base=0, layout={"font_scale": 1.1})
    job_id = start_compile(client)
    wait_job(client, job_id, {"succeeded"})
    assert (workdir(root) / "agent" / "translated.md").read_text(encoding="utf-8") == before


# --------------------------------------------------------------------------- #
# 失败路径：绝不破坏上一版 PDF
# --------------------------------------------------------------------------- #
def test_build_failure_keeps_previous_pdf(client, root, control):
    patch_draft(client, base=0)
    first = start_compile(client)
    wait_job(client, first, {"succeeded"})
    assert output_pdf(root).read_bytes() == PDF_OK

    # 草稿再改一版 + 编译失败
    patch_draft(client, "第二版", base=1)
    set_mode(control, "build_fail")
    second = start_compile(client)
    record = wait_job(client, second, {"failed"})

    assert record["error_code"] == "tool_exception"
    assert "build 期间崩了" in (record["error_message"] or "")
    # 上一版 PDF 一个字节都没动，还能下载
    assert output_pdf(root).read_bytes() == PDF_OK
    state = read_compile_state(workdir(root))
    assert state["status"] == "failed"
    assert state["error_code"] == "tool_exception"
    assert state["revision"] == 1  # revision/artifact 仍指上一版成功产物
    assert state["artifact"]["revision"] == 1
    assert not isolated_dir(root, second).exists()


def test_failed_compile_marks_artifact_stale_in_detail(client, control):
    patch_draft(client, base=0)
    wait_job(client, start_compile(client), {"succeeded"})
    assert detail(client)["compile"]["stale"] is False

    patch_draft(client, "第二版", base=1)  # 草稿又变了 → 已编译的那版就旧了
    body = detail(client)
    assert body["compile"]["status"] == "ok"
    assert body["compile"]["revision"] == 1
    assert body["compile"]["stale"] is True

    set_mode(control, "build_fail")
    wait_job(client, start_compile(client), {"failed"})
    body = detail(client)
    assert body["compile"]["status"] == "failed"
    assert body["compile"]["stale"] is True  # 旧 PDF 仍在，但明确标成过期
    assert body["compile"]["artifact"]["revision"] == 1


def test_unparsed_envelope_never_publishes(client, root, control):
    """stub 写了 PDF 但信封不可解析：**不发布**（判据必须两条同时满足）。"""
    set_mode(control, "no_envelope")
    job_id = start_compile(client)
    record = wait_job(client, job_id, {"failed"})
    assert record["error_code"] == "envelope_unparsed"
    assert not output_pdf(root).exists()
    assert read_compile_state(workdir(root))["status"] == "failed"


def test_build_ok_without_pdf_never_publishes(client, root, control):
    set_mode(control, "build_ok_no_pdf")
    job_id = start_compile(client)
    record = wait_job(client, job_id, {"failed"})
    assert record["error_code"] == "build_output_missing"
    assert not output_pdf(root).exists()


def test_cancel_keeps_previous_pdf_and_cleans_copy(client, root, control):
    patch_draft(client, base=0)
    wait_job(client, start_compile(client), {"succeeded"})

    set_mode(control, "sleep")
    job_id = start_compile(client)
    wait_job(client, job_id, {"running"})
    assert isolated_dir(root, job_id).is_dir()
    # 编译期间详情端点如实报 running
    assert detail(client)["compile"]["status"] == "running"

    cancel = client.post(f"{API}/jobs/{job_id}/cancel")
    assert cancel.status_code == 202, cancel.text
    record = wait_job(client, job_id, {"canceled"})
    assert record["error_code"] == "canceled"
    assert output_pdf(root).read_bytes() == PDF_OK
    state = read_compile_state(workdir(root))
    assert state["status"] == "failed" and state["error_code"] == "canceled"
    assert not isolated_dir(root, job_id).exists()


def test_missing_source_pdf_fails_before_spawning(client, root):
    """副本里找不到源 PDF → 前置失败（``source_pdf_missing``），不 spawn。"""
    (workdir(root) / "input.pdf").unlink()
    state_path = workdir(root) / "agent" / "state.pkl"
    state = pickle.loads(state_path.read_bytes())  # noqa: S301 - 测试自己刚写的文件
    state["temp_pdf_path"] = "/nonexistent/input.pdf"
    state["pdf_path"] = ""
    state_path.write_bytes(pickle.dumps(state))

    job_id = start_compile(client)
    record = wait_job(client, job_id, {"failed"})
    assert record["error_code"] == "source_pdf_missing"
    assert record["pid"] is None  # 从来没起过子进程
    assert not output_pdf(root).exists()


def test_stale_isolated_dirs_are_swept(client, root):
    leftover = workdir(root) / STATE_DIR / "compile-j_OLDLEFTOVER"
    leftover.mkdir(parents=True)
    (leftover / "junk").write_text("x", encoding="utf-8")
    patch_draft(client, base=0)
    wait_job(client, start_compile(client), {"succeeded"})
    assert not leftover.exists()


# --------------------------------------------------------------------------- #
# 物化：canonical 占位符 → translated.md 短锚点
# --------------------------------------------------------------------------- #
def test_draft_body_is_converted_to_markdown_anchors():
    """草稿 target 与 ``/paragraphs`` 同为 canonical 形式；写 md 前转成短锚点。"""
    convert = compile_mod.draft_body_to_markdown
    assert convert("<style id='1'>附录 </style><style id='3'>C.2</style>") == (
        "[[S1]]附录 [[/S1]][[S3]]C.2[[/S3]]"
    )
    assert convert("公式 {v3} 与 </style>") == "公式 [[F3]] 与 [[/S]]"
    assert convert("普通文本") == "普通文本"
    assert convert("[[S1]]已经是锚点[[/S1]]") == "[[S1]]已经是锚点[[/S1]]"  # 幂等


def test_prepare_compile_materialises_into_the_copy_only(tmp_path):
    """``prepare_compile`` 只动隔离副本：译文转锚点、排版写覆盖、源 PDF 落地。"""
    root = tmp_path / "root"
    workdir_path = _make_workdir(root, "alpha")
    before = (workdir_path / "agent" / "translated.md").read_text(encoding="utf-8")
    asyncio.run(
        DraftStore(workdir_path).patch(
            base_revision=0,
            paragraphs={
                PID: {
                    "target": "<style id='1'>附录 </style>草稿改后的译文",
                    "layout": {"font_scale": 1.05},
                }
            },
        )
    )
    record = JobRecord(
        job_id="j_TESTPREPARE0000000000000",
        did="alpha",
        action="compile",
        created_at=utc_now(),
        from_stage="apply",
        profile=None,
    )
    plan = compile_mod.prepare_compile(record, workdir_path)
    try:
        body = (plan.isolated / "agent" / "translated.md").read_text(encoding="utf-8")
        assert f"<!-- id={PID}" in body
        assert "[[S1]]附录 [[/S1]]草稿改后的译文" in body
        assert "<style id='1'>" not in body  # 副本里必须是锚点形式
        overrides = json.loads(
            (plan.isolated / "agent" / "layout_overrides.json").read_text(encoding="utf-8")
        )
        assert overrides["paragraphs"][PID]["font_scale"] == 1.05
        # 源 PDF 落到 TranslationConfig 的工作目录约定位置，state.pkl 已重指
        assert (plan.isolated / "paper" / "input.pdf").is_file()
        state = pickle.loads((plan.isolated / "agent" / "state.pkl").read_bytes())  # noqa: S301
        assert state["temp_pdf_path"] == str(plan.isolated / "paper" / "input.pdf")
        assert state["pdf_path"] == "paper.pdf"  # 产物名依赖它，不能改
        # 详情端点看到的是 running（编译还没结束）
        assert read_compile_state(workdir_path)["status"] == "running"
        assert plan.revision == 1
    finally:
        shutil.rmtree(plan.isolated, ignore_errors=True)
    # 真 workdir 的译文一个字节都没动
    assert (workdir_path / "agent" / "translated.md").read_text(encoding="utf-8") == before
    assert not (workdir_path / "agent" / "layout_overrides.json").exists()


# --------------------------------------------------------------------------- #
# scope / base_revision
# --------------------------------------------------------------------------- #
def test_pages_scope_downgrades_to_full_with_reason(client, control):
    set_mode(control, "sleep")
    job_id = start_compile(client, scope="pages")
    record = wait_job(client, job_id, {"running"})
    assert record["requested_scope"] == "pages"
    assert record["effective_scope"] == "full"
    assert record["downgrade_reason"]
    client.post(f"{API}/jobs/{job_id}/cancel")
    wait_job(client, job_id, {"canceled"})


def test_full_scope_has_no_downgrade_reason(client, control):
    set_mode(control, "sleep")
    job_id = start_compile(client, scope="full")
    record = wait_job(client, job_id, {"running"})
    assert record["requested_scope"] == "full"
    assert record["effective_scope"] == "full"
    assert record["downgrade_reason"] is None
    client.post(f"{API}/jobs/{job_id}/cancel")
    wait_job(client, job_id, {"canceled"})


def test_explicit_compile_base_revision_mismatch_is_409(client):
    patch_draft(client, base=0)  # 当前 revision = 1
    status, payload = post_compile(client, base_revision=7)
    assert status == 409
    assert payload["error"]["code"] == "revision_conflict"
    assert payload["error"]["detail"]["current_revision"] == 1
    assert compile_jobs(client) == []


def test_explicit_compile_with_matching_base_revision_is_accepted(client):
    patch_draft(client, base=0)
    job_id = start_compile(client, base_revision=1)
    assert wait_job(client, job_id, {"succeeded"})["status"] == "succeeded"


def test_compile_ignores_profile_and_second_job_is_busy(client, control):
    """compile 不接 provider（给了也不进记录）；同文档第二个活动 job → 409。"""
    set_mode(control, "sleep")
    job_id = start_compile(client, profile="whatever")
    record = wait_job(client, job_id, {"running"})
    assert record["profile"] is None

    status, payload = post_compile(client)
    assert status == 409
    assert payload["error"]["code"] == "document_busy"
    assert payload["error"]["detail"]["job_id"] == job_id
    client.post(f"{API}/jobs/{job_id}/cancel")
    wait_job(client, job_id, {"canceled"})


def test_run_only_fields_are_rejected_on_compile(client):
    status, payload = post_compile(client, pages="1-3")
    assert status == 422
    assert payload["error"]["code"] == "forbidden_field"


# --------------------------------------------------------------------------- #
# 活动任务期间编辑只读
# --------------------------------------------------------------------------- #
def test_patch_during_active_compile_is_409_document_busy(client, control):
    set_mode(control, "sleep")
    job_id = start_compile(client)
    wait_job(client, job_id, {"running"})

    response = client.patch(
        f"{API}/documents/alpha/draft",
        json={"base_revision": 0, "paragraphs": {PID: {"target": "改不了"}}},
    )
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "document_busy"
    assert error["detail"]["job_id"] == job_id
    assert client.delete(f"{API}/documents/alpha/draft").status_code == 409

    client.post(f"{API}/jobs/{job_id}/cancel")
    wait_job(client, job_id, {"canceled"})


# --------------------------------------------------------------------------- #
# 防抖
# --------------------------------------------------------------------------- #
def test_debounce_triggers_exactly_one_compile(client, root, monkeypatch):
    monkeypatch.setattr(compile_mod, "DEBOUNCE_SECONDS", 0.2)
    patch_draft(client, "改后的译文", base=0, layout={"font_scale": 1.05})

    body = wait_compile_status(client, statuses={"ok"})
    assert body["compile"]["revision"] == 1
    jobs = compile_jobs(client)
    assert len(jobs) == 1
    assert jobs[0]["status"] == "succeeded"
    assert jobs[0]["requested_scope"] == "full"
    assert output_pdf(root).read_bytes() == PDF_OK


def test_two_quick_patches_reset_the_timer(client, monkeypatch):
    monkeypatch.setattr(compile_mod, "DEBOUNCE_SECONDS", 0.4)
    patch_draft(client, "第一版", base=0)
    time.sleep(0.15)  # 计时器还没到点 → 被下一次 PATCH 重置
    patch_draft(client, "第二版", base=1)

    body = wait_compile_status(client, statuses={"ok"})
    assert body["compile"]["revision"] == 2  # 编译的是最后那一版草稿
    assert len(compile_jobs(client)) == 1


def test_debounce_skips_while_an_explicit_compile_runs(client, control, monkeypatch):
    """防抖到点时已有活动 compile → 跳过（不重复编译，人工触发优先）。"""
    monkeypatch.setattr(compile_mod, "DEBOUNCE_SECONDS", 0.5)
    set_mode(control, "sleep")  # 显式 compile 一直在跑，直到测试主动取消
    patch_draft(client, "改后的译文", base=0)
    explicit = start_compile(client)  # 抢在防抖到点之前显式提交
    wait_job(client, explicit, {"running"})

    time.sleep(0.8)  # 让防抖到点（此刻编译正在跑）
    jobs = compile_jobs(client)
    assert [job["job_id"] for job in jobs] == [explicit]

    client.post(f"{API}/jobs/{explicit}/cancel")
    wait_job(client, explicit, {"canceled"})


def test_delete_draft_also_schedules_a_compile(client, root, monkeypatch):
    monkeypatch.setattr(compile_mod, "DEBOUNCE_SECONDS", 0.2)
    patch_draft(client, base=0)
    response = client.delete(f"{API}/documents/alpha/draft")
    assert response.status_code == 200

    body = wait_compile_status(client, statuses={"ok"})
    assert body["compile"]["revision"] == 2  # DELETE 也 +1
    jobs = compile_jobs(client)
    assert [job["status"] for job in jobs][0] in {"queued", "running", "succeeded"}
    assert output_pdf(root).read_bytes() == PDF_OK


# --------------------------------------------------------------------------- #
# 详情接真 + 重启核对
# --------------------------------------------------------------------------- #
def test_detail_compile_defaults_to_none_without_compile(client):
    body = detail(client)
    assert body["compile"] == {
        "status": "none",
        "revision": 0,
        "stale": False,
        "artifact": None,
    }


def test_restart_settles_a_running_compile_state(root):
    """serve 重启：残留在 running 的编译状态落定为 failed（不自动重跑）。"""
    store = DocumentStore.for_root(root)
    workdir_path = workdir(root)
    compile_state_path(workdir_path).parent.mkdir(parents=True, exist_ok=True)
    compile_state_path(workdir_path).write_text(
        json.dumps(
            {
                "status": "running",
                "revision": 0,
                "artifact": None,
                "revision_attempted": 3,
                "job_id": "j_OLDRUN",
                "started_at": "2026-01-01T00:00:00.000Z",
            }
        ),
        encoding="utf-8",
    )
    with TestClient(create_app(store)) as client:
        state = read_compile_state(workdir_path)
        assert state["status"] == "failed"
        assert state["error_code"] == "server_restart"
        assert state["revision_attempted"] == 3
        assert client.get(f"{API}/documents/alpha").json()["compile"]["status"] == "failed"


def test_new_root_gets_no_state_files(root):
    """create_app 纯工厂：没有需要恢复的状态时不写盘。"""
    before = sorted(path.name for path in (root / "alpha").rglob("*"))
    create_app(DocumentStore.for_root(root))
    assert sorted(path.name for path in (root / "alpha").rglob("*")) == before


# --------------------------------------------------------------------------- #
# W12：发布后归档成版本（api.md §3.7）
# --------------------------------------------------------------------------- #
def version_pdf_path(root: Path, revision: int, did: str = "alpha") -> Path:
    return workdir(root, did) / STATE_DIR / "versions" / f"{revision}.pdf"


def version_items(root: Path, did: str = "alpha") -> list[dict]:
    """版本清单的 ``items``（升序）；清单缺失 → 空列表。"""
    path = workdir(root, did) / STATE_DIR / "versions.json"
    if not path.is_file():
        return []
    return json.loads(path.read_text(encoding="utf-8"))["items"]


def test_publish_archives_a_version_with_the_published_facts(client, root):
    patch_draft(client, base=0, layout={"scale_cap": 0.9})
    wait_job(client, start_compile(client), {"succeeded"})

    items = version_items(root)
    assert [row["revision"] for row in items] == [1]
    row = items[0]
    assert row["trigger"] == "manual"  # 显式 POST compile
    assert row["artifact_name"] == PDF_NAME
    assert row["bytes"] == len(PDF_OK)
    assert row["sha256_head"] == hashlib.sha256(PDF_OK).hexdigest()
    # quality 只记录不门禁：这份 workdir 的 run_state 没有门禁结论 → not_available/false
    assert row["quality"] == {"check_verdict": "not_available", "pipeline_ok": False}
    # 归档文件就是发布出去的那一份字节 + 下一次发布也不会换掉它
    assert version_pdf_path(root, 1).read_bytes() == PDF_OK
    assert version_pdf_path(root, 1).stat().st_ino == output_pdf(root).stat().st_ino

    listed = client.get(f"{API}/documents/alpha/versions").json()
    assert listed["current_revision"] == 1
    assert [item["revision"] for item in listed["items"]] == [1]
    download = client.get(f"{API}/documents/alpha/versions/1/pdf")
    assert download.status_code == 200
    assert download.content == PDF_OK
    assert download.headers["content-disposition"] == (
        'inline; filename="paper.no_watermark.zh.mono.r1.pdf"'
    )


def test_each_publish_keeps_its_own_version_bytes(client, root, control):
    """两次编译 → 两版都在；下载 r1 拿到的仍是 r1 当时的字节与指纹。"""
    patch_draft(client, "第一版", base=0)
    wait_job(client, start_compile(client), {"succeeded"})
    first_sha = version_items(root)[0]["sha256_head"]

    patch_draft(client, "第二版", base=1)
    set_mode(control, "quality_fail")  # 第二版产物字节不同（门禁不过但编译成功）
    wait_job(client, start_compile(client), {"succeeded"})

    items = version_items(root)
    assert [row["revision"] for row in items] == [1, 2]
    assert items[1]["sha256_head"] == hashlib.sha256(PDF_QUALITY).hexdigest()
    assert items[1]["sha256_head"] != first_sha
    assert version_pdf_path(root, 1).read_bytes() == PDF_OK
    assert version_pdf_path(root, 2).read_bytes() == PDF_QUALITY
    assert output_pdf(root).read_bytes() == PDF_QUALITY  # output/ 仍是最新发布
    assert client.get(f"{API}/documents/alpha/versions/1/pdf").content == PDF_OK
    # 详情/下载主路径（W09/W10 语义）不变：artifact 恒指最新发布
    body = detail(client)
    assert body["compile"]["revision"] == 2
    assert body["compile"]["artifact"]["name"] == PDF_NAME
    listed = client.get(f"{API}/documents/alpha/versions").json()
    assert [item["revision"] for item in listed["items"]] == [2, 1]


def test_failed_compile_does_not_archive(client, root, control):
    patch_draft(client, base=0)
    wait_job(client, start_compile(client), {"succeeded"})
    assert [row["revision"] for row in version_items(root)] == [1]

    patch_draft(client, "第二版", base=1)
    set_mode(control, "build_fail")
    wait_job(client, start_compile(client), {"failed"})

    assert [row["revision"] for row in version_items(root)] == [1]  # 没多一行
    assert not version_pdf_path(root, 2).exists()
    assert version_pdf_path(root, 1).read_bytes() == PDF_OK  # 上一版仍可下载


def test_canceled_compile_does_not_archive(client, root, control):
    patch_draft(client, base=0)
    wait_job(client, start_compile(client), {"succeeded"})

    patch_draft(client, "第二版", base=1)
    set_mode(control, "sleep")
    job_id = start_compile(client)
    wait_job(client, job_id, {"running"})
    client.post(f"{API}/jobs/{job_id}/cancel")
    wait_job(client, job_id, {"canceled"})

    assert [row["revision"] for row in version_items(root)] == [1]
    assert not version_pdf_path(root, 2).exists()


def test_trigger_marks_debounce_and_manual_compiles(client, root, monkeypatch):
    """``trigger`` 区分防抖自动与显式 POST（job 记录与版本清单两头都有）。"""
    monkeypatch.setattr(compile_mod, "DEBOUNCE_SECONDS", 0.2)
    patch_draft(client, "自动编译的草稿", base=0)
    wait_compile_status(client, statuses={"ok"})

    # 第二次：关掉防抖（拉长窗口），只用显式 POST
    monkeypatch.setattr(compile_mod, "DEBOUNCE_SECONDS", 60.0)
    patch_draft(client, "手动编译的草稿", base=1)
    wait_job(client, start_compile(client), {"succeeded"})

    jobs = compile_jobs(client)
    assert [job["trigger"] for job in jobs] == ["manual", "debounce"]  # 新 → 旧
    assert [row["trigger"] for row in version_items(root)] == ["debounce", "manual"]
    listed = client.get(f"{API}/documents/alpha/versions").json()
    assert [item["trigger"] for item in listed["items"]] == ["manual", "debounce"]


def test_archive_failure_still_reports_a_successful_publish(client, root, monkeypatch):
    """归档出错不改编译结论（发布已经发生）：job 照旧 succeeded、产物照旧可下载。"""
    from babeldoc_tools.serve import versions  # noqa: PLC0415 - 只在这个用例里替换它

    def boom(*_args, **_kwargs):
        raise OSError("模拟磁盘满：清单写不进去")

    monkeypatch.setattr(versions, "archive", boom)
    patch_draft(client, base=0)
    record = wait_job(client, start_compile(client), {"succeeded"})

    assert record["exit_code"] == 0
    assert output_pdf(root).read_bytes() == PDF_OK
    assert read_compile_state(workdir(root))["status"] == "ok"
    assert version_items(root) == []  # 归档失败 → 历史里少这一版（日志里有告警）
    assert detail(client)["compile"]["artifact"]["revision"] == 1
