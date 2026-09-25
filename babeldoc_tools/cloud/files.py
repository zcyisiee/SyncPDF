"""按需生成的文件：预览 WebP（宽 1600px）与中英对照 PDF，都是可重建缓存。

- 预览按 ``<key>/<page>.webp`` 缓存；key 是原文 sha、完成后的译文 sha，或翻译中的
  ``<tid>-<attempt>.<revision>``。命中刷新 mtime，清理按 mtime 从旧到新淘汰。
- 对照版由 ``syncpdf-cli dual`` 从原文与译文生成，同一 translation 并发请求共用一把锁。
"""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

from babeldoc_tools import rust_backend
from babeldoc_tools.cloud.db import Paths
from babeldoc_tools.common import ToolError

__all__ = ["PREVIEW_WIDTH", "Duals", "Previews"]

PREVIEW_WIDTH = 1600
_WEBP_QUALITY = 80
_DUAL_TIMEOUT = 600


def _touch(path: Path) -> Path:
    os.utime(path)
    return path


class Previews:
    """单页渲染；同时最多两个渲染（每个约 10MB 像素缓冲）。"""

    def __init__(self, paths: Paths, workers: int = 2) -> None:
        self.paths = paths
        self._slots = threading.Semaphore(workers)

    def page(self, pdf: Path, key: str, page: int) -> Path:
        target = self.paths.preview / key / f"{page}.webp"
        if target.is_file():
            return _touch(target)
        with self._slots:
            if target.is_file():
                return _touch(target)
            import pymupdf
            from PIL import Image

            if not pdf.is_file():
                raise ToolError("preview_unavailable", "这一页暂时没有可预览的内容")
            with pymupdf.open(pdf) as doc:
                if not 1 <= page <= doc.page_count:
                    raise ToolError("page_not_found", f"没有第 {page} 页")
                sheet = doc[page - 1]
                zoom = PREVIEW_WIDTH / sheet.rect.width
                pix = sheet.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
                image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_name(f".{target.name}.{threading.get_ident()}.tmp")
            image.save(partial, "WEBP", quality=_WEBP_QUALITY)
            partial.replace(target)
        return target


class Duals:
    """中英对照：首次下载时生成到 ``dual/<tid>.pdf``。"""

    def __init__(self, paths: Paths, engine: str | None) -> None:
        self.paths = paths
        self.engine = Path(engine).expanduser().resolve() if engine else rust_backend._ENGINE
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def get(self, tid: str, source: Path, translated: Path) -> Path:
        target = self.paths.dual / f"{tid}.pdf"
        with self._guard:
            lock = self._locks.setdefault(tid, threading.Lock())
        with lock:
            if target.is_file():
                return _touch(target)
            completed = subprocess.run(  # noqa: S603 - 固定引擎路径，argv 不经 shell
                [
                    str(self.engine), "dual", "--source", str(source),
                    "--translated", str(translated), "--output", str(target),
                ],
                capture_output=True,
                text=True,
                timeout=_DUAL_TIMEOUT,
                check=False,
            )
            if completed.returncode != 0 or not target.is_file():
                raise ToolError("dual_failed", "中英对照版生成失败", stderr=completed.stderr[-2000:])
        return target
