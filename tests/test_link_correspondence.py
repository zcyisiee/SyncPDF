from types import SimpleNamespace

import pymupdf
import pytest
from babeldoc.format.pdf.document_il.backend import link_remap
from babeldoc.format.pdf.document_il.backend.link_text import anchor_variants


def test_decimal_identifiers_have_complete_boundaries():
    chars = [
        (c, pymupdf.Rect(i, 0, i + 1, 10)) for i, c in enumerate("13.1 / 3.12 / 3.1")
    ]
    assert len(link_remap._find_anchor_occurrences(chars, "3.1")) == 1
    assert not link_remap._find_anchor_occurrences(chars, "3")


def test_translated_reference_variants_keep_whole_identifier():
    assert "图8" in anchor_variants("Figure 8")
    assert "表VIII" in anchor_variants("Table VIII")
    assert "六-B3" in anchor_variants("VI-B3")
    assert "3" not in anchor_variants("VI-B3")


def _link_page(text, uri="https://example.org"):
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((30, 70), text, fontsize=12)
    page.insert_link({"kind": 2, "from": pymupdf.Rect(30, 30, 40, 40), "uri": uri})
    return pymupdf.open(stream=doc.tobytes(), filetype="pdf")


def test_wrong_style_stamp_is_rejected_and_relocated():
    doc = _link_page("Wrong 13; correct 3.")
    page = doc[0]
    wrong = page.search_for("Wrong")[0]
    entry = {
        "link_index": 0,
        "from": [30, 30, 40, 40],
        "source_text": "3",
        "paragraph_ids": ["P"],
    }
    paragraph = SimpleNamespace(box=SimpleNamespace(x=20, y=200, x2=350, y2=260))
    result = link_remap.remap_page_links(
        page,
        [entry],
        [],
        {"P": paragraph},
        300,
        alive_ids=set(),
        stamp_rects={0: [wrong]},
    )
    assert result.stamp_resolved == 0
    assert result.anchor_resolved == 1
    saved = pymupdf.open(stream=doc.tobytes(), filetype="pdf")
    assert saved[0].get_text(clip=saved[0].get_links()[0]["from"]).strip() == "3"


def test_same_number_keeps_footnote_figure_and_citation_distinct():
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((30, 70), "MinerU1, Figure 1, [1].", fontsize=12)
    entries = []
    for i, name in enumerate(("Hfootnote.1", "figure.1", "cite.example")):
        rect = pymupdf.Rect(30 + i * 20, 30, 40 + i * 20, 40)
        page.insert_link({"kind": 2, "from": rect, "uri": f"https://example.org/{i}"})
        entries.append(
            {
                "link_index": i,
                "from": list(rect),
                "source_text": "1",
                "nameddest": name,
                "paragraph_ids": ["P"],
            }
        )
    doc = pymupdf.open(stream=doc.tobytes(), filetype="pdf")
    paragraph = SimpleNamespace(box=SimpleNamespace(x=20, y=200, x2=350, y2=260))
    result = link_remap.remap_page_links(
        doc[0], entries, [], {"P": paragraph}, 300, alive_ids=set()
    )
    assert result.anchor_resolved == 3
    saved = pymupdf.open(stream=doc.tobytes(), filetype="pdf")
    links = sorted(saved[0].get_links(), key=lambda link: link["uri"])
    assert links[0]["from"].x0 < links[1]["from"].x0 < links[2]["from"].x0


def test_multiline_clone_preserves_literal_pdf_key_strings():
    uri = "https://example.org/?q=/Rect [1 2 3 4]/NM(test)"
    doc = _link_page("reference", uri)
    page = doc[0]
    rects = [pymupdf.Rect(20, 50, 40, 60), pymupdf.Rect(20, 80, 40, 90)]
    result = link_remap.remap_page_links(
        page,
        [{"link_index": 0, "from": [30, 30, 40, 40]}],
        [],
        {},
        300,
        stamp_rects={0: rects},
    )
    assert result.physical_annotations == 2
    saved = pymupdf.open(stream=doc.tobytes(), filetype="pdf")
    assert [link["uri"] for link in saved[0].get_links()] == [uri, uri]
    identities = [
        saved.xref_get_key(link["xref"], "BabelDOCLink")[1]
        for link in saved[0].get_links()
    ]
    assert identities == ["0:0", "0:0"]


def test_relocated_link_drops_stale_quadpoints():
    doc = _link_page("reference")
    page = doc[0]
    xref = page.get_links()[0]["xref"]
    doc.xref_set_key(xref, "QuadPoints", "[30 270 40 270 30 260 40 260]")
    result = link_remap.remap_page_links(
        page,
        [{"link_index": 0, "from": [30, 30, 40, 40]}],
        [],
        {},
        300,
        stamp_rects={0: [pymupdf.Rect(100, 100, 120, 110)]},
    )
    assert result.remapped == 1
    assert doc.xref_get_key(xref, "QuadPoints")[0] == "null"


def test_remote_named_destination_with_link_substring_survives_serialization():
    doc = pymupdf.open()
    page = doc.new_page()
    link_remap.safe_insert_link(
        page,
        {
            "kind": 5,
            "from": pymupdf.Rect(10, 10, 50, 30),
            "file": "other.pdf",
            "page": -1,
            "to": "chapter/LinkDetails",
        },
    )
    saved = pymupdf.open(stream=doc.tobytes(), filetype="pdf")
    link = link_remap.normalize_link_action(saved, saved[0].get_links()[0])
    assert link["kind"] == 5
    assert link["to"] == "chapter/LinkDetails"


@pytest.mark.parametrize("first", [False, True])
def test_dual_destination_uses_destination_page_dimensions(first):
    from babeldoc.format.pdf.document_il.backend.pdf_creater import PDFCreater

    source = pymupdf.open()
    source.new_page(width=300, height=400)
    source.new_page(width=600, height=800)
    source[0].insert_text((30, 50), "see next")
    source[1].insert_text((30, 100), "target")
    source[0].insert_link(
        {
            "kind": 1,
            "from": pymupdf.Rect(30, 35, 80, 52),
            "page": 1,
            "to": pymupdf.Point(30, 100),
        }
    )
    data = source.tobytes()
    source = pymupdf.open(stream=data, filetype="pdf")
    translated = pymupdf.open(stream=data, filetype="pdf")
    creator = PDFCreater.__new__(PDFCreater)
    creator.original_pdf_path = "fixture.pdf"
    config = SimpleNamespace(dual_translate_first=first, input_file="fixture.pdf")
    dual = creator.create_side_by_side_dual_pdf(
        source, translated, "unused.pdf", config
    )
    saved = pymupdf.open(stream=dual.tobytes(), filetype="pdf")
    links = sorted(saved[0].get_links(), key=lambda link: link["from"].x0)
    assert [link["page"] for link in links] == [1, 1]
    assert [link["to"].x for link in links] == pytest.approx([30, 630])
    assert [link["to"].y for link in links] == pytest.approx([100, 100])
