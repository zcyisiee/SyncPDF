"""Translation selection helper: protected content vs. translatable prose.

These tests exercise ``babeldoc.tools.agent.translation_selection`` with
lightweight ``SimpleNamespace`` fakes instead of real PDF parsing, so no LLM
or ONNX model is involved.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from babeldoc.tools.agent.translation_selection import SelectionContext
from babeldoc.tools.agent.translation_selection import normalize_label
from babeldoc.tools.agent.translation_selection import select_page_paragraphs
from babeldoc.tools.agent.translation_selection import select_paragraph


def make_box(x: float, y: float, x2: float, y2: float) -> SimpleNamespace:
    return SimpleNamespace(x=x, y=y, x2=x2, y2=y2)


def make_paragraph(
    unicode: str,
    *,
    layout_label: str | None = "plain text",
    box: SimpleNamespace | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(unicode=unicode, layout_label=layout_label, box=box)


def make_page(
    *paragraphs,
    page_number: int = 1,
    page_layout=(),
    cropbox=None,
) -> SimpleNamespace:
    return SimpleNamespace(
        pdf_paragraph=list(paragraphs),
        page_number=page_number,
        page_layout=list(page_layout),
        cropbox=cropbox,
    )


def decide(paragraph, page, *, paragraphs=None, context=None):
    if paragraphs is None:
        paragraphs = [paragraph]
    if context is None:
        context = SelectionContext()
    return select_paragraph(paragraph, page, context, paragraphs)


def test_protected_label_reference_is_not_translated():
    paragraph = make_paragraph("Smith et al. 2020.", layout_label="reference")
    page = make_page(paragraph)
    decision = decide(paragraph, page)
    assert decision.translate is False
    assert decision.reason == "reference"


def test_figure_and_table_captions_are_translated():
    for label in ("figure_caption", "table_caption"):
        paragraph = make_paragraph("Figure 1: overview.", layout_label=label)
        page = make_page(paragraph)
        decision = decide(paragraph, page)
        assert decision.translate is True, label
        assert decision.reason is None


def test_paragraph_inside_figure_region_is_not_translated():
    figure_region = SimpleNamespace(
        class_name="figure", box=make_box(50, 100, 300, 400)
    )
    paragraph = make_paragraph(
        "axis label",
        layout_label="text",
        box=make_box(60, 150, 200, 250),
    )
    page = make_page(paragraph, page_layout=[figure_region])
    decision = decide(paragraph, page)
    assert decision.translate is False
    assert decision.reason == "protected_layout_region"


def test_ordinary_plain_text_is_translated():
    paragraph = make_paragraph(
        "We propose a deterministic selection helper for prose paragraphs.",
        layout_label="plain text",
        box=make_box(72, 500, 500, 560),
    )
    page = make_page(paragraph, page_number=3)
    decision = decide(paragraph, page)
    assert decision.translate is True
    assert decision.reason is None


def test_first_page_author_band_is_not_translated():
    title = make_paragraph(
        "A Great Paper",
        layout_label="title",
        box=make_box(72, 700, 500, 730),
    )
    author_band = make_paragraph(
        "Jane Doe, John Smith",
        layout_label="plain text",
        box=make_box(72, 660, 400, 680),
    )
    abstract = make_paragraph(
        "Abstract—We study translation selection in PDF documents.",
        layout_label="plain text",
        box=make_box(72, 600, 500, 650),
    )
    paragraphs = [title, author_band, abstract]
    page = make_page(*paragraphs, page_number=0)
    decision = decide(author_band, page, paragraphs=paragraphs)
    assert decision.translate is False
    assert decision.reason == "author_affiliation"


def test_references_heading_and_following_reference_are_skipped():
    heading = make_paragraph("REFERENCES", layout_label="paragraph_title")
    reference = make_paragraph("Smith et al. 2020.", layout_label="text")
    page = make_page(heading, reference)
    context = SelectionContext()
    decisions = list(select_page_paragraphs(page, context))
    assert [(p.unicode, d.translate, d.reason) for p, d in decisions] == [
        ("REFERENCES", False, "references_heading"),
        ("Smith et al. 2020.", False, "references"),
    ]


def test_ordinary_heading_does_not_end_references_section():
    """A section heading inside a reference list must not resume translation.

    Real regression: reference entries often contain numbered headings
    (``"1. METHOD"``), and a title/paragraph_title label used to terminate
    the references state, leaking reference entries into the prompt.
    """
    paragraphs = [
        make_paragraph("REFERENCES", layout_label="paragraph_title"),
        make_paragraph("Smith et al. 2020.", layout_label="text"),
        make_paragraph("1. METHOD", layout_label="paragraph_title"),
        make_paragraph("Jones and Lee, 2021.", layout_label="text"),
    ]
    page = make_page(*paragraphs)
    context = SelectionContext()
    decisions = list(select_page_paragraphs(page, context))
    assert [d.translate for _, d in decisions] == [False, False, False, False]
    assert [d.reason for _, d in decisions] == [
        "references_heading",
        "references",
        "references",
        "references",
    ]
    assert context.references_started is True


def test_appendix_boundary_ends_references_section():
    """Only an explicit appendix/open-science boundary resumes translation."""
    paragraphs = [
        make_paragraph("BIBLIOGRAPHY", layout_label="paragraph_title"),
        make_paragraph("Smith et al. 2020.", layout_label="text"),
        make_paragraph("Appendix A: Proofs", layout_label="paragraph_title"),
        make_paragraph("We now prove the main theorem.", layout_label="plain text"),
    ]
    page = make_page(*paragraphs)
    context = SelectionContext()
    decisions = list(select_page_paragraphs(page, context))
    assert [d.translate for _, d in decisions] == [False, False, True, True]
    assert context.references_started is False


def test_open_science_and_supplementary_boundaries_end_references():
    for heading in ("Open Science", "Artifact", "Supplementary Material"):
        context = SelectionContext()
        context.references_started = True
        paragraph = make_paragraph(heading, layout_label="paragraph_title")
        page = make_page(paragraph)
        decision = decide(paragraph, page, context=context)
        assert decision.translate is True, heading
        assert context.references_started is False


def test_extra_labels_are_normalized_like_paragraph_labels():
    """``extra_labels`` must fold spaces/slashes/case just like labels."""
    paragraph = make_paragraph("raw figure body", layout_label="Figure/Table")
    page = make_page(paragraph)
    context = SelectionContext()
    decision = select_paragraph(
        paragraph, page, context, [paragraph], extra_labels={"figure table"}
    )
    assert decision.translate is False
    assert decision.reason == "figure_table"


def test_normalize_label_folds_spaces_slashes_and_case():
    assert normalize_label("Figure/Table") == "figure_table"
    assert normalize_label("  plain text ") == "plain_text"
    assert normalize_label("REFERENCE") == "reference"
    assert normalize_label(None) == ""


def test_single_letter_appendix_heading_boundary():
    """NeurIPS 风格裸字母附录标题（"A. Extended Related Work"）结束 references。"""
    paragraphs = [
        make_paragraph("References", layout_label="title"),
        make_paragraph("Smith et al. 2020.", layout_label="text"),
        make_paragraph("A. Extended Related Work", layout_label="title"),
        make_paragraph("We review additional related work here.", layout_label="text"),
    ]
    page = make_page(*paragraphs)
    context = SelectionContext()
    decisions = list(select_page_paragraphs(page, context))
    assert [d.translate for _, d in decisions] == [False, False, True, True]
    assert context.references_started is False


@pytest.mark.parametrize(
    "heading",
    [
        "A. Extended Related Work",
        "B. Extended Results and Figures",
        "C. Proofs and Derivations",
        "D. Experimental Details",
        "E. Usage of LLMs",
    ],
)
def test_single_letter_appendix_headings_resume_translation(heading):
    context = SelectionContext()
    context.references_started = True
    paragraph = make_paragraph(heading, layout_label="title")
    page = make_page(paragraph)
    decision = decide(paragraph, page, context=context)
    assert decision.translate is True, heading
    assert context.references_started is False


@pytest.mark.parametrize(
    ("text", "label"),
    [
        # 参考文献条目以 "A. " 开头但不是标题标签 → 不得结束 references
        ("A. Smith and B. Jones. Some paper title that is long enough.", "text"),
        ("A. Smith. Short paper.", "plain text"),
        # 普通标题标签但不是单字母附录格式 → 不变（原 1. METHOD 行为）
        ("1. METHOD", "paragraph_title"),
    ],
)
def test_non_boundary_text_does_not_end_references(text, label):
    context = SelectionContext()
    context.references_started = True
    paragraph = make_paragraph(text, layout_label=label)
    page = make_page(paragraph)
    decision = decide(paragraph, page, context=context)
    assert decision.translate is False, text
    assert context.references_started is True
