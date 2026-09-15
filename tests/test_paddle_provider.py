from types import SimpleNamespace as NS

import pytest

from babeldoc.docvision.paddle_provider import _align_formula
from babeldoc.docvision.paddle_provider import build_provider_page
from babeldoc.docvision.paddle_provider import formula_text
from babeldoc.docvision.provider_ir import ProviderDocument


def chars(text, y=50):
    return [
        NS(
            char_unicode=c,
            visual_bbox=None,
            box=NS(x=10 + 6 * i, y=y, x2=16 + 6 * i, y2=y + 10),
        )
        for i, c in enumerate(text)
    ]


def test_formula_alignment_is_unique_and_does_not_mutate_native():
    native = chars("size |A|=1 where A is the set")
    before = [vars(c).copy() for c in native]
    box, reason = _align_formula("|A|=1", native, 100)
    assert box == [40, 40, 70, 50]
    assert reason is None
    assert [vars(c) for c in native] == before


@pytest.mark.parametrize(
    "source,latex,reason",
    [
        ("O(k) and O(k)", "O(k)", "ambiguous_native_match"),
        ("O(n)", "O(k)", "native_text_mismatch"),
        ("(e.g., majority voting)", "(e.g.,", "unsupported_latex"),
        ("O(k)", r"\frac{O}{k}", "unsupported_latex"),
    ],
)
def test_formula_alignment_falls_back_on_ambiguity_or_prose(source, latex, reason):
    assert _align_formula(latex, chars(source), 100) == (None, reason)


def test_block_reference_is_not_pretended_to_be_precise_ocr():
    page = NS(
        page_number=0,
        cropbox=NS(box=NS(y=0, y2=100)),
        pdf_character=chars("size |A|=1"),
    )
    provider, report = build_provider_page(
        page,
        [
            {
                "block_label": "text",
                "block_bbox": [0, 0, 100, 100],
                "block_content": "size $|A|=1$",
                "block_order": 3,
            }
        ],
        lambda b: b,
    )
    assert provider.blocks[0].index == 3
    spans = list(provider.blocks[0].iter_spans())
    assert spans[0].metadata["precision"] == "block"
    assert spans[0].score is None
    assert spans[1].metadata["alignment"] == "unique_exact"
    assert spans[1].kind == "inline_equation"
    assert report[0]["status"] == "aligned"
    doc = ProviderDocument("rev", "paddle", 1, [provider], metadata={"readonly": True})
    assert ProviderDocument.from_json(doc.to_json()).to_dict() == doc.to_dict()
