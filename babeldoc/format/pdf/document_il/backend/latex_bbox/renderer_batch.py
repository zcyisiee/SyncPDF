r"""整文档轮次制批编译（``BatchStampRenderer``）。

背景：逐段 subprocess 编译是 LaTeX bbox 路径上最贵的一步——200+ 段文档在 P3
实测耗时 177–350s（单篇）。把同一轮里所有待定段拼成**一份** TeX（每段一页），
一次 xelatex 进程即可完成数十段，编译开销从「段数 ×（进程启动 + 排版）」降到
「块数 ×（进程启动 + 排版）」。

必须遵守的排版约束（``/tmp/latex-spike`` 结论）：

- ``geometry`` 的 ``\newgeometry`` **不能**改纸张尺寸（第 2 页版心仍是旧值、
  右侧被裁）→ **逐页显式设置** ``\pdfpagewidth/\pdfpageheight`` 与
  ``\hsize/\vsize/\textwidth/\textheight/\columnwidth/\linewidth``；
- 每段正文**直放页面**（版心/段落参数与单段渲染器完全一致），``\vsize`` 放大到
  单段不可能触发分页：于是「一段 = 一页」成立，且与单段渲染逐像素等价。**不要用
  ``minipage``/``\vbox`` 包装**：``minipage`` 的段落恢复会改变首行缩进的断行
  语义（实测 overfull 量恰等于 ``first_line_dx``、续行也被缩进），``\vbox`` 又缺
  页面级 ``\topskip`` 规则（整段上移数 bp），固定高度 ``\vbox`` 还会把
  ``\hangindent`` 的悬挂缩进吃掉。页级 ``Overfull \vbox`` 在此不作为失败信号，
  溢出一律以 ``_measure_fit`` 的墨迹/文本为准。
- ``fontspec`` 的 ``\\setmainfont`` 只能在导言区使用（实测正文里调用报
  ``Can be used only in preamble`` 并把参数当正文排出）→ 一块内的请求按
  ``serif`` 分组，字体在导言区一次性声明。
- ``-interaction=nonstopmode`` **不加** ``-halt-on-error``：单段错误不停机
  （实测错误段之后的段落照常排版），坏段由逐段归属 + 超时二分隔离；
- 段前后 ``\message{@@S n@@}``/``\message{@@E n@@}`` 标记 + ``at lines X--Y``
  行号归属（见 :func:`_attribute_log`）；
- 批超时 = 基础超时 + 0.2s × 段数；超时按块二分重试以隔离坏段。

状态机（每段，最多 :data:`_MAX_BATCH_ROUNDS` 轮批编译）：源字号 + 源行距 →
行距 ×1.1 → 行距 ×0.9；仍不成功的段交回 :class:`BboxStampRenderer` 单段渲染
（含 ×0.95 有界缩小阶梯）。内容级失败（``text-mismatch``）、TeX 错误（``!``）
与超时**不再重试**，与单段渲染一致，直接回退现有渲染路径。
"""

from __future__ import annotations

import contextlib
import logging
import re
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pymupdf

from babeldoc.debug_recorder import CompileCandidate
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import _WIDTH_TOLERANCE
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import (
    DEFAULT_LEAD_RATIO,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import TEX_COMMON
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import (
    BboxStampRenderer,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import StampRequest
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import StampResult
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import (
    _measure_page_fit,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import (
    font_setup_clauses,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import (
    overfull_hbox_exceeds,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import topskip_clause

logger = logging.getLogger(__name__)

#: 一块 tex 里的段数（spike 结论：~50 段/块 × CPU 并行）。
_BLOCK_SIZE = 50
#: 批超时 = 基础超时 + 该系数 × 段数（计划约定 0.2s/段）。
_BATCH_TIMEOUT_PER_SEGMENT = 0.2
#: 超时二分隔离的最大深度（一份最多裂成 2^3 = 8 块）。
_MAX_SPLIT_DEPTH = 3
#: 批编译每页的 ``\vsize``：大到单段正文不可能触发分页（超出页面的部分由
#: ``_measure_fit`` 的墨迹/文本判定发现并回退），从而「一段 = 一页」始终成立。
_SEGMENT_VSIZE_BP = 6000.0
#: 每段候选档位（``scale`` 相对源字号、``lead_ratio`` 相对推导行距），
#: 顺序即优先级：源字号+源行距 → 行距 ±10% → ×0.95 有界缩小。
#: 第 1 轮只编译首选档（最省）；失败的段在第 2 轮把剩余档位**一次**编译完，
#: 而不是每轮只试一档（逐轮等待会把壁钟时间乘上轮数）。
_VARIANT_LADDER: tuple[tuple[float, float], ...] = (
    (1.0, 1.0),
    (1.0, 1.1),
    (1.0, 0.9),
    (0.95, 0.95),
    (0.9025, 0.9025),
    (0.857375, 0.857375),
)
#: 批编译轮数上限（第 1 轮首选档、第 2 轮剩余档位）；超出仍失败的段交单段渲染。
_MAX_BATCH_ROUNDS = 2
#: 内容级失败：缩小/重试都修不了（与单段渲染一致，直接回退现有渲染路径）。
#: 批编译失败原因带 ``s{n}:`` 前缀（如 ``s0:text-mismatch``），因此按子串判定。
_TERMINAL_REASONS = frozenset({"text-mismatch", "no-extractable-text", "timeout"})
#: TeX 错误（``!``）前缀：单段渲染同样不重试，避免坏段拖慢整篇。
_TERMINAL_PREFIXES = ("compile:",)
#: 归属不可用（页数/标记不符）：不再重试批编译，直接交单段渲染。
_SINGLE_ONLY_REASONS = frozenset({"batch-attribution-failed"})

_MARKER_START = re.compile(r"@@S (\d+)@@")
_MARKER_END = re.compile(r"@@E (\d+)@@")
_OVERFULL_VBOX = re.compile(r"Overfull \\vbox")
#: ``at lines X--Y`` / ``on line X``：TeX 报出的源 tex 行号。
_LINES_RANGE = re.compile(r"(?:at lines (\d+)--(\d+)|on line (\d+))")
#: ``!`` 错误上下文里的出错行号（``l.NNN``）。
_ERROR_LINE = re.compile(r"^l\.(\d+)")
#: ``!`` 错误后查找 ``l.NNN`` 的行窗口。
_ERROR_CONTEXT_LINES = 25


class BatchStampRenderer:
    """轮次制批编译渲染器：对外与 :class:`BboxStampRenderer` 同构。"""

    def __init__(
        self,
        capability,
        timeout_seconds: float = 45.0,
        max_workers: int = 2,
        block_size: int = _BLOCK_SIZE,
        cache=None,
        fallback: BboxStampRenderer | None = None,
        debug_recorder=None,
    ):
        self._capability = capability
        self._timeout = max(5.0, float(timeout_seconds))
        self._max_workers = max(1, int(max_workers))
        self._block_size = max(1, int(block_size))
        self._cache = cache
        #: 可选的诊断 recorder；None 时零额外 IO、行为与非 debug 完全一致。
        self._debug_recorder = debug_recorder
        self._cache_hits = 0
        self._compile_seconds = 0.0
        self._batch_count = 0
        self._segment_count = 0
        self._round_count = 0
        self._render_calls = 0
        #: 每轮明细：``{call, attempt, segments, blocks, seconds}``（写入报告）。
        self._round_log: list[dict] = []
        self._fallback_segments = 0
        self._lock = threading.Lock()
        #: 诊断专用：request key → 最近一次记录的候选 id（跨轮/回退父链）。
        self._last_candidates: dict[str, str] = {}
        #: 单段回退渲染器（×0.95 有界缩小 + 进程内缓存）；与批编译共享持久缓存。
        self._fallback = (
            fallback
            if fallback is not None
            else BboxStampRenderer(
                capability,
                timeout_seconds=self._timeout,
                max_workers=self._max_workers,
                cache=cache,
                debug_recorder=debug_recorder,
            )
        )

    @property
    def fallback(self) -> BboxStampRenderer:
        return self._fallback

    @property
    def cache_hits(self) -> int:
        """批编译 + 单段回退的缓存命中总数。"""
        return self._cache_hits + self._fallback.cache_hits

    @property
    def compile_seconds(self) -> float:
        """批编译 + 单段回退的累计编译耗时（秒）。"""
        return self._compile_seconds + self._fallback.compile_seconds

    @property
    def batch_count(self) -> int:
        """实际启动的 xelatex 批次数（含超时二分与下扩重试）。"""
        return self._batch_count

    @property
    def segment_count(self) -> int:
        """进入批编译的段数（不含缓存命中与单段回退）。"""
        return self._segment_count

    @property
    def fallback_segments(self) -> int:
        """交给单段渲染器的段数（含缓存命中）。"""
        return self._fallback_segments

    @property
    def fallback_seconds(self) -> float:
        """单段渲染器耗掉的编译时间（秒）。"""
        return self._fallback.compile_seconds

    @property
    def round_log(self) -> list[dict]:
        """每轮批编译明细（供报告核对轮数/每轮段数/耗时）。"""
        return list(self._round_log)

    # ------------------------------------------------------------------
    def render_many(
        self, requests: list[tuple[StampRequest, Path]]
    ) -> dict[str, StampResult]:
        """批量编译：多轮批编译（阶梯档位）+ 坏段回退单段渲染。

        与 :meth:`BboxStampRenderer.render_many` 同签名/同语义（key → 结果）。
        """
        if not requests:
            return {}
        request_id = self._record_requests(requests)
        results: dict[str, StampResult] = {}
        pending: dict[str, tuple[StampRequest, Path]] = {}
        # Paragraph ids are unique in normal documents, but repeated translated
        # text is common (for example, list labels and recurring captions).  A
        # request's cache key includes every layout input, so requests sharing
        # it are byte-for-byte interchangeable and only one needs compiling.
        aliases: dict[str, list[str]] = {}
        pending_by_cache: dict[tuple, str] = {}
        for request, workdir in requests:
            cached = self._cache.get(request) if self._cache is not None else None
            if cached is not None:
                self._cache_hits += 1
                results[request.key] = cached
                self._record_reuse(
                    request, cached, "persistent_cache", request_id=request_id
                )
            else:
                # Keep the legacy no-cache behavior (each request is compiled
                # independently); deduplication is enabled when the persistent
                # stamp cache is active, where avoiding duplicate work is safe
                # across repeated translation runs as well.
                if self._cache is not None:
                    canonical = pending_by_cache.get(request.cache_key)
                    if canonical is not None:
                        aliases.setdefault(canonical, []).append(request.key)
                        continue
                    pending_by_cache[request.cache_key] = request.key
                pending[request.key] = (request, Path(workdir))
        if len(pending) <= 1:
            # 单段没有批量收益：直接走单段渲染（完整缩小阶梯）。
            for key, (request, _workdir) in pending.items():
                request.debug_parent = self._last_candidates.get(key)
                self._record_fallback(
                    key, "single-segment", request_id=request_id
                )
            results = self._render_leftovers(pending, results)
            results = _copy_alias_results(results, aliases)
            self._record_alias_reuse(aliases, results, request_id=request_id)
            return results

        self._render_calls += 1
        single_only: dict[str, tuple[StampRequest, Path]] = {}
        last_reasons: dict[str, str] = {}
        for attempt in range(1, _MAX_BATCH_ROUNDS + 1):
            if not pending:
                break
            with self._lock:
                self._round_count += 1
            outcomes = self._compile_variants(pending, attempt, request_id)
            for key, outcome in outcomes.items():
                if key not in pending:
                    continue
                reason = outcome.reason or ""
                last_reasons[key] = reason
                if outcome.ok:
                    entry = pending.pop(key)
                    results[key] = self._store(entry[0], outcome)
                elif _is_terminal_reason(reason):
                    pending.pop(key)
                    results[key] = outcome
                elif reason in _SINGLE_ONLY_REASONS:
                    single_only[key] = pending.pop(key)
        single_only.update(pending)
        for key, (request, _workdir) in single_only.items():
            request.debug_parent = self._last_candidates.get(key)
            self._record_fallback(
                key,
                last_reasons.get(key) or "batch-rounds-exhausted",
                request_id=request_id,
            )
        results = self._render_leftovers(single_only, results)
        results = _copy_alias_results(results, aliases)
        self._record_alias_reuse(aliases, results, request_id=request_id)
        return results

    def _render_leftovers(
        self,
        leftovers: dict[str, tuple[StampRequest, Path]],
        results: dict[str, StampResult],
    ) -> dict[str, StampResult]:
        """剩余段交单段渲染器（含 ×0.95 有界缩小与缓存）。"""
        if leftovers:
            self._fallback_segments += len(leftovers)
            outcomes = self._fallback.render_many(list(leftovers.values()))
            for key, outcome in outcomes.items():
                entry = leftovers.get(key)
                if entry is None:
                    continue
                results[key] = self._store(entry[0], outcome)
        return results

    def _store(self, request: StampRequest, outcome: StampResult) -> StampResult:
        """成功结果落持久缓存（返回指向缓存 PDF 的结果）。"""
        if outcome.ok and self._cache is not None:
            return self._cache.put(request, outcome) or outcome
        return outcome

    # ------------------------------------------------------------------
    def _compile_variants(
        self,
        pending: dict[str, tuple[StampRequest, Path]],
        attempt: int,
        request_id: str | None = None,
    ) -> dict[str, StampResult]:
        """本轮所有待定段的候选档位按块编译（块间并行，块内一份 tex）。"""
        # 一轮内的候选项（每段可多页）：按轮次取阶梯档位。
        # 按 serif 分组：``\setmainfont`` 只能在导言区声明（实测正文里调用报
        # “Can be used only in preamble”），因此一块内字体必须一致。
        groups: dict[bool, list[str]] = {}
        for key in pending:
            groups.setdefault(bool(pending[key][0].serif), []).append(key)
        blocks: list[list[str]] = []
        for group in groups.values():
            blocks.extend(
                group[start : start + self._block_size]
                for start in range(0, len(group), self._block_size)
            )
        results: dict[str, StampResult] = {}
        started = time.perf_counter()
        # items 为 (key, variant, priority) 三元组：priority 是该变体在整段
        # 候选阶梯里的全局序号（第 1 轮首选档 = 0，第 2 轮剩余档 = 1..n）。
        items_by_block: list[list[tuple[str, StampRequest, int]]] = [
            [
                (key, variant, position + attempt - 1)
                for key in block
                for position, variant in enumerate(
                    _round_variants(pending[key][0], attempt)
                )
            ]
            for block in blocks
        ]
        if len(blocks) == 1 or self._max_workers <= 1:
            for block_keys, items in zip(blocks, items_by_block, strict=True):
                results.update(
                    self._run_block(block_keys, items, pending, attempt, request_id)
                )
        else:
            with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
                futures = [
                    pool.submit(
                        self._run_block,
                        block_keys,
                        items,
                        pending,
                        attempt,
                        request_id,
                    )
                    for block_keys, items in zip(
                        blocks, items_by_block, strict=True
                    )
                ]
                for future in futures:
                    results.update(future.result())
        self._round_log.append(
            {
                "call": self._render_calls,
                "attempt": attempt,
                "segments": len(pending),
                "pages": sum(len(items) for items in items_by_block),
                "blocks": len(blocks),
                "seconds": round(time.perf_counter() - started, 3),
            }
        )
        return results

    def _run_block(
        self,
        keys: list[str],
        items: list[tuple[str, StampRequest, int]],
        pending: dict[str, tuple[StampRequest, Path]],
        attempt: int,
        request_id: str | None = None,
    ) -> dict[str, StampResult]:
        """编译一块；块内异常只影响本块（标记归属失败，交单段渲染）。"""
        try:
            workdir = Path(pending[keys[0]][1])
            base_by_key = {key: pending[key][0] for key in keys}
            return self._compile_block(
                items, workdir, attempt, base_by_key, request_id=request_id
            )
        except Exception:  # noqa: BLE001 - 单块失败不拖垮整篇
            logger.warning("LaTeX bbox 批编译块失败，交回单段渲染", exc_info=True)
            self._record_batch_failure(
                items,
                attempt=attempt,
                request_id=request_id,
                base_by_key=locals().get("base_by_key"),
                reason="batch-attribution-failed",
            )
            return {
                key: StampResult(
                    key=key, ok=False, reason="batch-attribution-failed"
                )
                for key in keys
            }

    def _compile_block(
        self,
        items: list[tuple[str, StampRequest, int]],
        workdir: Path,
        attempt: int,
        base_by_key: dict[str, StampRequest],
        depth: int = 0,
        request_id: str | None = None,
    ) -> dict[str, StampResult]:
        """编译一块（每个候选项一页）；超时按二分隔离坏段。"""
        if not items:
            return {}
        keys = list(dict.fromkeys(key for key, _request, _prio in items))
        with self._lock:
            self._batch_count += 1
            self._segment_count += len(keys)
        recorder = self._debug_recorder
        batch_id = recorder.new_id("batch") if recorder else None
        try:
            tex, ranges = self.build_batch_tex(
                [request for _key, request, _prio in items]
            )
            # 调用方传入的 workdir 可能尚不存在（与单段渲染器的 attempt 目录
            # 一致，这里自行建目录，不让路径问题把整块推给单段回退）。
            Path(workdir).mkdir(parents=True, exist_ok=True)
            block_dir = Path(tempfile.mkdtemp(dir=workdir, prefix="batch-"))
            stem = "batch"
            # 批超时 = 基础超时 + 0.2s × 段数（计划约定；按段而非按候选页计，
            # 每页编译实测仅 ~0.04s，余量充足）。
            timeout = self._timeout + _BATCH_TIMEOUT_PER_SEGMENT * len(keys)
            started = time.perf_counter()
            log, timed_out = self._run_xelatex(
                tex,
                block_dir,
                stem,
                timeout,
                debug_context={
                    "batch_id": batch_id,
                    "round": attempt,
                    "segments": len(keys),
                    "pages": len(items),
                    "depth": depth,
                },
            )
            self._compile_seconds += time.perf_counter() - started
            artifacts = self._archive_block_artifacts(
                batch_id, tex, block_dir, stem
            )
            if timed_out:
                if len(items) > 1 and depth < _MAX_SPLIT_DEPTH:
                    half = len(items) // 2
                    merged = self._compile_block(
                        items[:half],
                        workdir,
                        attempt,
                        base_by_key,
                        depth + 1,
                        request_id=request_id,
                    )
                    merged.update(
                        self._compile_block(
                            items[half:],
                            workdir,
                            attempt,
                            base_by_key,
                            depth + 1,
                            request_id=request_id,
                        )
                    )
                    return merged
                logger.warning(
                    "LaTeX bbox 批编译超时（%.1fs，%d 段/%d 页），回退现有渲染",
                    timeout,
                    len(keys),
                    len(items),
                )
                # 超时产物不可信：页归属无法验证，pdf_page_index 留空。
                self._record_batch_failure(
                    items,
                    attempt=attempt,
                    request_id=request_id,
                    batch_id=batch_id,
                    base_by_key=base_by_key,
                    reason="timeout",
                    artifacts=artifacts,
                    ranges=ranges,
                )
                return {
                    key: StampResult(key=key, ok=False, reason="timeout")
                    for key in keys
                }
            return self._evaluate_block(
                items,
                ranges,
                block_dir,
                stem,
                log,
                attempt,
                base_by_key,
                batch_id=batch_id,
                request_id=request_id,
                block_artifacts=artifacts,
            )
        except Exception:
            # 块级异常只影响本块（与 ``_run_block`` 语义一致：归属失败交单段
            # 渲染）。候选已尽量记录（归属不明 → 页索引留空），不再上抛，
            # 避免外层重复记录同一批候选。
            logger.warning("LaTeX bbox 批编译块内部失败，交回单段渲染", exc_info=True)
            self._record_batch_failure(
                items,
                attempt=attempt,
                request_id=request_id,
                batch_id=batch_id,
                base_by_key=base_by_key,
                reason="batch-exception",
            )
            return {
                key: StampResult(
                    key=key, ok=False, reason="batch-attribution-failed"
                )
                for key in keys
            }

    def _evaluate_block(
        self,
        items: list[tuple[str, StampRequest, int]],
        ranges: list[tuple[int, int]],
        block_dir: Path,
        stem: str,
        log: str,
        attempt: int,
        base_by_key: dict[str, StampRequest],
        *,
        batch_id: str | None = None,
        request_id: str | None = None,
        block_artifacts: dict | None = None,
    ) -> dict[str, StampResult]:
        """逐页判定，再按段取优先级最高的通过档位（归属 → 抽页 → fit 测量）。"""
        keys = list(dict.fromkeys(key for key, _request, _prio in items))
        attribution_failed = {
            key: StampResult(
                key=key, ok=False, reason="batch-attribution-failed"
            )
            for key in keys
        }
        pdf_path = block_dir / f"{stem}.pdf"
        info = _attribute_log(log, ranges)
        if info["preamble_error"] or not pdf_path.is_file():
            logger.warning("LaTeX bbox 批编译产物/导言区异常，交回单段渲染")
            self._record_batch_failure(
                items,
                attempt=attempt,
                request_id=request_id,
                batch_id=batch_id,
                base_by_key=base_by_key,
                reason="batch-attribution-failed",
                artifacts=block_artifacts,
                ranges=ranges,
            )
            return attribution_failed
        if set(range(len(items))) - info["starts"]:
            logger.warning("LaTeX bbox 批编译标记缺失，交回单段渲染")
            self._record_batch_failure(
                items,
                attempt=attempt,
                request_id=request_id,
                batch_id=batch_id,
                base_by_key=base_by_key,
                reason="batch-attribution-failed",
                artifacts=block_artifacts,
                ranges=ranges,
            )
            return attribution_failed
        # Keep the block PDF open while measuring candidate pages.  Previously
        # every candidate was extracted to a one-page PDF and reopened just
        # for measurement, which adds substantial filesystem/PDF parsing I/O
        # when a block contains several variants per paragraph.
        try:
            doc = pymupdf.open(pdf_path)
        except Exception:  # noqa: BLE001 - unreadable output means attribution failure
            logger.debug("打开批编译产物失败", exc_info=True)
            self._record_batch_failure(
                items,
                attempt=attempt,
                request_id=request_id,
                batch_id=batch_id,
                base_by_key=base_by_key,
                reason="batch-attribution-failed",
                artifacts=block_artifacts,
                ranges=ranges,
            )
            return attribution_failed
        # 诊断：每个候选项一条记录（页索引 = 块内序号；tex 行区间来自
        # build_batch_tex 的确定映射）。
        item_candidates: dict[int, str] = {}
        item_outcomes: dict[int, dict] = {}
        try:
            page_count = len(doc)
            if page_count != len(items):
                # 页码 = 候选项序号是归属前提；页数不符时整块交回单段渲染。
                logger.warning(
                    "LaTeX bbox 批编译页数 %d != 候选页数 %d，交回单段渲染",
                    page_count,
                    len(items),
                )
                self._record_batch_failure(
                    items,
                    attempt=attempt,
                    request_id=request_id,
                    batch_id=batch_id,
                    base_by_key=base_by_key,
                    reason="batch-attribution-failed",
                    artifacts=block_artifacts,
                    ranges=ranges,
                )
                return attribution_failed

            per_key: dict[str, dict] = {
                key: {"candidate": None, "error": None, "reason": None}
                for key in keys
            }
            for index, (key, request, priority) in enumerate(items):
                bucket = info["segments"].get(index) or {}
                record = per_key[key]
                status = "pending"
                reason: str | None = None
                fits: bool | None = None
                fit_reason: str | None = None
                if index not in info["ends"]:
                    status, reason = "skipped", "marker-missing"
                else:
                    errors = bucket.get("errors") or []
                    if errors and record["error"] is None:
                        # TeX 错误：与单段渲染一致，不缩小重试，只该段回退。
                        record["error"] = errors[0]
                        status, reason = "failed", f"compile:{errors[0][:160]}"
                    else:
                        fits, fit_reason, _chars = _measure_page_fit(
                            doc[index],
                            request.width,
                            request.height,
                            request.expected_text,
                        )
                        if bucket.get("overfull_hbox"):
                            fits, fit_reason = False, "overfull-hbox"
                        # 页级 ``Overfull \vbox`` 在批编译里只剩「vbox 高度 +
                        # 末行 depth」的系统噪声（内容超高已被 ``\vbox to``
                        # 封在一页内），因此不作为失败信号；真正的垂直溢出由
                        # ``_measure_page_fit`` 的墨迹/文本判定发现。
                        if fits and record["candidate"] is None:
                            # 同一轮的页按档位优先级排列：先到先得。
                            record["candidate"] = (request, index)
                            status, reason = "ok", "ok"
                        elif not fits:
                            record["reason"] = fit_reason
                            status, reason = "failed", fit_reason
                        else:
                            # 通过但已有更高优先级候选被采用。
                            status, reason = "ok", "superseded"
                item_outcomes[index] = {
                    "status": status,
                    "reason": reason,
                    "fit": (
                        {"fits": bool(fits), "reason": fit_reason}
                        if fits is not None
                        else {}
                    ),
                }
                candidate_id = self._record_batch_candidate(
                    key,
                    request,
                    priority,
                    attempt=attempt,
                    request_id=request_id,
                    batch_id=batch_id,
                    base_by_key=base_by_key,
                    pdf_page_index=index,
                    tex_line_range=(
                        list(ranges[index]) if index < len(ranges) else None
                    ),
                    status=status,
                    reason=reason,
                    fit=item_outcomes[index]["fit"],
                    artifacts=block_artifacts,
                )
                if candidate_id:
                    item_candidates[index] = candidate_id
        finally:
            doc.close()

        key_last_candidate: dict[str, str] = {}
        for index, (key, _request, _prio) in enumerate(items):
            if index in item_candidates:
                key_last_candidate[key] = item_candidates[index]

        results: dict[str, StampResult] = {}
        for key in keys:
            record = per_key[key]
            if record["error"]:
                result = StampResult(
                    key=key,
                    ok=False,
                    compile_attempts=attempt,
                    reason=f"compile:{record['error']}",
                    log_excerpt=[record["error"]],
                )
                result.debug_ref = {
                    "candidate_id": key_last_candidate.get(key),
                    "request_id": request_id,
                    "batch_id": batch_id,
                }
                results[key] = result
                continue
            candidate = record["candidate"]
            if candidate is None:
                if record["reason"] is None:
                    # 该段的页没有任何可信判定（标记/抽页缺失）。
                    result = attribution_failed[key]
                else:
                    result = StampResult(
                        key=key,
                        ok=False,
                        compile_attempts=attempt,
                        reason=f"s{attempt - 1}:{record['reason']}",
                    )
                result.debug_ref = {
                    "candidate_id": key_last_candidate.get(key),
                    "request_id": request_id,
                    "batch_id": batch_id,
                }
                results[key] = result
                continue
            request, page_index = candidate
            stamp_path = block_dir / f"{stem}-p{page_index}.pdf"
            if not _extract_page(pdf_path, page_index, stamp_path):
                result = attribution_failed[key]
                result.debug_ref = {
                    "candidate_id": key_last_candidate.get(key),
                    "request_id": request_id,
                    "batch_id": batch_id,
                }
                results[key] = result
                continue
            result = StampResult(
                key=key, ok=True, compile_attempts=attempt, reason="ok"
            )
            base = base_by_key.get(key, request)
            result.pdf_path = str(stamp_path)
            result.font_size = request.font_size
            result.scale = (
                request.font_size / base.font_size if base.font_size else 1.0
            )
            result.lead = (
                float(request.lead)
                if request.lead
                else request.font_size * DEFAULT_LEAD_RATIO
            )
            selected_id = item_candidates.get(page_index)
            result.debug_ref = {
                "candidate_id": selected_id,
                "request_id": request_id,
                "batch_id": batch_id,
                "pdf_page_index": page_index,
            }
            self._record_selected(
                key,
                selected_id,
                items[page_index][2],
                request_id=request_id,
                batch_id=batch_id,
                pdf_page_index=page_index,
            )
            results[key] = result
        self._last_candidates.update(key_last_candidate)
        return results

    # ------------------------------------------------------------------
    def build_batch_tex(
        self, requests: list[StampRequest]
    ) -> tuple[str, list[tuple[int, int]]]:
        """拼一份「每段一页」的 TeX；返回 (tex, 每段占用的 tex 行号区间)。

        - 逐页显式设置纸张与版心（``\\newgeometry`` 不能改纸张尺寸）；
        - 每段正文直放页面、``\vsize`` 取大常量（一段 = 一页，且与单段渲染同构）；
        - 段前后写 ``\\message{@@S n@@}``/``@@E n@@`` 标记；
        - 正文按**行**计数（body 可能含换行），保证 ``at lines X--Y`` 的行号
          映射稳定，供 :func:`_attribute_log` 精确归属。
        """
        if not requests:
            return "", []
        first = requests[0]
        header = [
            "\\documentclass{article}",
            (
                f"\\usepackage[paperwidth={first.width:.4f}bp,"
                f"paperheight={first.height:.4f}bp,margin=0pt]{{geometry}}"
            ),
        ]
        header.extend(
            (TEX_COMMON % {"fontsetup": font_setup_clauses(self._capability, first.serif)}).split(
                "\n"
            )
        )
        header.append("\\begin{document}")
        lines: list[str] = list(header)
        line_no = _line_count("\n".join(header))
        ranges: list[tuple[int, int]] = []
        for index, request in enumerate(requests):
            if index:
                lines.append("\\newpage")
                line_no += 1
            start = line_no + 1
            lead = (
                float(request.lead)
                if request.lead
                else request.font_size * DEFAULT_LEAD_RATIO
            )
            parindent, hangindent = request.indentation
            block = [
                f"\\message{{@@S {index}@@}}",
                (
                    f"\\pdfpagewidth={request.width:.4f}bp "
                    f"\\pdfpageheight={request.height:.4f}bp"
                ),
                (
                    f"\\hsize={request.width:.4f}bp "
                    f"\\vsize={_SEGMENT_VSIZE_BP:.4f}bp "
                    f"\\textwidth={request.width:.4f}bp "
                    f"\\textheight={request.height:.4f}bp "
                    f"\\columnwidth={request.width:.4f}bp "
                    f"\\linewidth={request.width:.4f}bp"
                ),
                (
                    f"\\setlength{{\\parindent}}{{{parindent:.4f}bp}}"
                    "\\setlength{\\parskip}{0pt}"
                ),
            ]
            topskip = topskip_clause(
                self._capability, request.serif, request.font_size, request.ascent_top
            )
            if topskip:
                block.append(topskip.rstrip("\n"))
            block.append(
                f"\\fontsize{{{request.font_size:.4f}bp}}{{{lead:.4f}bp}}\\selectfont"
            )
            # 正文直放页面（与单段渲染器完全同构：网页尺寸 + parindent +
            # topskip + 字号/行距 + 悬挂缩进 + 正文）；``\vsize`` 取自常量，保证
            # 单段不可能被分页截成多页。
            if hangindent:
                block.append(
                    f"\\setlength{{\\hangindent}}{{{hangindent:.4f}bp}}\\hangafter=1"
                )
            block.extend([request.body, f"\\message{{@@E {index}@@}}"])
            for line in block:
                lines.append(line)
                line_no += _line_count(line)
            ranges.append((start, line_no))
        lines.append("\\end{document}")
        return "\n".join(lines) + "\n", ranges

    # ------------------------------------------------------------------
    def _run_xelatex(
        self,
        tex: str,
        workdir: Path,
        stem: str,
        timeout: float,
        *,
        debug_context: dict | None = None,
    ) -> tuple[str, bool]:
        """跑一次 xelatex；返回 (日志, 是否超时)。

        ``-interaction=nonstopmode`` 且**不加** ``-halt-on-error``：单段错误
        不停机（实测错误段之后的段落照常排版），坏段由逐段归属隔离。
        """
        tex_path = workdir / f"{stem}.tex"
        tex_path.write_text(tex, encoding="utf-8")
        argv = [
            self._capability.xelatex_path,
            "-interaction=nonstopmode",
            f"-output-directory={workdir}",
            str(tex_path),
        ]
        recorder = self._debug_recorder
        capture = (
            recorder.process(
                "build", "xelatex", argv, timeout=timeout, **(debug_context or {})
            )
            if recorder
            else contextlib.nullcontext()
        )
        try:
            with capture as evidence:
                proc = subprocess.run(  # noqa: S603 - 可执行文件已由能力探测校验
                    argv,
                    cwd=workdir,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                if evidence is not None:
                    evidence["result"] = proc
        except subprocess.TimeoutExpired:
            logger.warning("LaTeX bbox 批编译超时（%.1fs）", timeout)
            return "", True
        except OSError as exc:
            logger.warning("LaTeX bbox 批编译进程失败: %s", exc)
            return "", False
        log = proc.stdout or ""
        if not _MARKER_START.search(log):
            # 终端输出被裁剪/被调用方吞掉时退回 .log 文件（两者内容一致）。
            try:
                log_path = workdir / f"{stem}.log"
                if log_path.is_file():
                    log = log_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                logger.debug("读取批编译日志失败", exc_info=True)
        return log, False

    # ------------------------------------------------------------------
    # 诊断采集（recorder 为 None 时全部为 no-op；不改变编译行为）
    # ------------------------------------------------------------------
    def _record_requests(
        self, requests: list[tuple[StampRequest, Path]]
    ) -> str | None:
        """``compile_requests`` 事件：进入渲染器的原始请求清单（含别名）。"""
        recorder = self._debug_recorder
        if not recorder:
            return None
        request_id = recorder.new_id("request")
        recorder.record_event(
            "build",
            "compile_requests",
            {
                "request_id": request_id,
                "renderer": "batch",
                "requests": [
                    {
                        "key": request.key,
                        "width": round(float(request.width), 3),
                        "height": round(float(request.height), 3),
                        "font_size": round(float(request.font_size), 3),
                        "lead": (
                            round(float(request.lead), 3) if request.lead else None
                        ),
                        "expected_chars": len(request.expected_text or ""),
                    }
                    for request, _workdir in requests
                ],
            },
        )
        return request_id

    def _record_reuse(
        self,
        request: StampRequest,
        result: StampResult,
        kind: str,
        *,
        request_id: str | None = None,
    ) -> None:
        """``compile_reuse`` 事件：持久缓存命中，本次真实编译调用数为 0。"""
        recorder = self._debug_recorder
        if not recorder:
            return
        debug_ref = getattr(result, "debug_ref", None) or {}
        recorder.record_event(
            "build",
            "compile_reuse",
            {
                "kind": kind,
                "key": request.key,
                "request_id": request_id,
                "candidate_id": debug_ref.get("candidate_id"),
                "actual_compile_calls": 0,
                "pdf_path": result.pdf_path,
                "font_size": result.font_size,
                "scale": result.scale,
                "lead": result.lead,
            },
        )

    def _record_alias_reuse(
        self,
        aliases: dict[str, list[str]],
        results: dict[str, StampResult],
        *,
        request_id: str | None = None,
    ) -> None:
        """``compile_reuse(deduplicated)``：别名段复用正段的真实编译候选。"""
        recorder = self._debug_recorder
        if not recorder or not aliases:
            return
        for canonical, keys in aliases.items():
            result = results.get(canonical)
            candidate_id = (
                (getattr(result, "debug_ref", None) or {}).get("candidate_id")
                if result is not None
                else None
            )
            for key in keys:
                recorder.record_event(
                    "build",
                    "compile_reuse",
                    {
                        "kind": "deduplicated",
                        "key": key,
                        "canonical": canonical,
                        "request_id": request_id,
                        "candidate_id": candidate_id,
                        "actual_compile_calls": 0,
                    },
                )

    def _record_fallback(
        self, key: str, reason: str, *, request_id: str | None = None
    ) -> None:
        """``compile_fallback`` 事件：该段交回单段渲染器（每段一次）。"""
        recorder = self._debug_recorder
        if not recorder:
            return
        recorder.record_event(
            "build",
            "compile_fallback",
            {
                "key": key,
                "reason": reason,
                "request_id": request_id,
                "parent_id": self._last_candidates.get(key),
                "from": "batch",
                "to": "single",
            },
        )

    def _record_selected(
        self,
        key: str,
        candidate_id: str | None,
        priority: int,
        *,
        request_id: str | None = None,
        batch_id: str | None = None,
        pdf_page_index: int | None = None,
    ) -> None:
        """``candidate_selected`` 事件：段内最终采用的候选。"""
        recorder = self._debug_recorder
        if not recorder or not candidate_id:
            return
        recorder.record_event(
            "build",
            "candidate_selected",
            {
                "id": candidate_id,
                "key": key,
                "priority": priority,
                "request_id": request_id,
                "batch_id": batch_id,
                "pdf_page_index": pdf_page_index,
            },
        )

    def _archive_block_artifacts(
        self, batch_id: str | None, tex: str, block_dir: Path, stem: str
    ) -> dict:
        """归档块级证据：一份 tex/log/pdf 供块内全部候选共享引用。"""
        recorder = self._debug_recorder
        if not recorder or not batch_id:
            return {}
        artifacts = {
            "tex": recorder.archive_text(
                "build", f"compile/batch/{batch_id}.tex", tex
            )
        }
        for suffix in ("pdf", "log"):
            path = block_dir / f"{stem}.{suffix}"
            artifacts[suffix] = (
                recorder.archive_file(
                    "build", f"compile/batch/{batch_id}.{suffix}", path
                )
                if path.is_file()
                else None
            )
        return artifacts

    def _record_batch_candidate(
        self,
        key: str,
        request: StampRequest,
        priority: int,
        *,
        attempt: int,
        request_id: str | None,
        batch_id: str | None,
        base_by_key: dict | None,
        pdf_page_index: int | None,
        tex_line_range,
        status: str,
        reason: str | None,
        fit: dict | None = None,
        artifacts: dict | None = None,
    ) -> str | None:
        """记录一个批编译候选；返回其候选 id（供选中/父链引用）。"""
        recorder = self._debug_recorder
        if not recorder:
            return None
        candidate_id = recorder.new_id("candidate")
        base = (base_by_key or {}).get(key, request)
        effective_lead = (
            float(request.lead)
            if request.lead
            else request.font_size * DEFAULT_LEAD_RATIO
        )
        record = CompileCandidate(
            id=candidate_id,
            paragraph_id=str(key),
            request_id=request_id or "",
            renderer="batch",
            round=attempt,
            priority=priority,
            width=round(float(request.width), 3),
            height=round(float(request.height), 3),
            font_size_initial=round(float(base.font_size), 3),
            font_size=round(float(request.font_size), 3),
            lead=round(float(effective_lead), 3),
            parent_id=(
                self._last_candidates.get(key)
                or getattr(request, "debug_parent", None)
            ),
            batch_id=batch_id,
            pdf_page_index=pdf_page_index,
            tex_line_range=tex_line_range,
            font={"serif": bool(request.serif)},
            status=status,
            fit=fit or {},
            reason=reason,
            artifacts=dict(artifacts or {}),
        )
        payload = record.to_dict()
        snapshot = recorder.write_snapshot(
            "build", f"candidates/{candidate_id}", payload
        )
        recorder.record_event(
            "build", "candidate_evaluated", {**payload, "snapshot": snapshot}
        )
        return candidate_id

    def _record_batch_failure(
        self,
        items: list[tuple[str, StampRequest, int]],
        *,
        attempt: int,
        request_id: str | None = None,
        batch_id: str | None = None,
        base_by_key: dict | None = None,
        reason: str,
        artifacts: dict | None = None,
        ranges=None,
    ) -> None:
        """归属失败/超时/异常路径：逐项记录候选（页索引留空，不伪造映射）。"""
        recorder = self._debug_recorder
        if not recorder:
            return
        for index, (key, request, priority) in enumerate(items):
            line_range = (
                list(ranges[index])
                if ranges is not None and index < len(ranges)
                else None
            )
            candidate_id = self._record_batch_candidate(
                key,
                request,
                priority,
                attempt=attempt,
                request_id=request_id,
                batch_id=batch_id,
                base_by_key=base_by_key,
                pdf_page_index=None,
                tex_line_range=line_range,
                status="failed",
                reason=reason,
                artifacts=artifacts,
            )
            if candidate_id:
                self._last_candidates[key] = candidate_id


# ----------------------------------------------------------------------
def _is_terminal_reason(reason: str) -> bool:
    """该失败原因是否无需再试（内容级失败 / TeX 错误 / 超时）。

    批编译的尺寸类失败带 ``s{n}:`` 前缀，因此内容级失败按子串匹配；TeX 错误
    已经是 ``compile:`` 前缀。
    """
    if reason.startswith(_TERMINAL_PREFIXES):
        return True
    return any(token in reason for token in _TERMINAL_REASONS)


def _round_variants(request: StampRequest, attempt: int) -> list[StampRequest]:
    """第 ``attempt`` 轮（1-based）的候选档位（按优先级）。

    第 1 轮只给首选档（源字号 + 源行距），最省；第 2 轮把剩余阶梯档位一次
    全部编出（行距 ±10% 与 ×0.95 有界缩小），由调用方按优先级取首个通过者。
    """
    if attempt <= 1:
        return [request]
    base_lead = (
        float(request.lead)
        if request.lead
        else request.font_size * DEFAULT_LEAD_RATIO
    )
    variants: list[StampRequest] = []
    for scale, lead_ratio in _VARIANT_LADDER[1:]:
        variants.append(
            replace(
                request,
                font_size=request.font_size * scale,
                lead=base_lead * lead_ratio,
            )
        )
    return variants


def _line_count(text: str) -> int:
    """一段 tex 片段占用的行数（含末尾换行）。"""
    return text.count("\n") + 1


def _copy_alias_results(
    results: dict[str, StampResult], aliases: dict[str, list[str]]
) -> dict[str, StampResult]:
    """Expose one compiled result under every deduplicated request key."""
    if not aliases:
        return results
    for canonical, keys in aliases.items():
        result = results.get(canonical)
        if result is None:
            continue
        for key in keys:
            # StampResult is mutable (the overlay records paths and metrics),
            # so clone it instead of sharing the canonical object's key field.
            results[key] = replace(result, key=key)
    return results


def _owner_of(number: int, ranges: list[tuple[int, int]]) -> int | None:
    """tex 行号 → 段序号（区间不重叠，命中唯一）。"""
    for index, (start, end) in enumerate(ranges):
        if start <= number <= end:
            return index
    return None


def _line_owner(line: str, ranges: list[tuple[int, int]]) -> int | None:
    """从 ``at lines X--Y`` / ``on line X`` 里解析行号并归属。"""
    match = _LINES_RANGE.search(line)
    if match is None:
        return None
    number = int(match.group(1) or match.group(3))
    return _owner_of(number, ranges)


def _error_line_owner(
    lines: list[str], index: int, ranges: list[tuple[int, int]]
) -> int | None:
    """``!`` 错误：从随后的 ``l.NNN`` 上下文行取行号归属。"""
    for offset in range(index + 1, min(index + _ERROR_CONTEXT_LINES, len(lines))):
        match = _ERROR_LINE.match(lines[offset])
        if match is not None:
            return _owner_of(int(match.group(1)), ranges)
    return None


def _marker_owner(
    markers: list[tuple[int, int]], index: int
) -> int | None:
    """标记兜底：日志中最近一个 ``@@S n@@`` 所在段。

    页级 ``Overfull \\vbox`` 在 shipout（``@@E n@@`` 之后、``@@S n+1@@`` 之前）
    才写出，同样归属第 n 段；行号信息不可用时才走这条路径。
    """
    owner: int | None = None
    for position, number in markers:
        if position > index:
            break
        owner = number
    return owner


def _attribute_log(log: str, ranges: list[tuple[int, int]]) -> dict:
    """把日志里的 ``!`` 错误与 Overfull 按段归属。

    归属优先级：**行号**（``at lines X--Y`` / ``l.X``；行号区间由
    :meth:`BatchStampRenderer.build_batch_tex` 生成，确定且不受 TeX 日志缓冲
    影响）→ 「最近 ``@@S n@@``」标记兜底。
    """
    lines = log.split("\n")
    markers: list[tuple[int, int]] = []
    starts: set[int] = set()
    ends: set[int] = set()
    for index, line in enumerate(lines):
        for match in _MARKER_START.finditer(line):
            markers.append((index, int(match.group(1))))
            starts.add(int(match.group(1)))
        for match in _MARKER_END.finditer(line):
            ends.add(int(match.group(1)))

    segments: dict[int, dict] = {}
    preamble_error = False
    for index, line in enumerate(lines):
        if line.startswith("!"):
            kind = "error"
        elif overfull_hbox_exceeds(line, _WIDTH_TOLERANCE):
            # 轻微超宽（≤ _WIDTH_TOLERANCE）不算失败（口径同单段渲染器）。
            kind = "overfull_hbox"
        elif _OVERFULL_VBOX.search(line):
            kind = "overfull_vbox"
        else:
            continue
        owner = _line_owner(line, ranges)
        if owner is None and kind == "error":
            owner = _error_line_owner(lines, index, ranges)
        if owner is None:
            owner = _marker_owner(markers, index)
        if owner is None:
            if kind == "error":
                preamble_error = True
            continue
        bucket = segments.setdefault(owner, {})
        if kind == "error":
            bucket.setdefault("errors", []).append(line.strip()[:200])
        else:
            bucket[kind] = bucket.get(kind, 0) + 1
    return {
        "segments": segments,
        "starts": starts,
        "ends": ends,
        "preamble_error": preamble_error,
    }


def _extract_page(source: Path, page_index: int, out_path: Path) -> bool:
    """把块 PDF 的第 ``page_index`` 页另存为单页贴片（贴片只吃单页）。"""
    try:
        doc = pymupdf.open(source)
    except Exception:  # noqa: BLE001 - 打不开按归属失败处理
        logger.debug("打开批编译产物失败", exc_info=True)
        return False
    try:
        if page_index < 0 or page_index >= len(doc):
            return False
        doc.select([page_index])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if doc.page_count != 1:
            logger.warning("抽取后页数异常: %s", doc.page_count)
            return False
        doc.save(out_path, garbage=3, deflate=True)
    except Exception:  # noqa: BLE001 - 抽取失败按归属失败处理
        logger.debug("抽取批编译页面失败", exc_info=True)
        return False
    finally:
        doc.close()
    return out_path.is_file()
