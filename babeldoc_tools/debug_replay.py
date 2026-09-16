"""旧 workdir 回放：把 ``agent/`` 历史产物打包成一个 ``mode=replay`` 的 debug run。

只读 JSON/Markdown/PDF 复制——不反序列化 ``state.pkl``、不调模型、不编译。
无法唯一确定的证据（源 PDF、输出 PDF）如实标注 ``unavailable``，可用
``bdt debug --source-pdf/--mono`` 显式绑定（写 ``debug/bindings.json``）。

产出（``<workdir>/debug/runs/<run_id>/``）：

- ``parse/page-frames`` + ``parse/paragraphs`` + ``parse/selection`` 快照
  （由 layout_geometry + anchors 重建，字段标 ``replayed``）；
- ``translate/texts/replay`` 快照 + ``text_version(phase=replayed)`` 事件
  （由 translated.jsonl 重建）；
- ``build/typesetting_geometry`` 快照（原样拷贝，标 pre-overlay 参照）；
- ``artifacts/``：apply_report / layout_lint / link_audit / review_verdict /
  latex_bbox_report / reconstruct_report 原样复制，mono/dual/source PDF 绑定；
- manifest：``mode=replay``、``source=legacy_workdir``、``unavailable`` 列表、
  ``events_available=false``（历史目录没有事件流，逐候选树不可回放）。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from babeldoc import debug_recorder as _dr

#: replay 快照里挂在数据上的统一标注键。
REPLAY_NOTE = "replayed from legacy agent/ artifacts"


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    except OSError:
        return []
    return rows


def _il_to_topleft(box, height):
    """IL 左下原点框 → 左上原点 ``{x0,y0,x1,y1}``（与采集侧坐标约定一致）。"""
    if not box or len(box) != 4:
        return None
    x0, y0, x1, y1 = (float(v) for v in box)
    return {"x0": x0, "y0": float(height) - y1, "x1": x1, "y1": float(height) - y0}


def _atomic_write_json(path: Path, payload) -> None:
    """原子写 JSON：先写同目录临时文件再 ``Path.replace``（与采集侧一致）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _find_unique(workdir: Path, patterns) -> Path | None:
    """``output/*.mono.pdf`` 这类自动发现：唯一候选才绑定。"""
    hits: list[Path] = []
    for pattern in patterns:
        hits.extend(sorted(workdir.glob(pattern)))
    hits = [h for h in hits if h.is_file()]
    return hits[0] if len(hits) == 1 else None


def _resolve_bound(workdir: Path, explicit, bindings: dict, key: str,
                   patterns) -> tuple[Path | None, bool]:
    """→ (path, uncertain)：显式 > bindings.json > 唯一自动发现。"""
    if explicit:
        return Path(explicit), False
    bound = bindings.get(key)
    if bound:
        return Path(bound), False
    found = _find_unique(workdir, patterns)
    return found, found is None  # True = 存在多个/没有 → 不确定


def build_replay_run(
    workdir,
    *,
    source_pdf: str | None = None,
    mono: str | None = None,
) -> str | None:
    """在 ``<workdir>/debug/runs/`` 生成 replay run；返回 ``run_id``。

    调用方负责先拿写锁。``agent/`` 不存在（没有任何可回放产物）→ ``None``。
    """
    workdir = Path(workdir)
    agent = workdir / "agent"
    anchors = _read_json(agent / "anchors.json")
    geometry = _read_json(agent / "layout_geometry.json")
    translated_rows = _read_jsonl(agent / "translated.jsonl")
    report = _read_json(agent / "latex_bbox_report.json")
    if anchors is None and geometry is None and translated_rows == []:
        return None  # 没有可回放的最小证据集

    bindings = _read_json(workdir / "debug" / "bindings.json") or {}
    src_path, src_uncertain = _resolve_bound(
        workdir, source_pdf, bindings, "source_pdf",
        ["agent/source.pdf", "output/*.en.pdf"],
    )
    mono_path, mono_uncertain = _resolve_bound(
        workdir, mono, bindings, "mono", ["output/*.mono.pdf"],
    )
    dual_path, _ = _resolve_bound(
        workdir, None, bindings, "dual", ["output/*.dual.pdf"],
    )

    unavailable = []
    if src_uncertain:
        unavailable.append("source_pdf")
    if mono_uncertain:
        unavailable.append("mono_pdf")
    # 识别阶段原始证据（provider 响应/字符层/布局框）不可回放
    unavailable += ["layout_snapshot", "native_chars", "events_stream"]

    run_id = _dr.new_run_id()
    run_dir = workdir / "debug" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    recorder = _dr.DebugRecorder(run_dir)
    try:
        recorder.set_config(stages=["replay"], options={})
        recorder.record_event(
            "replay",
            "replay_notice",
            {
                "note": REPLAY_NOTE,
                "source": "legacy_workdir",
                "unavailable": unavailable,
                "events_available": False,
            },
        )

        # ------------------------------------------------ parse（尽力重建）
        recorder.start_stage("parse")
        recorder.record_event("parse", "stage_started", {"replayed": True})
        page_info = (geometry or {}).get("page_info") or []
        frames = []
        heights: dict[int, float] = {}
        for i, info in enumerate(page_info):
            crop = info.get("cropbox") or [0, 0, 612, 792]
            w = round(float(crop[2]) - float(crop[0]), 3)
            h = round(float(crop[3]) - float(crop[1]), 3)
            heights[i] = h
            frames.append(
                {
                    "page_index": i,
                    "width": w,
                    "height": h,
                    "cropbox": crop,
                    "rotation": 0,
                    "page_number_original": i + 1,
                    "page_number_displayed": i + 1,
                    "replayed": True,
                }
            )
        if frames:
            recorder.write_snapshot(
                "parse", "page-frames",
                {
                    "version": 1,
                    "coord_system": _dr.PDF_TOPLEFT,
                    "replayed": True,
                    "frames": frames,
                },
            )

        para_entities = []
        anchor_by_id = {}
        for row in (anchors or {}).get("rows") or []:
            if isinstance(row, dict) and row.get("id"):
                anchor_by_id[row["id"]] = row
        for para in (geometry or {}).get("paragraphs") or []:
            pid = para.get("id")
            if not pid:
                continue
            page = int(para.get("page") or 0)
            height = heights.get(page - 1, 792.0)
            box = _il_to_topleft(
                para.get("src_box") or para.get("layout_box"), height
            )
            anchor = anchor_by_id.get(pid) or {}
            para_entities.append(
                {
                    "id": pid,
                    "kind": "paragraph",
                    "label": para.get("layout_label")
                    or anchor.get("layout_label")
                    or "text",
                    "page": page,
                    "box": box,
                    "attrs": {
                        "unicode": anchor.get("canonical") or para.get("text"),
                        "replayed": True,
                    },
                }
            )
        if para_entities:
            recorder.write_snapshot(
                "parse", "paragraphs",
                {
                    "version": 1,
                    "coord_system": _dr.PDF_TOPLEFT,
                    "replayed": True,
                    "entities": para_entities,
                    "relations": [],
                },
            )

        if anchors is not None:
            selected = [
                {
                    "id": row.get("id"),
                    "page": row.get("page"),
                    "source": row.get("canonical") or row.get("markdown"),
                    "layout_label": row.get("layout_label"),
                    "replayed": True,
                }
                for row in anchors.get("rows") or []
                if isinstance(row, dict)
            ]
            skipped = [
                {
                    "id": row.get("id"),
                    "page": row.get("page"),
                    "source": row.get("source") or row.get("canonical"),
                    "layout_label": row.get("layout_label"),
                    "replayed": True,
                }
                for row in anchors.get("skipped") or []
                if isinstance(row, dict)
            ]
            recorder.write_snapshot(
                "parse", "selection",
                {
                    "replayed": True,
                    "selected": selected,
                    "skipped": skipped,
                },
            )
        if src_path and src_path.is_file():
            recorder.archive_file("parse", "input.pdf", src_path)
        recorder.record_event(
            "parse", "stage_finished",
            {"status": "ok", "replayed": True},
        )
        recorder.finish_stage("parse")

        # ------------------------------------------------ translate
        if translated_rows:
            recorder.start_stage("translate")
            recorder.record_event(
                "translate", "stage_started", {"replayed": True}
            )
            rows = [
                {
                    "id": row.get("id"),
                    "target": row.get("target"),
                    "matched": True,
                    "replayed": True,
                }
                for row in translated_rows
            ]
            snapshot = recorder.write_snapshot(
                "translate", "texts/replay", {"replayed": True, "rows": rows}
            )
            recorder.record_event(
                "translate", "text_version",
                {
                    "phase": "replayed",
                    "rows": len(rows),
                    "snapshot": snapshot,
                    "note": "历史目录只保留最终写回文本",
                },
            )
            recorder.record_event(
                "translate", "stage_finished",
                {"status": "ok", "replayed": True},
            )
            recorder.finish_stage("translate")

        # ------------------------------------------------ apply / build / check
        for stage, names in (
            ("apply", ["apply_report.json"]),
            ("build", ["reconstruct_report.json", "latex_bbox_report.json",
                        "layout_geometry.json"]),
            ("check", ["review_verdict.json", "layout_lint.json",
                        "link_audit.json", "agent_review.json"]),
        ):
            recorder.start_stage(stage)
            recorder.record_event(stage, "stage_started", {"replayed": True})
            for name in names:
                path = agent / name
                if path.is_file():
                    recorder.archive_file(stage, name, path)
            if stage == "build":
                if geometry is not None:
                    payload = dict(geometry)
                    payload["note"] = "pre-overlay reference; replayed"
                    recorder.write_snapshot(
                        "build", "typesetting_geometry", payload
                    )
                if mono_path and mono_path.is_file():
                    recorder.archive_file("build", "mono.pdf", mono_path)
                if dual_path and dual_path.is_file():
                    recorder.archive_file("build", "dual.pdf", dual_path)
            recorder.record_event(
                stage, "stage_finished",
                {"status": "ok", "replayed": True},
            )
            recorder.finish_stage(stage)

        # ------------------------------------------------ manifest 标注
        # finish() 会重写 manifest：在其之后一次性读取 + 标注 + 原子写回。
        recorder.finish("finished")
        manifest = _read_json(recorder.run_dir / "manifest.json") or {}
        manifest["mode"] = "replay"
        manifest["source"] = "legacy_workdir"
        manifest["unavailable"] = unavailable
        manifest["events_available"] = False
        _atomic_write_json(recorder.run_dir / "manifest.json", manifest)
    finally:
        recorder.close()
    return run_id


def replay_run_needed(workdir) -> bool:
    """``debug/runs/`` 为空但 ``agent/`` 有历史产物 → 可回放。"""
    workdir = Path(workdir)
    runs = workdir / "debug" / "runs"
    if runs.is_dir() and any(
        child.is_dir() for child in runs.iterdir()
    ):
        return False
    return (workdir / "agent").is_dir()
