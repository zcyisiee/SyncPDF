from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import logging
import os
import tempfile
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
from babeldoc.docvision.provider_ir import ProviderDocument

logger = logging.getLogger(__name__)


def _wait_span(translate_config, origin: str, *, label: str | None = None, **context):
    """远端等待型工作的耗时片段；未开启 debug 采集时 no-op。

    MinerU 走 httpx 而非子进程，没有 ``call_started`` / ``call_finished`` 事件，
    不显式采集就看不出「等待 MinerU」占了多少时间。
    """
    recorder = getattr(translate_config, "debug_recorder", None)
    if not recorder:
        return contextlib.nullcontext()
    return recorder.span("parse", origin, label=label, **context)


class MinerUDocLayoutModel(DocLayoutModel):
    """DocLayoutModel implementation backed by MinerU API."""

    def __init__(
        self,
        api_token: str | None,
        base_url: str = "https://mineru.net",
        model_version: str = "vlm",
        language: str | None = "en",
        poll_interval_seconds: float = 5.0,
        timeout_seconds: int = 900,
        chunk_pages: int = 10,
        max_chunks: int = 50,
    ):
        self.api_token = api_token
        self.base_url = base_url.rstrip("/")
        self.model_version = model_version
        self.language = "en" if language is None else language
        self.poll_interval_seconds = poll_interval_seconds
        self.timeout_seconds = timeout_seconds
        # MinerU 服务端对大文件解析容易失败：超过 chunk_pages 页的文档切成
        # chunk_pages 页的分片，作为一个 batch 的多个文件一次性提交。
        self.chunk_pages = max(1, int(chunk_pages))
        self.max_chunks = max(1, int(max_chunks))
        self._stride = 32
        # 最近一次 handle_document 构建的 provider IR（完整 block/line/span 树）。
        # 每次 handle_document 调用都会重置，避免多文档/多次调用串数据。
        self.provider_document: ProviderDocument | None = None

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

    def _build_provider_document(self, layout_json: dict[str, Any]) -> None:
        """构建并缓存 provider IR（完整 block/line/span 树），失败不阻断解析。"""
        self.provider_document = ProviderDocument.from_layout_json(layout_json)

    def _provider_ir_output_path(self, translate_config) -> Path | None:
        """provider IR 落盘路径。

        工具层通过 ``translate_config.provider_ir_dir`` 显式指定 agent 产物根目录
        （即 ``<workdir>/agent``），文件落在其下的 ``source/mineru/provider_ir.json``；
        未指定时回退到 ``working_dir/agent/source/mineru/``，两者都没有则跳过落盘。
        """
        if translate_config is None:
            return None
        provider_ir_dir = getattr(translate_config, "provider_ir_dir", None)
        if provider_ir_dir:
            return Path(provider_ir_dir) / "source" / "mineru" / "provider_ir.json"
        working_dir = getattr(translate_config, "working_dir", None)
        if not working_dir:
            return None
        return Path(working_dir) / "agent" / "source" / "mineru" / "provider_ir.json"

    def _persist_provider_document(self, translate_config) -> None:
        """落盘 provider IR（规范化产物）。落盘失败只 warning，不影响 YoloResult 路径。"""
        document = self.provider_document
        if document is None:
            return
        output_path = self._provider_ir_output_path(translate_config)
        if output_path is None:
            return
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = output_path.with_suffix(".tmp")
            tmp.write_text(document.to_json(indent=2), encoding="utf-8")
            tmp.replace(output_path)
            logger.info("MinerU provider IR written: %s", output_path)
        except OSError:
            logger.warning("Failed to write MinerU provider IR", exc_info=True)

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
        pdf_paths: list[Path],
    ) -> tuple[str, list[str]]:
        """为一个 batch 申请全部分片的上传地址。

        ``pdf_paths`` 是同一文档切出的分片（≤ chunk_pages 页时就是原文件本身），
        MinerU 的 file-urls/batch 天然支持一次提交多个文件，全部算一个 batch。
        """
        payload: dict[str, Any] = {
            "files": [
                {"name": path.name, "data_id": f"babeldoc-{path.stem}"}
                for path in pdf_paths
            ],
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
        if len(file_urls) != len(pdf_paths):
            raise RuntimeError(
                "MinerU file-urls/batch returned wrong url count: "
                f"expected={len(pdf_paths)} got={len(file_urls)}"
            )
        return batch_id, list(file_urls)

    def _upload_pdf(
        self, client: httpx.Client, upload_url: str, pdf_path: Path
    ) -> None:
        response = client.put(
            upload_url,
            content=pdf_path.read_bytes(),
        )
        response.raise_for_status()

    def _poll_full_zip_urls(
        self,
        client: httpx.Client,
        batch_id: str,
        translate_config,
        chunk_paths: list[Path],
    ) -> list[str]:
        """轮询 batch，直到全部分片完成，按提交顺序返回各分片 zip URL。"""
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
            extract_results = [
                item
                for item in (data.get("extract_result") or [])
                if isinstance(item, dict)
            ]
            if len(extract_results) >= len(chunk_paths) and all(
                item.get("state") in {"done", "failed"}
                for item in extract_results[: len(chunk_paths)]
            ):
                results = self._align_extract_results(extract_results, chunk_paths)
                failed = [
                    item
                    for item in results
                    if item.get("state") == "failed" or not item.get("full_zip_url")
                ]
                if failed:
                    first = failed[0]
                    raise RuntimeError(
                        "MinerU task failed: "
                        f"file={first.get('file_name') or '?'} "
                        f"batch_id={batch_id} trace_id={last_trace_id} "
                        f"err_msg={first.get('err_msg')}"
                    )
                return [item["full_zip_url"] for item in results]
            time.sleep(max(0.1, float(self.poll_interval_seconds)))

        raise TimeoutError(
            f"MinerU polling timed out after {self.timeout_seconds}s. batch_id={batch_id} trace_id={last_trace_id}"
        )

    @staticmethod
    def _align_extract_results(
        extract_results: list[dict[str, Any]], chunk_paths: list[Path]
    ) -> list[dict[str, Any]]:
        """把 extract_result 对齐回提交顺序。

        优先按 ``file_name`` 匹配分片文件名；服务端不返回 file_name 或对不上时
        回退按返回顺序（此时要求条数与分片数一致）。
        """
        by_name = {
            item.get("file_name"): item
            for item in extract_results
            if isinstance(item.get("file_name"), str)
        }
        aligned: list[dict[str, Any]] = []
        for path in chunk_paths:
            item = by_name.get(path.name)
            if item is None:
                if len(extract_results) != len(chunk_paths):
                    raise RuntimeError(
                        "MinerU extract-results cannot be aligned to chunks: "
                        f"expected={len(chunk_paths)} got={len(extract_results)}"
                    )
                item = extract_results[chunk_paths.index(path)]
            aligned.append(item)
        return aligned

    def _download_zip_bytes(self, client: httpx.Client, zip_url: str) -> bytes:
        response = client.get(zip_url, headers={"Accept": "*/*"})
        response.raise_for_status()
        return response.content

    def _split_pdf_chunks(
        self, pdf_path: Path, page_count: int
    ) -> tuple[list[Path], tempfile.TemporaryDirectory | None]:
        """超过 chunk_pages 页的 PDF 切成 ≤ chunk_pages 页的分片文件。

        返回 ``(chunk_paths, tmpdir)``：tmpdir 持有分片文件，存活到下载完成为止；
        未超过阈值时返回 ``([pdf_path], None)``（单文件即一个 batch 条目）。
        分片数超过 max_chunks 时直接报错（限制一次提交的文件数上限）。
        """
        if page_count <= self.chunk_pages:
            return [pdf_path], None
        chunk_count = (page_count + self.chunk_pages - 1) // self.chunk_pages
        if chunk_count > self.max_chunks:
            raise ValueError(
                "PDF too large for MinerU chunking: "
                f"pages={page_count} chunk_pages={self.chunk_pages} "
                f"chunks={chunk_count} max_chunks={self.max_chunks} "
                f"(page limit={self.chunk_pages * self.max_chunks})"
            )
        tmpdir = tempfile.TemporaryDirectory(prefix="babeldoc-mineru-chunks-")
        chunk_paths: list[Path] = []
        with pymupdf.open(pdf_path) as src:
            for start in range(0, page_count, self.chunk_pages):
                end = min(start + self.chunk_pages, page_count)
                out = Path(tmpdir.name) / (
                    f"{pdf_path.stem}-p{start + 1:04d}-{end:04d}{pdf_path.suffix or '.pdf'}"
                )
                with pymupdf.open() as dst:
                    dst.insert_pdf(src, from_page=start, to_page=end - 1)
                    dst.save(out)
                chunk_paths.append(out)
        return chunk_paths, tmpdir

    @staticmethod
    def _merge_layout_jsons(
        chunk_jsons: list[tuple[int, dict[str, Any]]],
    ) -> dict[str, Any]:
        """把各分片的 layout.json 按页偏移合并回整篇文档的 layout.json。

        分片内 page_idx 是 0-based 分片局部页号，合并时加上页偏移并重打全局
        顺序；块 index 保持分片内相对值（YoloResult / IR 均不依赖全局唯一）。
        其余顶层字段（_backend、_version_name 等）取首个分片。
        """
        if len(chunk_jsons) == 1:
            return chunk_jsons[0][1]
        merged: dict[str, Any] = {}
        pdf_info: list[dict[str, Any]] = []
        for offset, chunk_json in chunk_jsons:
            if not merged:
                merged = {
                    key: copy.deepcopy(value)
                    for key, value in chunk_json.items()
                    if key != "pdf_info"
                }
            for page_info in chunk_json.get("pdf_info") or []:
                if not isinstance(page_info, dict):
                    continue
                page_info = copy.deepcopy(page_info)
                page_info["page_idx"] = (
                    int(page_info.get("page_idx", 0)) + offset
                )
                pdf_info.append(page_info)
        pdf_info.sort(key=lambda item: item.get("page_idx", 0))
        merged["pdf_info"] = pdf_info
        return merged

    def _fetch_layout_json(
        self, pdf_path: Path, translate_config
    ) -> dict[str, Any]:
        """完整链路：切分片 → 一个 batch 提交 → 轮询 → 下载合并。"""
        try:
            with pymupdf.open(pdf_path) as doc:
                page_count = doc.page_count
        except Exception:  # noqa: BLE001 - 不可读时退回单文件提交（旧路径）
            page_count = 0
        chunk_paths, tmpdir = self._split_pdf_chunks(pdf_path, page_count)
        if tmpdir is None:
            logger.info(
                "MinerU single-file submission: pages=%d", page_count
            )
        else:
            logger.info(
                "MinerU chunked submission: pages=%d chunks=%d chunk_pages=%d",
                page_count,
                len(chunk_paths),
                self.chunk_pages,
            )
        try:
            with httpx.Client(timeout=float(self.timeout_seconds)) as client:
                with _wait_span(
                    translate_config,
                    "mineru.request_upload_urls",
                    label="MinerU 申请上传地址",
                    chunks=len(chunk_paths),
                ) as span:
                    batch_id, upload_urls = self._request_upload_urls(
                        client, chunk_paths
                    )
                    if span is not None:
                        span["batch_id"] = batch_id
                with _wait_span(
                    translate_config,
                    "mineru.upload",
                    label="MinerU 上传 PDF 分片",
                    batch_id=batch_id,
                    chunks=len(chunk_paths),
                ):
                    for upload_url, chunk_path in zip(upload_urls, chunk_paths, strict=True):
                        self._upload_pdf(client, upload_url, chunk_path)
                with _wait_span(
                    translate_config,
                    "mineru.poll",
                    label="等待 MinerU 解析（轮询任务状态）",
                    batch_id=batch_id,
                    chunks=len(chunk_paths),
                ):
                    full_zip_urls = self._poll_full_zip_urls(
                        client, batch_id, translate_config, chunk_paths
                    )
                chunk_jsons: list[tuple[int, dict[str, Any]]] = []
                with _wait_span(
                    translate_config,
                    "mineru.download",
                    label="下载 MinerU 结果压缩包",
                    batch_id=batch_id,
                    chunks=len(chunk_paths),
                ) as span:
                    for index, (_chunk_path, zip_url) in enumerate(
                        zip(chunk_paths, full_zip_urls, strict=True)
                    ):
                        zip_bytes = self._download_zip_bytes(client, zip_url)
                        if span is not None:
                            span[f"chunk_{index}_bytes"] = len(zip_bytes)
                        offset = index * self.chunk_pages
                        chunk_jsons.append(
                            (offset, self._load_layout_json_from_zip_bytes(zip_bytes))
                        )
            with _wait_span(
                translate_config,
                "mineru.parse_zip",
                label="解包并合并 MinerU 结果（本地）",
                chunks=len(chunk_paths),
            ):
                return self._merge_layout_jsons(chunk_jsons)
        finally:
            if tmpdir is not None:
                tmpdir.cleanup()

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

    def _write_layout_cache(
        self, cache_file: Path, layout_json: dict[str, Any]
    ) -> None:
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

    def _dump_raw_layout_json(
        self, layout_json: dict[str, Any], translate_config
    ) -> None:
        """debug 采集开启时把 provider 原始 layout JSON 落盘（``layout_raw.json``）。

        解析管线随后把它归档进 debug run（``provider-layout.json``）；recorder
        关闭时不写（原始 JSON 不含解析后的规范化结构，正常流程用不到）。
        """
        if getattr(translate_config, "debug_recorder", None) is None:
            return
        output_path = self._provider_ir_output_path(translate_config)
        if output_path is None:
            return
        try:
            raw_path = output_path.with_name("layout_raw.json")
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = raw_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(layout_json, ensure_ascii=False), encoding="utf-8"
            )
            tmp.replace(raw_path)
        except OSError:
            logger.warning("Failed to write MinerU raw layout JSON", exc_info=True)

    def _prepare_provider_ir(
        self, layout_json: dict[str, Any], translate_config
    ) -> None:
        """构建并落盘 provider IR。IR 构建失败不阻断 YoloResult 路径。"""
        self._dump_raw_layout_json(layout_json, translate_config)
        try:
            self._build_provider_document(layout_json)
        except Exception:  # noqa: BLE001 - IR 是附加产物，不应影响布局解析
            self.provider_document = None
            logger.warning("Failed to build MinerU provider IR", exc_info=True)
            return
        self._persist_provider_document(translate_config)

    def handle_document(
        self,
        pages,
        mupdf_doc: pymupdf.Document,
        translate_config,
        save_debug_image,
    ):
        # 每次 handle_document 重置 provider IR，避免跨调用残留。
        self.provider_document = None
        replay_path = os.environ.get("BABELDOC_MINERU_LAYOUT_JSON")
        if replay_path:
            layout_json = json.loads(Path(replay_path).read_text(encoding="utf-8"))
            self._prepare_provider_ir(layout_json, translate_config)
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
            with _wait_span(
                translate_config,
                "mineru.cache",
                label="MinerU 布局缓存命中（读本地缓存，无网络等待）",
                cache_file=str(cache_file),
            ):
                layout_json = json.loads(cache_file.read_text(encoding="utf-8"))
        else:
            layout_json = self._fetch_layout_json(pdf_path, translate_config)
            if cache_file is not None:
                self._write_layout_cache(cache_file, layout_json)
        self._prepare_provider_ir(layout_json, translate_config)
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
