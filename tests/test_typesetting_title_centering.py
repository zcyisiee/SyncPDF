"""Native main-title centering uses source geometry, not the title label alone."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from babeldoc.format.pdf.document_il import Box
from babeldoc.format.pdf.document_il import il_version_1 as il
from babeldoc.format.pdf.document_il.midend.typesetting import Typesetting
from babeldoc.format.pdf.document_il.midend.typesetting import TypesettingUnit


class Font:
    def __init__(self, font_id="base"):
        self.font_id = font_id

    def char_lengths(self, _text, size):
        return (size,)

    def has_glyph(self, codepoint):
        return codepoint


def fixture():
    ts = object.__new__(Typesetting)
    ts.is_cjk = True
    source_box = Box(100, 680, 500, 760)
    source = {
        "box": [100, 680, 500, 760],
        "line_boxes": [[100, 740, 500, 760], [160, 710, 440, 730]],
    }
    ts.translation_config = SimpleNamespace(
        source_line_geometry={"P01-001": source},
        paragraph_layout_overrides={},
        line_skip_override=None,
        layout_warnings=[],
        enable_latex_bbox_layout=False,
    )
    ts.font_mapper = SimpleNamespace(base_font=Font())
    title = il.PdfParagraph(
        debug_id="P01-001",
        layout_label="title",
        box=source_box,
        pdf_style=il.PdfStyle(font_size=20),
        first_line_indent=False,
    )
    body = il.PdfParagraph(
        debug_id="P01-002",
        layout_label="text",
        box=Box(100, 400, 500, 600),
        pdf_style=il.PdfStyle(font_size=10),
    )
    page = il.Page(
        page_number=0,
        cropbox=il.Cropbox(box=Box(0, 0, 600, 800)),
        pdf_paragraph=[title, body],
    )
    return ts, title, page, source


def units(count):
    result = []
    for i in range(count):
        style = il.PdfStyle(
            font_id="bold" if i < 3 else "regular",
            font_size=20,
            graphic_state=il.GraphicState(passthrough_per_char_instruction=f"{i} g"),
        )
        unit = TypesettingUnit(
            unicode=chr(0x4E00 + i),
            font=Font(style.font_id),
            font_size=20,
            style=style,
            xobj_id=0,
        )
        result.append(unit)
    return result


def rows(laid_out):
    grouped = {}
    for unit in laid_out:
        grouped.setdefault(unit.box.y, []).append(unit)
    return list(grouped.values())


@pytest.mark.parametrize(
    "count,forced,expected_lengths",
    [(5, None, [5]), (25, None, [20, 5]), (25, 12, [12, 13])],
)
def test_each_wrapped_or_forced_line_is_centered(count, forced, expected_lengths):
    ts, title, page, _ = fixture()
    original = units(count)
    if forced:
        ts.translation_config.paragraph_layout_overrides = {
            title.debug_id: {"force_break_after_text": [original[forced - 1].unicode]}
        }
    uncentered, fits = ts._layout_typesetting_units(original, title.box, 1, 1.5, title)
    assert fits
    scale, centered = ts._find_optimal_scale_and_layout(
        title, page, original, apply_layout=True
    )
    assert scale == 1
    assert [len(row) for row in rows(centered)] == expected_lengths
    for row in rows(centered):
        assert (row[0].box.x + row[-1].box.x2) / 2 == pytest.approx(300)
        assert row[0].box.x >= 100 and row[-1].box.x2 <= 500
    assert [u.box.y for u in centered] == [u.box.y for u in uncentered]
    assert [u.font_size for u in centered] == [u.font_size for u in original]
    chars = [c.pdf_character for c in title.pdf_paragraph_composition]
    assert "".join(c.char_unicode for c in chars) == "".join(
        u.unicode for u in original
    )
    assert [c.pdf_style.font_id for c in chars] == [u.font_id for u in original]
    assert [c.pdf_style.graphic_state for c in chars] == [
        u.style.graphic_state for u in original
    ]


def test_single_source_line_needs_page_center_evidence():
    ts, title, page, source = fixture()
    source["line_boxes"] = [[200, 740, 400, 760]]
    source["box"] = [200, 740, 400, 760]
    assert ts._centered_main_title_box(title, page) == Box(*source["box"])
    source["line_boxes"] = [[100, 740, 300, 760]]
    source["box"] = [100, 740, 300, 760]
    assert ts._centered_main_title_box(title, page) is None


@pytest.mark.parametrize(
    "kind",
    ["body", "section", "subsection", "left-aligned", "later-page", "missing-source"],
)
def test_non_main_or_left_aligned_text_keeps_existing_layout(kind):
    ts, title, page, source = fixture()
    if kind == "body":
        title.layout_label = "text"
    elif kind == "section":
        title.pdf_style.font_size = 10
    elif kind == "subsection":
        title.layout_label = "paragraph_title"
    elif kind == "left-aligned":
        source["line_boxes"][1] = [100, 710, 380, 730]
    elif kind == "later-page":
        page.page_number = 1
    elif kind == "missing-source":
        ts.translation_config.source_line_geometry = {}
    assert ts._centered_main_title_box(title, page) is None
    original = units(25)
    expected, fits = ts._layout_typesetting_units(original, title.box, 1, 1.5, title)
    assert fits
    _, actual = ts._find_optimal_scale_and_layout(
        title, page, original, apply_layout=True
    )
    assert [u.box for u in actual] == [u.box for u in expected]
    assert [row[0].box.x for row in rows(actual)] == [100, 100]


def test_source_region_survives_asymmetric_box_override():
    ts, title, page, source = fixture()
    title.box = Box(80, 680, 560, 760)
    original_source = copy.deepcopy(source)
    _, actual = ts._find_optimal_scale_and_layout(
        title, page, units(25), apply_layout=True
    )
    assert [len(row) for row in rows(actual)] == [20, 5]
    for row in rows(actual):
        assert (row[0].box.x + row[-1].box.x2) / 2 == pytest.approx(300)
    assert source == original_source


@pytest.mark.parametrize("has_saved_geometry", [True, False])
def test_reconstruct_loads_source_geometry_before_overrides_without_latex(
    tmp_path, monkeypatch, has_saved_geometry
):
    from babeldoc.tools.agent import layout_overrides
    from babeldoc.tools.agent import workflow

    ts, title, page, source = fixture()
    original_box = copy.deepcopy(title.box)
    source_char = il.PdfCharacter(char_unicode="A", box=Box(100, 740, 500, 760))
    pdf_path = tmp_path / "input.pdf"
    pdf_path.touch()
    state = {
        "doc": il.Document(page=[page]),
        "source_line_geometry": {title.debug_id: source} if has_saved_geometry else {},
        "page_char_objects": {0: [source_char]},
        "temp_pdf_path": str(pdf_path),
        "pdf_path": str(pdf_path),
        "lang_in": "en",
        "lang_out": "zh",
    }
    workflow.agent_dir(tmp_path).mkdir()
    workflow.state_path(tmp_path).touch()
    monkeypatch.setattr(workflow.pickle, "load", lambda _file: state)
    monkeypatch.setattr(workflow, "_base_config", lambda *_args: ts.translation_config)
    layout_overrides.apply_patch(
        tmp_path, {"paragraphs": {title.debug_id: {"box": [80, 680, 560, 760]}}}
    )

    class TypesettingReachedError(Exception):
        pass

    class InspectTypesetting:
        def __init__(self, config):
            self.config = config

        def typesetting_document(self, doc):
            assert not self.config.enable_latex_bbox_layout
            assert doc.page[0].pdf_paragraph[0].box == Box(80, 680, 560, 760)
            captured = self.config.source_line_geometry[title.debug_id]
            assert Box(*captured["box"]) == original_box
            assert ts._centered_main_title_box(title, page) == original_box
            raise TypesettingReachedError

    monkeypatch.setattr(workflow, "Typesetting", InspectTypesetting)
    with pytest.raises(TypesettingReachedError):
        workflow.reconstruct(tmp_path, latex_bbox=False)
