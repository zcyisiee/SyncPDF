"""The bdt adapter keeps the Rust engine's result and files observable."""

from __future__ import annotations

import json
import os
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
        "emit(type='paragraph', paragraph_id='P1', status='pending')\n"
        "emit(type='paragraph', paragraph_id='P1', status='typeset')\n"
        "emit(type='paragraph', paragraph_id='P2', status='fallback')\n"
        "emit(type='paragraph', paragraph_id='P3', status='not_replaced')\n"
        "emit(type='page_ready', page=0)\n"
        "mode = os.getenv('FAKE_MODE', 'success')\n"
        "if mode != 'no_final':\n"
        "    emit(type='run_finished', ok=(mode == 'success'))\n"
        "out.write_bytes(b'%PDF-fake')\n"
        "print('engine stderr', file=sys.stderr)\n"
        "sys.exit(0 if mode != 'partial' else 1)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _run(pdf: Path, workdir: Path, engine: Path, *, mode: str = "success") -> tuple:
    env = os.environ.copy()
    env["FAKE_MODE"] = mode
    completed = subprocess.run(  # noqa: S603 - fixed Python executable and argv list
        [
            sys.executable, "-m", "babeldoc_tools", "rust-translate", str(pdf),
            "--workdir", str(workdir), "--engine", str(engine), "--pages", "1-3",
            "--layout-device", "coreml",
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
    assert payload["data"]["successful_blocks"] == 1
    assert payload["data"]["unsuccessful_blocks"] == 1
    assert payload["data"]["not_replaced_blocks"] == 1
    assert (workdir / "events.jsonl").read_text().count("\n") == 7
    assert (workdir / "stderr.log").read_text().strip() == "engine stderr"
    assert (workdir / "result.json").is_file()
    invocation = json.loads((workdir / "invocation.json").read_text())
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
    [("partial", "engine_incomplete"), ("no_final", "engine_result_missing")],
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
