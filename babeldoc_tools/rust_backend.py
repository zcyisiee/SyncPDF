"""Thin ``bdt rust-translate`` adapter for the existing syncpdf CLI."""

from __future__ import annotations

import json
import math
import os
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path

_ARTIFACTS = ("translated.pdf", "events.jsonl", "stderr.log", "result.json")
_ENGINE = Path(__file__).resolve().parents[1] / "engine/target/release/syncpdf-cli"


def _error(code: str, message: str, **details: object) -> dict:
    return {"ok": False, "error": {"code": code, "message": message, **details}}


def _progress(event: dict) -> None:
    kind = event.get("type")
    if kind == "stage_started":
        sys.stderr.write(f"rust-translate: {event.get('stage')}\n")
    elif kind == "page_ready":
        page = event.get("page")
        if isinstance(page, int):
            sys.stderr.write(f"rust-translate: page {page} ready\n")


def translate_pdf(**kwargs: object) -> dict:
    try:
        return _translate_pdf(**kwargs)
    except OSError as exc:
        return _error("artifact_io", f"无法创建或写入本次运行产物：{exc}")


def _translate_pdf(
    *,
    pdf: str,
    workdir: str,
    pages: str | None,
    model: str,
    thinking: str,
    source_lang: str,
    target_lang: str,
    layout_device: str,
    engine: str | None,
    cached_from: str | None = None,
    font_scale: float = 1.0,
    line_height: float | None = None,
) -> dict:
    """Run the Rust CLI once, retaining its events and incomplete-result semantics."""
    for name, value in (("font_scale", font_scale), ("line_height", line_height)):
        if value is not None and (not math.isfinite(value) or value <= 0):
            return _error("invalid_typography", f"{name} 必须是有限正数倍数")
    source = Path(pdf).expanduser().resolve()
    destination = Path(workdir).expanduser().resolve()
    binary = Path(engine).expanduser().resolve() if engine else _ENGINE
    output = destination / "translated.pdf"

    if not source.is_file():
        return _error("input_missing", f"输入 PDF 不存在：{source}")
    if source.suffix.lower() != ".pdf":
        return _error("input_not_pdf", f"输入文件不是 PDF：{source}")
    if source == output.resolve():
        return _error("input_output_conflict", "输入 PDF 不能是 workdir/translated.pdf")
    if not binary.is_file() or not os.access(binary, os.X_OK):
        return _error(
            "engine_missing",
            f"Rust 引擎不可执行：{binary}；请先自行构建 release 或传入 --engine",
        )
    if any((destination / name).exists() for name in _ARTIFACTS):
        return _error("workdir_used", f"运行产物已存在，请指定新的 --workdir：{destination}")

    cached_db = Path(cached_from).expanduser().resolve() / "cache/translate.db" if cached_from else None
    if cached_db is not None and not cached_db.is_file():
        return _error("translation_cache_missing", f"找不到已保存译文缓存：{cached_db}")
    if cached_db is not None and cached_db == (destination / "cache/translate.db").resolve():
        return _error("cache_output_conflict", "缓存来源与本次运行目录不能相同")
    destination.mkdir(parents=True, exist_ok=True)
    temporary = destination / "tmp"
    temporary.mkdir(exist_ok=True)
    paths = {name: str(destination / name) for name in _ARTIFACTS}
    command = [
        str(binary), "translate", "--input", str(source), "--output", str(output),
        "--translator", "pi", "--model", model, "--thinking", thinking,
        "--source-lang", source_lang, "--target-lang", target_lang,
        "--cache-dir", str(destination / "cache"),
    ]
    if cached_db is not None:
        if (destination / "cache/translate.db").exists():
            return _error("workdir_used", "本次运行目录已有译文缓存，请指定新的目录")
        (destination / "cache").mkdir(exist_ok=True)
        try:
            with sqlite3.connect(cached_db.as_uri() + "?mode=ro", uri=True) as previous:
                with sqlite3.connect(destination / "cache/translate.db") as copied:
                    previous.backup(copied)
        except sqlite3.Error:
            return _error("translation_cache_invalid", "无法只读复制已有译文缓存")
        command.append("--cache-only")
    if pages is not None:
        command.extend(("--pages", pages))
    if font_scale != 1.0:
        command.extend(("--font-scale", str(font_scale)))
    if line_height is not None:
        command.extend(("--line-height", str(line_height)))
    child_env = os.environ.copy()
    child_env["SYNCPDF_LAYOUT_DEVICE"] = layout_device
    child_env["TMPDIR"] = str(temporary)

    status_by_id: dict[str, str] = {}
    paragraph_pages: dict[str, int] = {}
    ready_pages: set[int] = set()
    blocked_ids: set[str] = set()
    coverage_gap_pages: set[int] = set()
    finished: bool | None = None
    event_error = False
    exit_code: int | None = None
    launch_error = False
    try:
        with Path(paths["events.jsonl"]).open("x", encoding="utf-8") as events, Path(
            paths["stderr.log"]
        ).open("x", encoding="utf-8"
        ) as errors:
            try:
                process = subprocess.Popen(  # noqa: S603 - explicit engine path, argv without shell
                    command,
                    stdout=subprocess.PIPE,
                    stderr=errors,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=child_env,
                    start_new_session=True,
                )
            except OSError as exc:
                errors.write(f"启动 Rust 引擎失败：{exc}\n")
                launch_error = True
            else:
                assert process.stdout is not None
                try:
                    for line in process.stdout:
                        events.write(line)
                        events.flush()
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            event_error = True
                            continue
                        if not isinstance(event, dict):
                            event_error = True
                            continue
                        _progress(event)
                        kind = event.get("type")
                        if kind == "paragraph":
                            paragraph_id = event.get("paragraph_id")
                            status = event.get("status")
                            if isinstance(paragraph_id, str) and isinstance(status, str):
                                status_by_id[paragraph_id] = status
                                if isinstance(event.get("page"), int):
                                    paragraph_pages[paragraph_id] = event["page"]
                        elif kind == "page_ready" and isinstance(event.get("page"), int):
                            ready_pages.add(event["page"])
                        elif kind == "issue":
                            if event.get("code") in {
                                "protected_source_overlap", "translatable_region_overlap", "rotated_source_text",
                            } and isinstance(event.get("paragraph_id"), str):
                                blocked_ids.add(event["paragraph_id"])
                            if event.get("code") == "coverage_gap" and isinstance(event.get("page"), int):
                                coverage_gap_pages.add(event["page"])
                        elif kind == "run_finished":
                            finished = event.get("ok") if isinstance(event.get("ok"), bool) else None
                except (KeyboardInterrupt, OSError):
                    # The sidecar owns a pi child; interrupt the whole isolated job.
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                    raise
                exit_code = process.wait()
    except FileExistsError:
        return _error("workdir_used", f"运行日志已存在，请指定新的 --workdir：{destination}")

    prepared = sum(status == "typeset" for status in status_by_id.values())
    successful = sum(
        status == "typeset" and paragraph_pages.get(pid) in ready_pages
        for pid, status in status_by_id.items()
    )
    blocked = sum(status_by_id.get(pid) == "not_replaced" for pid in blocked_ids)
    unsuccessful = sum(status != "not_replaced" for status in status_by_id.values()) - successful + blocked
    data = {
        "typography": {"font_scale": font_scale, "line_height": line_height},
        "successful_blocks": successful,
        "typeset_blocks": prepared,
        "saved_pages": len(ready_pages),
        "unsuccessful_blocks": unsuccessful,
        "blocked_before_translation": blocked,
        "coverage_gap_pages": sorted(coverage_gap_pages),
        "not_replaced_blocks": sum(status == "not_replaced" for status in status_by_id.values()),
        "engine_exit_code": exit_code,
        "run_finished_ok": finished,
        "artifacts": paths,
        "output_exists": output.is_file(),
    }
    if launch_error:
        code, message = "engine_launch_failed", "Rust 引擎启动失败；详见 stderr.log"
    elif event_error:
        code, message = "engine_events_invalid", "Rust 引擎输出含无效事件；详见 events.jsonl"
    elif finished is None:
        code, message = "engine_result_missing", "Rust 引擎未发出有效 run_finished 事件"
    elif not finished or exit_code != 0 or unsuccessful or coverage_gap_pages:
        code, message = "engine_incomplete", "Rust 引擎未完整翻译；部分结果和事件已保留"
    elif not output.is_file():
        code, message = "output_missing", "Rust 引擎报告成功，但译文 PDF 不存在"
    else:
        code, message = "", ""
    result = {"ok": not code, "data": data} if not code else _error(code, message, **data)
    Path(paths["result.json"]).write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result
