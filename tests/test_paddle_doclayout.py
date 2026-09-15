import numpy as np
import pymupdf

from babeldoc.docvision.paddle_doclayout import (
    PaddleDocLayoutModel,
    PaddleDependencyError,
)


def test_paddle_adapter_normalizes_labels_and_preserves_order():
    model = PaddleDocLayoutModel(
        predictor=lambda image: [
            {"bbox": [1, 2, 30, 40], "score": 0.9, "label": "text", "order_rank": 3},
            {
                "bbox": [5, 6, 20, 25],
                "score": 0.8,
                "label": "new_label",
                "order_rank": 1,
            },
        ]
    )
    result = model._normalize(
        model.predictor(np.zeros((50, 60, 3), dtype=np.uint8)), 60, 50
    )
    assert result.names == {1: "plain text"}
    assert result.order == [1, 3]
    assert result.unknown_types == ["new_label"]
    assert tuple(result.boxes[0].xyxy) == (5, 6, 20, 25)


def test_paddle_adapter_handles_page_and_rotation_with_injected_predictor():
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=100)
    page.set_rotation(90)
    model = PaddleDocLayoutModel(predictor=lambda image: [], dpi=72)
    page_obj = type("Page", (), {"page_number": 0})()
    page_obj_result = list(model.handle_document([page_obj], doc, None, False))
    assert len(page_obj_result) == 1
    assert page_obj_result[0][1].metadata["backend"] == "paddle"
    doc.close()


def test_paddle_dependency_error_is_explicit(monkeypatch):
    import builtins

    original_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "paddlex":
            raise ImportError("blocked for test")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    model = PaddleDocLayoutModel()
    try:
        model._predict(np.zeros((2, 2, 3), dtype=np.uint8))
    except PaddleDependencyError as exc:
        assert "paddlex" in str(exc)
    else:
        raise AssertionError("missing PaddleX must be reported explicitly")


import pytest
from types import SimpleNamespace as NS


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("dpi", [72, 144, 216])
def test_pixel_to_point_coordinates_with_crop_and_rotation(rotation, dpi):
    with pymupdf.open() as doc:
        page = doc.new_page(width=240, height=160)
        page.set_cropbox(pymupdf.Rect(20, 30, 220, 130))
        page.set_rotation(rotation)

        def predict(image):
            h, w = image.shape[:2]
            return [
                {
                    "bbox": [w * 0.1, h * 0.2, w * 0.5, h * 0.7],
                    "label": "text",
                    "order": 1,
                }
            ]

        model = PaddleDocLayoutModel(predictor=predict, dpi=dpi)
        result = list(model.handle_document([NS(page_number=0)], doc, None, False))[0][
            1
        ]
        assert result.boxes[0].xyxy == pytest.approx([40, 50, 120, 100], abs=0.001)
        assert doc[0].rotation == rotation


def test_pixel_guard_does_not_lower_resolution():
    with pymupdf.open() as doc:
        doc.new_page(width=200, height=100)
        model = PaddleDocLayoutModel(predictor=lambda im: [], max_pixels=100, dpi=144)
        with pytest.raises(ValueError, match="without reducing DPI"):
            list(model.handle_document([NS(page_number=0)], doc, None, False))


@pytest.mark.parametrize("bbox", [[float("nan"), 0, 1, 1], [3, 0, 1, 1], [0, 0, 1]])
def test_malformed_detections_fail_instead_of_dropping_characters(bbox):
    with pytest.raises(ValueError):
        PaddleDocLayoutModel(predictor=lambda im: [])._normalize(
            [{"bbox": bbox, "label": "text"}], 100, 100
        )


def test_all_twenty_five_labels_have_a_downstream_role():
    from babeldoc.docvision.layout_labels import (
        PADDLE_LABELS,
        PADDLE_TO_LAYOUT,
        PADDLE_ROLES,
    )

    assert len(PADDLE_LABELS) == 25
    assert set(PADDLE_LABELS) == set(PADDLE_TO_LAYOUT) == set(PADDLE_ROLES)
    assert PADDLE_TO_LAYOUT["content"] == "content"
    assert PADDLE_ROLES["content"] == "translate"
    assert PADDLE_TO_LAYOUT["vision_footnote"] == "figure_caption"


def test_model_backend_and_input_parameters_isolate_cache():
    models = [
        PaddleDocLayoutModel(device="cpu"),
        PaddleDocLayoutModel(device="mlx"),
        PaddleDocLayoutModel(dpi=216),
        PaddleDocLayoutModel(retry_threshold=0.3),
    ]
    assert len({m.cache_namespace for m in models}) == len(models)


def test_uncovered_char_indices_mirrors_coverage_gate_frame():
    from babeldoc.docvision.paddle_doclayout import uncovered_char_indices

    class Box:
        def __init__(self, x, y, x2, y2):
            self.x, self.y, self.x2, self.y2 = x, y, x2, y2

    class Char:
        def __init__(self, box):
            self.visual_bbox = NS(box=box)
            self.box = box

    page = NS(
        pdf_character=[
            Char(Box(10, 10, 20, 20)),
            Char(Box(40, 40, 50, 50)),
        ]
    )
    page_height = 800
    # IL y is top-down; region boxes are y-up media points (the to_points
    # convention), so the same physical region must round-trip to IL (10,10).
    region_boxes_tl = [[10, page_height - 20, 20, page_height - 10]]
    assert uncovered_char_indices(page, region_boxes_tl, page_height) == {1}


def test_retry_adoption_requires_strict_subset():
    from babeldoc.docvision.paddle_doclayout import retry_covers_more

    assert retry_covers_more({0, 1, 2}, {0}) is True
    assert retry_covers_more({0}, {0}) is False
    assert retry_covers_more({0}, {0, 1}) is False
    assert retry_covers_more({0, 1}, {1, 2}) is False
    assert retry_covers_more(set(), set()) is False
