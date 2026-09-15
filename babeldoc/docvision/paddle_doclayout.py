"""Paddle block layout and read-only VLM references in normalized PDF points."""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from babeldoc.docvision.base_doclayout import DocLayoutModel
from babeldoc.docvision.base_doclayout import YoloBox
from babeldoc.docvision.base_doclayout import YoloResult
from babeldoc.docvision.layout_labels import PADDLE_LABELS
from babeldoc.docvision.layout_labels import PADDLE_TO_LAYOUT
from babeldoc.docvision.paddle_model_lock import MODEL_LOCK
from babeldoc.docvision.paddle_runtime import RUNTIME_CONTRACT
from babeldoc.docvision.paddle_runtime import (
    PaddleDependencyError as _PaddleDependencyError,
)
from babeldoc.docvision.paddle_runtime import PaddleRuntime
from babeldoc.docvision.provider_ir import ProviderDocument
from babeldoc.docvision.provider_paths import provider_artifact_path
from babeldoc.format.pdf.document_il.utils.raster_geometry import with_fixed_dpi

logger = logging.getLogger(__name__)
GEOMETRY_VERSION = "unrotated-media-points-v2"
__all__ = ["PaddleDocLayoutModel", "PaddleDependencyError"]
PaddleDependencyError = _PaddleDependencyError


def validate_prediction(raw):
    """A cache hit has the same complete-pipeline contract as fresh inference."""
    if not isinstance(raw, dict) or "layout_det_res" not in raw:
        raise ValueError("Paddle prediction lacks layout_det_res")
    boxes = _raw_boxes(raw)
    for box in boxes:
        _box_values(box)
    blocks = raw.get("parsing_res_list")
    if not isinstance(blocks, list) or (boxes and not blocks):
        raise ValueError("Paddle prediction lacks VLM parsing results")
    for block in blocks:
        if not isinstance(block, dict) or not isinstance(
            block.get("block_content"), str
        ):
            raise ValueError("Malformed VLM block content")
        if not isinstance(block.get("block_label"), str):
            raise ValueError("Malformed VLM block label")
        _box_values({"bbox": block.get("block_bbox"), "label": block["block_label"]})


def _raw_boxes(raw):
    if isinstance(raw, dict):
        raw = raw.get("layout_det_res", raw)
        if isinstance(raw, dict):
            raw = raw.get("boxes", raw.get("result", []))
    if raw is None:
        return []
    if not isinstance(raw, list | tuple):
        raise ValueError("Paddle layout output must contain a boxes list")
    return raw


def _box_values(item):
    if not isinstance(item, dict):
        raise ValueError("Malformed Paddle region: expected an object")
    box = item.get("bbox", item.get("box", item.get("coordinate")))
    if not isinstance(box, list | tuple) or len(box) != 4:
        raise ValueError("Malformed Paddle region: missing four-coordinate bbox")
    coords = np.asarray(box, dtype=float)
    score = float(item.get("score", item.get("confidence", 1)))
    if not np.all(np.isfinite(coords)) or not np.isfinite(score):
        raise ValueError("Non-finite Paddle region")
    if coords[2] <= coords[0] or coords[3] <= coords[1]:
        raise ValueError("Empty or inverted Paddle region")
    return (
        coords,
        score,
        str(item.get("label", item.get("class_name", item.get("type", "unknown")))),
    )


def _overlaps(char_box, region_box):
    """Positive-area intersection, matching layout_parser._box_overlaps."""

    return min(char_box.x2, region_box[2]) > max(char_box.x, region_box[0]) and min(
        char_box.y2, region_box[3]
    ) > max(char_box.y, region_box[1])


def uncovered_char_indices(page, region_boxes_tl, page_height):
    """Indices of native characters lying outside every region box.

    Mirrors layout_parser.compute_layout_coverage so a page can retry
    detection with a lower threshold before the downstream gate fires.
    region_boxes_tl are unrotated media points from the crop view origin
    (the convention to_points produces); flipping them with the mediabox
    height yields the same top-left IL frame LayoutParser compares against.
    Returning indices lets the caller require that a retry never uncovers a
    character the primary pass had covered.
    """

    il_boxes = [
        (b[0], page_height - b[3], b[2], page_height - b[1]) for b in region_boxes_tl
    ]
    uncovered = set()
    for index, char in enumerate(getattr(page, "pdf_character", None) or []):
        box = char.visual_bbox.box if char.visual_bbox is not None else char.box
        if box is None:
            continue
        if any(_overlaps(box, region) for region in il_boxes):
            continue
        uncovered.add(index)
    return uncovered


def retry_covers_more(primary_uncovered, retry_uncovered):
    """Adopt a retry only when it uncovers a strict subset of characters."""

    return retry_uncovered < primary_uncovered


class PaddleDocLayoutModel(DocLayoutModel):
    def __init__(
        self,
        *,
        predictor: Callable[[np.ndarray], Any] | None = None,
        model_revision: str = MODEL_LOCK["layout"]["revision"],
        device="auto",
        dpi=144,
        max_pixels=12_000_000,
        cache_dir=None,
        layout_model_dir=None,
        vlm_model_dir=None,
        onnx_model=None,
        retry_threshold=0.2,
    ):
        if predictor is None and model_revision not in {
            "PP-DocLayoutV3",
            MODEL_LOCK["layout"]["revision"],
        }:
            raise ValueError(
                "Model revision must match the verified PP-DocLayoutV3 lock"
            )
        if dpi < 72 or max_pixels < 1:
            raise ValueError("Paddle requires dpi >= 72 and a positive pixel guard")
        if not 0 < retry_threshold < 1:
            raise ValueError("retry_threshold must be in (0, 1)")
        self.predictor = predictor
        self.model_revision = model_revision
        self.device = device
        self.dpi = dpi
        self.max_pixels = max_pixels
        self.retry_threshold = retry_threshold
        self.cache_dir = Path(
            cache_dir or Path.home() / ".cache/babeldoc/paddle-layout.v2"
        )
        self.runtime_options = {
            "device": device,
            "layout_model_dir": layout_model_dir,
            "vlm_model_dir": vlm_model_dir,
            "onnx_model": onnx_model,
        }
        self.runtime = None
        self.provider_document = None
        self.unknown_types = []
        self.metadata = {
            "backend": "paddle",
            "model_revision": model_revision,
            "device": device,
            "geometry": GEOMETRY_VERSION,
        }
        self.page_reports = []

    @property
    def stride(self):
        return 32

    @property
    def cache_namespace(self):
        # Backend and all artifact hashes isolate numerically distinct outputs.
        data = {
            "models": MODEL_LOCK,
            "dpi": self.dpi,
            "max_pixels": self.max_pixels,
            "geometry": GEOMETRY_VERSION,
            "runtime": self.runtime_options,
            "adapter": RUNTIME_CONTRACT,
            "revision": self.model_revision,
            "retry_threshold": self.retry_threshold,
        }
        return hashlib.sha256(
            json.dumps(data, sort_keys=True, default=str).encode()
        ).hexdigest()

    def _predict(self, image, layout_threshold=None):
        if self.predictor is not None:
            return self.predictor(image)
        if self.runtime is None:
            self.runtime = PaddleRuntime(**self.runtime_options)
        return self.runtime.predict(image, layout_threshold=layout_threshold)

    def _normalize(self, raw, width, height, *, to_points=None):
        entries = []
        for position, item in enumerate(_raw_boxes(raw)):
            coords, score, label = _box_values(item)
            coords = np.clip(coords, [0, 0, 0, 0], [width, height, width, height])
            if coords[2] <= coords[0] or coords[3] <= coords[1]:
                raise ValueError("Paddle region lies outside rendered page")
            rank = item.get("order_rank", item.get("order"))
            if rank is not None and (
                not isinstance(rank, int | float) or not np.isfinite(rank)
            ):
                raise ValueError("Invalid Paddle reading order")
            if label not in PADDLE_LABELS and label not in self.unknown_types:
                self.unknown_types.append(label)
                logger.warning(
                    "Unknown Paddle layout class %r; preserving text and reporting unknown_types",
                    label,
                )
            entries.append((rank, position, coords, score, label))
        entries.sort(
            key=lambda e: (e[0] is None, e[0] if e[0] is not None else e[1], e[1])
        )
        names = {}
        ids = {}
        boxes = []
        for rank, position, coords, score, label in entries:
            canonical = PADDLE_TO_LAYOUT.get(label, "plain text")
            if canonical not in ids:
                ids[canonical] = len(ids) + 1
                names[ids[canonical]] = canonical
            box = YoloBox(
                xyxy=np.asarray(
                    to_points(coords) if to_points else coords, dtype=np.float32
                ),
                conf=np.float32(score),
                cls=np.int32(ids[canonical]),
            )
            box.order_rank = rank
            box.provider_label = label
            box.provider_index = position
            boxes.append(box)
        result = YoloResult(names, boxes=boxes, preserve_order=True)
        result.order = [box.order_rank for box in boxes]
        result.metadata = dict(self.metadata)
        result.unknown_types = list(self.unknown_types)
        return result

    def handle_document(self, pages, mupdf_doc, translate_config, save_debug_image):
        from babeldoc.docvision.paddle_provider import build_provider_page

        self.unknown_types = []
        self.page_reports = []
        self.provider_document = ProviderDocument(
            MODEL_LOCK["vlm"]["revision"], "paddle", len(mupdf_doc)
        )
        from babeldoc.docvision.paddle_runtime import sha256_file

        source_path = getattr(translate_config, "input_file", None)
        source_hash = (
            sha256_file(Path(source_path))
            if source_path and Path(source_path).is_file()
            else hashlib.sha256(mupdf_doc.tobytes()).hexdigest()
        )
        formulas = []
        try:
            for page in pages:
                if translate_config is not None:
                    translate_config.raise_if_cancelled()
                pdf_page = mupdf_doc[page.page_number]
                geometry = with_fixed_dpi(
                    pdf_page,
                    self.dpi,
                    normalize_rotation=True,
                    max_pixels=self.max_pixels,
                )
                # Raster origin is the unrotated crop view. Convert once to media
                # points; the LayoutParser owns the later y-axis flip to IL.
                origin_x = pdf_page.cropbox.x0 - pdf_page.mediabox.x0
                origin_y = pdf_page.cropbox.y0

                def to_points(
                    box,
                    *,
                    _geometry=geometry,
                    _origin_x=origin_x,
                    _origin_y=origin_y,
                ):
                    return [
                        float(box[0]) / _geometry.x_scale + _origin_x,
                        float(box[1]) / _geometry.y_scale + _origin_y,
                        float(box[2]) / _geometry.x_scale + _origin_x,
                        float(box[3]) / _geometry.y_scale + _origin_y,
                    ]

                key = hashlib.sha256(
                    (
                        self.cache_namespace
                        + source_hash
                        + str(page.page_number)
                        + hashlib.sha256(geometry.image.tobytes()).hexdigest()
                    ).encode()
                ).hexdigest()
                path = self.cache_dir / f"{key}.json"
                cached = False
                raw = None
                provenance = None
                retry_evidence = None
                if self.predictor is None and path.is_file():
                    try:
                        payload = json.loads(path.read_text())
                        validate_prediction(payload["result"])
                        if payload.get(
                            "contract"
                        ) != RUNTIME_CONTRACT or not isinstance(
                            payload.get("runtime"), dict
                        ):
                            raise ValueError("Cache lacks verified runtime provenance")
                        raw = payload["result"]
                        provenance = payload["runtime"]
                        retry_evidence = payload.get("threshold_retry")
                        cached = True
                    except (OSError, ValueError, KeyError, TypeError):
                        logger.warning("Ignoring corrupt Paddle cache %s", path)
                if raw is None:
                    raw = self._predict(geometry.image)
                    if self.predictor is None:
                        validate_prediction(raw)
                        provenance = self.runtime.last_page_report
                if not cached and self.predictor is None:
                    # A page whose native characters fall outside every region
                    # would fail the downstream coverage gate. Retry that page
                    # once at a lower detection threshold and adopt the result
                    # only when it covers strictly more native characters while
                    # losing none, so the retry can never introduce an omission.
                    mediabox = getattr(page, "mediabox", None)
                    page_height = (
                        mediabox.box.y2 - mediabox.box.y
                        if mediabox is not None and mediabox.box is not None
                        else 0
                    )
                    if page_height:
                        unknown_before = list(self.unknown_types)
                        primary_boxes = [
                            b.xyxy.tolist()
                            for b in self._normalize(
                                raw,
                                geometry.pixel_width,
                                geometry.pixel_height,
                                to_points=to_points,
                            ).boxes
                        ]
                        primary_uncovered = uncovered_char_indices(
                            page, primary_boxes, page_height
                        )
                        if primary_uncovered:
                            logger.warning(
                                "Page %d: %d native characters uncovered by layout; "
                                "retrying detection at threshold %.2f",
                                page.page_number + 1,
                                len(primary_uncovered),
                                self.retry_threshold,
                            )
                            retry_raw = self._predict(
                                geometry.image,
                                layout_threshold=self.retry_threshold,
                            )
                            validate_prediction(retry_raw)
                            retry_boxes = [
                                b.xyxy.tolist()
                                for b in self._normalize(
                                    retry_raw,
                                    geometry.pixel_width,
                                    geometry.pixel_height,
                                    to_points=to_points,
                                ).boxes
                            ]
                            retry_uncovered = uncovered_char_indices(
                                page, retry_boxes, page_height
                            )
                            adopted = retry_covers_more(
                                primary_uncovered, retry_uncovered
                            )
                            retry_evidence = {
                                "reason": "uncovered_native_characters",
                                "primary_uncovered": len(primary_uncovered),
                                "retry_uncovered": len(retry_uncovered),
                                "threshold": self.retry_threshold,
                                "adopted": adopted,
                            }
                            if adopted:
                                raw = retry_raw
                                provenance = self.runtime.last_page_report
                        # Intermediate passes must not leak unknown labels;
                        # the final normalize below tracks the adopted raw only.
                        self.unknown_types = unknown_before
                result = self._normalize(
                    raw,
                    geometry.pixel_width,
                    geometry.pixel_height,
                    to_points=to_points,
                )
                parsing = (
                    raw.get("parsing_res_list", []) if isinstance(raw, dict) else []
                )
                if parsing:
                    provider_page, aligned = build_provider_page(
                        page, parsing, to_points
                    )
                    self.provider_document.pages.append(provider_page)
                    formulas.extend(aligned)
                report = {
                    "page_index": page.page_number,
                    "cache_key": key,
                    "cache_hit": cached,
                    "raster_size": [geometry.pixel_width, geometry.pixel_height],
                    "dpi": geometry.render_dpi,
                    "point_size": [geometry.page_width_pt, geometry.page_height_pt],
                    "boxes": [
                        {
                            "bbox": b.xyxy.tolist(),
                            "label": result.names[b.cls],
                            "provider_label": b.provider_label,
                            "score": float(b.conf),
                            "order_rank": b.order_rank,
                        }
                        for b in result.boxes
                    ],
                    "raw": raw,
                    "threshold_retry": retry_evidence,
                }
                self.page_reports.append(report)
                report["runtime"] = provenance
                if self.predictor is None and not cached:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.NamedTemporaryFile(
                        mode="w",
                        dir=path.parent,
                        suffix=".tmp",
                        delete=False,
                        encoding="utf-8",
                    ) as stream:
                        tmp = Path(stream.name)
                        json.dump(
                            {
                                "contract": RUNTIME_CONTRACT,
                                "result": raw,
                                "runtime": provenance,
                                "threshold_retry": retry_evidence,
                            },
                            stream,
                            ensure_ascii=False,
                        )
                    tmp.replace(path)
                if (
                    hasattr(page, "pdf_character")
                    and not page.pdf_character
                    and _raw_boxes(raw)
                ):
                    report["native_text_status"] = "missing"
                    raise ValueError(
                        f"Page {page.page_number + 1} has no native text layer. VLM references are saved, but OCR text insertion is not supported; page is not accepted."
                    )
                yield page, result
        finally:
            self.provider_document.unknown_types = [
                {"type": t, "where": "layout"} for t in self.unknown_types
            ]
            self.provider_document.metadata = {
                "precision": "block; aligned math uses native characters",
                "native_text_authoritative": True,
                "formula_alignment": formulas,
                "models": MODEL_LOCK,
                "geometry": GEOMETRY_VERSION,
            }
            if translate_config is not None:
                root = provider_artifact_path(
                    getattr(translate_config, "provider_ir_dir", None),
                    getattr(translate_config, "working_dir", None),
                )
                if root:
                    root.parent.mkdir(parents=True, exist_ok=True)
                    root.write_text(self.provider_document.to_json(indent=2))
                    (root.parent / "layout.json").write_text(
                        json.dumps(
                            {
                                "pages": self.page_reports,
                                "unknown_types": self.unknown_types,
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    runtime = (
                        self.runtime.report()
                        if self.runtime
                        else {
                            "backend": "paddle",
                            "execution": "cache-replay"
                            if self.predictor is None
                            else "injected-predictor",
                        }
                    )
                    (root.parent / "runtime.json").write_text(
                        json.dumps(runtime, ensure_ascii=False, indent=2)
                    )
