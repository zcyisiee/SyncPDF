"""单线程翻译 runner：一次只跑一篇，把引擎事件翻成面向用户的进度。

:class:`EventMapper` 是纯函数式的映射（便于用真实 ``events.jsonl`` 回放测试）：

- 阶段 → 五步进度（读取文件 / 识别版面 / 翻译 / 排版 / 生成文件）与里程碑；
- ``page_ready`` → 每页完成（首次就绪才写一条文字；同页重发只刷新预览 ``rev``）；
- 保留原文的段落（回退，或翻译前就被判定不可替换的段）→ 页内虚线框与收尾提醒；
  ``coverage_gap`` 页 → 「少量内容未识别」提醒。

「保留原文」的判据与 :func:`babeldoc_tools.rust_backend._translate_pdf` 判定
``engine_incomplete`` 的口径一致，所以黄色的「部分完成」与提醒条数互相对得上。
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from babeldoc_tools import rust_backend
from babeldoc_tools.cloud.db import Paths
from babeldoc_tools.cloud.jobs import Service

__all__ = ["STEPS", "EventMapper", "Runner", "page_geometry"]

STEPS = ("读取文件", "识别版面", "翻译", "排版", "生成文件")
_STEP_OF = {
    "preflight": 0,
    "source_analysis": 0,
    "layout_analysis": 1,
    "paragraph_analysis": 1,
    "translating": 2,
    "typesetting": 3,
    "validating": 4,
    "publishing": 4,
}
#: 虚线框在段落外接框四周留的余量（pt）。
BOX_PAD = 3.0
_STDERR_TAIL = 4000

#: ``(页号 1 基, pdf_user 矩形 (x0, y0, x1, y1)) → 页面比例 [左, 上, 宽, 高]``。
ToFraction = Callable[[int, tuple[float, float, float, float]], list[float]]


def page_geometry(pdf: Path) -> ToFraction:
    """按原文每页的 PDF→页面变换，把 pdf_user 矩形换算成可见页面上的比例。"""
    import pymupdf

    with pymupdf.open(pdf) as doc:
        # transformation_matrix 只翻转 y 到未旋转页面；再乘 rotation_matrix 才落到可见（旋转后）页面
        pages = [
            (page.transformation_matrix * page.rotation_matrix, page.rect.width, page.rect.height)
            for page in doc
        ]

    def to_fraction(page: int, rect: tuple[float, float, float, float]) -> list[float]:
        matrix, width, height = pages[page - 1]
        x0, y0, x1, y1 = rect
        box = (pymupdf.Rect(x0 - BOX_PAD, y0 - BOX_PAD, x1 + BOX_PAD, y1 + BOX_PAD) * matrix).normalize()
        box &= pymupdf.Rect(0, 0, width, height)
        return [round(v, 5) for v in (box.x0 / width, box.y0 / height, box.width / width, box.height / height)]

    return to_fraction


def _rect(event: dict) -> tuple[float, float, float, float] | None:
    bbox = event.get("source_bbox")
    if isinstance(bbox, dict):
        return (bbox["x0"], bbox["y0"], bbox["x1"], bbox["y1"])
    boxes = event.get("boxes") or []
    if not boxes:
        return None
    return (
        min(b["x0"] for b in boxes), min(b["y0"] for b in boxes),
        max(b["x1"] for b in boxes), max(b["y1"] for b in boxes),
    )


class EventMapper:
    """一次运行（attempt）的引擎事件 → ``(kind, payload)`` 用户事件。"""

    def __init__(self, pages: int, attempt: int, to_fraction: ToFraction) -> None:
        self.pages = pages
        self.attempt = attempt
        self.to_fraction = to_fraction
        self.step = 0
        self.status: dict[str, str] = {}
        self.page_of: dict[str, int] = {}
        self.rect: dict[str, tuple[float, float, float, float]] = {}
        self.blocked: set[str] = set()
        self.gaps: set[int] = set()
        self.ready: set[int] = set()
        self.published = False
        self.fatal: str | None = None
        self._half = False

    def start(self) -> list[tuple[str, dict]]:
        return [("status", {"status": "running"}), ("milestone", {"text": "开始处理"}), ("step", {"step": 0})]

    def feed(self, event: dict) -> list[tuple[str, dict]]:
        out: list[tuple[str, dict]] = []
        kind = event.get("type")
        if kind == "stage_started":
            step = _STEP_OF.get(event.get("stage"))
            if step is not None and step > self.step:
                if self.step < 1 <= step:
                    out.append(("milestone", {"text": f"解析完成，共 {self.pages} 页"}))
                if self.step < 2 <= step:
                    out.append(("milestone", {"text": "版面识别完成"}))
                    out.append(("milestone", {"text": "开始翻译"}))
                self.step = step
                out.append(("step", {"step": step}))
        elif kind == "paragraph":
            pid, page = event.get("paragraph_id"), event.get("page")
            if isinstance(pid, str) and isinstance(page, int):
                self.status[pid] = str(event.get("status"))
                self.page_of[pid] = page
                rect = _rect(event)
                if rect is not None:
                    self.rect[pid] = rect
        elif kind == "issue":
            if event.get("code") in rust_backend.BLOCKED_ISSUE_CODES and isinstance(
                event.get("paragraph_id"), str
            ):
                self.blocked.add(event["paragraph_id"])
            if event.get("code") == "coverage_gap" and isinstance(event.get("page"), int):
                self.gaps.add(event["page"])
        elif kind == "page_ready" and isinstance(event.get("page"), int):
            page = event["page"]
            first = page not in self.ready
            self.ready.add(page)
            payload = {
                "page": page,
                "rev": f"{self.attempt}.{event.get('revision', 0)}",
                "done": len(self.ready),
                "boxes": self._boxes(page, final=False),
            }
            if first:
                payload["text"] = "第一页译文已就绪" if len(self.ready) == 1 else f"第 {page} 页已完成"
                payload["first"] = len(self.ready) == 1
            out.append(("page", payload))
            if not self._half and len(self.ready) * 2 >= self.pages:
                self._half = True
                out.append(("milestone", {"text": "完成 50%"}))
        elif kind == "document_finished":
            self.published = True
        elif kind == "error" and event.get("fatal"):
            self.fatal = str(event.get("message") or event.get("code") or "")
        return out

    def _retained(self, pid: str, final: bool) -> bool:
        status = self.status[pid]
        if pid in self.blocked and status == "not_replaced":
            return True
        if status == "fallback":
            return True
        # 收尾时，没能在已发布页上排版成功的可译段落也算保留原文。
        return final and status != "not_replaced" and not (
            status == "typeset" and self.page_of[pid] in self.ready
        )

    def _boxes(self, page: int, final: bool) -> list[list[float]]:
        return [
            self.to_fraction(page, self.rect[pid])
            for pid, on in sorted(self.page_of.items())
            if on == page and pid in self.rect and self._retained(pid, final)
        ]

    def finish(self, status: str, elapsed_s: float) -> tuple[list[tuple[str, dict]], dict]:
        """终态收尾事件与 ``stats``（``status`` 是 ``done|partial|failed``）。"""
        stats: dict = {"pages": self.pages, "elapsed_s": round(elapsed_s, 1)}
        if status == "failed":
            events = [
                ("error", {"text": "翻译没有完成", "sub": "处理中出现错误，本次不计入今日额度，可以稍后重试"}),
                ("step", {"step": self.step, "fail": True}),
            ]
            return events, stats
        retained: dict[int, int] = {}
        for pid, page in self.page_of.items():
            if self._retained(pid, final=True):
                retained[page] = retained.get(page, 0) + 1
        events: list[tuple[str, dict]] = []
        warn_pages = sorted(set(retained) | self.gaps)
        for page in warn_pages:
            if page in retained:
                events.append(("warn", {
                    "page": page,
                    "text": f"第 {page} 页有 {retained[page]} 段保留原文",
                    "sub": "这几段暂未替换成译文，已用虚线框标出",
                }))
            if page in self.gaps:
                events.append(("warn", {
                    "page": page,
                    "text": f"第 {page} 页有少量内容未识别",
                    "sub": "未识别的部分保持原样，不影响其他内容",
                }))
        events += [
            ("milestone", {"text": "全部完成"}),
            ("step", {"step": len(STEPS)}),
            ("milestone", {"text": "可以下载"}),
        ]
        boxes = {str(page): self._boxes(page, final=True) for page in sorted(retained)}
        stats.update({
            "warnings": sum(kind == "warn" for kind, _ in events),
            "warn_pages": warn_pages,
            "boxes": {page: b for page, b in boxes.items() if b},
        })
        return events, stats


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tail(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-_STDERR_TAIL:]
    except OSError:
        return ""


class Runner:
    """后台单线程：取最早排队的 translation 跑完，再取下一篇。"""

    def __init__(
        self,
        service: Service,
        paths: Paths,
        *,
        engine: str | None,
        translator: str,
        layout_device: str,
    ) -> None:
        self.service = service
        self.paths = paths
        self.engine = engine
        self.translator = translator
        self.layout_device = layout_device
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._current: str | None = None
        self._process: subprocess.Popen | None = None
        self._canceled: set[str] = set()
        self._thread: threading.Thread | None = None
        service.on_enqueue = self._wake.set
        service.on_cancel_running = self.cancel

    # ---- 生命周期 ------------------------------------------------------------ #
    def recover(self) -> None:
        """上次异常退出留下的 running 放回队首；残留 workdir 全部清掉（每次从头跑）。"""
        self.service.recover()
        for stale in self.paths.work.iterdir():
            shutil.rmtree(stale, ignore_errors=True)

    def start(self) -> None:
        self.recover()
        self._thread = threading.Thread(target=self._loop, name="bdt-cloud-runner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停服：终止当前引擎，translation 保持 running，下次启动由 :meth:`recover` 重排。"""
        self._stop.set()
        self._wake.set()
        with self._lock:
            process = self._process
        if process is not None:
            rust_backend.terminate(process)
        if self._thread is not None:
            self._thread.join(timeout=30)

    def cancel(self, tid: str) -> None:
        """名下已无人等待的运行中 translation：终止引擎（异步，不阻塞请求）。"""
        with self._lock:
            self._canceled.add(tid)
            process = self._process if self._current == tid else None
        if process is not None:
            threading.Thread(target=rust_backend.terminate, args=(process,), daemon=True).start()

    def _on_spawn(self, tid: str, process: subprocess.Popen) -> None:
        with self._lock:
            self._process = process
            canceled = tid in self._canceled
        if canceled:  # 启动前一刻被取消
            threading.Thread(target=rust_backend.terminate, args=(process,), daemon=True).start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                if self.run_once():
                    continue
            except Exception as exc:  # noqa: BLE001 - runner 线程不能因单篇失败退出
                sys.stderr.write(f"bdt cloud: runner 出错：{exc!r}\n")
                time.sleep(5)
            self._wake.wait(timeout=30)

    # ---- 单篇 --------------------------------------------------------------- #
    def run_once(self) -> bool:
        """跑一篇；队列为空返回 ``False``。"""
        tr = self.service.claim_next()
        if tr is None:
            return False
        tid = tr["id"]
        with self._lock:
            self._current = tid
            self._process = None
        try:
            self._run(tr)
        finally:
            with self._lock:
                self._current = None
                self._process = None
                self._canceled.discard(tid)
        return True

    def _run(self, tr) -> None:
        tid = tr["id"]
        source = self.paths.source(tr["source_sha"])
        work = self.paths.work / tid
        shutil.rmtree(work, ignore_errors=True)
        started = time.monotonic()
        mapper = EventMapper(tr["pages"], tr["attempt"], page_geometry(source))
        self.service.emit(tid, mapper.start())

        def on_event(event: dict) -> None:
            out = mapper.feed(event)
            if out:
                self.service.emit(tid, out)

        try:
            result = rust_backend.translate_pdf(
                pdf=str(source),
                workdir=str(work),
                pages=None,
                model=tr["model"],
                thinking=tr["thinking"],
                source_lang="auto",
                target_lang="zh-CN",
                layout_device=self.layout_device,
                engine=self.engine,
                font_scale=1.0,
                line_height=1.5,
                translator=self.translator,
                on_event=on_event,
                on_spawn=lambda process: self._on_spawn(tid, process),
            )
        except Exception as exc:  # noqa: BLE001 - 回调或 I/O 失败都按本篇失败处理
            result = {"ok": False, "error": {"code": "runner_error", "message": repr(exc)}}

        if self._stop.is_set():
            return  # 停服中断：保持 running，重启后 recover 重新排队
        with self._lock:
            canceled = tid in self._canceled
        if canceled:
            shutil.rmtree(work, ignore_errors=True)
            if self.service.has_waiting_jobs(tid):  # 取消后又有人加入：重新排队
                self.service.finish(tid, "queued", events=[("milestone", {"text": "任务将重新开始"})])
            else:
                self.service.finish(tid, "canceled", events=[])
            return

        output = work / "translated.pdf"
        error = result.get("error") or {}
        if result.get("ok"):
            status = "done"
        elif error.get("code") == "engine_incomplete" and mapper.published and output.is_file():
            status = "partial"
        else:
            status = "failed"
        events, stats = mapper.finish(status, time.monotonic() - started)
        keep = self.paths.translations / tid
        keep.mkdir(parents=True, exist_ok=True)
        translated_sha = None
        if status != "failed":
            translated_sha = _sha256(output)
            output.replace(self.paths.translated(tid))
        raw = work / "events.jsonl"
        if raw.is_file():
            with raw.open("rb") as src, gzip.open(keep / "events.jsonl.gz", "wb") as dst:
                shutil.copyfileobj(src, dst)
        summary = {"ok": bool(result.get("ok")), "code": error.get("code"), "message": error.get("message")}
        data = result.get("data") or error
        for key in ("successful_blocks", "saved_pages", "unsuccessful_blocks", "coverage_gap_pages", "engine_exit_code"):
            if key in data:
                summary[key] = data[key]
        if status == "failed":
            summary["stderr_tail"] = _tail(work / "stderr.log")
            summary["fatal"] = mapper.fatal
            stats["error"] = error.get("code")
        with contextlib.suppress(OSError):
            shutil.rmtree(work)
        self.service.finish(
            tid, status, events=events, stats=stats, result=summary, translated_sha=translated_sha
        )

