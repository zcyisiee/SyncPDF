"""跨进程持久化 stamp 缓存（``working_dir/latex_cache/``）。

XeLaTeX 编译是 LaTeX bbox 路径上最贵的一步（P3 实测三篇 177–350s）。同一篇
文档的重复回放（验收时先 ``reconstruct`` 再 ``reconstruct --latex-bbox``）会让
相同段落重复编译，因此把编译产物按「模板/字体命名空间 + 请求内容键」落盘：

- key = ``sha1(namespace | StampRequest.cache_key)``；namespace 含模板版本与
  字体签名（:func:`renderer.cache_namespace`），模板或字体一变缓存自然失效，
  不会串用旧字形/旧版式的贴片；
- 值 = 单页贴片 PDF（``<key>.pdf``）+ 元数据（``<key>.json``：字号/缩放/行距/
  原始尝试次数），命中时按元数据重建 :class:`StampResult`，``seconds=0``、
  ``compile_attempts=0``（本次运行确实没有编译）；
- 写入用临时文件 + ``os.replace`` 原子替换，多线程与多次运行都安全。

缓存缺失、损坏、不可写都只是「不命中」（调用方照常编译），绝不抛异常阻断
翻译（架构原则 1）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import StampRequest
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import StampResult
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import cache_namespace

logger = logging.getLogger(__name__)

#: 缓存目录名（相对 ``working_dir``）。
CACHE_DIR_NAME = "latex_cache"


class StampCache:
    """按请求内容键落盘的单页贴片缓存。"""

    def __init__(self, cache_dir: Path, namespace: str):
        self.cache_dir = Path(cache_dir)
        self.namespace = namespace

    def key_for(self, request: StampRequest) -> str:
        """请求 → 缓存键（命名空间 + 模板/字体 + 请求内容）。"""
        payload = f"{self.namespace}|{request.cache_key!r}"
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()  # noqa: S324 - 非密码学用途

    def get(self, request: StampRequest) -> StampResult | None:
        """命中时返回指向缓存 PDF 的结果；未命中/损坏返回 None。"""
        key = self.key_for(request)
        pdf_path = self.cache_dir / f"{key}.pdf"
        meta_path = self.cache_dir / f"{key}.json"
        try:
            if not pdf_path.is_file() or not meta_path.is_file():
                return None
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.debug("读取 LaTeX stamp 缓存失败", exc_info=True)
            return None
        return StampResult(
            key=request.key,
            ok=True,
            pdf_path=str(pdf_path),
            font_size=meta.get("font_size"),
            scale=meta.get("scale"),
            lead=meta.get("lead"),
            seconds=0.0,
            compile_attempts=0,
            reason="cache",
        )

    def put(self, request: StampRequest, result: StampResult) -> StampResult | None:
        """把编译产物落盘；返回指向缓存 PDF 的结果（写失败返回 None）。"""
        if not result.ok or not result.pdf_path:
            return None
        source = Path(result.pdf_path)
        if not source.is_file():
            return None
        key = self.key_for(request)
        pdf_path = self.cache_dir / f"{key}.pdf"
        meta_path = self.cache_dir / f"{key}.json"
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            if not pdf_path.is_file():
                self._atomic_copy(source, pdf_path)
            meta_path.write_text(
                json.dumps(
                    {
                        "namespace": self.namespace,
                        "font_size": result.font_size,
                        "scale": result.scale,
                        "lead": result.lead,
                        "attempts": result.compile_attempts,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            logger.debug("写入 LaTeX stamp 缓存失败", exc_info=True)
            return None
        return StampResult(
            key=request.key,
            ok=True,
            pdf_path=str(pdf_path),
            font_size=result.font_size,
            scale=result.scale,
            lead=result.lead,
            # 本次确实编译了：保留真实尝试次数/耗时（命中时才归零）。
            seconds=result.seconds,
            compile_attempts=result.compile_attempts,
            reason=result.reason,
        )

    def _atomic_copy(self, source: Path, target: Path) -> None:
        """先写临时文件再原子替换，避免并发读到半个 PDF。"""
        handle, tmp_name = tempfile.mkstemp(
            dir=self.cache_dir, prefix=".tmp-", suffix=".pdf"
        )
        os.close(handle)
        tmp_path = Path(tmp_name)
        try:
            shutil.copyfile(source, tmp_path)
            tmp_path.replace(target)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    logger.debug("清理 stamp 缓存临时文件失败", exc_info=True)


def build_stamp_cache(config, capability) -> StampCache | None:
    """按配置构造 ``<working_dir>/latex_cache`` 缓存；无 working_dir 时不落盘。"""
    working_dir = getattr(config, "working_dir", None)
    if not working_dir:
        return None
    return StampCache(
        Path(working_dir) / CACHE_DIR_NAME,
        namespace=cache_namespace(capability),
    )
