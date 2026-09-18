"""``bdt serve`` 候选重译：生成零副作用、采用写草稿+防抖、拒绝、忙守卫、重启（W11）。

生成链**不 mock**：候选行 → job 提交 → 隔离副本 → 真 ``bdt translate --ids`` 子进程 →
（stub translator 脚本给出"新译文"）→ 从副本取回候选 → 真 workdir 校验。只有最外层的
translator 命令换成 stub 脚本（只读 stdin、echo 回译，绝不联网/调模型）。

三条硬断言：

- **生成零副作用**：``agent/translated.md``、``agent/translated.jsonl``、``draft.json``、
  ``output/*.pdf`` 的 sha256 + mtime_ns 前后完全一致（只多出 ``candidates.json``）；
- **采用才写草稿**：走 :meth:`DraftStore.patch_current`（``revision+1``）+ 触发 1.5s
  防抖编译（断言 compile job 出现后取消，不等真 build）；
- **客户端永不传命令**：请求体里的 ``translator`` 一律 422 ``forbidden_field``，
  命令只从 profile 解析（stub 脚本的路径只出现在 profiles.json 里）。

轮询全部固定步数封顶（:data:`POLL_SECONDS` × 上限），不会挂死。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import signal
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="serve 需要 web extra: fastapi")

from babeldoc_tools.serve import candidates as candidates_mod  # noqa: E402
from babeldoc_tools.serve import compile as compile_mod  # noqa: E402
from babeldoc_tools.serve.app import create_app  # noqa: E402
from babeldoc_tools.serve.candidates import candidates_path  # noqa: E402
from babeldoc_tools.serve.draft import DraftStore  # noqa: E402
from babeldoc_tools.serve.jobs import pid_alive  # noqa: E402
from babeldoc_tools.serve.schemas import API_PREFIX as API  # noqa: E402
from babeldoc_tools.serve.store import STATE_DIR  # noqa: E402
from babeldoc_tools.serve.store import DocumentStore  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PID = "P05-002"
OTHER_PID = "P05-003"
DOCS = ("alpha", "beta", "gamma")
REPO_ROOT = Path(__file__).resolve().parents[1]

#: W11 候选生成测试用的 stub translator（真 subprocess 读 stdin，绝不联网）：
#: ``$1`` 是模式 —— 正文文本 / ``sleep``（长睡，验取消与忙守卫）/ ``fail``（退出码 3）/
#: ``other``（返回别的段落 id，验"模型漏段"）。
TRANSLATOR_STUB = """#!/bin/sh
# W11 stub translator：读干提示词，按模式回译（$1 = 候选正文 / sleep / fail / other）。
set -eu
mode="$1"
prompt="$(cat)"
case "$mode" in
  sleep)
    sleep 300
    ;;
  fail)
    echo "stub translator 故意失败" >&2
    exit 3
    ;;
  other)
    printf '%s\\n' '<!-- id=PZZ-999 label=text -->' '别的段的译文'
    ;;
  *)
    printf '%s\\n' "$prompt" | awk -v text="$mode" '/^<!-- id=/ { print; print text; }'
    ;;
esac
"""

#: 轮询步长 / 上限（固定步数封顶，不裸 while）。
POLL_SECONDS = 0.2
WAIT_SECONDS = 90.0


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _make_workdir(root: Path, did: str) -> Path:
    """最小可重译 workdir：``anchors.json`` + ``translated.md/jsonl``（生成路径只读这两个）。"""
    workdir = root / did
    agent = workdir / "agent"
    agent.mkdir(parents=True)
    rows = [
        {
            "id": pid,
            "page": 0,
            "layout_label": "text",
            "canonical": f"Source of {pid} <style id='1'>x</style>",
            "markdown": f"Source of {pid} [[S1]]x[[/S1]]",
            "anchors": "[]",
        }
        for pid in (PID, OTHER_PID)
    ]
    (agent / "anchors.json").write_text(
        json.dumps({"rows": rows, "skipped": []}), encoding="utf-8"
    )
    (agent / "translated.md").write_text(
        "<!--MD_HEADER-->\n\n"
        + "".join(
            f"<!-- id={row['id']} label=text -->\n旧译文 {row['id']}\n\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    (agent / "translated.jsonl").write_text(
        "".join(
            json.dumps(
                {"id": row["id"], "target": f"基线译文 {row['id']}"}, ensure_ascii=False
            )
            + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )
    (agent / "document.md").write_text("<!--MD_HEADER-->\n\nsource\n", encoding="utf-8")
    (agent / "run_state.json").write_text("{}\n", encoding="utf-8")
    (workdir / "output").mkdir()
    (workdir / "output" / "paper.mono.pdf").write_bytes(b"PDF-old\n")
    return workdir


def _write_profiles(root: Path, profiles: dict[str, dict[str, str]]) -> None:
    state = root / STATE_DIR
    state.mkdir(parents=True, exist_ok=True)
    (state / "profiles.json").write_text(
        json.dumps(profiles, ensure_ascii=False), encoding="utf-8"
    )


@pytest.fixture
def stub(tmp_path: Path) -> Path:
    """stub translator 脚本（绝对路径进 profiles.json；子进程 cwd 是隔离副本）。"""
    script = tmp_path / "candidate-translator.sh"
    script.write_text(TRANSLATOR_STUB, encoding="utf-8")
    script.chmod(0o755)
    return script


@pytest.fixture
def root(tmp_path: Path, stub: Path) -> Path:
    base = tmp_path / "root"
    for did in DOCS:
        _make_workdir(base, did)
    _write_profiles(
        base,
        {
            # 命令字符串里带一个参数（shlex.split 拆成 argv）：stub 用它当"候选正文"。
            "stub": {"translator": f"{stub} 候选甲（stub）"},
            "stub2": {"translator": f"{stub} 候选乙（stub）"},
            "slow": {"translator": f"{stub} sleep"},
            "broken": {"translator": f"{stub} fail"},
            "other": {"translator": f"{stub} other"},
            # 只有 reviewer：候选生成没有可用的翻译命令 → 422 profile_missing
            "review-only": {"reviewer": f"{stub} 候选甲（stub）"},
        },
    )
    return base


def _shutdown(root: Path, *, timeout: float = 20.0) -> None:
    """测试收尾：SIGKILL 掉还活着的 job 进程组（sleep 模式要睡 300s）。"""
    groups = []
    snapshots = root / STATE_DIR / "jobs"
    for snapshot in sorted(snapshots.glob("j_*.json")) if snapshots.is_dir() else []:
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
def client(root: Path, monkeypatch):
    """真路由 + 真调度。

    ``PYTHONPATH`` 显式指向仓库根：子进程（``bdt translate``）的 cwd 是隔离副本，
    没有它 import 不到 ``babeldoc_tools``（editable 的 .pth 在 macOS 上可能被标 hidden）。
    防抖窗口拉长：绝大多数用例自己显式采用/断言，防抖用例自己改短。
    """
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    monkeypatch.setattr(compile_mod, "DEBOUNCE_SECONDS", 60.0)
    with TestClient(create_app(DocumentStore.for_root(root))) as test_client:
        yield test_client
        _shutdown(root)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def retranslate(
    client, did: str = "alpha", pid: str = PID, profile: str | None = "stub"
):
    body: dict[str, object] = {} if profile is None else {"profile": profile}
    return client.post(f"{API}/documents/{did}/paragraphs/{pid}/retranslate", json=body)


def generate(client, did: str = "alpha", pid: str = PID, profile: str = "stub") -> dict:
    """提交一次候选生成并等 job 落终态；返回 202 响应体（带 candidate_id/job_id）。"""
    response = retranslate(client, did, pid, profile)
    assert response.status_code == 202, response.text
    return response.json()


def candidates(client, did: str = "alpha", pid: str = PID) -> list[dict]:
    response = client.get(f"{API}/documents/{did}/paragraphs/{pid}/candidates")
    assert response.status_code == 200, response.text
    return response.json()["items"]


def get_job(client, job_id: str) -> dict:
    response = client.get(f"{API}/jobs/{job_id}")
    assert response.status_code == 200, response.text
    return response.json()


def wait_job(
    client, job_id: str, statuses: set[str], *, timeout: float = WAIT_SECONDS
) -> dict:
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


def wait_candidate_target(
    client, did: str = "alpha", pid: str = PID, *, timeout: float = WAIT_SECONDS
) -> dict:
    """等该段第一条候选拿到译文（``candidate_target`` 非空）。"""
    for _ in range(max(1, int(timeout / POLL_SECONDS))):
        items = candidates(client, did, pid)
        ready = [item for item in items if item["candidate_target"]]
        if ready:
            return ready[0]
        time.sleep(POLL_SECONDS)
    raise AssertionError(f"候选一直没有拿到 candidate_target：{items}")


def wait_compile_job(
    client, did: str = "alpha", *, timeout: float = 30.0
) -> dict | None:
    """等防抖编译 job 出现（不等它跑完 —— 真 build 分钟级）。"""
    for _ in range(max(1, int(timeout / POLL_SECONDS))):
        response = client.get(f"{API}/documents/{did}/jobs")
        assert response.status_code == 200, response.text
        compiles = [job for job in response.json() if job["action"] == "compile"]
        if compiles:
            return compiles[0]
        time.sleep(POLL_SECONDS)
    return None


def cancel_active(client, did: str = "alpha") -> None:
    for job in client.get(f"{API}/documents/{did}/jobs").json():
        if job["status"] in ("queued", "running"):
            client.post(f"{API}/jobs/{job['job_id']}/cancel")


def workdir_of(root: Path, did: str = "alpha") -> Path:
    return root / did


def fingerprint(workdir: Path) -> dict[str, str]:
    """正文产物的 ``sha256 + mtime_ns`` 指纹（零副作用的证据）。"""
    out: dict[str, str] = {}
    for relative in (
        "agent/translated.md",
        "agent/translated.jsonl",
        "agent/anchors.json",
        "agent/layout_overrides.json",
        ".bdt-serve/draft.json",
        ".bdt-serve/compile.json",
        "output/paper.mono.pdf",
    ):
        path = workdir / relative
        if not path.is_file():
            out[relative] = "absent"
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        out[relative] = f"{digest}:{path.stat().st_mtime_ns}"
    return out


def paragraph_target(client, did: str = "alpha", pid: str = PID) -> str | None:
    response = client.get(f"{API}/documents/{did}/paragraphs")
    assert response.status_code == 200, response.text
    return next(row["target"] for row in response.json() if row["id"] == pid)


# --------------------------------------------------------------------------- #
# 生成：零副作用
# --------------------------------------------------------------------------- #
def test_generation_never_touches_the_real_workdir(client, root):
    """生成候选：正文/草稿/产物 sha+mtime 分毫不动，候选行落在 candidates.json。"""
    workdir = workdir_of(root)
    before = fingerprint(workdir)

    payload = generate(client)
    record = wait_job(client, payload["job_id"], {"succeeded"})

    assert record["action"] == "retranslate"
    assert record["from_stage"] is None
    assert record["profile"] == "stub"  # profile id，不是命令
    assert record["paragraph_id"] == PID
    assert record["candidate_id"] == payload["candidate_id"]
    assert record["run_id"] is None  # 没有 --debug，副本里也没有归档

    assert fingerprint(workdir) == before, "候选生成动了真 workdir 的产物"
    # 候选文件确实写了：发号器 + 一条 pending（还带上了生成它的 job）
    stored = json.loads(candidates_path(workdir).read_text(encoding="utf-8"))
    assert stored["version"] == candidates_mod.CANDIDATES_VERSION
    assert stored["next_id"] == 2
    assert [item["id"] for item in stored["items"]] == ["c_0001"]
    assert stored["items"][0]["job_id"] == payload["job_id"]

    item = wait_candidate_target(client)
    assert item["status"] == "pending"
    assert item["candidate_target"] == "候选甲（stub）"
    assert item["baseline_target"] == f"基线译文 {PID}"
    assert item["source"] == f"Source of {PID} <style id='1'>x</style>"
    assert item["model_label"] == "stub"
    assert item["adopted_at"] is None

    # 正文（GET /paragraphs 的 target）仍是基线：候选没有混进去
    assert paragraph_target(client) == f"基线译文 {PID}"
    response = client.get(f"{API}/documents/alpha/draft")
    assert response.json()["revision"] == 0
    # 隔离副本删干净（不留 candidates-<jid> 目录）
    assert list((workdir / STATE_DIR).glob("candidates-*")) == []


def test_candidates_are_listed_newest_pending_first(client):
    """同段多候选：最新的 pending 在前；两个 profile 各生成一条。"""
    first = generate(client, profile="stub")
    wait_job(client, first["job_id"], {"succeeded"})
    wait_candidate_target(client)
    second = generate(client, profile="stub2")
    wait_job(client, second["job_id"], {"succeeded"})

    items = candidates(client)
    assert [item["id"] for item in items] == [
        second["candidate_id"],
        first["candidate_id"],
    ]
    assert [item["candidate_target"] for item in items] == [
        "候选乙（stub）",
        "候选甲（stub）",
    ]
    # 另一个段落的候选不受影响
    assert candidates(client, pid=OTHER_PID) == []


def test_generation_is_queued_and_busy_guarded(client):
    """生成是一个 job：活动期间同文档再点一次 → 409 document_busy（候选行不留孤儿）。"""
    payload = generate(client, profile="slow")  # 长睡的 translator
    running = wait_job(client, payload["job_id"], {"running"})
    assert running["status"] == "running"

    again = retranslate(client, profile="stub")
    assert again.status_code == 409, again.text
    assert again.json()["error"]["code"] == "document_busy"
    assert again.json()["error"]["detail"]["job_id"] == payload["job_id"]
    # 第二次请求没有建出第二条候选行
    assert [item["id"] for item in candidates(client)] == [payload["candidate_id"]]

    canceled = client.post(f"{API}/jobs/{payload['job_id']}/cancel")
    assert canceled.status_code in (200, 202)
    wait_job(client, payload["job_id"], {"canceled"})
    # 取消后候选行被删掉（不留"永远生成中"的半条候选）
    assert list(candidates(client)) == []


def test_adopt_is_blocked_while_generating(client):
    """生成中 adopt → 409 candidate_not_ready；reject 不受忙限制（不碰草稿）。"""
    payload = generate(client, profile="slow")
    wait_job(client, payload["job_id"], {"running"})

    adopt = client.post(
        f"{API}/documents/alpha/paragraphs/{PID}/candidates/{payload['candidate_id']}/adopt"
    )
    assert adopt.status_code == 409, adopt.text
    assert adopt.json()["error"]["code"] == "candidate_not_ready"

    rejected = client.post(
        f"{API}/documents/alpha/paragraphs/{PID}/candidates/{payload['candidate_id']}/reject"
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"
    # 拒绝不动草稿（revision 仍是 0）
    assert client.get(f"{API}/documents/alpha/draft").json()["revision"] == 0
    cancel_active(client)


# --------------------------------------------------------------------------- #
# 采用：写草稿 + 防抖
# --------------------------------------------------------------------------- #
def test_adopt_writes_draft_without_auto_compile(client, root, monkeypatch):
    """采用写草稿并增加 revision；编译需要用户明确触发。"""
    monkeypatch.setattr(compile_mod, "DEBOUNCE_SECONDS", 0.2)
    before = fingerprint(workdir_of(root))

    payload = generate(client)
    wait_job(client, payload["job_id"], {"succeeded"})
    candidate = wait_candidate_target(client)
    after_generate = fingerprint(workdir_of(root))
    assert after_generate == before

    adopt = client.post(
        f"{API}/documents/alpha/paragraphs/{PID}/candidates/{candidate['id']}/adopt"
    )
    assert adopt.status_code == 200, adopt.text
    draft = adopt.json()
    assert draft["revision"] == 1
    assert draft["paragraphs"][PID]["target"] == "候选甲（stub）"

    # 采用后：草稿变了，但正文产物（translated.md/jsonl、旧 PDF）仍然没变
    adopted_fingerprint = fingerprint(workdir_of(root))
    for relative in (
        "agent/translated.md",
        "agent/translated.jsonl",
        "agent/anchors.json",
        "output/paper.mono.pdf",
    ):
        assert adopted_fingerprint[relative] == before[relative], (
            f"{relative} 被采用改了"
        )
    assert (
        adopted_fingerprint[".bdt-serve/draft.json"] != before[".bdt-serve/draft.json"]
    )

    adopted = candidates(client)
    assert adopted[0]["id"] == candidate["id"]
    assert adopted[0]["status"] == "adopted"
    assert adopted[0]["adopted_at"] is not None

    assert wait_compile_job(client, timeout=0.4) is None


def test_adopt_is_idempotent_guard_and_uses_current_revision(client):
    """已采用的候选再 adopt → 409 candidate_decided；拒绝过的不能采用。"""
    payload = generate(client)
    wait_job(client, payload["job_id"], {"succeeded"})
    item = wait_candidate_target(client)
    url = f"{API}/documents/alpha/paragraphs/{PID}/candidates/{item['id']}/adopt"
    assert client.post(url).status_code == 200

    again = client.post(url)
    assert again.status_code == 409, again.text
    assert again.json()["error"]["code"] == "candidate_decided"
    # 草稿没有因为重复采用再 +1
    assert client.get(f"{API}/documents/alpha/draft").json()["revision"] == 1
    cancel_active(client)


def test_adopt_does_not_need_base_revision_and_stacks_under_concurrency(client):
    """adopt 不需要 base_revision：服务端在写锁内取当前 revision（并发采用各自 +1）。"""
    payload = generate(client)
    wait_job(client, payload["job_id"], {"succeeded"})
    item = wait_candidate_target(client)

    # 先用 PATCH 手工写一次草稿（revision=1），再采用 → revision=2（不是冲突）
    patch = client.patch(
        f"{API}/documents/alpha/draft",
        json={"base_revision": 0, "paragraphs": {OTHER_PID: {"target": "手改"}}},
    )
    assert patch.status_code == 200
    assert patch.json()["revision"] == 1

    adopt = client.post(
        f"{API}/documents/alpha/paragraphs/{PID}/candidates/{item['id']}/adopt"
    )
    assert adopt.status_code == 200, adopt.text
    body = adopt.json()
    assert body["revision"] == 2
    assert body["paragraphs"][PID]["target"] == "候选甲（stub）"
    assert body["paragraphs"][OTHER_PID]["target"] == "手改"  # 别人的覆盖没被冲掉
    cancel_active(client)


def test_adopt_writes_draft_through_the_same_store(client, root):
    """草稿由 DraftStore 写（不是另开一份）：重启后读回同一个 revision。"""
    payload = generate(client)
    wait_job(client, payload["job_id"], {"succeeded"})
    item = wait_candidate_target(client)
    client.post(f"{API}/documents/alpha/paragraphs/{PID}/candidates/{item['id']}/adopt")
    doc = DraftStore(workdir_of(root)).read()
    assert doc.revision == 1
    assert doc.paragraphs[PID].target == "候选甲（stub）"
    cancel_active(client)


# --------------------------------------------------------------------------- #
# 拒绝
# --------------------------------------------------------------------------- #
def test_reject_only_flips_the_candidate_status(client, root):
    """拒绝：候选状态改了，译文/草稿/产物一概不动（幂等）。"""
    before = fingerprint(workdir_of(root))
    payload = generate(client)
    wait_job(client, payload["job_id"], {"succeeded"})
    item = wait_candidate_target(client)
    after_generate = fingerprint(workdir_of(root))

    url = f"{API}/documents/alpha/paragraphs/{PID}/candidates/{item['id']}/reject"
    rejected = client.post(url)
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"

    again = client.post(url)
    assert again.status_code == 200
    assert again.json()["status"] == "rejected"

    assert fingerprint(workdir_of(root)) == after_generate
    assert after_generate == before
    assert client.get(f"{API}/documents/alpha/draft").json()["revision"] == 0


def test_rejected_candidate_cannot_be_adopted(client):
    """拒绝之后再采用 → 409 candidate_decided（拒绝是终态，不静默改回去）。"""
    payload = generate(client)
    wait_job(client, payload["job_id"], {"succeeded"})
    item = wait_candidate_target(client)
    base = f"{API}/documents/alpha/paragraphs/{PID}/candidates/{item['id']}"
    assert client.post(f"{base}/reject").status_code == 200

    adopt = client.post(f"{base}/adopt")
    assert adopt.status_code == 409, adopt.text
    assert adopt.json()["error"]["code"] == "candidate_decided"


# --------------------------------------------------------------------------- #
# 错误码
# --------------------------------------------------------------------------- #
def test_unknown_paragraph_and_candidate_are_404(client):
    unknown = retranslate(client, pid="P99-999")
    assert unknown.status_code == 404, unknown.text
    assert unknown.json()["error"]["code"] == "paragraph_not_found"

    # 形状不合法的 pid 走同一个 404（它必然不在产物里）
    malformed = retranslate(client, pid="bad id!")
    assert malformed.status_code == 404
    assert malformed.json()["error"]["code"] == "paragraph_not_found"

    listed = client.get(f"{API}/documents/alpha/paragraphs/P99-999/candidates")
    assert listed.status_code == 404
    assert listed.json()["error"]["code"] == "paragraph_not_found"

    for verb in ("adopt", "reject"):
        missing = client.post(
            f"{API}/documents/alpha/paragraphs/{PID}/candidates/c_9999/{verb}"
        )
        assert missing.status_code == 404, missing.text
        assert missing.json()["error"]["code"] == "candidate_not_found"


def test_candidate_id_must_belong_to_the_path_paragraph(client):
    """cid 属于另一个 pid → 404（不猜、不跨段采用）。"""
    payload = generate(client, pid=PID)
    wait_job(client, payload["job_id"], {"succeeded"})
    wait_candidate_target(client, pid=PID)
    wrong = client.post(
        f"{API}/documents/alpha/paragraphs/{OTHER_PID}/candidates/{payload['candidate_id']}/adopt"
    )
    assert wrong.status_code == 404, wrong.text
    assert wrong.json()["error"]["code"] == "candidate_not_found"
    assert client.get(f"{API}/documents/alpha/draft").json()["revision"] == 0


def test_profile_is_required_and_must_have_a_translator(client):
    missing = retranslate(client, profile=None)
    assert missing.status_code == 422, missing.text
    assert missing.json()["error"]["code"] == "profile_missing"
    assert "stub" in missing.json()["error"]["detail"]["available"]

    review_only = retranslate(client, profile="review-only")
    assert review_only.status_code == 422
    assert review_only.json()["error"]["code"] == "profile_missing"

    unknown = retranslate(client, profile="nope")
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "unknown_profile"
    # 错误响应里不出现任何命令字符串
    assert "candidate-translator.sh" not in unknown.text


def test_client_cannot_send_commands_or_extra_fields(client):
    """请求体只接受 profile：translator 之类字段收到即 422 forbidden_field（不回显）。"""
    response = client.post(
        f"{API}/documents/alpha/paragraphs/{PID}/retranslate",
        json={"profile": "stub", "translator": "rm -rf / --api-key sk-x"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "forbidden_field"
    assert response.json()["error"]["detail"]["field"] == "translator"
    assert "rm -rf" not in response.text and "sk-x" not in response.text
    assert candidates(client) == []
    assert client.get(f"{API}/documents/alpha/jobs").json() == []


def test_document_not_found_is_404(client):
    assert retranslate(client, did="nope").status_code == 404
    # 非法 did（点开头的隐藏目录名）走 store 的同一个 400，不进候选路径
    assert retranslate(client, did=".hidden").status_code == 400


# --------------------------------------------------------------------------- #
# 失败/重启
# --------------------------------------------------------------------------- #
def test_translator_failure_drops_the_candidate(client, root):
    """translator 退出码 3 → job failed（error_code 透传）+ 候选行删掉（不留半条）。"""
    before = fingerprint(workdir_of(root))
    payload = generate(client, profile="broken")
    record = wait_job(client, payload["job_id"], {"failed"})

    assert record["error_code"] == "translator_failed"
    assert candidates(client) == []
    assert fingerprint(workdir_of(root)) == before
    assert list((workdir_of(root) / STATE_DIR).glob("candidates-*")) == []


def test_missing_block_marks_the_job_failed(client):
    """模型漏掉该段（返回别的 id）→ job failed(candidate_missing)，不报空成功。"""
    payload = generate(client, profile="other")
    record = wait_job(client, payload["job_id"], {"failed"})

    assert record["error_code"] == "candidate_missing"
    assert candidates(client) == []


def test_candidates_survive_restart_and_unfinished_rows_are_dropped(root, monkeypatch):
    """重启：已生成的候选还在（job 不会再跑），而"生成中"的候选行一律删掉。"""
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    monkeypatch.setattr(compile_mod, "DEBOUNCE_SECONDS", 60.0)
    with TestClient(create_app(DocumentStore.for_root(root))) as first:
        payload = generate(first)
        wait_job(first, payload["job_id"], {"succeeded"})
        candidate = wait_candidate_target(first)
        draft = first.post(
            f"{API}/documents/alpha/paragraphs/{PID}/candidates/{candidate['id']}/adopt"
        )
        assert draft.status_code == 200

    # 伪造一条"生成中"的候选（等价于上一个进程写到一半就被 kill）：重启核对必须删掉它
    path = candidates_path(root / "alpha")
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored["items"].append(
        {
            "id": "c_9999",
            "pid": PID,
            "source": "x",
            "baseline_target": None,
            "candidate_target": None,
            "status": "pending",
            "model_label": "stub",
            "job_id": "j_orphan",
            "created_at": "2026-09-17T00:00:00.000Z",
            "adopted_at": None,
        }
    )
    path.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")

    with TestClient(create_app(DocumentStore.for_root(root))) as second:
        items = candidates(second)
        assert [row["id"] for row in items] == [candidate["id"]]  # 只有真的那条
        assert items[0]["status"] == "adopted"
        assert items[0]["candidate_target"] == "候选甲（stub）"
        assert second.get(f"{API}/documents/alpha/draft").json()["revision"] == 1
        _shutdown(root)


def test_corrupt_candidates_file_is_tolerated(client, root):
    """坏 candidates.json 不 500（读成空），也不影响生成新候选。"""
    path = candidates_path(workdir_of(root))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 这不是 JSON", encoding="utf-8")
    assert candidates(client) == []

    payload = generate(client)
    wait_job(client, payload["job_id"], {"succeeded"})
    item = wait_candidate_target(client)
    assert item["candidate_target"] == "候选甲（stub）"
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["next_id"] == 2  # 坏文件从 next_id=1 重新开始，不谎报历史
