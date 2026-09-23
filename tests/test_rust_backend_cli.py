"""The bdt adapter keeps the Rust engine's result and files observable."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def fake_engine(tmp_path: Path) -> Path:
    script = tmp_path / "syncpdf-cli"
    script.write_text(
        "#!" + sys.executable + "\n"
        "import json, os, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "out = pathlib.Path(args[args.index('--output') + 1])\n"
        "(out.parent / 'invocation.json').write_text(json.dumps({"
        "'args': args, 'layout': os.getenv('SYNCPDF_LAYOUT_DEVICE'), "
        "'tmpdir': os.getenv('TMPDIR'), 'path': os.getenv('PATH')}))\n"
        "def emit(**event):\n"
        "    print(json.dumps(event), flush=True)\n"
        "emit(type='stage_started', stage='preflight')\n"
        "emit(type='paragraph', paragraph_id='P1', page=1, status='pending')\n"
        "emit(type='paragraph', paragraph_id='P1', page=1, status='typeset')\n"
        "mode = os.getenv('FAKE_MODE', 'success')\n"
        "emit(type='paragraph', paragraph_id='P2', page=1, status='typeset' if mode == 'success' else 'fallback')\n"
        "emit(type='paragraph', paragraph_id='P3', page=1, status='not_replaced')\n"
        "if mode != 'unready':\n"
        "    emit(type='page_ready', page=1)\n"
        "mode = os.getenv('FAKE_MODE', 'success')\n"
        "if mode != 'no_final':\n"
        "    emit(type='run_finished', ok=(mode in ('success', 'false_success')))\n"
        "out.write_bytes(b'%PDF-fake')\n"
        "print('engine stderr', file=sys.stderr)\n"
        "sys.exit(0 if mode != 'partial' else 1)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _run(
    pdf: Path, workdir: Path, engine: Path, *, mode: str = "success", extra_args: tuple[str, ...] = ()
) -> tuple:
    env = os.environ.copy()
    env["FAKE_MODE"] = mode
    completed = subprocess.run(  # noqa: S603 - fixed Python executable and argv list
        [
            sys.executable, "-m", "babeldoc_tools", "rust-translate", str(pdf),
            "--workdir", str(workdir), "--engine", str(engine), "--pages", "1-3",
            "--layout-device", "coreml", *extra_args,
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed, json.loads(completed.stdout)


def test_success_keeps_one_json_envelope_and_engine_events(tmp_path: Path, fake_engine: Path) -> None:
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-input")
    workdir = tmp_path / "run"
    completed, payload = _run(pdf, workdir, fake_engine)
    assert completed.returncode == 0
    assert len(completed.stdout.splitlines()) == 1
    assert payload["ok"] is True
    assert payload["data"]["successful_blocks"] == 2
    assert payload["data"]["unsuccessful_blocks"] == 0
    assert payload["data"]["not_replaced_blocks"] == 1
    assert (workdir / "events.jsonl").read_text().count("\n") == 7
    assert (workdir / "stderr.log").read_text().strip() == "engine stderr"
    assert (workdir / "result.json").is_file()
    invocation = json.loads((workdir / "invocation.json").read_text())
    assert payload["data"]["typography"] == {"font_scale": 1.0, "line_height": None}
    assert "--font-scale" not in invocation["args"]
    assert "--line-height" not in invocation["args"]
    assert invocation["args"][:1] == ["translate"]
    assert invocation["args"][invocation["args"].index("--translator") + 1] == "pi"
    assert invocation["args"][invocation["args"].index("--pages") + 1] == "1-3"
    assert invocation["layout"] == "coreml"
    assert invocation["tmpdir"] == str(workdir / "tmp")
    assert invocation["path"] == os.environ.get("PATH")
    assert "preflight" in completed.stderr
    assert "page 1 ready" in completed.stderr
    assert pdf.read_bytes() == b"%PDF-input"


@pytest.mark.parametrize(
    ("mode", "code"),
    [("partial", "engine_incomplete"), ("no_final", "engine_result_missing"), ("false_success", "engine_incomplete")],
)
def test_incomplete_results_are_failures(
    tmp_path: Path, fake_engine: Path, mode: str, code: str
) -> None:
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-input")
    workdir = tmp_path / "run"
    completed, payload = _run(pdf, workdir, fake_engine, mode=mode)
    assert completed.returncode == 1
    assert payload["error"]["code"] == code
    assert payload["error"]["successful_blocks"] == 1
    assert payload["error"]["unsuccessful_blocks"] == 1
    assert payload["error"]["output_exists"] is True
    assert json.loads((workdir / "result.json").read_text()) == payload


def test_missing_engine_and_output_conflict(tmp_path: Path, fake_engine: Path) -> None:
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-input")
    workdir = tmp_path / "run"
    missing, missing_payload = _run(pdf, workdir, tmp_path / "missing")
    assert missing.returncode == 1
    assert missing_payload["error"]["code"] == "engine_missing"
    assert not workdir.exists()

    workdir.mkdir()
    translated = workdir / "translated.pdf"
    translated.write_bytes(b"%PDF-input")
    conflict, conflict_payload = _run(translated, workdir, fake_engine)
    assert conflict.returncode == 1
    assert conflict_payload["error"]["code"] == "input_output_conflict"
    assert translated.read_bytes() == b"%PDF-input"


def test_existing_run_logs_are_not_overwritten(tmp_path: Path, fake_engine: Path) -> None:
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-input")
    workdir = tmp_path / "run"
    workdir.mkdir()
    (workdir / "events.jsonl").write_text("keep\n")
    completed, payload = _run(pdf, workdir, fake_engine)
    assert completed.returncode == 1
    assert payload["error"]["code"] == "workdir_used"
    assert (workdir / "events.jsonl").read_text() == "keep\n"


def test_workdir_io_error_is_a_json_failure(tmp_path: Path, fake_engine: Path) -> None:
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-input")
    workdir = tmp_path / "file-not-directory"
    workdir.write_text("keep")
    completed, payload = _run(pdf, workdir, fake_engine)
    assert completed.returncode == 1
    assert payload["error"]["code"] == "artifact_io"
    assert workdir.read_text() == "keep"


def test_cached_from_copies_database_without_writing_source(tmp_path: Path, fake_engine: Path) -> None:
    from babeldoc_tools import rust_backend

    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-input")
    previous = tmp_path / "previous"
    (previous / "cache").mkdir(parents=True)
    db = previous / "cache/translate.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE marker (value TEXT)")
        connection.execute("INSERT INTO marker VALUES ('real translation')")
    original = db.read_bytes()
    workdir = tmp_path / "recompile"
    result = rust_backend.translate_pdf(
        pdf=str(source), workdir=str(workdir), pages=None, model="unused", thinking="low",
        source_lang="auto", target_lang="zh-CN", layout_device="cpu", engine=str(fake_engine),
        cached_from=str(previous),
    )
    assert result["ok"]
    assert "--cache-only" in json.loads((workdir / "invocation.json").read_text())["args"]
    with sqlite3.connect(workdir / "cache/translate.db") as connection:
        assert connection.execute("SELECT value FROM marker").fetchone() == ("real translation",)
    assert db.read_bytes() == original


def test_typeset_without_saved_page_is_not_reported_as_written(tmp_path: Path, fake_engine: Path) -> None:
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-input")
    completed, payload = _run(pdf, tmp_path / "run", fake_engine, mode="unready")
    assert completed.returncode == 1
    assert payload["error"]["successful_blocks"] == 0
    assert payload["error"]["typeset_blocks"] == 1
    assert payload["error"]["saved_pages"] == 0


def test_explicit_typography_forwards_multipliers_and_records_them(
    tmp_path: Path, fake_engine: Path
) -> None:
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-input")
    workdir = tmp_path / "run"
    completed, payload = _run(
        pdf, workdir, fake_engine, extra_args=("--font-scale", "0.9", "--line-height", "1.3")
    )
    assert completed.returncode == 0
    args = json.loads((workdir / "invocation.json").read_text())["args"]
    assert args[args.index("--font-scale") + 1] == "0.9"
    assert args[args.index("--line-height") + 1] == "1.3"
    assert payload["data"]["typography"] == {"font_scale": 0.9, "line_height": 1.3}
    assert json.loads((workdir / "result.json").read_text()) == payload


@pytest.mark.parametrize("flag", ["--font-scale", "--line-height"])
@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_invalid_typography_rejected_before_launch_or_artifacts(
    tmp_path: Path, fake_engine: Path, flag: str, value: str
) -> None:
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-input")
    workdir = tmp_path / "run"
    completed, payload = _run(pdf, workdir, fake_engine, extra_args=(f"{flag}={value}",))
    assert completed.returncode == 1
    assert payload["error"]["code"] == "invalid_typography"
    assert not workdir.exists()
