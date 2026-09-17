"""旧 workdir 回放（``babeldoc_tools/debug_replay.py``）的离线确定性测试。

覆盖：legacy 产物 → ``mode=replay`` run 的重建（selection/skipped、段落框
左上原点转换、page-frames 契约字段、PDF 绑定、``unavailable`` 诚实标注）、
``replay_run_needed`` 判定、``bdt debug`` 自动回放与 ``--run-id replay``
无产物时报错、以及回放不得触碰 ``state.pkl``。

不联网、不调模型、不编译、不用真实 PyMuPDF 打开（PDF 一律 bytes stub）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from babeldoc import debug_recorder as _dr
from babeldoc_tools import __main__ as cli
from babeldoc_tools import debug_replay
from babeldoc_tools import debug_runtime


def _main(*argv: str) -> dict:
    """进程内跑 ``bdt``；返回解析后的 stdout JSON 信封（同 test_debug_cli）。"""
    import contextlib
    import io

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.main(list(argv))
    payload = json.loads(out.getvalue().strip().splitlines()[-1])
    payload["_exit"] = code
    return payload


@pytest.fixture
def workdir(tmp_path: Path):
    wd = tmp_path / "wd"
    wd.mkdir()
    yield wd
    # 兜底清理：保证测试不遗留查看器进程。
    debug_runtime.DebugSession(wd).stop_viewer()


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _legacy_workdir(root: Path) -> Path:
    """手工构造最小 legacy workdir（agent/ 历史产物 + output/test.mono.pdf）。"""
    workdir = root / "legacy"
    agent = workdir / "agent"
    agent.mkdir(parents=True)
    _write_json(
        agent / "anchors.json",
        {
            "rows": [
                {
                    "id": "P01-001",
                    "page": 1,
                    "layout_label": "title",
                    "canonical": "Title text",
                    "markdown": "Title text",
                },
                {
                    "id": "P01-002",
                    "page": 1,
                    "layout_label": "text",
                    "canonical": "Body text",
                    "markdown": "Body text",
                },
            ],
            "skipped": [
                {
                    "id": "P01-003",
                    "page": 1,
                    "layout_label": "figure",
                    "source": "Figure 1",
                    "reason": "protected",
                }
            ],
        },
    )
    _write_json(
        agent / "layout_geometry.json",
        {
            "version": 1,
            "pages": 1,
            "page_info": [{"page": 1, "cropbox": [0, 0, 612, 792]}],
            "paragraphs": [
                {
                    "id": "P01-001",
                    "page": 1,
                    "layout_label": "title",
                    "src_box": [66, 672, 544, 713],
                    "layout_box": [66, 672, 544, 713],
                }
            ],
        },
    )
    (agent / "translated.jsonl").write_text(
        json.dumps({"id": "P01-001", "target": "标题"}, ensure_ascii=False)
        + "\n"
        + json.dumps({"id": "P01-002", "target": "正文"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    _write_json(agent / "apply_report.json", {"applied": 2})
    (agent / "document.md").write_text("# doc\n", encoding="utf-8")
    output = workdir / "output"
    output.mkdir()
    (output / "test.mono.pdf").write_bytes(b"%PDF-stub")
    return workdir


def _build_locked(workdir: Path, **kwargs):
    """拿写锁跑 ``build_replay_run``（调用方负责先拿锁的约定）。"""
    session = debug_runtime.DebugSession(workdir)
    session.acquire_write_lock()
    try:
        return debug_replay.build_replay_run(workdir, **kwargs)
    finally:
        session.release_write_lock()


def _run_dir(workdir: Path, run_id: str) -> Path:
    return workdir / "debug" / "runs" / run_id


# --------------------------------------------------------------------------- #
# build_replay_run：legacy 产物重建
# --------------------------------------------------------------------------- #
def test_build_replay_run_from_legacy_workdir(tmp_path):
    workdir = _legacy_workdir(tmp_path)
    run_id = _build_locked(workdir)
    assert run_id

    run_dir = _run_dir(workdir, run_id)
    manifest = json.loads((run_dir / "manifest.json").read_text("utf-8"))
    assert manifest["mode"] == "replay"
    assert manifest["source"] == "legacy_workdir"
    assert manifest["status"] == "finished"
    assert manifest["events_available"] is False
    unavailable = manifest["unavailable"]
    assert "layout_snapshot" in unavailable
    assert "native_chars" in unavailable
    assert "events_stream" in unavailable
    # output 下唯一 mono 被绑定 → 不应标 unavailable；
    # 无源 PDF 且根目录无 *.en.pdf（根目录 *.pdf 不再自动发现）→ 标 unavailable。
    assert "mono_pdf" not in unavailable
    assert "source_pdf" in unavailable

    # selection：真实 skipped 行（含 reason 之外的可回放字段）如实写出。
    selection = json.loads(
        (run_dir / "snapshots" / "parse" / "selection.json").read_text("utf-8")
    )
    assert len(selection["skipped"]) == 1
    skipped_row = selection["skipped"][0]
    assert skipped_row["id"] == "P01-003"
    assert skipped_row["page"] == 1
    assert skipped_row["source"] == "Figure 1"
    assert skipped_row["layout_label"] == "figure"
    assert skipped_row["replayed"] is True
    assert len(selection["selected"]) == 2

    # paragraphs：IL 左下原点 → 左上原点（y 翻转），带契约字段。
    paragraphs = json.loads(
        (run_dir / "snapshots" / "parse" / "paragraphs.json").read_text("utf-8")
    )
    assert paragraphs["version"] == 1
    assert paragraphs["coord_system"] == "pdf_topleft"
    assert len(paragraphs["entities"]) == 1
    box = paragraphs["entities"][0]["box"]
    # [66,672,544,713] @ page height 792 → y0 = 792-713 = 79.0, y1 = 792-672 = 120.0
    assert box["x0"] == 66.0
    assert box["y0"] == pytest.approx(79.0, abs=0.1)
    assert box["x1"] == 544.0
    assert box["y1"] == pytest.approx(120.0, abs=0.1)
    assert box["y0"] < box["y1"]

    # page-frames：补齐 version / coord_system 契约字段。
    frames = json.loads(
        (run_dir / "snapshots" / "parse" / "page-frames.json").read_text("utf-8")
    )
    assert frames["version"] == 1
    assert frames["coord_system"] == "pdf_topleft"
    assert frames["frames"][0]["width"] == 612.0
    assert frames["frames"][0]["height"] == 792.0

    # mono 绑定成功。
    assert (run_dir / "artifacts" / "build" / "mono.pdf").is_file()

    events = _dr.read_events(run_dir)
    assert events
    kinds = [event["kind"] for event in events]
    assert "stage_started" in kinds
    assert "stage_finished" in kinds


def test_replay_uses_bindings_and_explicit_overrides(tmp_path):
    workdir = _legacy_workdir(tmp_path)
    bound_source = tmp_path / "bound-source.pdf"
    bound_source.write_bytes(b"%PDF-bound")
    explicit_mono = tmp_path / "explicit.mono.pdf"
    explicit_mono.write_bytes(b"%PDF-explicit-mono")
    _write_json(
        workdir / "debug" / "bindings.json",
        {"source_pdf": str(bound_source)},
    )

    run_id = _build_locked(workdir, mono=str(explicit_mono))
    assert run_id

    run_dir = _run_dir(workdir, run_id)
    manifest = json.loads((run_dir / "manifest.json").read_text("utf-8"))
    assert "source_pdf" not in manifest["unavailable"]
    assert (run_dir / "artifacts" / "parse" / "input.pdf").is_file()
    assert (run_dir / "artifacts" / "parse" / "input.pdf").read_bytes() == b"%PDF-bound"
    mono = run_dir / "artifacts" / "build" / "mono.pdf"
    assert mono.is_file()
    assert mono.read_bytes() == b"%PDF-explicit-mono"


def test_replay_skips_when_nothing_to_replay(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    session = debug_runtime.DebugSession(empty)
    session.acquire_write_lock()
    try:
        assert debug_replay.build_replay_run(empty) is None
    finally:
        session.release_write_lock()

    # 无 agent/ → 无需回放
    assert debug_replay.replay_run_needed(empty) is False

    workdir = _legacy_workdir(tmp_path)
    # 有 agent/ 但 debug/runs 为空 → 需要回放
    assert debug_replay.replay_run_needed(workdir) is True

    # debug/runs 已有 run 目录 → 不再自动回放
    run_id = _build_locked(workdir)
    assert run_id
    assert debug_replay.replay_run_needed(workdir) is False


def test_replay_does_not_touch_state_pickle(tmp_path):
    workdir = _legacy_workdir(tmp_path)
    state = workdir / "agent" / "state.pkl"
    payload = b"\x80\x04not-a-real-pickle\xff\x00"
    state.write_bytes(payload)

    run_id = _build_locked(workdir)
    assert run_id
    # 回放不得反序列化/修改 state.pkl。
    assert state.read_bytes() == payload


# --------------------------------------------------------------------------- #
# bdt debug：自动回放 / --run-id replay 无产物
# --------------------------------------------------------------------------- #
def test_bdt_debug_auto_replays_legacy_workdir(tmp_path):
    workdir = _legacy_workdir(tmp_path)
    try:
        payload = _main("debug", "--workdir", str(workdir), "--no-open")
        assert payload["ok"] is True, payload
        assert payload["data"]["url"].startswith("http://127.0.0.1:")
        runs_dir = workdir / "debug" / "runs"
        manifests = sorted(runs_dir.glob("*/manifest.json"))
        assert manifests
        replay_manifests = [
            json.loads(path.read_text("utf-8")) for path in manifests
        ]
        assert any(
            manifest.get("mode") == "replay" for manifest in replay_manifests
        )
    finally:
        debug_runtime.DebugSession(workdir).stop_viewer()


def test_bdt_debug_replay_flag_without_artifacts_errors(workdir):
    payload = _main(
        "debug", "--workdir", str(workdir), "--no-open", "--run-id", "replay"
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == "replay_unavailable"
