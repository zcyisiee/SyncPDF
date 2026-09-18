from __future__ import annotations

import json
import pickle
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from babeldoc.debug_recorder import DebugRecorder
from babeldoc.debug_recorder import read_events
from babeldoc.tools.agent import markdown_view
from babeldoc_tools import common
from babeldoc_tools import translate


@pytest.fixture
def recorder(tmp_path):
    value = DebugRecorder(tmp_path / "debug" / "runs" / "translation-test")
    yield value
    value.close()


def _events(recorder, kind):
    return [item["data"] for item in read_events(recorder.run_dir) if item["kind"] == kind]


def _workdir(path):
    agent = path / "agent"
    agent.mkdir(parents=True)
    rows = [
        {"id": "P01-001", "page": 1, "layout_label": "text", "canonical": "First paragraph.", "markdown": "First paragraph."},
        {"id": "P01-002", "page": 1, "layout_label": "text", "canonical": "Second paragraph.", "markdown": "Second paragraph."},
    ]
    (agent / "anchors.json").write_text(json.dumps({"rows": rows}))
    (agent / "sheet.jsonl").write_text("\n".join(json.dumps({**row, "source": row["canonical"]}) for row in rows))
    (agent / "document.md").write_text(markdown_view.render_rows_markdown(rows))
    with (agent / "state.pkl").open("wb") as handle:
        pickle.dump({"inputs": {row["id"]: SimpleNamespace(unicode=row["canonical"]) for row in rows}}, handle)
    return path


@pytest.mark.parametrize("mode,code", [("ok", None), ("failed", "translator_failed"), ("empty", "translator_empty"), ("timeout", "translator_timeout"), ("oserror", "translator_failed")])
def test_subprocess_evidence_preserves_protocol_and_partial_output(monkeypatch, recorder, mode, code):
    stdout = "原始正文\n" if mode != "empty" else "  "
    stderr = "provider diagnostic"
    received = []

    def run(argv, **kwargs):
        received.append((argv, kwargs))
        if mode == "timeout":
            raise subprocess.TimeoutExpired(argv, 3, output=stdout.encode(), stderr=stderr.encode())
        if mode == "oserror":
            raise FileNotFoundError("fixture unavailable")
        return subprocess.CompletedProcess(argv, 7 if mode == "failed" else 0, stdout, stderr)

    monkeypatch.setattr(common.subprocess, "run", run)
    if code:
        with pytest.raises(common.ToolError) as caught:
            common.run_translator("完整提示词", "offline --model fixture", timeout_s=3, debug_recorder=recorder)
        assert caught.value.code == code
    else:
        assert common.run_translator("完整提示词", "offline --model fixture", timeout_s=3, debug_recorder=recorder) == stdout
    assert received[0][1]["input"] == "完整提示词"
    assert received[0][1]["timeout"] == 3
    call = _events(recorder, "call_finished")[0]
    assert call["status"] == ("ok" if not code else "error")
    assert call["error_code"] == code
    assert call["seconds"] >= 0
    assert (recorder.run_dir / call["prompt"]).read_text() == "完整提示词"
    if mode != "oserror":
        assert (recorder.run_dir / call["stdout"]).read_text() == stdout
        assert (recorder.run_dir / call["stderr"]).read_text() == stderr
    assert "--model" not in json.dumps(call["command"])


def test_retry_versions_keep_unmatched_response_and_do_not_change_translation(tmp_path, monkeypatch, recorder):
    responses = [
        "Unmatched model preface.\n<!-- id=P01-001 label=text -->\n第一段。\n",
        "Retry preface.\n<!-- id=P01-002 label=text -->\n第二段。\n<!-- id=extra label=text -->\nUnrequested block.\n",
    ]
    prompts = []

    def execute(path, debug):
        outputs = iter(responses)

        def run(argv, **kwargs):
            prompts.append(kwargs["input"])
            return subprocess.CompletedProcess(argv, 0, next(outputs), "fixture log")

        monkeypatch.setattr(common.subprocess, "run", run)
        monkeypatch.setattr("babeldoc_tools.process_stream.run_stream", run)
        return translate.translate_document(str(_workdir(path)), translator="offline", debug_recorder=debug)

    plain = execute(tmp_path / "plain", None)
    observed = execute(tmp_path / "observed", recorder)
    assert Path(plain["translated_md"]).read_bytes() == Path(observed["translated_md"]).read_bytes()
    assert prompts[:2] == prompts[2:]
    assert observed["missing_after_retry"] == []
    calls = _events(recorder, "call_finished")
    assert len(calls) == 2
    assert calls[0]["origin"] == "translator.whole"
    assert calls[1]["origin"] == "translator.retry"
    assert [(recorder.run_dir / call["stdout"]).read_text() for call in calls] == responses
    versions = _events(recorder, "text_version")
    assert {version["phase"] for version in versions} >= {"raw", "merged"}
    raw = [json.loads((recorder.run_dir / version["snapshot"]).read_text()) for version in versions if version["phase"] == "raw"]
    assert any(version["unmatched"] for version in raw)
    assert any(row["id"] == "extra" and not row["matched"] for version in raw for row in version["rows"])
    assert recorder.capture_status == {"ok": True}


def test_failed_translation_keeps_current_prompt_and_input(tmp_path, monkeypatch, recorder):
    workdir = _workdir(tmp_path / "failed")

    def fail(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 2, "partial response", "failure log")

    monkeypatch.setattr(common.subprocess, "run", fail)
    monkeypatch.setattr("babeldoc_tools.process_stream.run_stream", fail)
    with pytest.raises(common.ToolError):
        translate.translate_document(str(workdir), translator="offline", debug_recorder=recorder)
    assert recorder.manifest["stages"]["translate"]["status"] == "error"
    refs = recorder.manifest["artifacts"]
    assert any(path.endswith("document.md") for path in refs)
    call = _events(recorder, "call_finished")[0]
    assert (recorder.run_dir / call["stdout"]).read_text() == "partial response"
    assert (recorder.run_dir / call["prompt"]).is_file()


def test_import_does_not_call_provider_and_is_archived_before_overwrite(tmp_path, monkeypatch, recorder):
    workdir = _workdir(tmp_path / "import")
    imported = tmp_path / "import.md"
    original = "<!-- id=P01-001 label=text -->\n导入的第一段。\n"
    imported.write_text(original)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("import must not execute a provider")

    monkeypatch.setattr(common.subprocess, "run", unexpected)
    result = translate.translate_document(str(workdir), markdown=str(imported), debug_recorder=recorder)
    assert result["missing_after_retry"] == ["P01-002"]
    imported.write_text("overwritten")
    versions = _events(recorder, "text_version")
    imported_version = next(item for item in versions if item["phase"] == "imported")
    assert (recorder.run_dir / imported_version["artifact"]).read_text() == original
    assert _events(recorder, "call_finished") == []


def test_apply_keeps_validation_candidates_even_when_writeback_is_rejected(tmp_path, recorder):
    workdir = _workdir(tmp_path / "apply-rejected")
    translated = workdir / "agent" / "translated.md"
    translated.write_text("<!-- id=unknown label=text -->\n未知段落。\n")
    result = translate.apply_translation(str(workdir), debug_recorder=recorder)
    assert result["ok"] is False
    assert result["extra_ids"] == ["unknown"]
    evidence = _events(recorder, "apply_validation")[0]
    payload = json.loads((recorder.run_dir / evidence["snapshot"]).read_text())
    assert payload["extra_ids"] == ["unknown"]
    assert len(payload["entries"]) == 2
    assert payload["writeback_allowed"] is False
    assert not (workdir / "agent" / "translated.jsonl").exists()
