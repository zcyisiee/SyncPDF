from __future__ import annotations

import re
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest
from babeldoc.debug_recorder import DebugRecorder
from babeldoc.debug_recorder import read_events
from babeldoc.format.pdf.document_il.backend.latex_bbox import renderer as single_mod
from babeldoc.format.pdf.document_il.backend.latex_bbox.capability import (
    LatexCapability,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import (
    BboxStampRenderer,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import StampRequest
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import StampResult
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer_batch import (
    BatchStampRenderer,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.stamp_cache import StampCache
from babeldoc.format.pdf.document_il.backend.latex_bbox.stamp_cache import (
    build_stamp_cache,
)
from babeldoc.tools.agent import workflow
from babeldoc_tools import layout

CAPABILITY = LatexCapability(available=True, xelatex_path="/offline/xelatex")


@pytest.fixture
def recorder(tmp_path):
    value = DebugRecorder(tmp_path / "debug" / "runs" / "build-test")
    yield value
    value.close()


def _events(recorder, kind):
    return [event["data"] for event in read_events(recorder.run_dir) if event["kind"] == kind]


def _pdf(path, text="fit", pages=1, width=200, height=50):
    with pymupdf.open() as doc:
        for _ in range(pages):
            page = doc.new_page(width=width, height=height)
            page.insert_text((5, 18), text, fontsize=10)
        doc.save(path)
    return str(path)


def _request(key="P01-001"):
    return StampRequest(key=key, body="fit", width=200, height=50, font_size=10, lead=15, expected_text="fit")


def _compiler(monkeypatch, *, fail_calls=0, timeout=False, attribution=True):
    calls = []

    def run(argv, **kwargs):
        tex_path = Path(argv[-1])
        tex = tex_path.read_text()
        calls.append({"argv": list(argv), "tex": tex})
        if timeout:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output=b"partial compiler stdout", stderr=b"partial compiler stderr")
        starts = re.findall(r"\\message\{@@S (\d+)@@\}", tex)
        pages = len(starts) or 1
        _pdf(tex_path.with_suffix(".pdf"), pages=pages)
        overflow = "Overfull \\hbox (6pt too wide)\n" if len(calls) <= fail_calls else ""
        log = "\n".join(f"@@S {index}@@\n{overflow}@@E {index}@@" for index in starts) if starts else overflow
        if not attribution:
            log = "unattributed compiler output"
        tex_path.with_suffix(".log").write_text(log)
        return subprocess.CompletedProcess(argv, 0, log, "compiler stderr")

    monkeypatch.setattr(single_mod.subprocess, "run", run)
    return calls


def _observable_pdf(path):
    with pymupdf.open(path) as doc:
        return [(tuple(page.rect), page.get_text(), page.get_pixmap().samples, page.get_links()) for page in doc]


def test_single_candidate_order_fit_and_raw_artifacts_match_normal_run(tmp_path, monkeypatch, recorder):
    plain_calls = _compiler(monkeypatch, fail_calls=2)
    plain = BboxStampRenderer(CAPABILITY).render_one(_request(), tmp_path / "plain")
    observed_calls = _compiler(monkeypatch, fail_calls=2)
    observed = BboxStampRenderer(CAPABILITY, debug_recorder=recorder).render_one(_request(), tmp_path / "observed")
    assert [item["tex"] for item in plain_calls] == [item["tex"] for item in observed_calls]
    assert (plain.ok, plain.font_size, plain.lead, plain.scale, plain.compile_attempts, plain.reason) == (observed.ok, observed.font_size, observed.lead, observed.scale, observed.compile_attempts, observed.reason)
    assert _observable_pdf(plain.pdf_path) == _observable_pdf(observed.pdf_path)
    candidates = _events(recorder, "candidate_evaluated")
    assert [item["priority"] for item in candidates] == [0, 1, 2]
    assert [item["lead"] for item in candidates] == [15, 16.5, 13.5]
    assert candidates[1]["parent_id"] == candidates[0]["id"]
    assert candidates[2]["parent_id"] == candidates[1]["id"]
    assert len(_events(recorder, "call_finished")) == 3
    selected = _events(recorder, "candidate_selected")
    assert [item["id"] for item in selected] == [candidates[-1]["id"]]
    for candidate in candidates:
        assert candidate["artifacts"]["tex"]
        assert candidate["artifacts"]["pdf"]
        assert (recorder.run_dir / candidate["artifacts"]["log"]).is_file()
    assert recorder.capture_status == {"ok": True}


def test_batch_keeps_all_candidates_and_only_one_pdf_per_real_call(tmp_path, monkeypatch, recorder):
    calls = _compiler(monkeypatch, fail_calls=1)
    requests = [(_request("a"), tmp_path), (_request("b"), tmp_path)]
    results = BatchStampRenderer(CAPABILITY, max_workers=1, debug_recorder=recorder).render_many(requests)
    assert all(result.ok for result in results.values())
    assert len(calls) == 2
    candidates = _events(recorder, "candidate_evaluated")
    assert len(candidates) == 12
    assert len({item["artifacts"]["pdf"] for item in candidates}) == 2
    assert {item["priority"] for item in _events(recorder, "candidate_selected")} == {1}
    assert all(len(item["tex_line_range"]) == 2 for item in candidates)
    assert all(item["pdf_page_index"] is not None for item in candidates)
    assert len(_events(recorder, "call_finished")) == 2
    assert recorder.capture_status == {"ok": True}


def test_batch_attribution_failure_records_single_fallback_without_fake_page_mapping(tmp_path, monkeypatch, recorder):
    calls = _compiler(monkeypatch, attribution=False)
    # 两段内容不同 → 单段回退各自真实编译（cache_key 不同，不去重）。
    results = BatchStampRenderer(CAPABILITY, max_workers=1, debug_recorder=recorder).render_many([
        (_request("a"), tmp_path),
        (replace(_request("b"), body="other body"), tmp_path),
    ])
    assert all(result.ok for result in results.values())
    assert len(calls) == 3
    batch = [item for item in _events(recorder, "candidate_evaluated") if item["renderer"] == "batch"]
    assert len(batch) == 2
    assert all(item["reason"] == "batch-attribution-failed" and item["pdf_page_index"] is None for item in batch)
    assert len(_events(recorder, "compile_fallback")) == 2


def test_timeout_keeps_partial_compiler_output_and_missing_pdf_explicit(tmp_path, monkeypatch, recorder):
    _compiler(monkeypatch, timeout=True)
    result = BboxStampRenderer(CAPABILITY, debug_recorder=recorder).render_one(_request(), tmp_path)
    assert not result.ok and result.reason == "timeout"
    candidate = _events(recorder, "candidate_evaluated")[0]
    assert candidate["artifacts"]["pdf"] is None
    assert candidate["reason"] == "timeout"
    call = _events(recorder, "call_finished")[0]
    assert call["status"] == "error"
    assert (recorder.run_dir / call["stdout"]).read_text() == "partial compiler stdout"
    assert (recorder.run_dir / call["stderr"]).read_text() == "partial compiler stderr"


def test_cold_cache_skips_history_but_preserves_current_run_reuse_and_writes(tmp_path, recorder):
    request = _request()
    cache_dir = tmp_path / "cache"
    history = StampCache(cache_dir, "ns")
    old = StampResult(key=request.key, ok=True, pdf_path=_pdf(tmp_path / "old.pdf", "old"), font_size=10, scale=1, lead=15)
    history.put(request, old)
    original_key = history.key_for(request)
    cold = StampCache(cache_dir, "ns", bypass_reads=True, debug_recorder=recorder)
    assert cold.key_for(request) == original_key
    assert cold.get(request) is None
    fresh = replace(old, pdf_path=_pdf(tmp_path / "fresh.pdf", "fresh"), compile_attempts=2)
    stored = cold.put(request, fresh)
    assert _observable_pdf(stored.pdf_path) == _observable_pdf(fresh.pdf_path)
    hit = cold.get(request)
    assert hit is not None and hit.compile_attempts == 0
    assert _observable_pdf(hit.pdf_path) == _observable_pdf(fresh.pdf_path)
    assert (cache_dir / f"{original_key}.pdf").is_file()
    assert (cache_dir / f"{original_key}.json").is_file()
    assert _events(recorder, "cache_bypass")


def test_debug_recompile_configuration_reaches_cache(tmp_path, recorder):
    config = SimpleNamespace(working_dir=tmp_path, debug_recorder=recorder, latex_debug_recompile=True)
    cache = build_stamp_cache(config, CAPABILITY)
    assert cache.bypass_reads is True


def test_debug_cache_hit_and_alias_reference_actual_candidate(tmp_path, monkeypatch, recorder):
    calls = _compiler(monkeypatch)
    cache = StampCache(tmp_path / "cache", "ns", debug_recorder=recorder)
    requests = [(_request("a"), tmp_path), (_request("alias"), tmp_path)]
    first = BatchStampRenderer(CAPABILITY, cache=cache, debug_recorder=recorder).render_many(requests)
    assert len(calls) == 1
    second = BboxStampRenderer(CAPABILITY, cache=cache, debug_recorder=recorder).render_one(_request("a"), tmp_path)
    assert second.compile_attempts == 0
    assert len(calls) == 1
    reuses = _events(recorder, "compile_reuse")
    assert {item["kind"] for item in reuses} >= {"deduplicated", "persistent_cache"}
    assert all(item["actual_compile_calls"] == 0 for item in reuses)
    assert all(item["candidate_id"] == first["a"].debug_ref["candidate_id"] for item in reuses)


def test_failed_build_archives_new_geometry_without_claiming_old_output(tmp_path, monkeypatch, recorder):
    workdir = tmp_path / "workdir"
    agent = workdir / "agent"
    agent.mkdir(parents=True)
    (agent / "reconstruct_report.json").write_text('{"old": true}')

    def fail(*_args, **_kwargs):
        (agent / "layout_geometry.json").write_text('{"new": true}')
        raise ValueError("fixture quality gate")

    monkeypatch.setattr(workflow, "reconstruct", fail)
    with pytest.raises(ValueError, match="fixture quality gate"):
        layout.build_pdf(str(workdir), debug_recorder=recorder)
    assert recorder.manifest["stages"]["build"]["status"] == "error"
    outputs = [item for item in _events(recorder, "artifact_bundle") if item["phase"] == "partial_outputs"]
    assert outputs
    assert "layout_geometry.json" in outputs[0]["artifacts"]
    assert "reconstruct_report.json" not in outputs[0]["artifacts"]
