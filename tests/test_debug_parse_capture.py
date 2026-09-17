"""解析阶段证据采集测试：debug_capture helper + _run_parse 全接线。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from babeldoc import debug_recorder as dr
from babeldoc.tools.agent import debug_capture

# --------------------------------------------------------------------------- #
# fake IL 构造
# --------------------------------------------------------------------------- #


def _box(x, y, x2, y2):
    return SimpleNamespace(x=x, y=y, x2=x2, y2=y2)


def _char(unicode, x, y, x2, y2, font_id="F1", size=10.0):
    return SimpleNamespace(
        char_unicode=unicode,
        box=_box(x, y, x2, y2),
        pdf_style=SimpleNamespace(font_id=font_id, font_size=size),
    )


def _layout(id_, class_name, x, y, x2, y2, conf=0.9):
    return SimpleNamespace(
        id=id_, class_name=class_name, box=_box(x, y, x2, y2), conf=conf
    )


def _paragraph(debug_id, layout_id, label, unicode, x, y, x2, y2):
    return SimpleNamespace(
        debug_id=debug_id,
        layout_id=layout_id,
        layout_label=label,
        unicode=unicode,
        box=_box(x, y, x2, y2),
        pdf_paragraph_composition=[],
    )


def _font(font_id, name="Helvetica"):
    return SimpleNamespace(
        font_id=font_id,
        name=name,
        bold=False,
        italic=False,
        monospace=False,
        serif=True,
        encoding_length=1,
    )


def _page(number, height=792.0, chars=(), layouts=(), paras=(), fonts=()):
    return SimpleNamespace(
        page_number=number,
        cropbox=SimpleNamespace(box=_box(0, 0, 612, height)),
        mediabox=SimpleNamespace(box=_box(0, 0, 612, height)),
        pdf_font=list(fonts),
        pdf_character=list(chars),
        page_layout=list(layouts),
        pdf_paragraph=list(paras),
        pdf_curve=[],
        pdf_xobject=[],
    )


def _docs(*pages):
    return SimpleNamespace(page=list(pages))


def _recorder(tmp_path):
    run_dir = tmp_path / "debug" / "runs" / "run-1"
    return dr.DebugRecorder(run_dir)


def _snapshot(recorder, name):
    path = recorder.run_dir / "snapshots" / "parse" / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _events(recorder):
    return dr.read_events(recorder.run_dir)


# --------------------------------------------------------------------------- #
# helper 级
# --------------------------------------------------------------------------- #


def test_capture_native_chars_converts_to_topleft(tmp_path):
    recorder = _recorder(tmp_path)
    page = _page(
        0,
        height=792.0,
        chars=[_char("a", 10, 700, 20, 712), _char("b", 30, 700, 40, 712)],
        fonts=[_font("F1")],
    )
    debug_capture.capture_native_chars(_docs(page), recorder)

    payload = _snapshot(recorder, "native-chars")
    assert payload["coord_system"] == "pdf_topleft"
    page0 = payload["pages"][0]
    assert page0["fonts"]["F1"]["name"] == "Helvetica"
    first = page0["chars"][0]
    assert first["id"] == "C001-00000"
    assert first["u"] == "a"
    # IL y 向上（底 700/顶 712）→ topleft y0 = 792-712 = 80, y1 = 792-700 = 92
    assert first["box"]["y0"] == pytest.approx(80.0)
    assert first["box"]["y1"] == pytest.approx(92.0)
    assert first["font"] == "F1"
    assert first["size"] == 10.0
    assert _events(recorder)[-1]["kind"] == "native_chars"


def test_capture_layout_entities_and_failure_evidence(tmp_path):
    recorder = _recorder(tmp_path)
    page = _page(
        0,
        layouts=[
            _layout(1, "text", 50, 600, 500, 700),
            _layout(2, "unknown_thing", 60, 100, 300, 200, conf=0.3),
        ],
    )
    debug_capture.capture_layout(
        _docs(page), recorder, backend="paddle", error=RuntimeError("gate")
    )

    payload = _snapshot(recorder, "layout")
    assert payload["backend"] == "paddle"
    entities = payload["pages"][0]["entities"]
    assert [e["id"] for e in entities] == ["L01-001", "L01-002"]
    assert entities[0]["label"] == "text"
    assert entities[0]["box"]["y0"] == pytest.approx(92.0)  # 792-700
    assert entities[1]["label"] == "unknown_thing"  # 未知 label 原样保留
    assert entities[1]["attrs"]["conf"] == pytest.approx(0.3)

    event = _events(recorder)[-1]
    assert event["kind"] == "layout_parsed"
    assert event["data"]["error"] == "gate"
    assert event["data"]["labels"] == {"text": 1, "unknown_thing": 1}


def test_capture_paragraphs_relations_and_fallback_ids(tmp_path):
    recorder = _recorder(tmp_path)
    page = _page(
        0,
        paras=[
            _paragraph("P01-001", 1, "text", "hello", 50, 600, 300, 700),
            _paragraph(None, None, "text", "orphan", 50, 500, 300, 560),
        ],
    )
    debug_capture.capture_paragraphs(_docs(page), recorder)

    payload = _snapshot(recorder, "paragraphs")
    entities = {e["id"]: e for e in payload["entities"]}
    assert "P01-001" in entities
    assert entities["P01-001"]["attrs"]["unicode"] == "hello"
    # 无段落 id 的对象拿到稳定诊断 id，不伪造业务 id
    assert any(eid.startswith("px01-") for eid in entities)
    assert payload["relations"] == [
        {
            "from_id": "P01-001",
            "to_id": "L01-001",
            "kind": "in_layout",
            "method": "explicit",
            "ambiguous": False,
        }
    ]


def test_capture_selection_snapshot(tmp_path):
    recorder = _recorder(tmp_path)
    rows = [{"id": "P01-001", "page": 0, "layout_label": "text", "source": "abc"}]
    skipped = [
        {
            "id": "P01-002",
            "page": 0,
            "layout_label": "figure",
            "source": "",
            "reason": "protected",
        }
    ]
    debug_capture.capture_selection(
        rows, skipped, {"text": 1}, {"figure": 1}, recorder
    )
    payload = _snapshot(recorder, "selection")
    assert payload["selected"][0]["id"] == "P01-001"
    assert payload["skipped"][0]["reason"] == "protected"
    event = _events(recorder)[-1]
    assert event["data"]["selected"] == 1 and event["data"]["skipped"] == 1


def test_capture_page_frames_with_original_mapping(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    doc.new_page(width=612, height=792)
    doc.new_page(width=612, height=792)
    recorder = _recorder(tmp_path)

    debug_capture.capture_page_frames(
        doc,
        recorder,
        original_pages=[3, 7],
        mediabox_data={doc[0].xref: {"MediaBox": "[0 0 612 792]"}},
    )
    doc.close()

    payload = _snapshot(recorder, "page-frames")
    assert len(payload["frames"]) == 2
    frame0 = payload["frames"][0]
    assert frame0["page_number_displayed"] == 1
    assert frame0["page_number_original"] == 3
    assert frame0["mediabox"][2] == pytest.approx(612)
    assert frame0["original_boxes"]["MediaBox"] == "[0 0 612 792]"
    assert payload["frames"][1]["page_number_original"] == 7


def test_capture_coverage_and_provider_archives(tmp_path):
    workdir = tmp_path / "work"
    report_dir = workdir / "reports"
    report_dir.mkdir(parents=True)
    (report_dir / "layout_coverage.json").write_text(
        json.dumps(
            {
                "passed": False,
                "threshold": 0.005,
                "global": {
                    "uncovered_ratio": 0.5,
                    "uncovered_chars": 3,
                    "total_chars": 6,
                },
            }
        ),
        encoding="utf-8",
    )
    config = SimpleNamespace(
        get_working_file_path=lambda name: str(report_dir / name)
    )
    mineru_dir = workdir / "agent" / "source" / "mineru"
    mineru_dir.mkdir(parents=True)
    (mineru_dir / "provider_ir.json").write_text("{}", encoding="utf-8")
    (mineru_dir / "layout_raw.json").write_text("{}", encoding="utf-8")

    recorder = _recorder(tmp_path)
    debug_capture.capture_coverage(config, recorder)
    debug_capture.archive_provider_artifacts(recorder, workdir, backend="mineru")

    event = _events(recorder)[-2]
    assert event["kind"] == "layout_coverage"
    assert event["data"]["passed"] is False
    assert event["data"]["uncovered_chars"] == 3
    assert (recorder.run_dir / "artifacts" / "parse" / "layout-coverage.json").is_file()
    assert (recorder.run_dir / "artifacts" / "parse" / "provider-ir.json").is_file()
    assert (recorder.run_dir / "artifacts" / "parse" / "provider-layout.json").is_file()


# --------------------------------------------------------------------------- #
# _run_parse 全接线（stub 重型依赖）
# --------------------------------------------------------------------------- #


class _FakeConfig:
    def __init__(self, working_dir):
        self.working_dir = str(working_dir)
        self.debug_recorder = None
        self.provider_ir_dir = None
        self.mineru_doclayout_enabled = False
        self.mineru_use_ocr_text = False
        self.layout_coverage_threshold = 0.005
        self.skip_scanned_detection = True
        self.fix_enclosed_markers = True
        self.mineru_skip_translate_effective_labels = frozenset()
        self.doc_layout_model = None
        self.layout_warnings = []

    def get_working_file_path(self, name):
        return str(Path(self.working_dir) / name)

    def raise_if_cancelled(self):
        return None


def _stub_run_parse_deps(monkeypatch, tmp_path, docs, *, layout_error=None):
    """把 _run_parse 的重型依赖换成确定性的内存实现；返回 (doc, temp_pdf)。"""
    import pymupdf
    from babeldoc import const as const_mod
    from babeldoc.docvision import mineru_doclayout as mineru_mod
    from babeldoc.format.pdf.document_il.backend.latex_bbox import (
        source_geometry as sg_mod,
    )
    from babeldoc.format.pdf.document_il.midend import il_translator as ilt_mod
    from babeldoc.format.pdf.document_il.midend import inline_math_protector as imp_mod
    from babeldoc.format.pdf.document_il.midend import layout_parser as lp_mod
    from babeldoc.format.pdf.document_il.midend import paragraph_finder as pf_mod
    from babeldoc.format.pdf.document_il.midend import styles_and_formulas as sf_mod
    from babeldoc.format.pdf.document_il.midend import toc_detector as toc_mod
    from babeldoc.format.pdf.new_parser import native_parse
    from babeldoc.tools.agent import markdown_view
    from babeldoc.tools.agent import sheet_translator as st_mod
    from babeldoc.tools.agent import workflow

    workdir_holder = {}

    def fake_base_config(_pdf_path, workdir, _lang_in, _lang_out):
        workdir_holder["workdir"] = Path(workdir)
        return _FakeConfig(workdir)

    temp_pdf = tmp_path / "prepared.pdf"
    doc = pymupdf.open()
    doc.new_page(width=612, height=792)
    doc.save(temp_pdf)
    doc.close()
    prepared = pymupdf.open(temp_pdf)

    monkeypatch.setattr(workflow, "_base_config", fake_base_config)
    monkeypatch.setattr(
        workflow,
        "_prepare_pdf",
        lambda _pdf_path, _config: (prepared, temp_pdf, {}),
    )
    monkeypatch.setattr(
        native_parse,
        "parse_prepared_pdf_with_new_parser_to_legacy_ir",
        lambda *_a, **_k: docs,
    )

    class _StubLayoutParser:
        def __init__(self, config):
            self.config = config

        def process(self, docs, doc_pdf):
            for page in docs.page:
                page.page_layout = [
                    _layout(1, "text", 50, 600, 500, 700),
                ]
            coverage_path = Path(
                self.config.get_working_file_path("layout_coverage.json")
            )
            coverage_path.parent.mkdir(parents=True, exist_ok=True)
            coverage_path.write_text(
                json.dumps(
                    {
                        "passed": True,
                        "threshold": 0.005,
                        "global": {"uncovered_ratio": 0.0},
                    }
                ),
                encoding="utf-8",
            )
            if layout_error is not None:
                raise layout_error
            return docs

    monkeypatch.setattr(lp_mod, "LayoutParser", _StubLayoutParser)

    class _StubProtector:
        def __init__(self, config):
            pass

        def process(self, docs):
            return docs

    monkeypatch.setattr(imp_mod, "InlineMathProtector", _StubProtector)

    class _StubParagraphFinder:
        def __init__(self, config):
            pass

        def process(self, docs):
            for page in docs.page:
                for i, paragraph in enumerate(page.pdf_paragraph):
                    paragraph.debug_id = f"tmp-{i}"
            return docs

    monkeypatch.setattr(pf_mod, "ParagraphFinder", _StubParagraphFinder)

    class _StubToc:
        def __init__(self, config):
            pass

        def process(self, docs):
            return docs

    monkeypatch.setattr(toc_mod, "TocDetector", _StubToc)

    class _StubStyles:
        def __init__(self, config):
            pass

        def process(self, docs):
            return docs

    monkeypatch.setattr(sf_mod, "StylesAndFormulas", _StubStyles)

    class _StubTranslator:
        def __init__(self, *a, **k):
            pass

        def pre_translate_paragraph(self, paragraph, tracker, font_map, xobj_map):
            return (paragraph.unicode or "", {"unicode": paragraph.unicode})

    monkeypatch.setattr(ilt_mod, "ILTranslator", _StubTranslator)
    monkeypatch.setattr(
        ilt_mod,
        "PageTranslateTracker",
        lambda: SimpleNamespace(new_paragraph=lambda: object()),
    )
    monkeypatch.setattr(st_mod, "SheetProtocolTranslator", _StubTranslator)
    monkeypatch.setattr(mineru_mod, "MinerUDocLayoutModel", _StubTranslator)
    monkeypatch.setattr(
        sg_mod, "capture_source_line_geometry", lambda _docs: {"a": 1}
    )
    monkeypatch.setattr(const_mod, "close_process_pool", lambda: None)

    def _select(page, _ctx):
        for i, paragraph in enumerate(page.pdf_paragraph):
            yield paragraph, SimpleNamespace(
                translate=(i == 0), reason=None if i == 0 else "protected"
            )

    monkeypatch.setattr(markdown_view, "select_page_paragraphs", _select)
    return prepared, temp_pdf


def test_run_parse_emits_full_evidence(monkeypatch, tmp_path):
    docs = _docs(
        _page(
            0,
            chars=[_char("h", 10, 700, 20, 712)],
            paras=[
                _paragraph(None, 1, "text", "hello", 50, 600, 300, 700),
                _paragraph(None, 1, "figure", "", 60, 100, 300, 300),
            ],
            fonts=[_font("F1")],
        )
    )
    _stub_run_parse_deps(monkeypatch, tmp_path, docs)

    from babeldoc_tools import parse as bdt_parse

    pdf = tmp_path / "src.pdf"
    import pymupdf

    src = pymupdf.open()
    src.new_page(width=612, height=792)
    src.save(pdf)
    src.close()

    monkeypatch.setenv("BABELDOC_MINERU_LAYOUT_JSON", str(tmp_path / "m.json"))
    recorder = _recorder(tmp_path)
    workdir = tmp_path / "work"
    result = bdt_parse.parse_document(
        str(pdf),
        str(workdir),
        layout="mineru",
        mineru_json=str(tmp_path / "m.json"),
        debug_recorder=recorder,
    )
    assert result["paragraphs"] == 1

    kinds = [e["kind"] for e in _events(recorder)]
    for expected in (
        "stage_started",
        "pdf_prepared",
        "page_frames",
        "native_chars",
        "layout_parsed",
        "layout_coverage",
        "provider_artifacts",
        "inline_math",
        "ocr_backfill",
        "enclosed_marker",
        "toc",
        "styles_formulas",
        "paragraphs_found",
        "source_geometry",
        "links_snapshot",
        "selection",
        "stage_finished",
    ):
        assert expected in kinds, f"missing event kind: {expected}"

    for name in (
        "page-frames",
        "native-chars",
        "layout",
        "paragraphs",
        "selection",
    ):
        assert (
            recorder.run_dir / "snapshots" / "parse" / f"{name}.json"
        ).is_file(), name

    paragraphs = _snapshot(recorder, "paragraphs")
    ids = {e["id"] for e in paragraphs["entities"]}
    assert "P01-001" in ids  # _deterministic_ids 生效后再快照

    selection = _snapshot(recorder, "selection")
    assert len(selection["selected"]) == 1
    assert len(selection["skipped"]) == 1
    assert selection["skipped"][0]["reason"] == "protected"

    artifacts = recorder.run_dir / "artifacts" / "parse"
    assert (artifacts / "input.pdf").is_file()
    assert (artifacts / "prepared.pdf").is_file()
    assert (artifacts / "document.md").is_file()
    assert recorder.capture_status["ok"] is True


def test_run_parse_layout_snapshot_includes_inline_protection(monkeypatch, tmp_path):
    """layout 快照在行内公式保护之后采集：protector 追加的 formula 区域必须入档。

    回归锚点：快照若在 ``InlineMathProtector`` 之前拍，行内公式保护区
    （alignment.json 里有坐标）在 layout.json 里没有对应实体，查看器无从渲染。
    """
    docs = _docs(
        _page(
            0,
            chars=[_char("h", 10, 700, 20, 712)],
            paras=[_paragraph(None, 1, "text", "hello", 50, 600, 300, 700)],
            fonts=[_font("F1")],
        )
    )
    _stub_run_parse_deps(monkeypatch, tmp_path, docs)

    from babeldoc.format.pdf.document_il.midend import inline_math_protector as imp_mod

    class _AppendingProtector:
        """模拟真实 InlineMathProtector：追加 class_name="formula" 的保护区。"""

        def __init__(self, config):
            self.config = config

        def process(self, docs):
            for page in docs.page:
                page.page_layout.append(
                    _layout(2, "formula", 60, 500, 120, 512, conf=1.0)
                )
            return docs

    monkeypatch.setattr(imp_mod, "InlineMathProtector", _AppendingProtector)

    import pymupdf
    from babeldoc_tools import parse as bdt_parse

    pdf = tmp_path / "src.pdf"
    src = pymupdf.open()
    src.new_page(width=612, height=792)
    src.save(pdf)
    src.close()

    monkeypatch.setenv("BABELDOC_MINERU_LAYOUT_JSON", str(tmp_path / "m.json"))
    recorder = _recorder(tmp_path)
    result = bdt_parse.parse_document(
        str(pdf),
        str(tmp_path / "work"),
        layout="mineru",
        mineru_json=str(tmp_path / "m.json"),
        debug_recorder=recorder,
    )
    assert result["paragraphs"] == 1

    entities = _snapshot(recorder, "layout")["pages"][0]["entities"]
    by_id = {e["id"]: e for e in entities}
    assert by_id["L01-001"]["label"] == "text"
    assert by_id["L01-002"]["label"] == "formula"


def test_run_parse_keeps_evidence_on_gate_failure(monkeypatch, tmp_path):
    docs = _docs(_page(0, chars=[_char("x", 1, 700, 5, 712)]))
    _stub_run_parse_deps(
        monkeypatch,
        tmp_path,
        docs,
        layout_error=RuntimeError("layout_coverage_gate"),
    )

    from babeldoc_tools import parse as bdt_parse

    pdf = tmp_path / "src.pdf"
    import pymupdf

    src = pymupdf.open()
    src.new_page(width=612, height=792)
    src.save(pdf)
    src.close()

    monkeypatch.setenv("BABELDOC_MINERU_LAYOUT_JSON", str(tmp_path / "m.json"))
    recorder = _recorder(tmp_path)
    with pytest.raises(RuntimeError, match="layout_coverage_gate"):
        bdt_parse.parse_document(
            str(pdf),
            str(tmp_path / "work"),
            layout="mineru",
            mineru_json=str(tmp_path / "m.json"),
            debug_recorder=recorder,
        )

    kinds = [e["kind"] for e in _events(recorder)]
    assert "layout_parsed" in kinds
    assert "layout_coverage" in kinds
    assert "stage_error" in kinds
    # 失败现场已保留：layout 快照与覆盖率报告都可回放
    assert _snapshot(recorder, "layout")["pages"][0]["entities"]
    assert (
        recorder.run_dir / "artifacts" / "parse" / "layout-coverage.json"
    ).is_file()
    recorder.close()
