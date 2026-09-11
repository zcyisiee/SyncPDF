from __future__ import annotations

import json
from pathlib import Path

import pytest

from babeldoc_core import DocumentBlock
from babeldoc_core import DocumentIR
from babeldoc_core import DocumentJob
from babeldoc_core import FormulaAtom
from babeldoc_core import JobState
from babeldoc_core import validate_translation
from babeldoc_tools import dispatch
from babeldoc_tools import tool_names


def test_protocol_gate_detects_duplicate_order_and_formula(tmp_path):
    ir = DocumentIR(
        blocks=[
            DocumentBlock("P01-001", source_text="Text <formula id='F1'>x</formula>.", formulas=[FormulaAtom("F1", "x")]),
            DocumentBlock("P01-002", source_text="Second", translate=False),
        ]
    )
    translated = """<!-- id=P01-002 label=text -->
Second changed
<!-- id=P01-001 label=text -->
Text <formula id='F1'>y</formula>.
<!-- id=P01-001 label=text -->
Text <formula id='F1'>x</formula>.
"""
    violations = validate_translation(ir, translated)
    assert "duplicate_ids" in violations
    assert "paragraph_order_mismatch" in violations
    assert "skipped_changed:P01-002" in violations
    assert "formula_changed:P01-001:F1" in violations


def test_job_state_machine_manifest_and_resume(tmp_path):
    source = tmp_path / "paper.txt"
    source.write_text("Hello world.\n\nA second paragraph.", encoding="utf-8")
    workdir = tmp_path / "job"
    job = DocumentJob.create(source, workdir)
    assert job.state is JobState.CREATED
    job.parse()
    assert job.state is JobState.PARSED
    assert (workdir / "agent" / "manifest.json").exists()
    assert (workdir / "agent" / "state.pkl").exists()
    job.translate(lambda text, **_: f"译：{text}")
    job.validate_translation()
    resumed = DocumentJob.load(workdir).resume()
    assert resumed.state is JobState.PROTOCOL_VALIDATED
    resumed.review().reconstruct().render()
    assert resumed.state is JobState.ACCEPTED
    manifest = json.loads((workdir / "agent" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["state"] == "accepted"
    assert manifest["input"]["sha256"]
    assert manifest["artifacts"]["translated.md"]


def test_tool_registry_and_job_id_lookup(tmp_path):
    source = tmp_path / "paper.txt"
    source.write_text("Hello", encoding="utf-8")
    workdir = tmp_path / "job"
    created = dispatch("job_create", {"pdf": str(source), "workdir": str(workdir)})
    assert created["ok"]
    job_id = created["job_id"]
    assert {"job_create", "review_protocol", "layout_patch", "export_report"} <= set(tool_names())
    status = dispatch("job_status", {"job_id": job_id})
    assert status["ok"] and status["job_id"] == job_id
    parsed = dispatch("parse_document", {"pdf": str(source), "workdir": str(workdir)})
    assert parsed["ok"]
    translated = dispatch("translate_document", {"job_id": job_id})
    assert translated["ok"]
    checked = dispatch("validate_translation", {"job_id": job_id})
    assert checked["ok"]
    exported = dispatch("export_report", {"job_id": job_id})
    assert exported["ok"]
    assert Path(exported["data"]["report"]).exists()


def test_job_provider_failure_blocks_translation(tmp_path):
    source = tmp_path / "paper.txt"
    source.write_text("Hello", encoding="utf-8")
    job = DocumentJob.create(source, tmp_path / "job").parse()

    def broken(*args, **kwargs):
        raise RuntimeError("gateway down")

    with pytest.raises(Exception):
        job.translate(broken)
    assert job.state is JobState.BLOCKED_TRANSLATION
