from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pymupdf

from babeldoc.docvision.base_doclayout import DocLayoutModel
from babeldoc.docvision.base_doclayout import YoloBox
from babeldoc.docvision.base_doclayout import YoloResult

logger = logging.getLogger(__name__)


class MinerUDocLayoutModel(DocLayoutModel):
    """DocLayoutModel implementation backed by MinerU API."""

    def __init__(
        self,
        api_token: str | None,
        base_url: str = "https://mineru.net",
        model_version: str = "vlm",
        language: str | None = None,
        poll_interval_seconds: float = 5.0,
        timeout_seconds: int = 900,
    ):
        self.api_token = api_token
        self.base_url = base_url.rstrip("/")
        self.model_version = model_version
        self.language = language
        self.poll_interval_seconds = poll_interval_seconds
        self.timeout_seconds = timeout_seconds
        self._stride = 32

    @property
    def stride(self) -> int:
        return self._stride

    @staticmethod
    def _empty_result() -> YoloResult:
        return YoloResult(names={}, boxes=[])

    @staticmethod
    def _normalize_bbox(bbox: Any) -> np.ndarray:
        if not isinstance(bbox, list | tuple) or len(bbox) != 4:
            raise ValueError(f"Invalid MinerU bbox: {bbox!r}")
        x0, y0, x1, y1 = bbox
        return np.array(
            [
                np.float32(x0),
                np.float32(y0),
                np.float32(x1),
                np.float32(y1),
            ],
            dtype=np.float32,
        )

    def _iter_leaf_blocks(self, block: dict[str, Any]):
        children = block.get("blocks")
        if isinstance(children, list) and children:
            yielded_child = False
            for child in children:
                if not isinstance(child, dict):
                    continue
                yielded_child = True
                yield from self._iter_leaf_blocks(child)
            if not yielded_child:
                yield block
            return
        yield block

    def _iter_page_leaf_blocks(self, page_info: dict[str, Any]):
        for block in page_info.get("para_blocks", []) or []:
            if not isinstance(block, dict):
                continue
            yield from self._iter_leaf_blocks(block)

        for block in page_info.get("discarded_blocks", []) or []:
            if not isinstance(block, dict):
                continue
            yield from self._iter_leaf_blocks(block)

    def _map_mineru_block_to_layout_label(self, block: dict[str, Any]) -> str:
        block_type = str(block.get("type") or "").strip().lower()
        sub_type = block.get("sub_type")
        sub_type = str(sub_type).strip().lower() if sub_type else None

        if block_type == "text":
            return "text"
        if block_type == "title":
            return "title"
        if block_type in {"interline_equation", "equation"}:
            return "formula"
        if block_type == "ref_text":
            return "reference"
        if block_type == "table_caption":
            return "table_caption"
        if block_type == "table_body":
            return "table_text"
        if block_type == "table_footnote":
            return "table_footnote"
        if block_type in {"image_caption", "chart_caption"}:
            return "figure_caption"
        if block_type == "image_footnote":
            return "figure_text"
        if block_type in {"image_body", "chart_body", "chart"}:
            return "figure"
        if block_type in {
            "header",
            "footer",
            "page_number",
            "page_footnote",
            "aside_text",
        }:
            return block_type
        if block_type in {"code", "algorithm", "code_body"}:
            return "code"
        if block_type == "code_caption":
            return "code_caption"
        if block_type == "phonetic":
            return "text"

        # Container fallback only when no usable children exist
        if block_type == "table":
            return "table"
        if block_type == "image":
            return "figure"
        if block_type == "list" and sub_type == "ref_text":
            return "reference"
        if block_type == "list":
            return "list_item"

        has_lines = isinstance(block.get("lines"), list)
        logger.warning(
            "Unknown MinerU block type encountered. type=%s sub_type=%s index=%s",
            block_type,
            sub_type,
            block.get("index"),
        )
        return "text" if has_lines else "abandon"

    @staticmethod
    def _is_first_page_author_block(
        block: dict[str, Any], page_info: dict[str, Any]
    ) -> bool:
        """Recognize the author/affiliation band absent from MinerU's labels."""
        if page_info.get("page_idx") != 0 or block.get("type") != "text":
            return False
        bbox = block.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            return False
        blocks = page_info.get("para_blocks", [])
        titles = [b for b in blocks if b.get("type") == "title" and b.get("bbox")]
        abstracts = [
            b
            for b in blocks
            if b.get("bbox")
            and " ".join(
                str(s.get("content", ""))
                for line in b.get("lines", [])
                for s in line.get("spans", [])
            )
            .lstrip()
            .lower()
            .startswith("abstract")
        ]
        if not titles or not abstracts:
            return False
        title = min(titles, key=lambda b: b["bbox"][1])
        abstract = min(abstracts, key=lambda b: b["bbox"][1])
        return title["bbox"][3] <= bbox[1] and bbox[3] < abstract["bbox"][1]

    def _build_page_yolo_result(self, page_info: dict[str, Any]) -> YoloResult:
        names: dict[int, str] = {}
        label_to_id: dict[str, int] = {}
        boxes: list[YoloBox] = []

        for block in self._iter_page_leaf_blocks(page_info):
            bbox = block.get("bbox")
            if not bbox:
                continue

            label = (
                "author"
                if self._is_first_page_author_block(block, page_info)
                else self._map_mineru_block_to_layout_label(block)
            )
            if not label:
                continue

            if label not in label_to_id:
                cls_id = len(label_to_id) + 1
                label_to_id[label] = cls_id
                names[cls_id] = label
            else:
                cls_id = label_to_id[label]

            boxes.append(
                YoloBox(
                    xyxy=self._normalize_bbox(bbox),
                    conf=np.float32(1.0),
                    cls=np.int32(cls_id),
                )
            )

        return YoloResult(names=names, boxes=boxes)

    def _parse_layout_json_page_results(
        self,
        layout_json: dict[str, Any],
        total_pages: int,
    ) -> dict[int, YoloResult]:
        page_results = {i: self._empty_result() for i in range(total_pages)}
        pdf_info = layout_json.get("pdf_info")
        if not isinstance(pdf_info, list):
            raise ValueError("Invalid MinerU layout.json: missing pdf_info list")

        for page_info in pdf_info:
            if not isinstance(page_info, dict):
                continue
            page_idx = page_info.get("page_idx")
            if not isinstance(page_idx, int):
                continue
            if 0 <= page_idx < total_pages:
                page_results[page_idx] = self._build_page_yolo_result(page_info)

        return page_results

    def _headers(self) -> dict[str, str]:
        if not self.api_token:
            raise ValueError("MinerU API token is required")
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
        }

    def _request_upload_urls(
        self,
        client: httpx.Client,
        pdf_path: Path,
    ) -> tuple[str, str]:
        payload: dict[str, Any] = {
            "files": [{"name": pdf_path.name, "data_id": f"babeldoc-{pdf_path.stem}"}],
            "model_version": self.model_version,
            "enable_formula": True,
            "enable_table": True,
        }
        if self.language:
            payload["language"] = self.language

        response = client.post(
            f"{self.base_url}/api/v4/file-urls/batch",
            json=payload,
            headers=self._headers(),
        )
        response.raise_for_status()
        body = response.json()
        if body.get("code") != 0:
            raise RuntimeError(
                f"MinerU file-urls/batch failed: code={body.get('code')} msg={body.get('msg')}"
            )
        data = body.get("data") or {}
        batch_id = data.get("batch_id")
        file_urls = data.get("file_urls") or data.get("files") or []
        if not batch_id or not file_urls:
            raise RuntimeError(
                "MinerU file-urls/batch response missing batch_id/file_urls"
            )
        return batch_id, file_urls[0]

    def _upload_pdf(
        self, client: httpx.Client, upload_url: str, pdf_path: Path
    ) -> None:
        response = client.put(
            upload_url,
            content=pdf_path.read_bytes(),
        )
        response.raise_for_status()

    def _poll_full_zip_url(
        self, client: httpx.Client, batch_id: str, translate_config
    ) -> str:
        deadline = time.monotonic() + float(self.timeout_seconds)
        last_trace_id = None
        while time.monotonic() < deadline:
            translate_config.raise_if_cancelled()
            response = client.get(
                f"{self.base_url}/api/v4/extract-results/batch/{batch_id}",
                headers={
                    "Authorization": f"Bearer {self.api_token}",
                    "Accept": "*/*",
                },
            )
            response.raise_for_status()
            body = response.json()
            last_trace_id = body.get("trace_id")
            if body.get("code") != 0:
                raise RuntimeError(
                    f"MinerU extract-results/batch failed: code={body.get('code')} msg={body.get('msg')} trace_id={last_trace_id}"
                )

            data = body.get("data") or {}
            extract_results = data.get("extract_result") or []
            if extract_results and isinstance(extract_results[0], dict):
                result0 = extract_results[0]
                state = result0.get("state")
                if state == "done" and result0.get("full_zip_url"):
                    return result0["full_zip_url"]
                if state == "failed":
                    raise RuntimeError(
                        "MinerU task failed: "
                        f"batch_id={batch_id} trace_id={last_trace_id} err_msg={result0.get('err_msg')}"
                    )
            time.sleep(max(0.1, float(self.poll_interval_seconds)))

        raise TimeoutError(
            f"MinerU polling timed out after {self.timeout_seconds}s. batch_id={batch_id} trace_id={last_trace_id}"
        )

    def _download_zip_bytes(self, client: httpx.Client, zip_url: str) -> bytes:
        response = client.get(zip_url, headers={"Accept": "*/*"})
        response.raise_for_status()
        return response.content

    def _load_layout_json_from_zip_bytes(self, zip_bytes: bytes) -> dict[str, Any]:
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            names = zf.namelist()
            target_name = None
            if "layout.json" in names:
                target_name = "layout.json"
            else:
                for name in names:
                    if name.endswith("_middle.json"):
                        target_name = name
                        break
            if not target_name:
                raise RuntimeError(
                    "MinerU ZIP does not contain layout.json or *_middle.json"
                )
            return json.loads(zf.read(target_name).decode("utf-8"))

    LAYOUT_CACHE_DIR = Path.home() / ".cache" / "babeldoc" / "mineru-layout.v1"

    def _layout_cache_path(self, pdf_path: Path) -> Path | None:
        """按 PDF 内容哈希 keyed 的 layout.json 缓存路径（文件不可读时返回 None）。"""
        try:
            digest = hashlib.sha256(Path(pdf_path).read_bytes()).hexdigest()
        except OSError:
            return None
        return self.LAYOUT_CACHE_DIR / f"{digest}.json"

    def _write_layout_cache(self, cache_file: Path, layout_json: dict[str, Any]) -> None:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache_file.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(layout_json, ensure_ascii=False), encoding="utf-8"
            )
            tmp.replace(cache_file)
            logger.info("MinerU layout cached: %s", cache_file)
        except OSError:
            logger.warning("Failed to write MinerU layout cache", exc_info=True)

    def handle_document(
        self,
        pages,
        mupdf_doc: pymupdf.Document,
        translate_config,
        save_debug_image,
    ):
        replay_path = os.environ.get("BABELDOC_MINERU_LAYOUT_JSON")
        if replay_path:
            layout_json = json.loads(Path(replay_path).read_text(encoding="utf-8"))
            requested_page_numbers = {
                int(page.page_number) for page in pages if hasattr(page, "page_number")
            }
            pdf_info = layout_json.get("pdf_info") or []
            layout_pages = {
                int(item["page_idx"])
                for item in pdf_info
                if isinstance(item, dict) and isinstance(item.get("page_idx"), int)
            }
            if len(layout_pages) != mupdf_doc.page_count:
                raise RuntimeError(
                    "MinerU replay layout does not match input PDF page count: "
                    f"layout_pages={len(layout_pages)} input_pages={mupdf_doc.page_count}"
                )
            if requested_page_numbers and layout_pages != requested_page_numbers:
                raise RuntimeError(
                    "MinerU replay layout does not match input PDF: "
                    f"layout_pages={len(layout_pages)} requested_pages={len(requested_page_numbers)}"
                )
            total_pages = (
                max(requested_page_numbers) + 1 if requested_page_numbers else 0
            )
            page_results = self._parse_layout_json_page_results(
                layout_json, total_pages
            )
            for page in pages:
                yield page, page_results.get(page.page_number, self._empty_result())
            return
        if not self.api_token:
            raise ValueError(
                "MinerU API token is required when --mineru-doclayout is enabled"
            )

        pdf_path = Path(translate_config.input_file)
        cache_file = self._layout_cache_path(pdf_path)
        if cache_file is not None and cache_file.exists():
            logger.info("MinerU layout cache hit: %s", cache_file)
            layout_json = json.loads(cache_file.read_text(encoding="utf-8"))
        else:
            with httpx.Client(timeout=float(self.timeout_seconds)) as client:
                batch_id, upload_url = self._request_upload_urls(client, pdf_path)
                self._upload_pdf(client, upload_url, pdf_path)
                full_zip_url = self._poll_full_zip_url(client, batch_id, translate_config)
                zip_bytes = self._download_zip_bytes(client, full_zip_url)

            layout_json = self._load_layout_json_from_zip_bytes(zip_bytes)
            if cache_file is not None:
                self._write_layout_cache(cache_file, layout_json)
        pdf_info = layout_json.get("pdf_info") or []
        requested_page_numbers = {
            int(page.page_number)
            for page in pages
            if hasattr(page, "page_number") and isinstance(page.page_number, int)
        }
        if isinstance(pdf_info, list):
            available_page_numbers = {
                int(page_info["page_idx"])
                for page_info in pdf_info
                if isinstance(page_info, dict)
                and isinstance(page_info.get("page_idx"), int)
            }
            missing_page_numbers = sorted(
                requested_page_numbers - available_page_numbers
            )
            if missing_page_numbers:
                version = layout_json.get("_version_name")
                raise RuntimeError(
                    "MinerU page coverage mismatch: "
                    f"missing_pages={missing_page_numbers} "
                    f"layout_json_pages={len(available_page_numbers)} "
                    f"babeldoc_requested_pages={sorted(requested_page_numbers)} "
                    f"version={version}"
                )

        total_pages = (max(requested_page_numbers) + 1) if requested_page_numbers else 0
        page_results = self._parse_layout_json_page_results(
            layout_json, total_pages=total_pages
        )

        for page in pages:
            translate_config.raise_if_cancelled()
            yield page, page_results.get(page.page_number, self._empty_result())
