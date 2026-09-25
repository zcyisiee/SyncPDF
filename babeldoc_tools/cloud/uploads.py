"""上传：分块落盘并算 sha256，校验是可读的文字版 PDF，再按内容去重存进 ``sources/``。"""

from __future__ import annotations

import hashlib
import secrets
from pathlib import Path
from typing import BinaryIO

from babeldoc_tools.cloud.db import Paths
from babeldoc_tools.common import ToolError

__all__ = ["MAX_BYTES", "MAX_PAGES", "save_source"]

MAX_BYTES = 50 * 1024 * 1024
MAX_PAGES = 60
_CHUNK = 1 << 20
_MAGIC = b"%PDF-"


def _inspect(path: Path) -> list[list[float]]:
    """每页可见尺寸；打不开、加密、超页数、没有文字层 → ``ToolError``。"""
    import pymupdf

    try:
        doc = pymupdf.open(path)
    except Exception as exc:  # noqa: BLE001 - pymupdf 对坏文件抛多种异常
        raise ToolError("pdf_unreadable", "无法打开这个 PDF，文件可能已损坏") from exc
    with doc:
        if doc.needs_pass:
            raise ToolError("pdf_encrypted", "这个 PDF 有密码保护，请先解除后再上传")
        if doc.page_count == 0:
            raise ToolError("pdf_unreadable", "这个 PDF 没有页面")
        if doc.page_count > MAX_PAGES:
            raise ToolError("too_many_pages", f"单篇不超过 {MAX_PAGES} 页（这篇 {doc.page_count} 页）")
        if not any(page.get_text("text").strip() for page in doc):
            raise ToolError("no_text_layer", "没有找到可翻译的文字；目前只支持文字版 PDF，不支持扫描件")
        return [[round(page.rect.width, 2), round(page.rect.height, 2)] for page in doc]


def save_source(paths: Paths, stream: BinaryIO) -> tuple[str, int, list[list[float]]]:
    """返回 ``(sha256, 字节数, 每页尺寸)``；原文已存在则只校验、不覆盖。"""
    partial = paths.tmp / f"{secrets.token_hex(8)}.pdf"
    digest = hashlib.sha256()
    size = 0
    try:
        with partial.open("xb") as out:
            while chunk := stream.read(_CHUNK):
                if size == 0 and not chunk.startswith(_MAGIC):
                    raise ToolError("not_pdf", "只支持 PDF 文件")
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ToolError("file_too_large", f"单篇不超过 {MAX_BYTES >> 20} MB")
                digest.update(chunk)
                out.write(chunk)
        if size == 0:
            raise ToolError("not_pdf", "只支持 PDF 文件")
        page_sizes = _inspect(partial)
        sha = digest.hexdigest()
        target = paths.source(sha)
        if target.is_file():
            partial.unlink()
        else:
            partial.replace(target)
        return sha, size, page_sizes
    finally:
        partial.unlink(missing_ok=True)
