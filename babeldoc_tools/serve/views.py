"""把 workdir 产物组装成 ``docs/frontend/api.md`` §3.1 六个端点的视图。

分工：:mod:`babeldoc_tools.serve.workdir` 只负责"读产物"，本模块只负责"拼视图"
（多产物 join、缺侧补 null、时间归一成 UTC），HTTP 参数与响应模型在
:mod:`babeldoc_tools.serve.routers.documents`。全部只读，不写任何文件。

三条硬规则：

- **缺产物不报错**：某一侧产物缺失时对应字段 ``null``（详情端点另有 ``available``
  标志）；只有"整份数据都不存在"才抛 404 语义的 ``ToolError``
  （``snapshot_unavailable`` / ``geometry_unavailable`` / ``paragraphs_unavailable``）。
- **不静默转换坐标**：parse 快照是 ``pdf_topleft``、layout 几何是 ``pdf_native``，
  各自在 ``coord_system`` 里标注，换算留给前端。
- **不造假**：``compile`` 的真源是 ``<workdir>/.bdt-serve/compile.json``（W09 草稿
  编译写出；从没编译过才是 status="none"/revision=0）；阶段时间只来自 ``run_state`` /
  manifest，不编造百分比或 ETA。

页码统一 **1 基 PDF 页码**：``layout_geometry`` / parse 快照本身就是 1 基，只有
``anchors.json`` 是 0 基（``markdown_view`` 直接写 IL 的 ``page_number``），
仅在只回退到 anchors 时 +1 归一（见 :func:`_paragraph_page`）。
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any
from typing import Literal

from babeldoc_tools.common import ToolError
from babeldoc_tools.serve.compile import compile_status
from babeldoc_tools.serve.schemas import COORD_SYSTEM_LAYOUT
from babeldoc_tools.serve.schemas import COORD_SYSTEM_PARSE
from babeldoc_tools.serve.schemas import STAGE_NOT_RUN
from babeldoc_tools.serve.schemas import STAGES
from babeldoc_tools.serve.schemas import CheckAvailability
from babeldoc_tools.serve.schemas import CheckResponse
from babeldoc_tools.serve.schemas import DocumentAvailability
from babeldoc_tools.serve.schemas import DocumentDetail
from babeldoc_tools.serve.schemas import DocumentListItem
from babeldoc_tools.serve.schemas import DocumentPdf
from babeldoc_tools.serve.schemas import GeometryResponse
from babeldoc_tools.serve.schemas import ParagraphItem
from babeldoc_tools.serve.schemas import PdfOutput
from babeldoc_tools.serve.schemas import QualityCheck
from babeldoc_tools.serve.schemas import QualityReviewer
from babeldoc_tools.serve.schemas import QualityStatus
from babeldoc_tools.serve.schemas import StageStateItem
from babeldoc_tools.serve.schemas import StageStateResponse
from babeldoc_tools.serve.workdir import WorkdirReader

__all__ = [
    "check_view",
    "document_detail",
    "document_summary",
    "geometry_layout",
    "geometry_parse",
    "paragraphs",
    "stage_state",
]

#: ``layout_lint`` 的严重级别（``findings[].sev``）从重到轻。
_LAYOUT_SEVERITY_ORDER = ("P0", "P1", "P2")

#: 段落无 lint 缺陷 / lint 产物不可用时的 ``layout_status``。
_PARAGRAPH_LAYOUT_OK = "ok"
_PARAGRAPH_LAYOUT_UNAVAILABLE = "not_available"

#: 质量门禁结论词表（api.md §3.2）。
_CHECK_NOT_AVAILABLE = "not_available"

#: reviewer ``verdict`` → api.md 的 ``status`` 词表（只有这两个同形）。
_REVIEWER_VERDICT_STATUS = {"pass": "pass", "needs_fix": "needs_fix"}

#: 没有 ``fix_rounds`` 记录时的默认值（``run.py::_fix_rounds`` 的两个 kind）。
_NO_FIX_ROUNDS = {"retranslate": 0, "layout": 0}


# --------------------------------------------------------------------------- #
# 时间归一
# --------------------------------------------------------------------------- #
def _utc_iso(value: Any) -> str | None:
    """产物时间戳 → UTC ISO8601（毫秒 + ``Z``）；缺失/无法解析 → ``None``。

    ``run_state.json`` 的 ``at`` / ``updated_at`` 由 ``run.py::_now()`` 写成
    **本地墙钟、无时区**（``datetime.now().isoformat()``），而 api.md §1 要求一律
    UTC：naive 值按写盘机器的本地时间解释后转 UTC（``bdt`` 是本地 CLI，产物与
    服务同一台机器）。``manifest.json`` 的时间戳自带 ``+00:00``，原样归一。
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()  # naive = 本地墙钟 → 带时区
    return (
        parsed.astimezone(datetime.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _iso_before(iso: str | None, seconds: float | None) -> str | None:
    """``iso`` 往前推 ``seconds`` 秒（``at`` 是阶段完成时刻，起点靠耗时反推）。"""
    if iso is None or seconds is None:
        return None
    try:
        finished = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _utc_iso((finished - datetime.timedelta(seconds=seconds)).isoformat())


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_float(value: Any) -> float | None:
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _rows_by_id(rows: Any) -> dict[str, dict]:
    """``[{id, ...}]`` → ``{id: row}``（非 dict / 无 id 的行丢弃）。"""
    by_id: dict[str, dict] = {}
    if not isinstance(rows, list):
        return by_id
    for row in rows:
        if isinstance(row, dict):
            paragraph_id = row.get("id")
            if isinstance(paragraph_id, str) and paragraph_id:
                by_id[paragraph_id] = row
    return by_id


# --------------------------------------------------------------------------- #
# 阶段
# --------------------------------------------------------------------------- #
def _recorded_stages(reader: WorkdirReader) -> tuple[dict, str | None, dict]:
    """``(run_state.stages, 最新 run_id, 该 run manifest 的 stages)``。"""
    state = reader.run_state()
    recorded = state.get("stages") if isinstance(state.get("stages"), dict) else {}
    latest = reader.latest_manifest()
    if latest is None:
        return recorded, None, {}
    run_id, manifest = latest
    stages = manifest.get("stages") if isinstance(manifest.get("stages"), dict) else {}
    return recorded, run_id, stages


def _stage_status(entry: Any, measured: Any) -> str | None:
    """阶段状态：``run_state`` 优先，其次 manifest；都没有 → ``None``。"""
    for source in (entry, measured):
        if isinstance(source, dict):
            status = _as_str(source.get("status"))
            if status is not None:
                return status
    return None


def _stage_summary(recorded: dict, manifest_stages: dict) -> dict[str, str]:
    """7 个固定阶段 → 状态（没有记录 → ``not_run``，不谎报 ok）。"""
    summary: dict[str, str] = {}
    for stage in STAGES:
        summary[stage] = (
            _stage_status(recorded.get(stage), manifest_stages.get(stage))
            or STAGE_NOT_RUN
        )
    return summary


def _stage_at(state: dict, stage: str) -> str | None:
    """``run_state.stages.<stage>.at``（= 该阶段完成时刻）归一成 UTC。"""
    stages = state.get("stages") if isinstance(state.get("stages"), dict) else {}
    entry = stages.get(stage) if isinstance(stages.get(stage), dict) else {}
    return _utc_iso(entry.get("at"))


def _stage_state_item(stage: str, entry: Any, measured: Any) -> StageStateItem:
    entry = entry if isinstance(entry, dict) else {}
    measured = measured if isinstance(measured, dict) else {}
    status = _stage_status(entry, measured)
    ok = entry.get("ok") if isinstance(entry.get("ok"), bool) else None
    if ok is None and status is not None:
        ok = status == "ok"
    duration = _as_float(entry.get("duration_s"))
    started_at = finished_at = None
    timing_source: Literal["manifest", "run_state"] | None = None
    if measured.get("started_at") and measured.get("finished_at"):
        # recorder 归档的真实起止时间（UTC）。
        timing_source = "manifest"
        started_at = _utc_iso(measured.get("started_at"))
        finished_at = _utc_iso(measured.get("finished_at"))
    elif _utc_iso(entry.get("at")):
        # manifest 没有这段：只有完成时刻 + 实测耗时，起点由两者反推。
        timing_source = "run_state"
        finished_at = _utc_iso(entry.get("at"))
        started_at = _iso_before(finished_at, duration)
    return StageStateItem(
        stage=stage,
        status=status or STAGE_NOT_RUN,
        ok=ok,
        started_at=started_at,
        finished_at=finished_at,
        duration_s=duration,
        timing_source=timing_source,
    )


def stage_state(reader: WorkdirReader, did: str) -> StageStateResponse:
    """``GET /api/v1/documents/{did}/stage-state``（7 阶段 + 真实起止时间）。"""
    recorded, run_id, manifest_stages = _recorded_stages(reader)
    items = [
        _stage_state_item(stage, recorded.get(stage), manifest_stages.get(stage))
        for stage in STAGES
    ]
    return StageStateResponse(did=did, run_id=run_id, stages=items)


# --------------------------------------------------------------------------- #
# 计数与 meta
# --------------------------------------------------------------------------- #
def _counts(reader: WorkdirReader) -> tuple[int | None, int | None, int | None]:
    """``(页数, 段数, 已译段数)``；产物缺失 → ``None``（不是 0）。"""
    geometry = reader.layout_geometry()
    paragraphs = geometry.get("paragraphs") if isinstance(geometry, dict) else None
    pages = _as_int(geometry.get("pages")) if isinstance(geometry, dict) else None
    paragraph_count = len(paragraphs) if isinstance(paragraphs, list) else None
    if pages is None or paragraph_count is None:
        snapshot = reader.parse_snapshot()
        entities = snapshot[1].get("entities") if snapshot is not None else None
        if isinstance(entities, list):
            if pages is None:
                page_numbers = [
                    entity["page"]
                    for entity in entities
                    if isinstance(entity, dict)
                    and _as_int(entity.get("page")) is not None
                ]
                pages = max(page_numbers) if page_numbers else None
            if paragraph_count is None:
                paragraph_count = len(entities)
    targets = reader.translated_targets()
    translated_count = (
        sum(1 for target in targets.values() if target) if targets is not None else None
    )
    return pages, paragraph_count, translated_count


def _title(state: dict, manifest: dict | None) -> str | None:
    """文档标题 = 源 PDF 文件名（不含扩展名）；取不到就 ``None``（不造假）。

    来源顺序：``run_state.pdf``（``bdt run`` 收到的 ``--pdf``）→ 最新 run 的
    ``manifest.input.pdf.path``（recorder 记录的输入）。产物里没有真正的标题字段。
    """
    candidates = [state.get("pdf")]
    inputs = manifest.get("input") if isinstance(manifest, dict) else None
    pdf_input = inputs.get("pdf") if isinstance(inputs, dict) else None
    if isinstance(pdf_input, dict):
        candidates.append(pdf_input.get("path"))
    for candidate in candidates:
        text = _as_str(candidate)
        if text is None:
            continue
        stem = Path(text).stem
        if stem:
            return stem
    return None


def _updated_at(state: dict, manifest: dict | None) -> str | None:
    """最近活动时间：最新 run 的 ``finished_at`` → ``created_at`` → run_state。"""
    if isinstance(manifest, dict):
        for key in ("finished_at", "created_at"):
            stamp = _utc_iso(manifest.get(key))
            if stamp:
                return stamp
    return _utc_iso(state.get("updated_at"))


def document_summary(reader: WorkdirReader, did: str) -> DocumentListItem:
    """``GET /api/v1/documents`` 的一项。"""
    state = reader.run_state()
    recorded, _, manifest_stages = _recorded_stages(reader)
    latest = reader.latest_manifest()
    manifest = latest[1] if latest is not None else None
    pages, paragraph_count, translated_count = _counts(reader)
    return DocumentListItem(
        did=did,
        title=_title(state, manifest),
        pages=pages,
        paragraph_count=paragraph_count,
        translated_count=translated_count,
        stage_summary=_stage_summary(recorded, manifest_stages),
        updated_at=_updated_at(state, manifest),
    )


def _pdf_outputs(reader: WorkdirReader, state: dict) -> list[PdfOutput]:
    """``run_state`` 各阶段 ``artifacts`` 里指向 PDF 的产物（去重、按阶段顺序）。"""
    stages = state.get("stages") if isinstance(state.get("stages"), dict) else {}
    outputs: list[PdfOutput] = []
    seen: set[str] = set()
    for stage in STAGES:
        entry = stages.get(stage)
        artifacts = entry.get("artifacts") if isinstance(entry, dict) else None
        if not isinstance(artifacts, dict):
            continue
        for relative in artifacts.values():
            path_text = _as_str(relative)
            if path_text is None or not path_text.lower().endswith(".pdf"):
                continue
            if path_text in seen:
                continue
            seen.add(path_text)
            outputs.append(_pdf_output(reader, path_text))
    return outputs


def _pdf_output(reader: WorkdirReader, relative: str) -> PdfOutput:
    """产物路径 → 是否存在 + 大小（只 ``stat``，不读内容）。"""
    candidate = (reader.workdir / relative).resolve()
    root = reader.workdir.resolve()
    inside = candidate != root and root in candidate.parents
    if not inside:
        return PdfOutput(path=relative, exists=False)
    try:
        is_file = candidate.is_file()
        stat = candidate.stat() if is_file else None
    except OSError:
        return PdfOutput(path=relative, exists=False)
    return PdfOutput(
        path=relative, exists=is_file, bytes=stat.st_size if stat is not None else None
    )


def _reviewer_status(recorded: dict) -> str:
    """``run_state.quality.reviewer`` → api.md 的 ``status`` 词表。

    只用 ``run_state``：``status`` 优先（``waiting_for_reviewer`` /
    ``needs_human_review`` / ``needs_fix``），否则由 ``verdict`` 映射
    （``pass`` / ``needs_fix``）。``review_verdict.json`` 是 check 阶段的结构审查
    结论，不是 reviewer 的，不能拿来顶替。
    """
    status = _as_str(recorded.get("status"))
    if status is not None:
        return status
    verdict = _as_str(recorded.get("verdict"))
    if verdict in _REVIEWER_VERDICT_STATUS:
        return _REVIEWER_VERDICT_STATUS[verdict]
    return STAGE_NOT_RUN


def _fix_rounds(recorded: dict, reviewer: dict) -> dict[str, int]:
    for source in (recorded.get("fix_rounds"), reviewer.get("fix_rounds")):
        if isinstance(source, dict):
            rounds = {
                key: value
                for key, value in source.items()
                if isinstance(key, str) and _as_int(value) is not None
            }
            if rounds:
                return rounds
    return dict(_NO_FIX_ROUNDS)


def _quality(reader: WorkdirReader, state: dict) -> QualityStatus:
    """``quality``：门禁结论 + reviewer 结论（api.md §3.2，编译状态分开报）。"""
    recorded = state.get("quality") if isinstance(state.get("quality"), dict) else {}
    check = recorded.get("check") if isinstance(recorded.get("check"), dict) else {}
    reviewer = (
        recorded.get("reviewer") if isinstance(recorded.get("reviewer"), dict) else {}
    )
    review_verdict = reader.review_verdict() or {}
    verdict = _as_str(check.get("verdict"))
    if verdict is None:
        # run_state 没有门禁结论时退回结构审查的 verdict（没有 lint/link 结论）。
        verdict = _as_str(review_verdict.get("verdict"))
    blockers = (
        review_verdict.get("blockers")
        if isinstance(review_verdict.get("blockers"), list)
        else []
    )
    warnings = (
        review_verdict.get("warnings")
        if isinstance(review_verdict.get("warnings"), list)
        else []
    )
    reasons = check.get("reasons") if isinstance(check.get("reasons"), list) else []
    status = _reviewer_status(reviewer)
    return QualityStatus(
        check=QualityCheck(
            verdict=verdict or _CHECK_NOT_AVAILABLE,
            blockers=blockers,
            warnings=warnings,
            reasons=reasons,
            at=_stage_at(state, "check"),
        ),
        reviewer=QualityReviewer(
            status=status,
            fix_rounds=_fix_rounds(recorded, reviewer),
            at=_stage_at(state, "review"),
        ),
        # 门禁与 reviewer 都绿才算成功；任一缺失/非绿都是 false（编译成功不算成功）。
        pipeline_ok=verdict == "pass" and status == "pass",
    )


def document_detail(reader: WorkdirReader, did: str) -> DocumentDetail:
    """``GET /api/v1/documents/{did}``：只报能从现有产物推导的字段。"""
    state = reader.run_state()
    latest = reader.latest_manifest()
    manifest = latest[1] if latest is not None else None
    recorded, _, manifest_stages = _recorded_stages(reader)
    summary = document_summary(reader, did)
    snapshot = reader.parse_snapshot()
    anchors = reader.anchors()
    return DocumentDetail(
        **summary.model_dump(),
        pdf=DocumentPdf(
            source=_as_str(state.get("pdf")), outputs=_pdf_outputs(reader, state)
        ),
        config=state.get("config") if isinstance(state.get("config"), dict) else None,
        quality=_quality(reader, state),
        # 编译状态的真源是 <workdir>/.bdt-serve/compile.json（W09）；从来没有编译过
        # → status=none/revision=0。stale 由当前草稿 revision 算（api.md §3.2）。
        compile=compile_status(reader.workdir),
        available=DocumentAvailability(
            run_state=bool(state),
            anchors=anchors is not None,
            translated=reader.translated_targets() is not None,
            geometry=reader.layout_geometry() is not None,
            parse_snapshot=snapshot is not None,
            review_verdict=reader.review_verdict() is not None,
            layout_lint=reader.layout_lint() is not None,
            link_audit=reader.link_audit() is not None,
        ),
    )


# --------------------------------------------------------------------------- #
# 段落
# --------------------------------------------------------------------------- #
def _lint_severities(lint: dict | None) -> dict[str, set[str]] | None:
    """``{段落 id: {sev, ...}}``；``layout_lint.json`` 不可用 → ``None``。"""
    if lint is None:
        return None
    findings = lint.get("findings")
    severities: dict[str, set[str]] = {}
    if not isinstance(findings, list):
        return severities
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        paragraph_id = _as_str(finding.get("id"))
        severity = _as_str(finding.get("sev"))
        if paragraph_id and severity:
            severities.setdefault(paragraph_id, set()).add(severity)
    return severities


def _layout_status(paragraph_id: str, severities: dict[str, set[str]] | None) -> str:
    if severities is None:
        return _PARAGRAPH_LAYOUT_UNAVAILABLE
    found = severities.get(paragraph_id)
    if not found:
        return _PARAGRAPH_LAYOUT_OK
    for severity in _LAYOUT_SEVERITY_ORDER:
        if severity in found:
            return severity
    return sorted(found)[0]  # 未知级别：如实回报，不假装 ok


def _paragraph_page(
    geometry: dict | None, entity: dict | None, anchor: dict | None
) -> int | None:
    """段落页码（响应统一 1 基）：几何/快照直接可用，anchors 是 0 基要 +1。"""
    for row in (geometry, entity):
        page = _as_int(row.get("page")) if isinstance(row, dict) else None
        if page is not None:
            return page
    page = _as_int(anchor.get("page")) if isinstance(anchor, dict) else None
    return page + 1 if page is not None else None


def _paragraph_source(anchor: dict | None, entity: dict | None) -> str | None:
    """原文：``anchors.canonical`` 优先，退回快照 ``attrs.unicode``。"""
    canonical = _as_str(anchor.get("canonical")) if isinstance(anchor, dict) else None
    if canonical is not None:
        return canonical
    attrs = entity.get("attrs") if isinstance(entity, dict) else None
    return _as_str(attrs.get("unicode")) if isinstance(attrs, dict) else None


def _paragraph_label(
    geometry: dict | None, entity: dict | None, anchor: dict | None
) -> str | None:
    for row, key in (
        (geometry, "layout_label"),
        (anchor, "layout_label"),
        (entity, "label"),
    ):
        if isinstance(row, dict):
            label = _as_str(row.get(key))
            if label is not None:
                return label
    return None


def _paragraph_ids(*sources: Any) -> list[str]:
    """以几何产物为主序、其余产物补缺的稳定 id 序列（保持各产物的文件顺序）。"""
    ordered: dict[str, None] = {}
    for source in sources:
        if isinstance(source, dict):
            for key in source:
                ordered.setdefault(key, None)
        elif isinstance(source, list):
            for row in source:
                if isinstance(row, dict):
                    paragraph_id = _as_str(row.get("id"))
                    if paragraph_id:
                        ordered.setdefault(paragraph_id, None)
    return list(ordered)


def paragraphs(reader: WorkdirReader, page: int | None = None) -> list[ParagraphItem]:
    """``GET /api/v1/documents/{did}/paragraphs``：多产物按 id 左连接。

    id 集取各产物并集（几何产物通常最全，排在前面 = 文档顺序）：数量不一致是
    常态（``anchors``/``translated`` 只覆盖参与翻译的段落），缺侧字段补 ``null``。
    """
    geometry = reader.layout_geometry()
    geometry_rows = geometry.get("paragraphs") if isinstance(geometry, dict) else None
    geometry_by_id = _rows_by_id(geometry_rows)
    anchors = reader.anchors()
    anchor_by_id = _rows_by_id(
        anchors.get("rows") if isinstance(anchors, dict) else None
    )
    snapshot = reader.parse_snapshot()
    entity_by_id = _rows_by_id(
        snapshot[1].get("entities") if snapshot is not None else None
    )
    targets = reader.translated_targets() or {}
    severities = _lint_severities(reader.layout_lint())

    ids = _paragraph_ids(geometry_by_id, anchor_by_id, entity_by_id, targets)
    if not ids:
        raise ToolError(
            "paragraphs_unavailable",
            f"文档 {reader.workdir.name} 没有可用的段落产物（anchors.json / "
            "layout_geometry.json / parse 快照 / translated.jsonl 都不存在）",
            did=reader.workdir.name,
        )
    items: list[ParagraphItem] = []
    for paragraph_id in ids:
        geometry_row = geometry_by_id.get(paragraph_id)
        entity = entity_by_id.get(paragraph_id)
        anchor = anchor_by_id.get(paragraph_id)
        paragraph_page = _paragraph_page(geometry_row, entity, anchor)
        if page is not None and paragraph_page != page:
            continue
        items.append(
            ParagraphItem(
                id=paragraph_id,
                page=paragraph_page,
                layout_label=_paragraph_label(geometry_row, entity, anchor),
                source=_paragraph_source(anchor, entity),
                target=_as_str(targets.get(paragraph_id)),
                geometry=geometry_row,
                layout_status=_layout_status(paragraph_id, severities),
            )
        )
    return items


# --------------------------------------------------------------------------- #
# 几何
# --------------------------------------------------------------------------- #
def geometry_parse(
    reader: WorkdirReader, did: str, page: int | None = None
) -> GeometryResponse:
    """``geometry?kind=parse``：最新 run 的 parse 段落实体（``pdf_topleft``）。

    没有快照 → ``snapshot_unavailable``（404），不拿空数组冒充成功。
    """
    found = reader.parse_snapshot()
    if found is None:
        raise ToolError(
            "snapshot_unavailable",
            f"文档 {did} 没有 parse 段落快照"
            "（debug/runs/<run_id>/snapshots/parse/paragraphs.json）",
            did=did,
        )
    run_id, snapshot = found
    entities = [e for e in snapshot.get("entities") or [] if isinstance(e, dict)]
    relations = [r for r in snapshot.get("relations") or [] if isinstance(r, dict)]
    if page is not None:
        entities = [e for e in entities if _as_int(e.get("page")) == page]
        kept = {entity.get("id") for entity in entities}
        relations = [
            relation
            for relation in relations
            if relation.get("from_id") in kept or relation.get("to_id") in kept
        ]
    return GeometryResponse(
        did=did,
        kind="parse",
        coord_system=COORD_SYSTEM_PARSE,
        page=page,
        run_id=run_id,
        entities=entities,
        relations=relations,
    )


def geometry_layout(
    reader: WorkdirReader, did: str, page: int | None = None
) -> GeometryResponse:
    """``geometry?kind=layout``：段落排版几何（``pdf_native``：左下原点、y 向上）。

    box 不做转换，只标注 ``coord_system``，并附 ``page_info`` 的 cropbox 供前端换算。
    """
    geometry = reader.layout_geometry()
    rows = geometry.get("paragraphs") if isinstance(geometry, dict) else None
    if not isinstance(rows, list):
        raise ToolError(
            "geometry_unavailable",
            f"文档 {did} 没有 layout 几何产物（agent/layout_geometry.json）",
            did=did,
        )
    paragraphs_rows = [row for row in rows if isinstance(row, dict)]
    page_info = [
        row for row in (geometry.get("page_info") or []) if isinstance(row, dict)
    ]
    if page is not None:
        paragraphs_rows = [
            row for row in paragraphs_rows if _as_int(row.get("page")) == page
        ]
        page_info = [row for row in page_info if _as_int(row.get("page")) == page]
    return GeometryResponse(
        did=did,
        kind="layout",
        coord_system=COORD_SYSTEM_LAYOUT,
        page=page,
        pages=_as_int(geometry.get("pages")),
        paragraphs=paragraphs_rows,
        page_info=page_info,
    )


# --------------------------------------------------------------------------- #
# 检查
# --------------------------------------------------------------------------- #
def check_view(reader: WorkdirReader, did: str) -> CheckResponse:
    """``GET /api/v1/documents/{did}/check``：三源原样透传 + 可用性标志。"""
    review = reader.review_verdict()
    lint = reader.layout_lint()
    link = reader.link_audit()
    return CheckResponse(
        did=did,
        review_verdict=review,
        layout_lint=lint,
        link_audit=link,
        available=CheckAvailability(
            review=review is not None,
            lint=lint is not None,
            link=link is not None,
        ),
    )
