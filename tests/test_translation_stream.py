"""Offline streaming boundary, preview isolation, and moved-workdir regressions."""
import json
import shlex
import sys
import threading

import pytest
from babeldoc.tools.agent.prepared_pdf import resolve_source_pdf
from babeldoc_tools.common import ToolError
from babeldoc_tools.common import run_translator
from babeldoc_tools.harnesses import HarnessStream
from babeldoc_tools.stream_preview import StreamPreview
from babeldoc_tools.translate import TranslationBlocks


def anchors(tmp_path):
    agent = tmp_path / "agent"
    agent.mkdir(exist_ok=True)
    (agent / "anchors.json").write_text(json.dumps({"rows": [
        {"id": "P1", "anchors": [["", "F", "1"]], "layout_label": "text"},
        {"id": "P2", "anchors": [], "layout_label": "text"},
    ]}))


def test_moved_prepared_pdf_prefers_local_even_when_old_exists(tmp_path):
    old = tmp_path / "old/input.pdf"
    old.parent.mkdir()
    old.write_bytes(b"old")
    wd = tmp_path / "moved"
    prepared = wd / "paper/input.pdf"
    prepared.parent.mkdir(parents=True)
    prepared.write_bytes(b"normalized")
    state = {"pdf_path": "paper.pdf", "temp_pdf_path": str(old)}
    assert resolve_source_pdf(state, wd) == prepared
    old.unlink()
    assert resolve_source_pdf(state, wd) == prepared
    prepared.unlink()
    (wd / "source.pdf").write_bytes(b"raw original")
    assert resolve_source_pdf(state, wd) is None


def test_prepared_relative_record_and_root_fallback(tmp_path):
    source = tmp_path / "input.pdf"
    source.write_bytes(b"normalized")
    assert resolve_source_pdf({"temp_pdf_path": "gone/input.pdf"}, tmp_path) == source
    nested = tmp_path / "custom"
    nested.mkdir()
    source.rename(nested / "prepared.pdf")
    assert resolve_source_pdf({"temp_pdf_path": "custom/prepared.pdf"}, tmp_path) == nested / "prepared.pdf"


def test_block_waits_for_next_complete_marker_and_valid_anchors(tmp_path):
    anchors(tmp_path)
    seen = []
    stream = TranslationBlocks(tmp_path, lambda *args: seen.append(args))
    stream.feed("<!-- id=P1 -->译文 [[F1]]\n<!-- id=P")
    assert seen == []
    stream.feed("2 -->末段")
    assert seen == [("P1", "译文 [[F1]]", "text", 1, 2)]
    stream.finish()
    assert seen[-1] == ("P2", "末段", "text", 2, 2)
    bad = TranslationBlocks(tmp_path, lambda *_args: pytest.fail("invalid block consumed"))
    bad.feed("<!-- id=P1 -->缺锚点<!-- id=unknown -->text")
    bad.finish()


def test_process_stream_observable_before_command_exit(tmp_path):
    anchors(tmp_path)
    ack = tmp_path / "ack"
    script = tmp_path / "translator.py"
    script.write_text('''import sys, time
from pathlib import Path
sys.stdin.read()
sys.stdout.write("<!-- id=P1 -->译文 [[F1]]\\n<!-- id=P2 -->")
sys.stdout.flush()
deadline = time.monotonic() + 5
while not Path(sys.argv[1]).exists():
    if time.monotonic() > deadline: sys.exit(9)
    time.sleep(.01)
print("末段")
''')
    seen = []

    def completed(*args):
        seen.append(args)
        ack.touch()  # Child cannot exit until the parent has consumed block 1.

    stream = TranslationBlocks(tmp_path, completed)
    result = run_translator("prompt", shlex.join([sys.executable, str(script), str(ack)]),
                            timeout_s=10, on_chunk=stream.feed)
    assert len(seen) == 1
    assert "末段" in result
    stream.finish()
    assert len(seen) == 2


def test_failed_process_never_consumes_unterminated_tail(tmp_path):
    anchors(tmp_path)
    seen = []
    stream = TranslationBlocks(tmp_path, lambda *args: seen.append(args))
    code = 'import sys; print("<!-- id=P1 -->partial [[F1]]", flush=True); sys.exit(1)'
    with pytest.raises(ToolError):
        run_translator("prompt", shlex.join([sys.executable, "-c", code]), on_chunk=stream.feed)
    assert not seen


@pytest.mark.parametrize("harness,event", [
    ("pi", {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "译文"}}),
    ("agy", {"event": "step_update", "step_update": {"text_delta": "译文"}}),
])
def test_harness_stream_splits_json_and_does_not_duplicate_final(harness, event):
    chunks = []
    stream = HarnessStream(harness, chunks.append)
    raw = json.dumps(event) + "\n"
    stream.feed(raw[:15])
    assert chunks == []
    stream.feed(raw[15:])
    assert chunks == ["译文"]
    stream.finish("译文末尾")
    assert chunks == ["译文", "末尾"]
    with pytest.raises(ToolError):
        stream.finish("changed response")


class Recorder:
    run_id = "test-run"

    def __init__(self):
        self.events = []
        self.ready = threading.Event()

    def record_event(self, stage, kind, data):
        self.events.append((kind, data))
        self.ready.set()


def test_preview_coalesces_pending_blocks_and_never_writes_final_pdf(tmp_path, monkeypatch):
    recorder = Recorder()
    preview = StreamPreview(tmp_path, recorder)
    monkeypatch.setattr(preview, "_prepare", lambda: None)
    started = threading.Event()
    release = threading.Event()
    builds = []
    pdf = tmp_path / "render.pdf"
    pdf.write_bytes(b"validated PDF")
    output = tmp_path / "output/final.pdf"
    output.parent.mkdir()
    output.write_bytes(b"previous final")

    def build(blocks):
        builds.append(blocks)
        started.set()
        assert release.wait(5)
        return pdf

    monkeypatch.setattr(preview, "_build", build)
    preview.submit("P1", "first", "text")
    assert started.wait(5)
    preview.submit("P2", "second", "text")
    preview.submit("P3", "third", "text")
    release.set()
    assert recorder.ready.wait(5)
    # Wait for second publication using the same condition as the worker.
    import time
    deadline = time.monotonic() + 5
    while len(recorder.events) < 2 and time.monotonic() < deadline:
        time.sleep(.01)
    preview.close()
    assert [set(batch) for batch in builds] == [{"P1"}, {"P1", "P2", "P3"}]
    assert output.read_bytes() == b"previous final"
    assert all(kind == "preview_ready" for kind, _ in recorder.events)
    assert all((tmp_path / data["artifact"]).read_bytes() == b"validated PDF"
               for _, data in recorder.events)


@pytest.mark.parametrize("failure", [True, False])
def test_failed_or_canceled_preview_never_publishes(tmp_path, monkeypatch, failure):
    recorder = Recorder()
    preview = StreamPreview(tmp_path, recorder)
    monkeypatch.setattr(preview, "_prepare", lambda: None)
    started = threading.Event()
    release = threading.Event()

    def build(_blocks):
        started.set()
        assert release.wait(5)
        if failure:
            raise ValueError("invalid PDF")
        return tmp_path / "does-not-exist.pdf"

    monkeypatch.setattr(preview, "_build", build)
    preview.submit("P1", "text", "text")
    assert started.wait(5)
    with preview.condition:
        preview.stopped = True
    release.set()
    preview.close(failed=True)
    assert not (tmp_path / "preview").exists()
    assert not recorder.events


def test_preview_rejects_invalid_pdf_after_successful_build(tmp_path, monkeypatch):
    recorder = Recorder()
    preview = StreamPreview(tmp_path, recorder)
    (preview.isolated / "agent").mkdir(parents=True)
    anchors(preview.isolated)
    bad_pdf = preview.isolated / "broken.pdf"
    bad_pdf.write_bytes(b"not a pdf")
    (preview.isolated / "agent/reconstruct_report.json").write_text(json.dumps({"mono_pdf": str(bad_pdf)}))
    monkeypatch.setattr(preview, "_prepare", lambda: None)
    monkeypatch.setattr(preview, "_command", lambda _stage: True)
    preview.submit("P2", "second", "text")
    assert recorder.ready.wait(5)
    preview.close()
    assert [kind for kind, _ in recorder.events] == ["preview_failed"]
    assert not (tmp_path / "preview").exists()


def test_failed_translation_removes_provisional_previews(tmp_path):
    preview = StreamPreview(tmp_path, Recorder())
    directory = tmp_path / "preview"
    directory.mkdir()
    current = directory / "test-run-1.pdf"
    other = directory / "other-run-1.pdf"
    current.write_bytes(b"validated preview")
    other.write_bytes(b"another run")
    preview.close(failed=True)
    assert not current.exists()
    assert other.exists()


def test_stream_timeout_stops_child():
    import subprocess

    from babeldoc_tools.process_stream import run_stream

    seen = []
    with pytest.raises(subprocess.TimeoutExpired):
        run_stream([sys.executable, "-c", 'import time; print("partial", flush=True); time.sleep(30)'],
                   input="", timeout=.2, on_stdout=seen.append)
    assert "partial" in "".join(seen)
