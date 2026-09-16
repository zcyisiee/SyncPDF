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
    """按请求内容键落盘的单页贴片缓存。

    ``bypass_reads=True``（``--debug-recompile`` 冷编译）只跳过**历史**条目的
    读取：本实例 ``put`` 写入的键仍可读（同一次运行内的去重/复用不受影响），
    且正常写入不删除旧缓存。
    """

    def __init__(
        self,
        cache_dir: Path,
        namespace: str,
        *,
        bypass_reads: bool = False,
        debug_recorder=None,
    ):
        self.cache_dir = Path(cache_dir)
        self.namespace = namespace
        self.bypass_reads = bool(bypass_reads)
        #: 可选的诊断 recorder；None 时零额外 IO。
        self._debug_recorder = debug_recorder
        #: 本次运行写入的键：冷读绕过只挡历史条目，本实例产物仍可命中。
        self._written: set[str] = set()

    def key_for(self, request: StampRequest) -> str:
        """请求 → 缓存键（命名空间 + 模板/字体 + 请求内容）。"""
        payload = f"{self.namespace}|{request.cache_key!r}"
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()  # noqa: S324 - 非密码学用途

    def get(self, request: StampRequest) -> StampResult | None:
        """命中时返回指向缓存 PDF 的结果；未命中/损坏返回 None。"""
        key = self.key_for(request)
        if self.bypass_reads and key not in self._written:
            self._record_cache_event("cache_bypass", request, key)
            return None
        pdf_path = self.cache_dir / f"{key}.pdf"
        meta_path = self.cache_dir / f"{key}.json"
        try:
            if not pdf_path.is_file() or not meta_path.is_file():
                self._record_cache_event("cache_miss", request, key)
                return None
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.debug("读取 LaTeX stamp 缓存失败", exc_info=True)
            self._record_cache_event("cache_miss", request, key, error="unreadable")
            return None
        result = StampResult(
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
        # 诊断来源（可选字段）：旧缓存没有该字段时保持空 dict（证据缺失，
        # 查看器如实显示，绝不推断为首次成功）。
        result.debug_ref = dict(meta.get("debug") or {})
        self._record_cache_event("cache_hit", request, key, result=result)
        return result

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
        # 诊断来源（兼容性可选字段）：本次编译的候选证据 id + run_id。
        debug_ref = dict(getattr(result, "debug_ref", None) or {})
        if self._debug_recorder is not None:
            debug_ref.setdefault("run_id", self._debug_recorder.run_id)
        meta = {
            "namespace": self.namespace,
            "font_size": result.font_size,
            "scale": result.scale,
            "lead": result.lead,
            "attempts": result.compile_attempts,
        }
        if debug_ref:
            meta["debug"] = debug_ref
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            # 始终写入本次编译产物（原子替换）：``bypass_reads`` 冷编译下旧
            # 条目可能已存在，但本次结果才是权威内容；同键同内容时覆盖无害。
            self._atomic_copy(source, pdf_path)
            meta_path.write_text(
                json.dumps(meta, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            logger.debug("写入 LaTeX stamp 缓存失败", exc_info=True)
            return None
        self._written.add(key)
        stored = StampResult(
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
        stored.debug_ref = debug_ref
        self._record_cache_event("cache_write", request, key, result=stored)
        return stored

    def _record_cache_event(
        self, kind: str, request: StampRequest, key: str, *, result=None, error=None
    ) -> None:
        """缓存生命周期事件（``cache_bypass``/``cache_miss``/``cache_hit``/
        ``cache_write``）；命中事件回指真实候选证据。"""
        recorder = self._debug_recorder
        if not recorder:
            return
        debug_ref = dict(getattr(result, "debug_ref", None) or {})
        data = {
            "layer": "persistent_cache",
            "cache_key": key,
            "request_key": request.key,
            "candidate_id": debug_ref.get("candidate_id"),
            "evidence_run": debug_ref.get("run_id"),
        }
        if error:
            data["error"] = error
        recorder.record_event("build", kind, data)

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
    """按配置构造 ``<working_dir>/latex_cache`` 缓存；无 working_dir 时不落盘。

    ``config.latex_debug_recompile``（``--debug-recompile``）→ 冷读绕过历史
    条目；``config.debug_recorder`` → 缓存生命周期事件。
    """
    working_dir = getattr(config, "working_dir", None)
    if not working_dir:
        return None
    from babeldoc.debug_recorder import get_current

    return StampCache(
        Path(working_dir) / CACHE_DIR_NAME,
        namespace=cache_namespace(capability),
        bypass_reads=bool(getattr(config, "latex_debug_recompile", False)),
        debug_recorder=getattr(config, "debug_recorder", None) or get_current(),
    )
