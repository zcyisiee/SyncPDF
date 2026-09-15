"""Shared layout setup for extraction entry points."""

from babeldoc.docvision.layout_labels import PADDLE_ROLES
from babeldoc.docvision.layout_labels import PADDLE_TO_LAYOUT


def configure_paddle(config, *, options=None, extra_skipped=()):
    from babeldoc.docvision.paddle_doclayout import PaddleDocLayoutModel

    config.doc_layout_model = PaddleDocLayoutModel(**(options or {}))
    config.layout_backend = "paddle"
    config.layout_skip_translate_effective_labels = frozenset(
        {
            PADDLE_TO_LAYOUT[label]
            for label, role in PADDLE_ROLES.items()
            if role == "protected" and label != "inline_formula"
        }
        | set(extra_skipped)
    )


def apply_layout_reading_order(docs, config):
    if getattr(config, "layout_backend", None) != "paddle":
        return docs
    for page in docs.page:
        order = {region.id: rank for rank, region in enumerate(page.page_layout)}
        page.pdf_paragraph.sort(
            key=lambda paragraph: order.get(paragraph.layout_id, len(order))
        )
    return docs
