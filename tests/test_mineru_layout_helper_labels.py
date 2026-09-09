from types import SimpleNamespace

from babeldoc.format.pdf.document_il.il_version_1 import Box
from babeldoc.format.pdf.document_il.il_version_1 import PageLayout
from babeldoc.format.pdf.document_il.utils.layout_helper import Layout
from babeldoc.format.pdf.document_il.utils.layout_helper import get_character_layout
from babeldoc.format.pdf.document_il.utils.layout_helper import is_text_layout


class _FakeIndex:
    def __init__(self, ids):
        self._ids = ids

    def intersection(self, _bbox):
        return self._ids


def _mk_char_box():
    box = Box(x=0, y=0, x2=10, y2=10)
    return SimpleNamespace(visual_bbox=SimpleNamespace(box=box))


def _mk_layout(layout_id: int, class_name: str) -> PageLayout:
    return PageLayout(
        box=Box(x=0, y=0, x2=10, y2=10),
        id=layout_id,
        conf=1.0,
        class_name=class_name,
    )


def test_is_text_layout_accepts_new_mineru_related_labels():
    for label in (
        "reference",
        "page_number",
        "aside_text",
        "page_footnote",
        "code",
        "code_caption",
    ):
        assert is_text_layout(Layout(1, label)), label


def test_get_character_layout_prioritizes_reference_over_text():
    char = _mk_char_box()
    layout_index = _FakeIndex([1, 2])
    layout_map = {
        1: _mk_layout(1, "text"),
        2: _mk_layout(2, "reference"),
    }

    chosen = get_character_layout(char, layout_index, layout_map)
    assert chosen is not None
    assert chosen.name == "reference"

