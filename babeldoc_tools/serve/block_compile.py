"""Single-block stamp rendering with revision-fenced, immutable page publication.

No pipeline subprocess or workdir snapshot is used here.  A request renders exactly
one StampRequest; page redaction/link restoration reuse the existing overlay code.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
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


def hydrate_parse(workdir, temporary):
    """Resolve migrated immutable parser inputs without any legacy absolute path."""
    import shutil
    import zipfile

    from babeldoc_tools.serve.database import MetadataDB
    from babeldoc_tools.serve.migrate import SNAPSHOT_FILES

    base = next(
        (p for p in (workdir, workdir.parent) if (p / "app.db").is_file()), None
    )
    if base is None:
        return workdir
    database = MetadataDB(base)
    try:
        row = database.connection.execute(
            "SELECT snapshot_asset,prepared_pdf_asset FROM parse_results WHERE document_id=? AND status='ready'",
            (workdir.name,),
        ).fetchone()
        if not row or not row[0] or not row[1]:
            return workdir
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


def render_request(workdir, pid, target, box, temporary, cache_root, capability=None):
    """Restore parsed typography, apply only this translation in memory, then fuse.

    ``capability``：可选的已探测 LaTeX 能力（流式预览每段一编时由调用方缓存一次
    传入）；``None`` = 现场探测（单段编辑编译的冷路径，语义与旧版一致）。
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
    source_fonts = next(({font.font_id: font for font in page.pdf_font}
                         for page in state["doc"].page
                         if paragraph in page.pdf_paragraph), {})
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
    translator = workflow.ILTranslator(
        workflow.SheetProtocolTranslator(state["lang_in"], state["lang_out"], True),
        config,
    )
    tracker = workflow.PageTranslateTracker()
    translator.post_translate_paragraph(
        paragraph, tracker.new_paragraph(), source_input, target
    )
    config.provider_ir_dir = workdir / "agent"
    config.latex_source_pdf_path = str(source)
    config.latex_source_geometry = state.get("source_line_geometry") or {}
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
    if bold:
        body = r"{\bfseries " + body + "}"
    if italic:
        body = r"{\itshape " + body + "}"
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
            "font_size": meta["font_size"],
        }
        jobs = engine._substitute_fragments([job], temporary)
        if len(jobs) != 1:
            raise ToolError("compile_failed", "公式片段无法恢复")
        request = engine._stamp_request(jobs[0])
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
            if record.effective_scope not in ("block", "export"):
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
        return document_blocks(self.store, did)

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
            operation = (
                self.export if record.effective_scope == "export" else self.compile
            )
            result = await asyncio.to_thread(operation, record)
            self.runner.registry.mark_finished(
                record, status="succeeded", envelope=json.dumps(result)
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
                "target": edit.target
                if edit and edit.target is not None
                else row.target,
                "layout": edit.layout if edit else None,
            }
            if patches.get(pid, {}).get("input") != expected:
                dirty.append(pid)
        for pid in sorted(dirty):
            record.paragraph_id = pid
            self.compile(record)
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
                pdf.save(path)
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

    def compile(self, record):
        import pymupdf
        from babeldoc.format.pdf.document_il.backend.latex_bbox.overlay import (
            LatexBboxOverlay,
        )
        from babeldoc.format.pdf.document_il.backend.latex_bbox.overlay import (
            _links_by_signature,
        )

        started = time.monotonic()
        did, pid, revision = record.did, record.paragraph_id, record.revision
        workdir = self.store.resolve(did)
        draft = read_draft(workdir)
        if draft.revision != revision:
            raise ToolError("stale_job", "编译输入已过期")
        rows = self._rows(did)
        row = next(item for item in rows if item.id == pid)
        edit = draft.paragraphs.get(pid)
        geometry = row.geometry or {}
        box = (edit.layout or {}).get("box") if edit else None
        box = box or geometry.get("layout_box") or geometry.get("src_box")
        target = edit.target if edit and edit.target is not None else row.target
        if target is None or row.page is None or box is None:
            raise ToolError("compile_failed", "缺少译文或排版数据")
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
        # Capture base once. Subsequent local edits always compose from immutable pages.
        with database._lock:
            previous = database.connection.execute(
                "SELECT payload FROM local_pages WHERE document_id=? AND page=?",
                (did, row.page),
            ).fetchone()
        state = (
            json.loads(previous[0])
            if previous
            else {"base": assets.put(outputs[0], kind="baseline"), "patches": {}}
        )
        with pymupdf.open(assets.resolve(state["base"])) as baseline:
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
                stamp, hit = render_request(
                    workdir,
                    pid,
                    target,
                    box,
                    temporary,
                    self.store.store_base / "cache/stamps",
                    capability=self.capability(),
                )
                stamp_asset = assets.put(Path(stamp.pdf_path), kind="stamp")
                state["patches"][pid] = {
                    "asset": stamp_asset,
                    "box": box,
                    "old_box": geometry.get("rendered_box")
                    or geometry.get("layout_box")
                    or geometry.get("src_box"),
                    "input": {
                        "target": target,
                        "layout": edit.layout if edit else None,
                    },
                }
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

                    for key, patch in state["patches"].items():
                        # Redact the old footprint before stamping into the new bbox.
                        old_rect = flip(patch["old_box"] or patch["box"])
                        jobs.append({"debug_id": key, "page": 0, "rect": old_rect})
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
                    for job in jobs:
                        job["rect"] = flip(state["patches"][job["debug_id"]]["box"])
                    engine._stamp_pages(jobs, successful)
                    if not engine._verify_links(before, uris):
                        raise ToolError("compile_failed", "链接校验失败，已保留旧页面")
                    result_path = temporary / "page.pdf"
                    composed.save(result_path)
                    page_asset = assets.put(result_path, kind="page")
                    with database._lock:
                        latest = database.connection.execute(
                            "SELECT asset_sha256 FROM local_previews WHERE document_id=?",
                            (did,),
                        ).fetchone()
                    preview_base = assets.resolve(latest[0]) if latest else outputs[0]
                    with pymupdf.open(preview_base) as preview_pdf:
                        changed_page = preview_pdf[index]
                        empty = preview_pdf.get_new_xref()
                        preview_pdf.update_object(empty, "<<>>")
                        preview_pdf.update_stream(empty, b"")
                        changed_page.set_contents(empty)
                        changed_page.show_pdf_page(changed_page.rect, composed, 0)
                        preview_path = temporary / "preview.pdf"
                        preview_pdf.save(preview_path)
                    preview_asset = assets.put(preview_path, kind="preview")
                finally:
                    composed.close()
        input_hash = hashlib.sha256(
            json.dumps(state, sort_keys=True).encode()
        ).hexdigest()
        state["page_asset"] = page_asset
        with database._lock, database.connection:
            database.connection.execute("BEGIN IMMEDIATE")
            current = database.draft(did)
            if (current or {"revision": 0})["revision"] != revision:
                raise ToolError("stale_job", "草稿已更新，旧编译结果未发布")
            if record.cancel_requested_at is not None or record.status != "running":
                raise ToolError("stale_job", "任务已取消，编译结果未发布")
            self._check_job(database, record)
            database.connection.execute(
                "INSERT OR REPLACE INTO local_pages VALUES (?,?,?)",
                (did, row.page, json.dumps(state)),
            )
            database.connection.execute(
                "INSERT OR REPLACE INTO local_previews VALUES (?,?,?)",
                (did, preview_asset, revision),
            )
            database.connection.execute(
                "INSERT OR REPLACE INTO pages(document_id,page,input_hash,page_asset,dirty) VALUES (?,?,?,?,0)",
                (did, row.page, input_hash, page_asset),
            )
            database.connection.execute(
                "INSERT OR REPLACE INTO compile_blocks(document_id,block_id,input_hash,patch_asset,status,target_size) VALUES (?,?,?,?,?,?)",
                (did, pid, input_hash, stamp_asset, "ok", stamp.font_size),
            )
        result = {
            "block_id": pid,
            "page": row.page,
            "revision": revision,
            "asset": page_asset,
            "preview_asset": preview_asset,
            "cache_hit": hit,
            "duration_s": time.monotonic() - started,
        }
        database.append_event(
            record.job_id, did, "preview_ready", result, block_id=pid, page=row.page
        )
        return result
