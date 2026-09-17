"""``bdt serve`` 的全局词表（W13）：CRUD 端点 + 翻译 job 的注入 argv。

两条红线在这里被钉住：

- **客户端只给 ``use_glossary`` 布尔**：词表内容与注入用的文件路径全在服务端
  （``<store_base>/.bdt-serve/glossary.csv``），argv 里出现的路径由服务端拼装；
- **只有翻译阶段注入**：``action=run`` 且真的会跑 translate 阶段才注入；``compile`` /
  ``retranslate`` / ``check`` / ``run --from apply`` 之后一律不注入（重译候选不注入）。

CSV 编解码/校验与 CLI 共用 :mod:`babeldoc_tools.glossary`（"两端一个解析器"）：
测试里用 ``glossary.load_entries`` 读 serve 写出来的文件，直接验证这一点。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="serve 需要 web extra: fastapi")

from babeldoc_tools import glossary  # noqa: E402
from babeldoc_tools.glossary import GlossaryEntry  # noqa: E402
from babeldoc_tools.serve import runner as runner_module  # noqa: E402
from babeldoc_tools.serve.app import create_app  # noqa: E402
from babeldoc_tools.serve.glossary import GLOSSARY_FILE  # noqa: E402
from babeldoc_tools.serve.glossary import GlossaryStore  # noqa: E402
from babeldoc_tools.serve.jobs import JobRecord  # noqa: E402
from babeldoc_tools.serve.profiles import Profile  # noqa: E402
from babeldoc_tools.serve.runner import JobRunner  # noqa: E402
from babeldoc_tools.serve.runner import build_job_argv  # noqa: E402
from babeldoc_tools.serve.schemas import API_PREFIX as API  # noqa: E402
from babeldoc_tools.serve.store import STATE_DIR  # noqa: E402
from babeldoc_tools.serve.store import DocumentStore  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

#: 轮询步长（等待一律"固定步数"封顶，不裸 while）。
POLL_SECONDS = 0.5


def _make_workdir(root: Path, did: str) -> Path:
    """最小 workdir：够 ``--from translate`` 过前置校验（注入路径不需要真跑 pipeline）。"""
    agent = root / did / "agent"
    agent.mkdir(parents=True)
    (agent / "document.md").write_text("<!--P01-001-->\nHello world.\n", encoding="utf-8")
    (agent / "run_state.json").write_text("{}\n", encoding="utf-8")
    return root / did


def _write_profiles(root: Path, profiles: dict[str, dict[str, str]]) -> None:
    state = root / STATE_DIR
    state.mkdir(parents=True, exist_ok=True)
    (state / "profiles.json").write_text(
        json.dumps(profiles, ensure_ascii=False), encoding="utf-8"
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """一个最小文档 + 一个 profile（注入测试不真跑翻译）。"""
    base = tmp_path / "root"
    _make_workdir(base, "alpha")
    _write_profiles(base, {"stub": {"translator": "/usr/bin/true"}})
    return base


@pytest.fixture
def client(root: Path):
    with TestClient(create_app(DocumentStore.for_root(root))) as test_client:
        yield test_client


def _get_job(client, job_id: str) -> dict:
    response = client.get(f"{API}/jobs/{job_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _wait_status(client, job_id: str, statuses: set[str], timeout: float = 60.0) -> dict:
    """轮询到期望状态；到不了就带诊断失败（固定步数封顶，不无限等）。"""
    record = _get_job(client, job_id)
    for _ in range(max(1, int(timeout / POLL_SECONDS))):
        if record["status"] in statuses:
            return record
        time.sleep(POLL_SECONDS)
        record = _get_job(client, job_id)
    raise AssertionError(f"job 停在 {record['status']}（期望 {sorted(statuses)}）")


def _post_job(client, did: str = "alpha", *, from_stage: str | None = None, **body) -> str:
    """POST 一个 job（断言 202）。``from_stage`` 映射到契约里的 ``from``。"""
    payload: dict[str, object] = {"action": "run", "profile": "stub"}
    payload.update(body)
    if from_stage is not None:
        payload["from"] = from_stage
    response = client.post(f"{API}/documents/{did}/jobs", json=payload)
    assert response.status_code == 202, response.text
    return response.json()["job_id"]


# --------------------------------------------------------------------------- #
# 存储：路径 / 原子写 / 与 CLI 共用的 CSV 格式
# --------------------------------------------------------------------------- #
def test_store_path_is_under_store_base_not_any_workdir(root: Path):
    store = GlossaryStore(root)
    assert store.path == root / STATE_DIR / GLOSSARY_FILE
    assert root / "alpha" not in store.path.parents


def test_written_file_is_the_shared_cli_csv_format(root: Path):
    """serve 写出来的就是这个 CSV；CLI 的 ``glossary.load_entries`` 直接读得动。"""
    store = GlossaryStore(root)
    store.replace([GlossaryEntry("b", "乙"), GlossaryEntry("a", "甲")])

    assert store.path.read_text(encoding="utf-8") == "source,target,note\na,甲,\nb,乙,\n"
    assert glossary.load_entries(store.path) == [
        GlossaryEntry("a", "甲"),
        GlossaryEntry("b", "乙"),
    ]


def test_injection_path_only_when_there_are_entries(root: Path):
    store = GlossaryStore(root)
    assert store.injection_path() is None  # 文件都没有

    store.replace([])  # 只有表头
    assert store.path.is_file() and store.injection_path() is None

    store.replace([GlossaryEntry("attention", "注意力")])
    assert store.injection_path() == str(store.path)

    store.clear()
    assert store.injection_path() is None  # 幂等清空后仍然不注入


def test_read_is_lenient_on_a_broken_file(root: Path):
    """坏文件当空词表（注入少几条 < 整个服务起不来），与 profiles.json 的口径一致。"""
    store = GlossaryStore(root)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("这不是,合法表头\nx,y\n", encoding="utf-8")
    assert store.read() == []
    assert store.injection_path() is None


def test_replace_validates_before_touching_the_disk(root: Path):
    store = GlossaryStore(root)
    store.replace([GlossaryEntry("a", "甲")])
    before = store.path.read_text(encoding="utf-8")

    with pytest.raises(Exception) as excinfo:
        store.replace([GlossaryEntry("a", "甲"), GlossaryEntry("", "乙")])
    assert getattr(excinfo.value, "code", None) == "glossary_invalid"
    assert store.path.read_text(encoding="utf-8") == before


# --------------------------------------------------------------------------- #
# 端点：GET / PUT / DELETE
# --------------------------------------------------------------------------- #
def test_get_is_empty_not_404_before_any_write(client):
    response = client.get(f"{API}/glossary")
    assert response.status_code == 200
    assert response.json() == {"entries": [], "count": 0}


def test_put_replaces_the_whole_table_and_normalizes(client, root: Path):
    response = client.put(
        f"{API}/glossary",
        json={
            "entries": [
                {"source": "b", "target": "乙"},
                {"source": "a", "target": "甲", "note": "首选译名"},
                {"source": " b ", "target": "乙（覆盖）"},
            ]
        },
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "entries": [
            {"source": "a", "target": "甲", "note": "首选译名"},
            {"source": "b", "target": "乙（覆盖）", "note": None},
        ],
        "count": 2,
    }
    # GET 与 PUT 的响应同源，盘上就是 CLI 读的那份 CSV
    assert client.get(f"{API}/glossary").json() == response.json()
    assert glossary.load_entries(GlossaryStore(root).path)[1].source == "b"


def test_put_empty_list_clears_the_table(client):
    client.put(f"{API}/glossary", json={"entries": [{"source": "a", "target": "甲"}]})
    response = client.put(f"{API}/glossary", json={"entries": []})
    assert response.status_code == 200
    assert response.json() == {"entries": [], "count": 0}
    assert client.get(f"{API}/glossary").json()["count"] == 0


@pytest.mark.parametrize(
    ("entry", "field"),
    [
        ({"source": "", "target": "甲"}, "source"),
        ({"source": "a", "target": "   "}, "target"),
        ({"source": "a" * 201, "target": "甲"}, "source"),
    ],
)
def test_put_invalid_entry_is_422_and_leaves_the_file_alone(
    client, root: Path, entry, field
):
    client.put(f"{API}/glossary", json={"entries": [{"source": "keep", "target": "留着"}]})
    before = GlossaryStore(root).path.read_text(encoding="utf-8")

    response = client.put(f"{API}/glossary", json={"entries": [entry]})
    assert response.status_code == 422, response.text
    body = response.json()["error"]
    assert body["code"] == "glossary_invalid"
    assert body["detail"]["field"] == field
    assert GlossaryStore(root).path.read_text(encoding="utf-8") == before


def test_delete_clears_and_is_idempotent(client, root: Path):
    client.put(f"{API}/glossary", json={"entries": [{"source": "a", "target": "甲"}]})
    first = client.delete(f"{API}/glossary")
    assert first.status_code == 200 and first.json() == {"entries": [], "count": 0}
    assert not GlossaryStore(root).path.is_file()
    assert client.delete(f"{API}/glossary").status_code == 200


# --------------------------------------------------------------------------- #
# 注入：argv 里的 --glossaries 与服务端路径
# --------------------------------------------------------------------------- #
def test_glossary_path_is_injected_when_a_translation_run_has_entries(root: Path):
    store = GlossaryStore(root)
    store.replace([GlossaryEntry("attention", "注意力")])
    runner = JobRunner(DocumentStore.for_root(root), glossary=store)

    record = JobRecord(
        job_id="j_test",
        did="alpha",
        action="run",
        created_at="2026-01-01T00:00:00.000Z",
        from_stage="parse",
        use_glossary=True,
    )
    assert runner._glossary_path(record) == str(store.path)


@pytest.mark.parametrize(
    ("action", "from_stage", "use_glossary", "has_entries"),
    [
        # 翻译阶段（parse 缺省 / parse / translate）→ 注入
        ("run", None, True, True),
        ("run", "translate", True, True),
        # from=apply 之后不跑 translate → 不注入
        ("run", "apply", True, True),
        # 客户端关了开关 → 不注入
        ("run", "parse", False, True),
        # 空词表 → 不注入
        ("run", "parse", True, False),
        # 编译 / 重译候选 / 检查都不注入（重译候选走 translator-repair 模板）
        ("compile", None, True, True),
        ("retranslate", None, True, True),
        ("check", "check", True, True),
    ],
)
def test_glossary_path_rules(root, action, from_stage, use_glossary, has_entries):
    store = GlossaryStore(root)
    if has_entries:
        store.replace([GlossaryEntry("attention", "注意力")])
    runner = JobRunner(DocumentStore.for_root(root), glossary=store)

    record = JobRecord(
        job_id="j_test",
        did="alpha",
        action=action,
        created_at="2026-01-01T00:00:00.000Z",
        from_stage=from_stage,
        use_glossary=use_glossary,
    )
    expected = (
        str(store.path)
        if (use_glossary and has_entries and action == "run" and (from_stage or "parse") in ("parse", "translate"))
        else None
    )
    assert runner._glossary_path(record) == expected


def test_use_glossary_is_normalized_to_action_run_only(tmp_path: Path):
    """记录里的 ``use_glossary`` 要么真生效，要么别声称生效（compile/check 记 false）。"""
    runner = JobRunner(DocumentStore.for_root(tmp_path))
    run = runner.registry.create(
        did="a", action="run", from_stage="parse", profile="p", use_glossary=True
    )
    compile_ = runner.registry.create(
        did="b", action="compile", from_stage=None, profile=None, use_glossary=True
    )
    check = runner.registry.create(
        did="c", action="check", from_stage="check", profile="p", use_glossary=True
    )
    assert run.use_glossary is True
    assert compile_.use_glossary is False and check.use_glossary is False
    # 落盘快照（= GET /jobs/{jid} 的响应体）带着这个字段
    snapshot = json.loads(
        runner.registry.snapshot_path(run.job_id).read_text(encoding="utf-8")
    )
    assert snapshot["use_glossary"] is True


def test_build_job_argv_only_carries_glossaries_when_given(tmp_path: Path):
    profile = Profile(id="p")
    with_glossary = build_job_argv(
        workdir=tmp_path / "wd",
        action="run",
        from_stage="translate",
        pages=None,
        dual=False,
        profile=profile,
        glossaries="/store/.bdt-serve/glossary.csv",
    )
    assert with_glossary[with_glossary.index("--glossaries") + 1] == (
        "/store/.bdt-serve/glossary.csv"
    )
    without = build_job_argv(
        workdir=tmp_path / "wd",
        action="run",
        from_stage="translate",
        pages=None,
        dual=False,
        profile=profile,
    )
    assert "--glossaries" not in without


def test_run_job_passes_the_server_side_glossary_path_into_argv(client, root, monkeypatch):
    """端到端（不真跑 pipeline）：提交 run job → 捕获服务端构造的 argv。"""
    store = GlossaryStore(root)
    store.replace([GlossaryEntry("attention", "注意力")])
    captured: dict[str, object] = {}
    script = "import json; print(json.dumps({'ok': True, 'data': {}}))"

    def fake_build_job_argv(**kwargs):
        captured.update(kwargs)
        return [sys.executable, "-c", script]

    monkeypatch.setattr(runner_module, "build_job_argv", fake_build_job_argv)
    job_id = _post_job(client, from_stage="translate")
    record = _wait_status(client, job_id, {"succeeded"})

    assert captured["glossaries"] == str(store.path)  # 服务端路径，客户端给不了
    assert record["use_glossary"] is True  # 请求缺省 = true，且记录如实回显


def test_run_job_without_glossary_entries_does_not_inject(client, monkeypatch):
    captured: dict[str, object] = {}
    script = "import json; print(json.dumps({'ok': True, 'data': {}}))"

    def fake_build_job_argv(**kwargs):
        captured.update(kwargs)
        return [sys.executable, "-c", script]

    monkeypatch.setattr(runner_module, "build_job_argv", fake_build_job_argv)
    job_id = _post_job(client, from_stage="translate")
    record = _wait_status(client, job_id, {"succeeded"})

    assert captured["glossaries"] is None
    assert record["use_glossary"] is True  # 开关开着，但词表为空 → 不注入


def test_run_job_with_use_glossary_false_does_not_inject(client, root, monkeypatch):
    GlossaryStore(root).replace([GlossaryEntry("attention", "注意力")])
    captured: dict[str, object] = {}
    script = "import json; print(json.dumps({'ok': True, 'data': {}}))"

    def fake_build_job_argv(**kwargs):
        captured.update(kwargs)
        return [sys.executable, "-c", script]

    monkeypatch.setattr(runner_module, "build_job_argv", fake_build_job_argv)
    job_id = _post_job(client, from_stage="translate", use_glossary=False)
    record = _wait_status(client, job_id, {"succeeded"})

    assert captured["glossaries"] is None
    assert record["use_glossary"] is False


def test_run_job_from_apply_does_not_inject(client, root, monkeypatch):
    """``run --from apply`` 不跑 translate 阶段 → 不注入（词表只约束翻译阶段）。"""
    GlossaryStore(root).replace([GlossaryEntry("attention", "注意力")])
    captured: dict[str, object] = {}
    script = "import json; print(json.dumps({'ok': True, 'data': {}}))"

    def fake_build_job_argv(**kwargs):
        captured.update(kwargs)
        return [sys.executable, "-c", script]

    monkeypatch.setattr(runner_module, "build_job_argv", fake_build_job_argv)
    job_id = _post_job(client, from_stage="apply")
    _wait_status(client, job_id, {"succeeded"})

    assert captured["glossaries"] is None
