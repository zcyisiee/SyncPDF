"""Single-block stamp rendering with revision-fenced, immutable page publication.

No pipeline subprocess or workdir snapshot is used here.  A request renders exactly
one StampRequest; page redaction/link restoration reuse the existing overlay code.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import math
import os
import pickle
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from babeldoc_tools.common import ToolError
from babeldoc_tools.serve.asset_store import AssetStore
from babeldoc_tools.serve.draft import read_draft
from babeldoc_tools.serve.jobs import EVENT_STARTED
from babeldoc_tools.serve.jobs import utc_now
from babeldoc_tools.serve.views import paragraphs
from babeldoc_tools.serve.workdir import WorkdirReader

#: 解析快照缓存：workdir → (state.pkl 的 mtime_ns, 反序列化后的 state)。
#: 同一 job 的流式预览会按块反复读同一个 state.pkl（46MB 的 pickle，冷读约 0.6s），
#: 翻译期间它不再变化；mtime 变了（重新 parse）就失效重读。进程内共享，跨 job 复用。
_PARSE_STATE_CACHE: dict[str, tuple[int, dict]] = {}
_PARSE_STATE_LOCK = threading.Lock()

#: 批量块编译并行 worker 数上限（xelatex 是 CPU 密集进程；与流式预览同量级）。
MAX_BATCH_WORKERS = 8
_DEFAULT_BATCH_WORKERS = 4

#: 段落行缓存的存活时间（秒）。几何在一次 job 内不变，译文会变；见 ``_rows``。
_ROWS_CACHE_TTL_S = 5.0

#: 版面检测（ONNX Runtime）的 intra-op 线程数上限。0 = 让 ORT 自己按核数铺满，
#: 单独跑最快；但预览编译同时挂着十几个 xelatex 进程，铺满的线程池只会互相
#: 抢核（实测 16 进程负载下 0 → 0.78s/次，4 → 0.43s/次，空载 0.33s）。
_LAYOUT_DETECT_THREADS = 4

#: 回写译文用的 ``ILTranslator``：按语言对缓存（进程内）。
#: 构造一次要 ~0.5s，绝大部分花在 ``FontMapper``（扫字体文件）；而局部编译只用
#: ``post_translate_paragraph``，它整条调用链只碰三个预编译占位符正则
#: （``_formula_placeholder_pattern`` / ``_style_{left,right}_placeholder_pattern``），
#: 既不读 ``font_mapper`` 也不读 ``translation_config``。这些正则只由 translate
#: engine 的类决定，因此按 ``(lang_in, lang_out)`` 缓存是安全的——每块重建一个
#: 等于给每个段落白付 0.5s（实测 356 块 ≈ 178 核秒）。
_IL_TRANSLATOR_CACHE: dict[tuple[str, str], object] = {}
_IL_TRANSLATOR_LOCK = threading.Lock()


def _shared_il_translator(lang_in: str, lang_out: str, config):
    """按语言对复用 ``ILTranslator``（只用于 ``post_translate_paragraph``）。"""
    from babeldoc.tools.agent import workflow

    key = (lang_in, lang_out)
    # 构造期间**持锁**：16 个 worker 同时首编时若先放锁再构造，每个都会各建一份
    # （实测 16 次 × 2.2s），既白付核时，也让每个 worker 的首块各慢 2s。后到的
    # 线程在锁上等一份现成的，比各自再建一份便宜得多。
    with _IL_TRANSLATOR_LOCK:
        cached = _IL_TRANSLATOR_CACHE.get(key)
        if cached is None:
            cached = workflow.ILTranslator(
                workflow.SheetProtocolTranslator(lang_in, lang_out, True), config
            )
            _IL_TRANSLATOR_CACHE[key] = cached
        return cached


class PageLayoutCache:
    """每页一次的版面证据（PP-DocLayoutV3 区域 + 精确墨迹），按页缓存。

    浮动阶梯（同栏扩 → 跨栏扩 → 跨页迁移）原本每一级都各自 ``detect_page``
    一次同一张页面：一次检测 = 渲染位图 + ONNX 推理，实测约 0.37s，一个缩字块
    最多付 4 次。证据只取决于 baseline 页内容（整个 job 期间不变），所以按
    ``page_number`` 缓存一次即可，命中后浮动阶梯零检测开销。

    线程安全：每页一把构建锁，多 worker 并发首次命中同一页时只检测一次。
    """

    def __init__(self, detector):
        self.detector = detector
        self._entries: dict[int, tuple[list, list]] = {}
        self._guard = threading.Lock()
        self._page_locks: dict[int, threading.Lock] = {}

    def _lock_for(self, page_number: int) -> threading.Lock:
        with self._guard:
            return self._page_locks.setdefault(page_number, threading.Lock())

    def evidence(self, page, page_number: int) -> tuple[list, list]:
        """→ ``(regions, ink)``；``regions`` 空表示检测不可用（调用方不扩框）。"""
        with self._guard:
            cached = self._entries.get(page_number)
        if cached is not None:
            return cached
        with self._lock_for(page_number):
            with self._guard:
                cached = self._entries.get(page_number)
            if cached is not None:
                return cached
            from babeldoc.tools.agent import layout_refine

            regions = [
                region.box for region in layout_refine._regions_il(page, self.detector)
            ]
            ink = layout_refine.page_ink_rects(page) if regions else []
            entry = (regions, ink)
            with self._guard:
                self._entries[page_number] = entry
            return entry


class FloatReservations:
    """已落定贴片矩形的登记簿：让同页的浮动决策**互相可见**。

    浮动规划的障碍集原先只有 baseline 页的版面区域和源文墨迹，而 baseline 是
    原文页——兄弟贴片浮到哪里它一概不知。于是每个缩字块都以为落点页的顶部空白
    带是空的，实测 84 个跨页迁移**全部**顶对齐到同一条带子，产生 119 对肉眼可见
    的贴片重叠。这里把每个已定框按落点页登记，后续规划把它们并入障碍集，语义
    与一次性编译路径的 ``plan_build_refinement``（``obstacles.append(expanded)``）
    一致。

    ``reserve`` 同时是**先到先得的占位**：并发 worker 落在不同页时互不影响，落在
    同一页时由页锁串行，登记顺序即决策顺序。
    """

    def __init__(self):
        self._by_page: dict[int, dict[str, list[float]]] = {}
        self._lock = threading.Lock()

    def load(self, page_number: int, patches: dict) -> None:
        """把某页已持久化的 patch 框灌进登记簿（重启/续跑后的既有状态）。"""
        with self._lock:
            slot = self._by_page.setdefault(page_number, {})
            for pid, patch in (patches or {}).items():
                box = patch.get("box")
                if box and int(patch.get("page", page_number)) == page_number:
                    slot[pid] = [float(value) for value in box]

    def obstacles(self, page_number: int, *, exclude: str) -> list[list[float]]:
        """落在该页、且不属于 ``exclude`` 段的已定贴片框。"""
        with self._lock:
            return [
                list(box)
                for pid, box in self._by_page.get(page_number, {}).items()
                if pid != exclude
            ]

    def reserved_ids(self) -> set[str]:
        """全部已登记的段落 id（**不分页**）。

        已登记的段落一律用它登记的贴片框当障碍，不再用原文框：它可能已经迁到
        别的页，home 页那块原文脚印在合成时会被擦掉，继续当障碍会白挡净空。
        """
        with self._lock:
            return {pid for slot in self._by_page.values() for pid in slot}

    def reserve(self, pid: str, page_number: int, box) -> None:
        """登记 ``pid`` 的最终落点（同段重复登记即覆盖，含迁回主页）。"""
        values = [float(value) for value in box]
        with self._lock:
            for slot in self._by_page.values():
                slot.pop(pid, None)
            self._by_page.setdefault(page_number, {})[pid] = values


def batch_workers_from_environ(environ=None) -> int:
    """解析 ``BDT_SERVE_BATCH_WORKERS``：缺省 4，非法值夹到 [1, 8]。"""
    raw = (environ or os.environ).get("BDT_SERVE_BATCH_WORKERS")
    if raw is None or not str(raw).strip():
        return _DEFAULT_BATCH_WORKERS
    try:
        value = int(str(raw).strip())
    except ValueError:
        return _DEFAULT_BATCH_WORKERS
    return max(1, min(MAX_BATCH_WORKERS, value))


def _copy_page_shells(state: dict) -> dict:
    """拷贝 ``state`` 的**可写外壳**：顶层 dict + 每页对象 + 每页 ``pdf_paragraph`` 列表。

    ``render_request`` 会就地重写 ``page.pdf_paragraph``（只留当前 pid 那一段），而段落
    对象本身只读。所以共享缓存必须给出可以各自改写的页面外壳，否则第一个块编译后就把
    缓存里的 551 段裁成 1 段，后续块全部查不到 geometry（表现为「缺少译文或排版数据」），
    并行 worker 之间也会互相踩。段落与其余字段按引用共享：无拷贝成本、也不被改写。
    """
    pages = []
    for page in state["doc"].page:
        shell = copy.copy(page)
        shell.pdf_paragraph = list(page.pdf_paragraph)
        pages.append(shell)
    doc = copy.copy(state["doc"])
    doc.page = pages
    clone = dict(state)
    clone["doc"] = doc
    return clone


def _load_parse_state(workdir: Path) -> dict:
    """读 ``agent/state.pkl``，带 mtime 失效的进程内缓存。

    返回的 state 是**可安全改写页面外壳**的副本（见 :func:`_copy_page_shells`）：调用方
    可以重写 ``page.pdf_paragraph``，不会影响缓存与其它调用方。
    """
    state_path = workdir / "agent/state.pkl"
    try:
        stamp = state_path.stat().st_mtime_ns
    except OSError:
        # 让原有错误路径继续负责报错（下面的 open 会抛同样的异常）。
        with state_path.open("rb") as handle:
            return pickle.load(handle)  # noqa: S301 - private parser artifact
    key = str(workdir)
    with _PARSE_STATE_LOCK:
        cached = _PARSE_STATE_CACHE.get(key)
        if cached is not None and cached[0] == stamp:
            return _copy_page_shells(cached[1])
    with state_path.open("rb") as handle:
        state = pickle.load(handle)  # noqa: S301 - trusted private parser artifact
    with _PARSE_STATE_LOCK:
        _PARSE_STATE_CACHE[key] = (stamp, state)
    return _copy_page_shells(state)


def document_blocks(store, did):
    from babeldoc_tools.serve.schemas import ParagraphItem

    with store.database._lock:
        indexed = store.database.connection.execute(
            "SELECT id,page,source,layout FROM blocks WHERE document_id=? ORDER BY page,id",
            (did,),
        ).fetchall()
        targets = dict(
            store.database.connection.execute(
                "SELECT block_id,target FROM translation_blocks WHERE document_id=?",
                (did,),
            )
        )
    if indexed:
        rows = []
        for item in indexed:
            geometry = json.loads(item["layout"]) if item["layout"] else None
            rows.append(
                ParagraphItem(
                    id=item["id"],
                    page=item["page"],
                    source=item["source"],
                    geometry=geometry,
                    layout_label=(geometry or {}).get("layout_label"),
                    layout_status="not_available",
                )
            )
    else:
        rows = paragraphs(WorkdirReader(store.resolve(did)))
    if any(row.geometry is None for row in rows):
        state_path = store.resolve(did) / "agent/state.pkl"
        if state_path.is_file():
            parsed = _load_parse_state(store.resolve(did))
            geometry = {}
            for page in getattr(parsed.get("doc"), "page", []):
                for paragraph in page.pdf_paragraph:
                    box = paragraph.box
                    if box is not None:
                        geometry[paragraph.debug_id] = {
                            "src_box": [box.x, box.y, box.x2, box.y2],
                            "page": page.page_number + 1,
                            "src_font_size": getattr(
                                paragraph.pdf_style, "font_size", None
                            ),
                        }
            for row in rows:
                if row.geometry is None and row.id in geometry:
                    row.geometry = geometry[row.id]
                    row.page = row.geometry["page"]
    for row in rows:
        if row.id in targets:
            row.target = targets[row.id]
    return rows


def paragraph_styles(workdir):
    """pid → 编译样式摘要（解析状态派生）：字号 / 衬线 / 加粗 / 斜体 / 字体名。

    与 :func:`render_request` 同一派生源（段落级 ``pdf_style`` + 页字体表），
    供 ``GET /paragraphs`` 展示每个 bbox 的样式信息。state.pkl 缺失或不可读
    （迁移后未物化的文档）→ None，调用方保持旧行为。共享 ``_load_parse_state``
    的进程内缓存，重复调用无额外反序列化成本。
    """
    from babeldoc.format.pdf.document_il.backend.latex_bbox import overlay
    from babeldoc.format.pdf.document_il.backend.latex_bbox.fusion import _style_flags

    try:
        state = _load_parse_state(workdir)
    except Exception:  # noqa: BLE001 - 样式摘要缺失不该让段落列表 500
        return None
    styles: dict[str, dict] = {}
    for page in state["doc"].page:
        font_map = {font.font_id: font for font in page.pdf_font}
        for paragraph in page.pdf_paragraph:
            if not paragraph.debug_id:
                continue
            style = paragraph.pdf_style
            font = font_map.get(getattr(style, "font_id", None)) if style else None
            bold, italic = _style_flags(style, font_map)
            styles[paragraph.debug_id] = {
                "font_size": getattr(style, "font_size", None) if style else None,
                "bold": bold,
                "italic": italic,
                "serif": overlay._paragraph_serif(paragraph, font_map, None),
                "font_name": getattr(font, "name", None),
            }
    return styles


class NotReplaced(Exception):  # noqa: N818 - 不是错误，是「按设计不替换」的落定信号
    """该块按设计不做 LaTeX 替换（标签不合格 / 源文单行），保留基线原文。

    与 ``ToolError("compile_failed", ...)`` 的区别是**语义**：这不是失败，不该让用户
    看到「预览失败」。一次性编译路径把同样的判定记作 ``label-not-eligible`` /
    ``single-line`` 的 fallback（见 :meth:`overlay.LatexBboxOverlay._select_candidates`），
    流式预览此前没有这两道门禁，把单行标题当正文编译，缩字触发浮动扩框，标题整块上移。
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def valid_box(box, width, height):
    if (
        not isinstance(box, (list, tuple))
        or len(box) != 4
        or any(
            isinstance(v, bool)
            or not isinstance(v, (int, float))
            or not math.isfinite(v)
            for v in box
        )
    ):
        raise ToolError("bbox_invalid", "bbox 必须是有限数值四元组")
    x0, y0, x1, y1 = box
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise ToolError("bbox_invalid", "bbox 越界或面积无效")
    return list(box)


#: ``hydrate_parse`` 的快照查找缓存：workdir → (查找时刻, base, (snapshot, prepared) | None)。
_HYDRATE_CACHE: dict[str, tuple[float, Path | None, tuple[str, str] | None]] = {}
_HYDRATE_CACHE_LOCK = threading.Lock()
_HYDRATE_CACHE_TTL_S = 5.0


def _hydrate_lookup(workdir) -> tuple[Path | None, tuple[str, str] | None]:
    """查 ``parse_results`` 有没有可还原的解析快照，带短 TTL 的进程内缓存。

    每次查找都要新开一个 ``MetadataDB``（建表脚本 + commit，WAL 争用下要 fsync），
    而流式预览每块编译调 1–2 次：实测 512 次 × 0.21s ≈ 109 核秒，绝大多数文档
    根本没有快照、答案恒为「用 workdir」。快照状态只在重新解析时变（runner 置
    ``pending``），几秒的 TTL 足够及时。
    """
    from babeldoc_tools.serve.database import MetadataDB

    key = str(workdir)
    now = time.monotonic()
    with _HYDRATE_CACHE_LOCK:
        cached = _HYDRATE_CACHE.get(key)
        if cached is not None and now - cached[0] < _HYDRATE_CACHE_TTL_S:
            return cached[1], cached[2]
    base = next(
        (p for p in (workdir, workdir.parent) if (p / "app.db").is_file()), None
    )
    found = None
    if base is not None:
        database = MetadataDB(base)
        try:
            row = database.connection.execute(
                "SELECT snapshot_asset,prepared_pdf_asset FROM parse_results WHERE document_id=? AND status='ready'",
                (workdir.name,),
            ).fetchone()
        finally:
            database.close()
        if row and row[0] and row[1]:
            found = (str(row[0]), str(row[1]))
    with _HYDRATE_CACHE_LOCK:
        _HYDRATE_CACHE[key] = (time.monotonic(), base, found)
    return base, found


def hydrate_parse(workdir, temporary):
    """Resolve migrated immutable parser inputs without any legacy absolute path."""
    import shutil
    import zipfile

    from babeldoc_tools.serve.database import MetadataDB
    from babeldoc_tools.serve.migrate import SNAPSHOT_FILES

    base, found = _hydrate_lookup(workdir)
    if base is None or found is None:
        return workdir
    database = MetadataDB(base)
    try:
        row = found
        assets = AssetStore(base, database)
        restored = temporary / "parse"
        (restored / "agent").mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(assets.resolve(row[0])) as archive:
            for name in SNAPSHOT_FILES:
                member = f"agent/{name}"
                if member in archive.namelist():
                    (restored / member).parent.mkdir(parents=True, exist_ok=True)
                    (restored / member).write_bytes(archive.read(member))
        shutil.copyfile(assets.resolve(row[1]), restored / "prepared.pdf")
        return restored
    finally:
        database.close()


def _apply_font_family(
    meta: dict, layout: dict | None, serif_override: bool | None
) -> None:
    """按草稿 ``layout.font_family`` 覆盖该段 meta（字体族 + 拉丁衬线属性）。

    ``font_family`` 命中注册表（``font_families.FONT_FAMILY_IDS``）才写
    ``meta["font_family"]``（:meth:`overlay.LatexBboxOverlay._stamp_request` 读它，
    渲染器据此换中文主字体；该族在本机没探测到则渲染侧回落默认族）；该族的
    ``serif`` 与段落派生值不同时拉丁字形也跟着换 Noto Serif/Sans——除非用户显式
    给了 ``serif``（``serif_override`` 非 None，以用户为准）。认不出的 id 不改 meta
    （草稿校验已拦非法值）。
    """
    from babeldoc.format.pdf.document_il.backend.latex_bbox.font_families import (
        font_family_spec,
    )

    family_id = (layout or {}).get("font_family")
    family_spec = font_family_spec(family_id) if isinstance(family_id, str) else None
    if family_spec is None:
        return
    meta["font_family"] = family_spec.id
    if serif_override is None and bool(meta.get("serif")) != family_spec.serif:
        meta["serif"] = family_spec.serif


def render_request(
    workdir, pid, target, box, temporary, cache_root, capability=None, layout=None
):
    """Restore parsed typography, apply only this translation in memory, then fuse.

    ``capability``：可选的已探测 LaTeX 能力（流式预览每段一编时由调用方缓存一次
    传入）；``None`` = 现场探测（单段编辑编译的冷路径，语义与旧版一致）。
    ``layout``：该段草稿的排版覆盖（``layout_overrides`` 键）。局部编译消费其中
    的 ``font_scale``（字号乘数）、``line_skip``（行距系数）与 ``bold``/``italic``/
    ``serif`` 三态样式覆盖（布尔；缺省 = 跟随源文派生值）；``font_family`` 是
    中文字体族 id（``font_families.FONT_FAMILY_IDS``），命中注册表时写进该段的
    ``meta``：中文主字体用该族，拉丁字体的衬线属性跟随该族（除非用户同时也显式
    给了 ``serif``，那以用户为准）。``box`` 由调用方解析成 ``box`` 参数
    （含 ``box_scale``），这里不再重复应用。
    """

    import pymupdf
    from babeldoc.format.pdf.document_il.backend.latex_bbox import overlay
    from babeldoc.format.pdf.document_il.backend.latex_bbox.capability import (
        probe_latex_capability,
    )
    from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import (
        BboxStampRenderer,
    )
    from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import (
        cache_namespace,
    )
    from babeldoc.format.pdf.document_il.backend.latex_bbox.stamp_cache import (
        StampCache,
    )
    from babeldoc.tools.agent import protocol
    from babeldoc.tools.agent import workflow
    from babeldoc.tools.agent.prepared_pdf import resolve_source_pdf

    def flag(key):
        value = (layout or {}).get(key)
        return value if isinstance(value, bool) else None

    workdir = hydrate_parse(workdir, temporary)
    state = _load_parse_state(workdir)
    paragraph = next(
        (
            p
            for page in state["doc"].page
            for p in page.pdf_paragraph
            if p.debug_id == pid
        ),
        None,
    )
    source_input = state["inputs"].get(pid)
    source_fonts = next(
        (
            {font.font_id: font for font in page.pdf_font}
            for page in state["doc"].page
            if paragraph in page.pdf_paragraph
        ),
        {},
    )
    if paragraph is None or source_input is None:
        raise ToolError("block_not_found", "解析状态中没有该 block")
    violations = protocol.check_placeholders(pid, source_input.unicode, target)
    if violations:
        raise ToolError("compile_failed", "译文锚点校验失败", violations=violations)
    source = resolve_source_pdf(state, workdir)
    if source is None:
        raise ToolError("compile_failed", "prepared PDF 不可用，请重新解析")
    config = workflow._base_config(
        str(source), temporary, state["lang_in"], state["lang_out"]
    )
    translator = _shared_il_translator(state["lang_in"], state["lang_out"], config)
    tracker = workflow.PageTranslateTracker()
    translator.post_translate_paragraph(
        paragraph, tracker.new_paragraph(), source_input, target
    )
    config.provider_ir_dir = workdir / "agent"
    config.latex_source_pdf_path = str(source)
    config.latex_source_geometry = state.get("source_line_geometry") or {}
    # 行内公式片段裁到与 stamp 缓存同级的稳定目录（内容寻址）：片段的绝对路径
    # 会进 LaTeX body，而 body 是 stamp 缓存键的第一项；用临时目录会让含行内公式
    # 的段落永远不命中缓存（每次运行都是新路径）。见 overlay._fragment_path。
    config.latex_fragment_dir = str(cache_root / "fragments")
    # Capture only the affected paragraph; no other block is fused or compiled.
    for page in state["doc"].page:
        page.pdf_paragraph = [p for p in page.pdf_paragraph if p.debug_id == pid]
    captured = overlay.capture_layout_sources(
        state["doc"],
        config,
        source_texts={pid: source_input.unicode},
        link_state={
            "link_snapshot": state.get("link_snapshot") or {},
            "page_char_objects": state.get("page_char_objects") or {},
        },
    )
    meta = captured["paragraphs"].get(pid)
    body = captured["bodies"].get(pid)
    if not meta or not body:
        raise ToolError("compile_failed", "该 block 无法安全生成 LaTeX 贴片")
    # Plain translated runs have no <style> wrapper. Their paragraph-level
    # font still carries bold/italic (notably titles and Abstract headings).
    from babeldoc.format.pdf.document_il.backend.latex_bbox.fusion import _style_flags

    bold, italic = _style_flags(paragraph.pdf_style, source_fonts)
    if flag("bold") is not None:
        bold = flag("bold")
    if flag("italic") is not None:
        italic = flag("italic")
    if bold:
        body = r"{\bfseries " + body + "}"
    if italic:
        body = r"{\itshape " + body + "}"
    serif_override = flag("serif")
    if serif_override is not None:
        meta["serif"] = serif_override
    _apply_font_family(meta, layout, serif_override)
    font_size = meta["font_size"]
    font_scale = (layout or {}).get("font_scale")
    if isinstance(font_scale, (int, float)) and not isinstance(font_scale, bool):
        font_size = font_size * float(font_scale)
    line_skip = (layout or {}).get("line_skip")
    if capability is None:
        capability = probe_latex_capability()
    if not capability.available:
        raise ToolError(
            "compile_failed", "LaTeX 编译环境不可用", reasons=capability.reasons
        )
    namespace = cache_namespace(capability)
    cache = StampCache(
        cache_root / namespace.replace("/", "_").replace(":", "_"), namespace
    )
    renderer = BboxStampRenderer(capability, max_workers=1, cache=cache)
    with pymupdf.open(source) as pdf:
        engine = overlay.LatexBboxOverlay(pdf, state["doc"], config)
        engine._load_state()
        job = {
            "debug_id": pid,
            "page": meta["page"],
            "body": body,
            "width": box[2] - box[0],
            "height": box[3] - box[1],
            "font_size": font_size,
        }
        jobs = engine._substitute_fragments([job], temporary)
        if len(jobs) != 1:
            raise ToolError("compile_failed", "公式片段无法恢复")
        request = engine._stamp_request(jobs[0])
        if isinstance(line_skip, (int, float)) and not isinstance(line_skip, bool):
            # 行距覆盖：直接用字号 × 系数（与全量路径 line_skip 语义一致），
            # 不再走源行距推导的 clamp。
            request.lead = request.font_size * float(line_skip)
        result = renderer.render_one(request, temporary)
    if not result.ok or not result.pdf_path:
        raise ToolError("compile_failed", "单块编译失败", reason=result.reason)
    return result, renderer.cache_hits > 0


class BlockCompiler:
    def __init__(self, store, runner):
        self.store, self.runner = store, runner
        self.tasks = set()
        self.semaphore = asyncio.Semaphore(2)
        #: LaTeX 能力探测缓存（首次用到时探测一次）：探测要起 kpsewhich 子进程，
        #: 流式预览每段一编时不该重复付这笔钱。实例生命周期即缓存生命周期
        #: （serve 进程的编译器常驻；流式预览的编译器每 job 一个）。
        self._capability = None
        self._capability_lock = threading.Lock()
        #: 检测器与每页版面证据缓存（浮动阶梯四级复用同一份检测结果）。
        self._detector_lock = threading.Lock()
        self._page_layout_cache = None
        #: 已落定贴片矩形登记簿：浮动决策据此互相避让（见 FloatReservations）。
        self.reservations = FloatReservations()
        #: 段落行缓存：did → (取用时刻, rows)。见 :meth:`_rows`。
        self._rows_cache: dict[str, tuple[float, list]] = {}
        self._rows_lock = threading.Lock()
        #: 源文行数缓存：(did, pid) → n_lines。见 :meth:`_source_n_lines`。
        self._source_lines: dict[tuple[str, str], int | None] = {}
        self._source_lines_lock = threading.Lock()

    def capability(self):
        """按需探测并缓存的 LaTeX 能力（线程安全；多 worker 并发首编时只探一次）。"""
        from babeldoc.format.pdf.document_il.backend.latex_bbox.capability import (
            probe_latex_capability,
        )

        with self._capability_lock:
            if self._capability is None:
                self._capability = probe_latex_capability()
            return self._capability

    async def shutdown(self):
        for record in self.runner.registry.records.values():
            if record.effective_scope not in ("block", "blocks", "export"):
                continue
            if record.status == "running":
                self.runner.registry.mark_cancel_requested(record)
            elif record.status == "queued":
                self.runner.registry.mark_finished(record, status="canceled")
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

    @staticmethod
    def _check_job(database, record):
        row = database.connection.execute(
            "SELECT payload FROM job_snapshots WHERE id=?", (record.job_id,)
        ).fetchone()
        saved = json.loads(row[0]) if row else None
        if saved and (
            saved.get("status") != "running" or saved.get("cancel_requested_at")
        ):
            raise ToolError("stale_job", "任务已结束或取消，结果未发布")

    def _rows(self, did):
        """段落行，带短 TTL 的进程内缓存——**只保几何，不保译文**。

        每个块编译都要全量段落表来做「与邻居重叠」判定和取 geometry，而
        :func:`document_blocks` 每次都要重解 ``anchors.json``（535KB）+
        ``layout_geometry.json`` 等产物，实测约 0.48s——按块付费就是 356 × 0.48s
        ≈ 3 分钟纯 JSON 解析。几何在一次 job 内不变，缓存它是安全的。

        ``row.target`` **不可信**：流式预览每块都是「译文刚落库就 submit」
        （见 :mod:`babeldoc_tools.translate` 的 ``completed``），缓存里的快照必然
        还没有这一块的译文。曾经按 ``row.target`` 编译，结果一个 TTL 窗口只有第一个
        到达的块能编出来，同窗口其余块全部「缺少译文或排版数据」——实测 151 块只成
        33 块。要编译的译文一律走 :meth:`_target`（权威单行查询）。
        """
        now = time.monotonic()
        with self._rows_lock:
            cached = self._rows_cache.get(did)
            if cached is not None and now - cached[0] < _ROWS_CACHE_TTL_S:
                return cached[1]
        rows = document_blocks(self.store, did)
        with self._rows_lock:
            self._rows_cache[did] = (time.monotonic(), rows)
        return rows

    def _target(self, did, pid):
        """该块当前的权威译文：``translation_blocks`` 单行查询（主键命中）。

        不走 :meth:`_rows` 的缓存：那份快照的 ``target`` 永远落后于「刚落库就编译」
        的流式预览（见 :meth:`_rows`）。这里按 ``(document_id, block_id)`` 主键查一行，
        开销是微秒级，且没有任何时间窗口假设。
        """
        database = self.store.database
        with database._lock:
            row = database.connection.execute(
                "SELECT target FROM translation_blocks WHERE document_id=? AND block_id=?",
                (did, pid),
            ).fetchone()
        return row[0] if row else None

    def _mark_not_replaced(self, did, pid, job_id, reason="label-not-eligible"):
        """把「按设计不替换」记成落定状态 + 一条中性事件。

        状态用 ``not_replaced``（≠ ``preview_failed``）：既让 ``_page_complete`` 把该块
        算作已落定（否则整页永远等不到成页），又不在界面上报成预览失败——这类块显示
        基线原文就是正确结果。
        """
        database = self.store.database
        with database._lock, database.connection:
            database.connection.execute(
                "INSERT OR REPLACE INTO compile_blocks"
                "(document_id,block_id,input_hash,patch_asset,status,target_size,error)"
                " VALUES (?,?,NULL,NULL,'not_replaced',NULL,?)",
                (did, pid, reason),
            )
        if job_id:
            database.append_event(
                job_id,
                did,
                "block_not_replaced",
                {"paragraph_id": pid, "reason": reason},
                block_id=pid,
            )

    def _source_pdf(self, did):
        """**源文** PDF（prepared 优先，回退 parse 记录的输入文件）；拿不到 → None。

        刻意不走 :meth:`_outputs`：那里第一顺位是 ``output/*.mono.pdf``，文档跑过一轮
        之后它已经是译文页。贴片 baseline 用它没问题（就是要往最新成品上贴），但
        **量源文行数**用它就是量译文，口径全错。
        """
        database = self.store.database
        with database._lock:
            row = database.connection.execute(
                "SELECT prepared_pdf_asset FROM parse_results"
                " WHERE document_id=? AND status='ready'",
                (did,),
            ).fetchone()
        if row and row[0]:
            return AssetStore(self.store.store_base, database).resolve(row[0])
        from babeldoc.tools.agent.prepared_pdf import resolve_source_pdf

        workdir = self.store.resolve(did)
        return resolve_source_pdf(_load_parse_state(workdir), workdir)

    def _source_n_lines(self, did, row) -> int | None:
        """该块在源 PDF 上的文本行数（``overlay.measure_line_fill`` 同一函数）。

        按 ``(did, pid)`` 记忆：同一块重编不再开第二次 PDF。量不到（源文件缺失、
        页码越界、没有框）→ ``None``，由调用方按「无据不拦」处理。

        不用 parse 落盘的 ``n_lines``：``geometry`` 是 apply 之后的排版几何，它的
        ``n_lines`` 是**译文**排完的行数；``state.pkl`` 的 ``source_line_geometry``
        虽是源文侧，但与全量路径的判定实测对不齐（本文档 13 条判定里错 8 条）。
        现量现算才是同口径——实测与全量路径的 ``latex_candidates.rejected`` 逐条一致。
        """
        import pymupdf

        key = (did, row.id)
        with self._source_lines_lock:
            if key in self._source_lines:
                return self._source_lines[key]
        geometry = row.geometry or {}
        box = geometry.get("src_box") or geometry.get("layout_box")
        value = None
        # 量不到就是量不到（源文件缺失/不可解析）：由调用方按「无据不拦」处理，
        # 绝不让门禁本身把一次本可以成功的编译变成失败。
        with contextlib.suppress(Exception):
            source = self._source_pdf(did) if box and row.page else None
            if source is not None:
                from babeldoc.format.pdf.document_il.backend.latex_bbox.overlay import (
                    measure_line_fill,
                )

                with pymupdf.open(source) as doc:
                    if 0 <= row.page - 1 < doc.page_count:
                        page = doc[row.page - 1]
                        height = page.rect.height
                        clip = pymupdf.Rect(
                            box[0], height - box[3], box[2], height - box[1]
                        )
                        value = int(measure_line_fill(page, clip)["n_lines"])
        with self._source_lines_lock:
            self._source_lines[key] = value
        return value

    def _ineligible_reason(self, did, row) -> str | None:
        """与一次性编译同口径的贴片资格门禁 → 不合格原因 / None。

        对齐 :meth:`overlay.LatexBboxOverlay._select_candidates` 的两道判定：

        1. ``layout_label`` 必须是正文本体标签（``overlay._BODY_LABELS``，直接复用不
           复制常量）。``title`` 不在其中：标题本来就不该由 LaTeX 重排。
        2. 源文行数 ≥ 2（见 :meth:`_source_n_lines`）。

        流式预览缺这两道门禁时，单行标题会被当正文编译；它在源字号档放不下而被缩一档，
        ``layout_refine.expansion_reason``（scale < 0.995）随即判定要扩框，浮动阶梯就把
        框向上扩掉上一行的净空——实测标题顶边被抬高 26.6pt，就是用户看到的「标题上移」。

        依据缺失（``layout_label`` 未物化的迁移文档 / 源 PDF 量不到）时跳过对应那道，
        不拦：门禁只在有据可依时否决。
        """
        from babeldoc.format.pdf.document_il.backend.latex_bbox.overlay import (
            _BODY_LABELS,
        )

        label = getattr(row, "layout_label", None)
        if label is not None and label not in _BODY_LABELS:
            return "label-not-eligible"
        n_lines = self._source_n_lines(did, row)
        if n_lines is not None and n_lines < 2:
            return "single-line"
        return None

    async def submit(self, did, pid, revision):
        workdir = self.store.resolve(did)
        if read_draft(workdir).revision != revision:
            raise ToolError("revision_conflict", "草稿 revision 已变化")
        if pid is not None and not any(row.id == pid for row in self._rows(did)):
            raise ToolError("block_not_found", "文档中没有该 block")
        async with self.runner.registry.lock:
            active = self.runner.registry.active_for_did(did)
            if active:
                raise ToolError("document_busy", "已有编译任务", job_id=active.job_id)
            record = self.runner.registry.create(
                did=did,
                action="compile",
                from_stage="build",
                profile=None,
                paragraph_id=pid,
                requested_scope="block" if pid else "export",
                effective_scope="block" if pid else "export",
                trigger="manual",
            )
            record.revision = revision
            # Local workers are not pipeline subprocess jobs; remove from its queue.
            self.runner.registry._queue.remove(record.job_id)
            self.runner.registry.save(record)
        if pid is None:
            with self.store.database._lock, self.store.database.connection:
                self.store.database.connection.execute(
                    "UPDATE exports SET status='previous' WHERE document_id=?", (did,)
                )
        task = asyncio.create_task(self._run(record))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return record

    async def submit_batch(self, did, pids, revision):
        """批量块编译：一个 job 编一组 block（各自的草稿覆盖互不影响）。"""
        workdir = self.store.resolve(did)
        if read_draft(workdir).revision != revision:
            raise ToolError("revision_conflict", "草稿 revision 已变化")
        known = {row.id for row in self._rows(did)}
        missing = [pid for pid in pids if pid not in known]
        if missing:
            raise ToolError("block_not_found", f"文档中没有该 block：{missing[0]}")
        async with self.runner.registry.lock:
            active = self.runner.registry.active_for_did(did)
            if active:
                raise ToolError("document_busy", "已有编译任务", job_id=active.job_id)
            record = self.runner.registry.create(
                did=did,
                action="compile",
                from_stage="build",
                profile=None,
                paragraph_ids=list(pids),
                requested_scope="blocks",
                effective_scope="blocks",
                trigger="manual",
            )
            record.revision = revision
            self.runner.registry._queue.remove(record.job_id)
            self.runner.registry.save(record)
        task = asyncio.create_task(self._run(record))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return record

    async def _run(self, record):
        async with self.semaphore:
            await self._execute(record)

    async def _execute(self, record):
        if record.status == "canceled":
            return
        record.status = "running"
        record.started_at = utc_now()
        record.boot_id = self.runner.registry.boot_id
        self.runner.registry._write(record, EVENT_STARTED)
        try:
            scope = record.effective_scope
            operation = (
                self.export
                if scope == "export"
                else self.compile_blocks
                if scope == "blocks"
                else self.compile
            )
            result = await asyncio.to_thread(operation, record)
            self.runner.registry.mark_finished(
                record, status="succeeded", envelope=json.dumps(result)
            )
        except NotReplaced as exc:
            # 手动单块编译命中资格门禁：不是失败，据实说明该块保留基线原文。
            self._mark_not_replaced(
                record.did, record.paragraph_id, record.job_id, exc.reason
            )
            self.runner.registry.mark_finished(
                record,
                status="succeeded",
                envelope=json.dumps(
                    {
                        "block_id": record.paragraph_id,
                        "not_replaced": exc.reason,
                    }
                ),
            )
        except Exception as exc:
            canceled = record.cancel_requested_at is not None
            self.runner.registry.mark_finished(
                record,
                status="canceled" if canceled else "failed",
                error_code="canceled"
                if canceled
                else getattr(exc, "code", "compile_failed"),
                error_message=str(exc),
            )

    def _layout_detector(self):
        """懒加载 PP-DocLayoutV3 检测器；不可用时返回 None（不改变原行为）。"""
        from babeldoc.docvision.paddle_layout_regions import PaddleLayoutRegions

        with self._detector_lock:
            detector = getattr(self, "_detector", None)
            if detector is None:
                detector = PaddleLayoutRegions(threads=_LAYOUT_DETECT_THREADS)
                self._detector = detector
            return detector if detector.available else None

    def _layout_cache(self):
        """本编译器的每页版面证据缓存；检测器不可用时返回 None。"""
        with self._detector_lock:
            cache = getattr(self, "_page_layout_cache", None)
            if cache is not None:
                return cache
        detector = self._layout_detector()
        if detector is None:
            return None
        with self._detector_lock:
            if getattr(self, "_page_layout_cache", None) is None:
                self._page_layout_cache = PageLayoutCache(detector)
            return self._page_layout_cache

    def _float_if_shrunk(self, doc, page_number, rows, pid, box, stamp):
        """缩字/溢出贴片的浮动阶梯：同栏下/上扩 → 跨栏横向扩 → 跨页整框迁移。

        返回 ``(新框, 贴片页码, 浮动方式)``；无可行方案时 ``(原框, page_number, None)``。
        判定用 baseline 页的 PP-DocLayoutV3 区域 + 精确墨迹（与既有扩框同一套数据，
        见 :mod:`babeldoc.tools.agent.layout_refine`），**并含同页已落定的兄弟贴片
        框**（``self.reservations`` 登记簿）：baseline 是原文页，只看它的话每个块都
        以为落点是空的，实测会让全部跨页迁移叠在同一条顶部净空里。每页的检测证据
        按页缓存一次，浮动阶梯四级复用同一份。

        跨页迁移保持 x 范围不变，在下一页自上而下找第一个能容纳
        ``原框高 / scale`` 的空闲区间（顶对齐）。
        """
        from babeldoc.tools.agent import layout_refine

        reason = layout_refine.expansion_reason(
            scale=getattr(stamp, "scale", None),
            ok=bool(getattr(stamp, "ok", False)),
            reason=getattr(stamp, "reason", None),
        )
        if reason is None or not layout_refine.refine_enabled():
            return box, page_number, None
        cache = self._layout_cache()
        if cache is None:
            return box, page_number, None
        detector = cache.detector
        page = doc[page_number - 1]
        evidence = cache.evidence(page, page_number)
        reserved = self._page_obstacles(rows, pid, page_number)
        expanded = layout_refine.plan_page_expansion(
            page, box, detector, evidence=evidence, reserved=reserved
        )
        if expanded is not None:
            return list(expanded), page_number, "expand"
        for direction in ("right", "left"):
            widened = layout_refine.plan_widen_page_expansion(
                page,
                box,
                detector,
                direction=direction,
                evidence=evidence,
                reserved=reserved,
            )
            if widened is not None and self._float_box_is_clear(
                rows, pid, page_number, widened
            ):
                return list(widened), page_number, f"widen-{direction}"
        # 跨页迁移：仅当下一页存在且未旋转；所需高度按缩字比例放大回原字号。
        next_index = page_number  # 1 基页码正好是下一页的 0 基下标
        if next_index >= doc.page_count or doc[next_index].rotation:
            return box, page_number, None
        scale = getattr(stamp, "scale", None)
        required = (
            (box[3] - box[1]) / min(max(float(scale), 0.05), 1.0)
            if scale
            else (box[3] - box[1]) * 1.2
        )
        landing = next_index + 1
        moved = layout_refine.plan_next_page_float(
            doc[next_index],
            box,
            detector,
            required_height=required,
            evidence=cache.evidence(doc[next_index], landing),
            reserved=self._page_obstacles(rows, pid, landing),
        )
        if moved is not None and self._float_box_is_clear(rows, pid, landing, moved):
            return list(moved), landing, "next-page"
        return box, page_number, None

    def _page_obstacles(self, rows, pid, page_number):
        """浮动规划的「兄弟」障碍集：已定贴片框 + 尚未编译的段落原文框。

        只看已定贴片还不够：流式预览里兄弟段可能**还没编译**，此时它在版面上
        仍占着原文框那块地（合成时基线原文就画在那里）。区域检测虽然多半能覆盖
        这些墨迹，但它是启发式的——实测会把相邻小段并成一块或漏检，导致扩框
        吃掉邻居 1pt 左右。把原文框显式并进来，判定就不依赖检测器的召回率。
        """
        obstacles = self.reservations.obstacles(page_number, exclude=pid)
        reserved_ids = self.reservations.reserved_ids()
        for other in rows:
            if other.id == pid or other.id in reserved_ids:
                continue
            if other.page != page_number:
                continue
            geometry = other.geometry or {}
            other_box = geometry.get("layout_box") or geometry.get("src_box")
            if other_box:
                obstacles.append([float(value) for value in other_box])
        return obstacles

    def _float_box_is_clear(self, rows, pid, page_number, box):
        """浮动框不与目标页上的**原文版面框或已定贴片**相交（第二道保险）。

        原实现只查 ``rows`` 的原文 layout 框，对「兄弟贴片浮到同一处」完全无感
        （原文空白区里两个贴片可以互相完全覆盖）。这里同时查登记簿。
        """
        import pymupdf

        rect = pymupdf.Rect(box)
        for other in rows:
            if other.id == pid or other.page != page_number:
                continue
            other_box = (other.geometry or {}).get("layout_box") or (
                other.geometry or {}
            ).get("src_box")
            if other_box and rect.intersects(pymupdf.Rect(other_box)):
                return False
        for reserved in self.reservations.obstacles(page_number, exclude=pid):
            if rect.intersects(pymupdf.Rect(reserved)):
                return False
        return True

    def _outputs(self, did):
        workdir = self.store.resolve(did)
        outputs = sorted((workdir / "output").glob("*.mono.pdf"))
        if outputs:
            return outputs
        database = self.store.database
        with database._lock:
            row = database.connection.execute(
                "SELECT asset_sha256 FROM exports WHERE document_id=? ORDER BY id DESC LIMIT 1",
                (did,),
            ).fetchone()
        return (
            [AssetStore(self.store.store_base, database).resolve(row[0])] if row else []
        )

    def export(self, record):
        """Compile only changed draft inputs, then assemble immutable page assets."""
        import pymupdf

        workdir = self.store.resolve(record.did)
        draft = read_draft(workdir)
        if draft.revision != record.revision:
            raise ToolError("stale_job", "草稿 revision 已更新")
        database = self.store.database
        assets = AssetStore(self.store.store_base, database)
        with database._lock:
            states = {
                row[0]: json.loads(row[1])
                for row in database.connection.execute(
                    "SELECT page,payload FROM local_pages WHERE document_id=?",
                    (record.did,),
                )
            }
        patches = {
            pid: patch
            for state in states.values()
            for pid, patch in state["patches"].items()
        }
        rows = {row.id: row for row in self._rows(record.did)}
        dirty = []
        for pid in set(draft.paragraphs) | set(patches):
            row = rows.get(pid)
            if row is None:
                raise ToolError("block_not_found", f"block 不存在：{pid}")
            edit = draft.paragraphs.get(pid)
            expected = {
                # 与 compile_block_patch 同一口径的权威译文（见 _target）：用 _rows
                # 缓存里的 target 会把「译文已更新」误判成 clean，整块漏编。
                "target": edit.target
                if edit and edit.target is not None
                else self._target(record.did, pid),
                "layout": edit.layout if edit else None,
            }
            if patches.get(pid, {}).get("input") != expected:
                dirty.append(pid)
        for pid in sorted(dirty):
            record.paragraph_id = pid
            try:
                self.compile(record)
            except NotReplaced:
                # 按设计不替换的块（标题/单行）保留基线原文，不该让导出失败。
                continue
        record.paragraph_id = None
        outputs = self._outputs(record.did)
        if not outputs:
            raise ToolError("export_not_ready", "尚无基线 PDF")
        with database._lock:
            page_rows = database.connection.execute(
                "SELECT page,page_asset FROM pages WHERE document_id=?", (record.did,)
            ).fetchall()
        temporary_root = self.store.store_base / "tmp"
        temporary_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=record.job_id + "-export-", dir=temporary_root
        ) as folder:
            path = Path(folder) / "export.pdf"
            with pymupdf.open(outputs[0]) as pdf:
                for page_number, asset in page_rows:
                    page = pdf[page_number - 1]
                    # Keep the existing page object, annotations and GOTO destinations.
                    # Only its contents change; page-count and page xrefs stay stable.
                    empty = pdf.get_new_xref()
                    pdf.update_object(empty, "<<>>")
                    pdf.update_stream(empty, b"")
                    page.set_contents(empty)
                    with pymupdf.open(assets.resolve(asset)) as patch:
                        page.show_pdf_page(page.rect, patch, 0)
                pdf.save(path, garbage=4, deflate=True)
            asset = assets.put(path, kind="export")
        with database._lock, database.connection:
            database.connection.execute("BEGIN IMMEDIATE")
            current = database.draft(record.did)
            if (current or {"revision": 0})["revision"] != record.revision:
                raise ToolError("stale_job", "草稿已更新，导出未发布")
            if record.cancel_requested_at is not None:
                raise ToolError("stale_job", "导出已取消")
            self._check_job(database, record)
            database.connection.execute(
                "DELETE FROM exports WHERE document_id=?", (record.did,)
            )
            database.connection.execute(
                "INSERT INTO exports(document_id,asset_sha256,revision,status) VALUES (?,?,?,'ok')",
                (record.did, asset, record.revision),
            )
        return {
            "asset": asset,
            "revision": record.revision,
            "compiled_blocks": len(dirty),
        }

    def _page_state(self, database, did, page_number, *, base):
        """当前页的 patch 状态（数据库权威）；尚无行时用 ``base`` 起一份空状态。"""
        with database._lock:
            previous = database.connection.execute(
                "SELECT payload FROM local_pages WHERE document_id=? AND page=?",
                (did, page_number),
            ).fetchone()
        return json.loads(previous[0]) if previous else {"base": base, "patches": {}}

    def _prefetch_float_evidence(self, doc, page_number, stamp):
        """缩字贴片在进页锁**之前**先把浮动要用的版面证据算好（ORT 推理 ~0.4–1s）。

        证据只取决于 baseline 页，按页缓存；在锁外算意味着同页后续块的规划
        直接命中缓存，页锁内只剩纯几何规划。检测不可用时什么也不做。
        """
        from babeldoc.tools.agent import layout_refine

        reason = layout_refine.expansion_reason(
            scale=getattr(stamp, "scale", None),
            ok=bool(getattr(stamp, "ok", False)),
            reason=getattr(stamp, "reason", None),
        )
        if reason is None or not layout_refine.refine_enabled():
            return
        cache = self._layout_cache()
        if cache is None:
            return
        cache.evidence(doc[page_number - 1], page_number)
        if page_number < doc.page_count and not doc[page_number].rotation:
            cache.evidence(doc[page_number], page_number + 1)

    def compile_block_patch(self, record, *, page_lock=None):
        """编译一个块的贴片并写入该页 patch 状态。

        ``page_lock``：调用方提供的**页锁**（同页块并发时必传）。贴片渲染本身只
        取决于译文与框，不持锁并行跑；只有依赖页状态的三段持锁：读 patch 状态
        + 浮动规划/占位、以及最后的读-改-写提交。此前整个函数都在页锁（或同页
        单线程队列）里串行，一页二十个块要排 20 × 6s 的长队，是流式预览尾部
        最大的等待来源。
        """
        import pymupdf

        started = time.monotonic()
        did, pid, revision = record.did, record.paragraph_id, record.revision
        workdir = self.store.resolve(did)
        draft = read_draft(workdir)
        if draft.revision != revision:
            raise ToolError("stale_job", "编译输入已过期")
        rows = self._rows(did)
        row = next(item for item in rows if item.id == pid)
        edit = draft.paragraphs.get(pid)
        layout = edit.layout if edit else None
        geometry = row.geometry or {}
        box = (layout or {}).get("box")
        if box is None:
            box = geometry.get("layout_box") or geometry.get("src_box")
            box_scale = (layout or {}).get("box_scale")
            if (
                box
                and isinstance(box_scale, (int, float))
                and not isinstance(box_scale, bool)
            ):
                # 与 layout_overrides.scale_box 同语义：锚定左上角向右/向下生长。
                x, y, x2, y2 = (float(v) for v in box)
                factor = float(box_scale)
                box = [x, y2 - (y2 - y) * factor, x + (x2 - x) * factor, y2]
        # 译文优先级：草稿改写 > 库里的权威单行（见 _target）> 行快照。
        # 中间这层是关键：_rows 的快照对「刚落库就编译」的流式预览永远是旧的；
        # 库里没有该块时（迁移文档、上一轮的产物）才退回快照，行为与改动前一致。
        target = edit.target if edit and edit.target is not None else None
        if target is None:
            target = self._target(did, pid)
        if target is None:
            target = row.target
        if target is None or row.page is None or box is None:
            raise ToolError("compile_failed", "缺少译文或排版数据")
        # 资格门禁与一次性编译同口径：不合格的块保留基线原文，不是失败。只看 row，
        # 所以在开 PDF、起 xelatex 之前就判掉。
        ineligible = self._ineligible_reason(did, row)
        if ineligible is not None:
            raise NotReplaced(ineligible)
        database = self.store.database
        assets = AssetStore(self.store.store_base, database)
        outputs = self._outputs(did)
        if not outputs:
            with database._lock:
                prepared = database.connection.execute(
                    "SELECT prepared_pdf_asset FROM parse_results WHERE document_id=? AND status='ready'",
                    (did,),
                ).fetchone()
            if prepared and prepared[0]:
                outputs = [assets.resolve(prepared[0])]
        if not outputs:
            from babeldoc.tools.agent.prepared_pdf import resolve_source_pdf

            parsed = _load_parse_state(workdir)
            source = resolve_source_pdf(parsed, workdir)
            if source is None:
                raise ToolError("compile_failed", "prepared PDF 不可用")
            outputs = [source]
        lock = page_lock if page_lock is not None else contextlib.nullcontext()
        # Capture base once. Subsequent local edits always compose from immutable pages.
        # 资产按内容寻址：同页两个块并发首编各 put 一次 baseline 也只落一份。
        state = self._page_state(database, did, row.page, base=None)
        if state["base"] is None:
            state["base"] = assets.put(outputs[0], kind="baseline")
        base = state["base"]
        with pymupdf.open(assets.resolve(base)) as baseline:
            index = row.page - 1
            if index < 0 or index >= baseline.page_count:
                raise ToolError("bbox_invalid", "block 页码与 PDF 不一致")
            page = baseline[index]
            if page.rotation:
                raise ToolError(
                    "compile_failed", "旋转页面需要扩大编译范围，未执行全量回退"
                )
            box = valid_box(box, page.rect.width, page.rect.height)
            # Expansion into another block is explicitly rejected, never widened silently.
            rect = pymupdf.Rect(box)
            for other in rows:
                if other.id != pid and other.page == row.page:
                    other_box = (other.geometry or {}).get("layout_box") or (
                        other.geometry or {}
                    ).get("src_box")
                    if other_box and rect.intersects(pymupdf.Rect(other_box)):
                        raise ToolError(
                            "compile_failed", "需要扩大编译范围：bbox 与相邻 block 重叠"
                        )
            temporary_root = self.store.store_base / "tmp"
            temporary_root.mkdir(exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix=record.job_id + "-", dir=temporary_root
            ) as folder:
                temporary = Path(folder)
                cache_root = self.store.store_base / "cache/stamps"
                # 不持页锁：贴片只取决于译文与框，同页块可以并行渲染。
                stamp, hit = render_request(
                    workdir,
                    pid,
                    target,
                    box,
                    temporary,
                    cache_root,
                    capability=self.capability(),
                    layout=layout,
                )
                self._prefetch_float_evidence(baseline, row.page, stamp)
                # P6+浮动：被缩字/溢出时按当前译文版面找净空——同栏下/上扩、
                # 跨栏横向扩、跨页整框迁移（PP-DocLayoutV3 对译文页重识别），
                # 避免为了塞进原框而缩字号。规划依赖同页已落定的贴片，持页锁；
                # 选中的落点先占位再放锁重渲染，兄弟块规划时已能避开它。
                with lock:
                    state = self._page_state(database, did, row.page, base=base)
                    # 已持久化的本页 patch 进登记簿：重启/续跑/单段重编时，浮动
                    # 决策同样要看见既有贴片（登记簿是进程内的，数据库才是跨进程
                    # 的权威状态）。
                    self.reservations.load(row.page, state.get("patches") or {})
                    prev_stamp_page = int(
                        (state["patches"].get(pid) or {}).get("page", row.page)
                    )
                    float_box, stamp_page, floated = self._float_if_shrunk(
                        baseline, row.page, rows, pid, box, stamp
                    )
                    if floated:
                        self.reservations.reserve(pid, stamp_page, float_box)
                # 重渲染失败（超时/TeX 错）不算升级：保留原来可用的贴片。
                if floated:
                    try:
                        if stamp_page != row.page:
                            float_box = valid_box(
                                float_box,
                                baseline[stamp_page - 1].rect.width,
                                baseline[stamp_page - 1].rect.height,
                            )
                        retried, retry_hit = render_request(
                            workdir,
                            pid,
                            target,
                            float_box,
                            temporary,
                            cache_root,
                            capability=self.capability(),
                            layout=layout,
                        )
                    except ToolError:
                        retried = None
                    if retried is not None:
                        box, stamp, hit = float_box, retried, retry_hit
                    else:
                        float_box, stamp_page, floated = box, row.page, None
                stamp_asset = assets.put(Path(stamp.pdf_path), kind="stamp")
                with lock:
                    # 登记最终落点（含未浮动/浮动失败回退原框的情形）：后续块的
                    # 浮动规划要把它当障碍，才不会叠上来。
                    self.reservations.reserve(pid, stamp_page, box)
                    state = self._page_state(database, did, row.page, base=base)
                    state["patches"][pid] = {
                        "asset": stamp_asset,
                        "box": box,
                        "page": stamp_page,
                        "old_box": geometry.get("rendered_box")
                        or geometry.get("layout_box")
                        or geometry.get("src_box"),
                        "input": {
                            "target": target,
                            "layout": edit.layout if edit else None,
                        },
                    }
                    if floated:
                        self.store.database.append_event(
                            record.job_id,
                            did,
                            "compile_float",
                            {
                                "paragraph_id": pid,
                                "kind": floated,
                                "page": stamp_page,
                                "box": [round(float(v), 2) for v in box],
                            },
                            block_id=pid,
                        )
                    input_hash = hashlib.sha256(
                        json.dumps(state, sort_keys=True).encode()
                    ).hexdigest()
                    with database._lock, database.connection:
                        database.connection.execute("BEGIN IMMEDIATE")
                        current = database.draft(did)
                        if (current or {"revision": 0})["revision"] != revision:
                            raise ToolError("stale_job", "草稿已更新，旧编译结果未发布")
                        if (
                            record.cancel_requested_at is not None
                            or record.status != "running"
                        ):
                            raise ToolError("stale_job", "任务已取消，编译结果未发布")
                        self._check_job(database, record)
                        database.connection.execute(
                            "INSERT OR REPLACE INTO local_pages VALUES (?,?,?)",
                            (did, row.page, json.dumps(state)),
                        )
                        database.connection.execute(
                            "INSERT OR REPLACE INTO compile_blocks(document_id,block_id,input_hash,patch_asset,status,target_size) VALUES (?,?,?,?,?,?)",
                            (did, pid, input_hash, stamp_asset, "ok", stamp.font_size),
                        )
        result = {
            "block_id": pid,
            "page": row.page,
            # 贴片实际落在哪页（跨页浮动后 ≠ 段落主页）；previous_* 供上层
            # 重合成"上一版贴片所在页"（迁移回主页时要擦掉旧页上的贴片）。
            "stamp_page": stamp_page,
            "previous_stamp_page": prev_stamp_page,
            "revision": revision,
            "asset": stamp_asset,
            "cache_hit": hit,
            "duration_s": time.monotonic() - started,
        }
        return result

    def _foreign_patches(self, did, page_number):
        """落在 ``page_number`` 上、但 home 在其它页的贴片（跨页浮动的产物）。

        贴片的 home 页状态负责擦 ``old_box``；落点页负责把贴片盖上去。这里扫
        全文档的页面状态找"落在本页、home 不是本页"的贴片（量级 = 页数，可接受）。
        """
        database = self.store.database
        foreign: dict[str, dict] = {}
        with database._lock:
            rows = database.connection.execute(
                "SELECT page,payload FROM local_pages WHERE document_id=?",
                (did,),
            ).fetchall()
        for number, payload in rows:
            if number == page_number:
                continue
            try:
                state = json.loads(payload)
            except (TypeError, ValueError):
                continue
            for key, patch in (state.get("patches") or {}).items():
                if patch.get("page", number) == page_number:
                    foreign[key] = patch
        return foreign

    def compose_page_asset(self, record, page_number, *, complete=True, duration_s=0.0):
        import pymupdf
        from babeldoc.format.pdf.document_il.backend.latex_bbox.overlay import (
            LatexBboxOverlay,
        )
        from babeldoc.format.pdf.document_il.backend.latex_bbox.overlay import (
            _links_by_signature,
        )

        started = time.monotonic()
        did = record.did
        database = self.store.database
        assets = AssetStore(self.store.store_base, database)
        with database._lock:
            saved = database.connection.execute(
                "SELECT payload FROM local_pages WHERE document_id=? AND page=?",
                (did, page_number),
            ).fetchone()
        if saved is not None:
            state = json.loads(saved[0])
        else:
            # 跨页浮动的落点页可能没有自己的 patch 状态：用同一份 baseline 初始化。
            outputs = self._outputs(did)
            if not outputs:
                raise ToolError("compile_failed", "页面没有可用 patch")
            state = {"base": assets.put(outputs[0], kind="baseline"), "patches": {}}
        temporary_root = self.store.store_base / "tmp"
        temporary_root.mkdir(exist_ok=True)
        with pymupdf.open(assets.resolve(state["base"])) as baseline:
            index = page_number - 1
            page = baseline[index]
            with tempfile.TemporaryDirectory(
                prefix=record.job_id + "-page-", dir=temporary_root
            ) as folder:
                temporary = Path(folder)
                # Start with the baseline page, redact each old/new area, then reinsert
                # all cached patches. No other block invokes the renderer.
                composed = pymupdf.open()
                try:
                    composed.insert_pdf(
                        baseline, from_page=index, to_page=index, links=False
                    )
                    # Page-local GOTO links need the full PDF context at export. Keep their
                    # metadata with the page and preserve URI links in the preview.
                    links = page.get_links()
                    from babeldoc.format.pdf.document_il.backend.link_remap import (
                        safe_insert_link,
                    )

                    for link in links:
                        if link.get("kind") != pymupdf.LINK_GOTO:
                            safe_insert_link(composed[0], link)
                    engine = LatexBboxOverlay(
                        composed, SimpleNamespace(page=[]), SimpleNamespace()
                    )
                    before = {0: _links_by_signature(composed[0].get_links())}
                    uris = engine._uri_set(composed)
                    jobs, successful = [], {}
                    height = page.rect.height

                    def flip(value):
                        return pymupdf.Rect(
                            value[0], height - value[3], value[2], height - value[1]
                        )

                    # 落在本页的贴片 = 本页 home 的（未迁走的）+ 其它页迁进来的。
                    stamped = {
                        key: patch
                        for key, patch in {
                            **self._foreign_patches(did, page_number),
                            **state["patches"],
                        }.items()
                        if patch.get("page", page_number) == page_number
                    }
                    for key, patch in state["patches"].items():
                        # Redact the old footprint (home page) before stamping.
                        old_rect = flip(patch["old_box"] or patch["box"])
                        jobs.append({"debug_id": key, "page": 0, "rect": old_rect})
                    for key, patch in stamped.items():
                        successful[key] = SimpleNamespace(
                            pdf_path=str(assets.resolve(patch["asset"]))
                        )
                    # Reuse link-safe redaction in overlay by clearing old footprints,
                    # then use the requested rectangles for the actual stamps.
                    from babeldoc.format.pdf.document_il.backend.link_remap import (
                        safe_insert_link,
                    )

                    removed = composed[0].get_links()
                    for job in jobs:
                        composed[0].add_redact_annot(job["rect"])
                    composed[0].apply_redactions(images=0, graphics=1, text=0)
                    remaining = _links_by_signature(composed[0].get_links())
                    for link in removed:
                        if _links_by_signature([link]).keys().isdisjoint(remaining):
                            safe_insert_link(composed[0], link)
                    jobs = [
                        {"debug_id": key, "page": 0, "rect": flip(patch["box"])}
                        for key, patch in stamped.items()
                    ]
                    engine._stamp_pages(jobs, successful)
                    if not engine._verify_links(before, uris):
                        raise ToolError("compile_failed", "链接校验失败，已保留旧页面")
                    result_path = temporary / "page.pdf"
                    composed.save(result_path, deflate=True)
                    page_asset = assets.put(result_path, kind="page")
                finally:
                    composed.close()
        state["page_revision"] = int(state.get("page_revision", 0)) + 1
        state["page_asset"] = page_asset
        state["complete"] = complete
        state["updated_at"] = utc_now()
        input_hash = hashlib.sha256(
            json.dumps(state["patches"], sort_keys=True).encode()
        ).hexdigest()
        with database._lock, database.connection:
            database.connection.execute("BEGIN IMMEDIATE")
            if (database.draft(did) or {"revision": 0})["revision"] != record.revision:
                raise ToolError("stale_job", "草稿已更新，旧页面未发布")
            self._check_job(database, record)
            database.connection.execute(
                "INSERT OR REPLACE INTO local_pages VALUES (?,?,?)",
                (did, page_number, json.dumps(state)),
            )
            database.connection.execute(
                "INSERT OR REPLACE INTO pages(document_id,page,input_hash,page_asset,dirty,updated_at) VALUES (?,?,?,?,0,?)",
                (did, page_number, input_hash, page_asset, state["updated_at"]),
            )
        result = {
            "page": page_number,
            "asset": page_asset,
            "revision": record.revision,
            "complete": complete,
            "page_revision": state["page_revision"],
            "updated_at": state["updated_at"],
            "duration_s": duration_s + time.monotonic() - started,
        }
        database.append_event(
            record.job_id, did, "preview_ready", result, page=page_number
        )
        return result

    def compile(self, record, *, publish=True):
        result = self.compile_block_patch(record)
        if publish:
            home = result["page"]
            affected = {
                home,
                result.get("stamp_page") or home,
                result.get("previous_stamp_page") or home,
            }
            # 迁出的落点页先合成，主页最后合成（result 保留主页口径）。
            ordered = sorted(number for number in affected if number != home) + [home]
            for number in ordered:
                result.update(
                    self.compose_page_asset(
                        record,
                        number,
                        duration_s=result["duration_s"] if number == home else 0.0,
                    )
                )
            result["preview_asset"] = self.compose_full_preview(record)["asset"]
        return result

    def compile_blocks(self, record):
        """批量块编译：渲染并行、同页提交串行，最后每页只合成一次。

        每个块各自读当前草稿的 target/layout 覆盖（样式可以不一致）；单个块失败
        不中断其它块（失败清单进 error detail），全部完成后按受影响页各合成一次
        页资产、再合成一次完整预览——这是批编译省时间的主要来源。
        """
        from concurrent.futures import ThreadPoolExecutor

        started = time.monotonic()
        did, revision = record.did, record.revision
        workdir = self.store.resolve(did)
        draft = read_draft(workdir)
        if draft.revision != revision:
            raise ToolError("stale_job", "编译输入已过期")
        rows = {row.id: row for row in self._rows(did)}
        pids = list(record.paragraph_ids or [])
        if not pids:
            raise ToolError("compile_failed", "批量编译缺少 block 清单")
        workers = batch_workers_from_environ()
        # 进池前先把共享库建好：worker 里 read_draft 会各自开连接，库若在此刻
        # 才由某个线程首次创建，其余线程的建库脚本会撞上它的锁。
        database = self.store.database
        page_locks: dict[int, threading.Lock] = {}
        lock_guard = threading.Lock()
        results: dict[str, dict] = {}
        failures: list[dict] = []

        def page_lock(page):
            with lock_guard:
                if page not in page_locks:
                    page_locks[page] = threading.Lock()
                return page_locks[page]

        def one(pid):
            row = rows[pid]
            if record.cancel_requested_at is not None:
                return
            worker = record.model_copy()
            worker.paragraph_id = pid
            try:
                # 页锁交给编译器：只有依赖页状态的规划与提交持锁，渲染并行。
                result = self.compile_block_patch(worker, page_lock=page_lock(row.page))
                results[pid] = result
                self.store.database.append_event(
                    record.job_id,
                    did,
                    "block_compiled",
                    {
                        "paragraph_id": pid,
                        "page": result["page"],
                        "duration_s": round(result["duration_s"], 3),
                    },
                    block_id=pid,
                )
            except NotReplaced:
                # 按设计不替换（标签不合格/源文单行）：保留基线原文，不算失败。
                self._mark_not_replaced(did, pid, record.job_id)
            except ToolError as exc:
                failures.append(
                    {
                        "block_id": pid,
                        "code": exc.code,
                        "message": str(exc),
                    }
                )
            except Exception as exc:  # noqa: BLE001 - 单块失败不拖垮整批
                failures.append(
                    {"block_id": pid, "code": "compile_failed", "message": str(exc)}
                )

        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="block-batch"
        ) as pool:
            list(pool.map(one, pids))
        if record.cancel_requested_at is not None:
            raise ToolError("canceled", "任务已取消，批量编译结果未发布")
        if not results:
            raise ToolError("compile_failed", "批量编译全部失败", failures=failures)
        # 受影响页 = 每块的主页/贴片页/上一版贴片页；每页只合成一次。
        affected: set[int] = set()
        for result in results.values():
            home = result["page"]
            affected.add(home)
            affected.add(result.get("stamp_page") or home)
            affected.add(result.get("previous_stamp_page") or home)
        for number in sorted(affected):
            self.compose_page_asset(record, number)
        preview = self.compose_full_preview(record)
        if failures:
            raise ToolError(
                "compile_failed",
                f"{len(failures)} 个 block 编译失败（其余 {len(results)} 个已发布）",
                failures=failures,
                blocks=len(results),
                pages=sorted(affected),
            )
        return {
            "blocks": len(results),
            "pages": sorted(affected),
            "preview_asset": preview["asset"],
            "duration_s": time.monotonic() - started,
        }

    def compose_full_preview(self, record):
        import pymupdf

        database = self.store.database
        assets = AssetStore(self.store.store_base, database)
        with database._lock:
            rows = database.connection.execute(
                "SELECT p.page,p.page_asset,l.payload FROM pages p JOIN local_pages l "
                "ON p.document_id=l.document_id AND p.page=l.page WHERE p.document_id=? ORDER BY p.page",
                (record.did,),
            ).fetchall()
        if not rows:
            raise ToolError("export_not_ready", "尚无页面预览")
        baseline = json.loads(rows[0][2])["base"]
        temporary_root = self.store.store_base / "tmp"
        with tempfile.TemporaryDirectory(
            prefix=record.job_id + "-full-", dir=temporary_root
        ) as folder:
            path = Path(folder) / "preview.pdf"
            with pymupdf.open(assets.resolve(baseline)) as pdf:
                for number, digest, _ in rows:
                    page = pdf[number - 1]
                    empty = pdf.get_new_xref()
                    pdf.update_object(empty, "<<>>")
                    pdf.update_stream(empty, b"")
                    page.set_contents(empty)
                    with pymupdf.open(assets.resolve(digest)) as patch:
                        page.show_pdf_page(page.rect, patch, 0)
                pdf.save(path, garbage=4, deflate=True)
            asset = assets.put(path, kind="preview")
        with database._lock, database.connection:
            database.connection.execute("BEGIN IMMEDIATE")
            if (database.draft(record.did) or {"revision": 0})[
                "revision"
            ] != record.revision:
                raise ToolError("stale_job", "草稿已更新，完整预览未发布")
            self._check_job(database, record)
            database.connection.execute(
                "INSERT OR REPLACE INTO local_previews VALUES (?,?,?)",
                (record.did, asset, record.revision),
            )
        result = {
            "asset": asset,
            "preview_asset": asset,
            "revision": record.revision,
            "full": True,
        }
        database.append_event(record.job_id, record.did, "preview_ready", result)
        return result
