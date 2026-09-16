"""``babeldoc.debug_recorder`` 采集核心的离线确定性测试。

不依赖 PDF、网络或真实模型：pymupdf 仅用于内存构造旋转页做几何断言。
"""

from __future__ import annotations

import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pymupdf
import pytest
from babeldoc.debug_recorder import ARTIFACTS_DIR
from babeldoc.debug_recorder import EVENTS_FILE
from babeldoc.debug_recorder import MANIFEST_FILE
from babeldoc.debug_recorder import SCHEMA_VERSION
from babeldoc.debug_recorder import SNAPSHOTS_DIR
from babeldoc.debug_recorder import Box
from babeldoc.debug_recorder import DebugRecorder
from babeldoc.debug_recorder import Entity
from babeldoc.debug_recorder import NullRecorder
from babeldoc.debug_recorder import PageFrame
from babeldoc.debug_recorder import Relation
from babeldoc.debug_recorder import frame_from_pymupdf_page
from babeldoc.debug_recorder import get_current
from babeldoc.debug_recorder import model as model_mod
from babeldoc.debug_recorder import new_run_id
from babeldoc.debug_recorder import read_events
from babeldoc.debug_recorder import recorder as recorder_mod
from babeldoc.debug_recorder import set_current

RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")


def _recorder(
    tmp_path: Path, *, run_id: str = "20260916T083000Z-000001"
) -> DebugRecorder:
    return DebugRecorder(tmp_path / "debug" / "runs" / run_id)


@pytest.fixture(autouse=True)
def _reset_current():
    """每条测试前后复位进程内上下文，避免相互污染。"""
    set_current(None)
    yield
    set_current(None)


# --------------------------------------------------------------------------- #
# run_id 与目录布局
# --------------------------------------------------------------------------- #
def test_new_run_id_unique_and_sortable():
    ids = [new_run_id() for _ in range(200)]
    assert all(RUN_ID_RE.match(rid) for rid in ids), ids[:3]
    assert len(set(ids)) == len(ids)
    # 同一进程内后缀单调递增：排序不改变顺序（可复现、可排序）。
    assert sorted(ids) == ids


def test_run_dir_layout_and_manifest():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        recorder = _recorder(Path(tmp))
        assert (
            recorder.run_dir == Path(tmp) / "debug" / "runs" / "20260916T083000Z-000001"
        )
        assert recorder.run_id == "20260916T083000Z-000001"
        assert recorder.manifest_path == recorder.run_dir / MANIFEST_FILE
        assert recorder.events_path == recorder.run_dir / EVENTS_FILE
        assert (recorder.run_dir / SNAPSHOTS_DIR).is_dir()
        assert (recorder.run_dir / ARTIFACTS_DIR).is_dir()
        assert recorder.events_path.exists()

        manifest = json.loads(recorder.manifest_path.read_text(encoding="utf-8"))
        assert manifest["schema_version"] == SCHEMA_VERSION
        assert manifest["run_id"] == recorder.run_id
        assert manifest["status"] == "running"
        assert manifest["finished_at"] is None
        assert manifest["input"] == {}
        assert manifest["config"] == {"stages": [], "options": {}}
        assert manifest["artifact_count"] == 0
        recorder.close()


def test_stage_lifecycle_and_input_config():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "paper.pdf"
        source.write_bytes(b"%PDF-1.4 stub\n")
        recorder = _recorder(Path(tmp))
        recorder.set_config(stages=["parse"], options={"layout": "paddle"})
        recorder.add_input("pdf", source)
        recorder.start_stage("parse")
        recorder.record_event("parse", "stage_started", {"pages": 2})
        recorder.finish_stage("parse")
        recorder.finish()

        manifest = json.loads(recorder.manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "finished"
        assert manifest["finished_at"] is not None
        assert manifest["config"]["stages"] == ["parse"]
        assert manifest["config"]["options"] == {"layout": "paddle"}
        assert manifest["input"]["pdf"]["sha256"]
        assert manifest["stages"]["parse"]["status"] == "ok"
        assert manifest["stages"]["parse"]["events"] == 1
        recorder.close()


# --------------------------------------------------------------------------- #
# 事件：线程安全序号 + 尾行容错 + 增量读取
# --------------------------------------------------------------------------- #
def test_events_thread_safe_sequence():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        recorder = _recorder(Path(tmp))
        barrier = threading.Barrier(20)

        def worker(worker_index: int) -> None:
            barrier.wait()
            for index in range(50):
                recorder.record_event(
                    "build", "compile_candidate", {"w": worker_index, "i": index}
                )

        with ThreadPoolExecutor(max_workers=20) as pool:
            list(pool.map(worker, range(20)))

        events = read_events(recorder.run_dir)
        assert len(events) == 1000
        seqs = [event["seq"] for event in events]
        assert seqs == list(range(1, 1001))  # 严格递增、无重复
        assert all(event["stage"] == "build" for event in events)
        assert all(event["at"] for event in events)
        recorder.close()


def test_read_events_ignores_partial_and_filters_after_seq():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        recorder = _recorder(Path(tmp))
        for index in range(5):
            recorder.record_event("parse", "tick", {"i": index})
        recorder.close()

        # 手工追加半行（模拟读到正在写入的尾行）与损坏行。
        with recorder.events_path.open("a", encoding="utf-8") as handle:
            handle.write('{"seq": 6, "stage": "parse", "kind": "tick", "data":')
            handle.write("\n")
            handle.write("not-json\n")

        events = read_events(recorder.run_dir)
        assert [event["seq"] for event in events] == [1, 2, 3, 4, 5]

        tail = read_events(recorder.run_dir, after_seq=3)
        assert [event["seq"] for event in tail] == [4, 5]
        assert read_events(recorder.run_dir / "no-such-run") == []


def test_null_recorder_and_disabled_convention():
    """关闭状态：``get_current()`` 为 None，业务代码判 ``if recorder:``。"""
    assert get_current() is None

    calls: list[object] = []
    recorder = get_current()
    if recorder:  # 关闭时的业务写法（测试文档化这一点）
        calls.append(recorder)
    assert calls == []

    null = NullRecorder()
    assert null.capture_status == {"ok": True}
    assert null.record_event("parse", "kind", {}) is None
    assert null.write_snapshot("parse", "name", {}) is None
    assert null.archive_file("parse", "name", "missing") is None
    null.start_stage("parse")
    null.finish_stage("parse")
    null.finish()
    null.close()


def test_current_recorder_visible_in_thread_pool():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        recorder = _recorder(Path(tmp))
        set_current(recorder)
        with ThreadPoolExecutor(max_workers=4) as pool:
            seen = list(pool.map(lambda _: get_current(), range(4)))
        assert all(item is recorder for item in seen)

        set_current(None)
        with ThreadPoolExecutor(max_workers=4) as pool:
            seen = list(pool.map(lambda _: get_current(), range(4)))
        assert seen == [None, None, None, None]


# --------------------------------------------------------------------------- #
# 原子性
# --------------------------------------------------------------------------- #
def test_snapshot_write_is_atomic_and_leaves_no_tmp(tmp_path):
    recorder = _recorder(tmp_path)
    relative = recorder.write_snapshot("parse", "page-01", {"page": 1})
    assert relative == "snapshots/parse/page-01.json"
    assert (recorder.run_dir / relative).is_file()
    payload = json.loads((recorder.run_dir / relative).read_text(encoding="utf-8"))
    assert payload == {"page": 1}
    assert list((recorder.run_dir / SNAPSHOTS_DIR).rglob("*.tmp")) == []
    recorder.close()


def test_snapshot_failure_does_not_leak_tmp_and_is_reported(tmp_path, monkeypatch):
    recorder = _recorder(tmp_path)
    real_replace = os.replace

    def boom(src, dst, *args, **kwargs):
        if "snapshots" in str(dst):
            raise OSError("simulated replace failure")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(recorder_mod.os, "replace", boom)
    result = recorder.write_snapshot("parse", "page-01", {"page": 1})

    assert result is None
    snapshots_dir = recorder.run_dir / SNAPSHOTS_DIR
    assert list(snapshots_dir.rglob("*.tmp")) == []
    status = recorder.capture_status
    assert status["ok"] is False
    assert status["errors"][0]["operation"] == "write_snapshot"
    monkeypatch.undo()
    recorder.close()


def test_manifest_write_uses_atomic_replace(tmp_path, monkeypatch):
    recorder = _recorder(tmp_path)
    real_replace = os.replace
    replaced_dsts: list[str] = []

    def spy(src, dst, *args, **kwargs):
        replaced_dsts.append(str(dst))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(recorder_mod.os, "replace", spy)
    recorder.start_stage("parse")

    assert str(recorder.manifest_path) in replaced_dsts
    monkeypatch.undo()
    recorder.close()


def test_archive_file_copies_and_reports_missing(tmp_path):
    recorder = _recorder(tmp_path)
    source = tmp_path / "mono.pdf"
    source.write_bytes(b"%PDF-1.4 payload\n")

    relative = recorder.archive_file("build", "pdf/mono.pdf", source)
    assert relative == "artifacts/build/pdf/mono.pdf"
    archived = recorder.run_dir / relative
    assert archived.read_bytes() == source.read_bytes()
    assert recorder.capture_status == {"ok": True}
    manifest = json.loads(recorder.manifest_path.read_text(encoding="utf-8"))
    assert manifest["artifact_count"] == 1

    missing = recorder.archive_file("build", "pdf/absent.pdf", tmp_path / "nope.pdf")
    assert missing is None  # 不抛出
    status = recorder.capture_status
    assert status["ok"] is False
    assert status["errors"][0]["operation"] == "archive_file"
    assert status["errors"][0]["type"] == "FileNotFoundError"
    # 失败不产生残留临时文件
    assert list((recorder.run_dir / ARTIFACTS_DIR).rglob("*.tmp")) == []
    recorder.close()


def test_capture_status_aggregates_multiple_errors(tmp_path):
    recorder = _recorder(tmp_path)
    recorder.archive_file("parse", "a", tmp_path / "missing-a")
    recorder.archive_file("parse", "b", tmp_path / "missing-b")
    status = recorder.capture_status
    assert status["ok"] is False
    assert len(status["errors"]) == 2
    assert all(error["operation"] == "archive_file" for error in status["errors"])
    recorder.close()


# --------------------------------------------------------------------------- #
# 数据契约
# --------------------------------------------------------------------------- #
def test_frame_from_pymupdf_page_reads_geometry_and_rotation():
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=300)
    page.set_rotation(90)

    frame = frame_from_pymupdf_page(page, 1)
    assert frame.page_index == 0
    assert frame.page_number_original == 1
    assert frame.page_number_displayed == 1
    assert frame.rotation == 90
    assert frame.mediabox == [0.0, 0.0, 200.0, 300.0]
    assert frame.cropbox == [0.0, 0.0, 200.0, 300.0]
    # page.rect 是未旋转的裁剪显示尺寸（旋转后宽高互换）
    assert frame.width == 300.0
    assert frame.height == 200.0
    assert frame.display_matrix is not None
    assert len(frame.display_matrix) == 6
    assert frame.to_dict()["rotation"] == 90
    doc.close()


def test_contract_dataclasses_serialize():
    box = Box.from_rect((1.0, 2.0, 3.0, 4.0))
    assert box is not None
    assert box.coord_system == model_mod.PDF_TOPLEFT
    assert box.to_list() == [1.0, 2.0, 3.0, 4.0]
    assert Box.from_rect(None) is None

    entity = Entity(id="P01-001", kind="paragraph", label="text", page=1, box=box)
    payload = entity.to_dict()
    assert payload["id"] == "P01-001"
    assert payload["box"]["x0"] == 1.0
    assert payload["children_ids"] == []

    relation = Relation(from_id="P01-001", to_id="p0-b0", kind="layout", method="iou")
    assert relation.to_dict() == {
        "from_id": "P01-001",
        "to_id": "p0-b0",
        "kind": "layout",
        "method": "iou",
        "ambiguous": False,
    }

    frame = PageFrame(page_index=0, width=100.0, height=200.0)
    assert frame.to_dict()["page_index"] == 0


def test_concurrent_publication_keeps_manifest_and_artifact_refs(tmp_path):
    recorder = _recorder(tmp_path)

    def publish(index):
        name = recorder.new_id("call")
        text = recorder.archive_text("translate", f"{name}/stdout.txt", str(index))
        snapshot = recorder.write_snapshot("translate", name, {"stdout": text})
        recorder.record_event("translate", "call_finished", {"snapshot": snapshot})
        return name

    with ThreadPoolExecutor(max_workers=8) as pool:
        names = list(pool.map(publish, range(40)))
    recorder.finish()
    manifest = json.loads(recorder.manifest_path.read_text())
    assert len(set(names)) == 40
    assert manifest["artifact_count"] == 40
    assert len(manifest["artifacts"]) == 40
    assert len(manifest["snapshots"]) == 40
    assert recorder.capture_status == {"ok": True}
    for artifact in manifest["artifacts"].values():
        assert artifact["sha256"]
        assert (recorder.run_dir / artifact["path"]).is_file()
    recorder.close()


def test_text_capture_redacts_known_credentials_not_pipeline_inputs(tmp_path):
    recorder = _recorder(tmp_path)
    secret = "-".join(("fixture", "credential", "value"))
    command = f"provider --api-key {secret} --model offline"
    recorder.register_command(command)
    source = f"A paper paragraph. Bearer {secret}\nstdout: {secret}"
    path = recorder.archive_text("translate", "call/stdout.txt", source)
    recorder.record_event("translate", "failed", {"error": command})
    captured = (recorder.run_dir / path).read_text()
    assert secret not in captured
    assert "A paper paragraph." in captured
    assert "[REDACTED]" in captured
    assert secret not in recorder.events_path.read_text()
    assert secret in source
    recorder.close()


def test_archive_paths_cannot_escape_run(tmp_path):
    recorder = _recorder(tmp_path)
    assert recorder.archive_text("translate", "../../../../outside.txt", "no") is None
    assert not (tmp_path / "outside.txt").exists()
    assert recorder.capture_status["ok"] is False
    recorder.close()
