"""``bdt serve`` 的响应模型与公共契约词表。

只有**已实现**端点才在这里建模（W01 ``/api/v1/health``；W02 ``/api/v1/documents``
六个只读端点）；后续端点的字段形状冻结在 ``docs/frontend/api.md``，实现时再落到
本模块 —— 不预置假 stub。模型里的 ``dict`` 字段承载产物内部结构：产物是信任边界内
的本地文件，不在这里逐字段复刻它们的 schema。

HTTP 层约定（与 ``bdt`` CLI 的 stdout 信封不同）：

- 成功：直接返回资源 JSON（无 ``{"ok": true}`` 包装）；
- 失败：:class:`ErrorEnvelope`，即 ``{"error": {"code", "message", "detail"?}}``，
  其中 ``detail`` 缺省不出现。

时间一律 UTC ISO8601（毫秒 + ``Z``，见 api.md §1）。``run_state.json`` 里
``run.py::_now()`` 写的是本地墙钟无时区值，视图层统一归一后再进这些模型。
"""

from __future__ import annotations

from typing import Any
from typing import Literal

from babeldoc.debug_recorder.model import PDF_TOPLEFT
from pydantic import BaseModel

#: 全部端点前缀（唯一拼写来源：路由、CLI banner、api.md）。
API_PREFIX = "/api/v1"

#: 坐标系统标识（api.md §3.1 几何约定）。``PDF_TOPLEFT`` 复用采集层常量：
#: debug 归档的 box 一律 PDF point、左上原点、y 向下。
COORD_SYSTEM_PARSE = PDF_TOPLEFT
#: ``layout_geometry.json`` 的 box 是 BabelDOC IL 原生坐标（左下原点、y 向上）。
COORD_SYSTEM_LAYOUT = "pdf_native"

#: 阶段名（与 ``babeldoc_tools/run.py::STAGES`` 一致，api.md §3.1 已冻结）。
STAGES = ("parse", "translate", "apply", "build", "check", "review", "report")

#: 阶段在 ``run_state.json`` / manifest 里都没有记录时的状态（不谎报 ok）。
STAGE_NOT_RUN = "not_run"

__all__ = [
    "API_PREFIX",
    "COORD_SYSTEM_LAYOUT",
    "COORD_SYSTEM_PARSE",
    "STAGES",
    "STAGE_NOT_RUN",
    "CheckAvailability",
    "CheckResponse",
    "CompileStatus",
    "DocumentAvailability",
    "DocumentDetail",
    "DocumentListItem",
    "DocumentPdf",
    "ErrorBody",
    "ErrorEnvelope",
    "GeometryResponse",
    "HealthResponse",
    "ParagraphItem",
    "PdfOutput",
    "QualityCheck",
    "QualityReviewer",
    "QualityStatus",
    "StageStateItem",
    "StageStateResponse",
]


class ErrorBody(BaseModel):
    """机读错误：``code`` 稳定可分支，``message`` 面向人。"""

    code: str
    message: str
    detail: dict[str, Any] | None = None


class ErrorEnvelope(BaseModel):
    """所有非 2xx 响应的统一形状。"""

    error: ErrorBody


class HealthResponse(BaseModel):
    """``GET /api/v1/health``：进程存活 + 当前可见文档数。"""

    status: Literal["ok"] = "ok"
    api: str = API_PREFIX
    #: ``bdt`` 工具层版本（与 ``bdt --version`` 相同）。
    version: str
    #: ``root`` = 枚举子目录；``workdir`` = 只公开 --workdir 那一个文档。
    mode: Literal["root", "workdir"]
    #: 实际被枚举的根目录（绝对路径，已 resolve）。
    root: str
    documents: int


# --------------------------------------------------------------------------- #
# W02：只读文档端点（api.md §3.1 前六个）
# --------------------------------------------------------------------------- #
class DocumentListItem(BaseModel):
    """文档列表项 / 详情里的公共计数部分（``GET /api/v1/documents``）。

    ``None`` = 对应产物缺失或损坏（不是 0）：缺 ``translated.jsonl`` 时
    ``translated_count`` 为 null，产物存在但没译文才是 0。
    """

    did: str
    #: 源 PDF 文件名（不含扩展名）。产物里没有真正的标题字段，拿不到就 ``None``。
    title: str | None = None
    #: 页数（``layout_geometry.pages``，缺则快照最大页码）。
    pages: int | None = None
    #: 段落数（``layout_geometry.paragraphs`` 条数，缺则快照实体数）。
    paragraph_count: int | None = None
    #: 已译段数（``translated.jsonl`` 里 target 非空的行数）。
    translated_count: int | None = None
    #: 7 个固定阶段 → ``run_state``/manifest 里的状态；没有记录 → ``not_run``。
    stage_summary: dict[str, str]
    #: 最近活动时间：最新 run 的 ``finished_at`` → ``created_at`` → run_state，UTC。
    updated_at: str | None = None


class PdfOutput(BaseModel):
    """``run_state`` 记录的 PDF 产物（相对 workdir 的路径）。"""

    path: str
    #: 路径仍在 workdir 内且确实存在（越界或不存在的只报事实，不读内容）。
    exists: bool
    bytes: int | None = None


class DocumentPdf(BaseModel):
    """详情里的 PDF 来源与产物路径。"""

    #: ``run_state.pdf``（``bdt run`` 收到的 ``--pdf``）；没有记录 → null。
    source: str | None = None
    outputs: list[PdfOutput] = []


class QualityCheck(BaseModel):
    """质量门禁里的 check 结论（api.md §3.2）。

    ``verdict`` 取 ``run_state.quality.check.verdict``（check 阶段合并 review_verdict +
    layout_lint + link_audit 后的**门禁结论**），缺失才退回 ``review_verdict.json``
    自己的 verdict（那是结构审查的结论，不含 lint/link）。
    ``blockers``/``warnings`` 是 ``review_verdict.json`` 的原样列表。
    """

    #: ``pass`` | ``needs_fix``；没有任何可用结论 → ``not_available``。
    verdict: str
    blockers: list[Any] = []
    warnings: list[Any] = []
    #: check 判 ``needs_fix`` 的原因（``run_state.quality.check.reasons``）。
    reasons: list[Any] = []
    at: str | None = None


class QualityReviewer(BaseModel):
    """reviewer 结论（api.md §3.2）。

    只取 ``run_state.quality.reviewer``：它有时写 ``status``
    （``waiting_for_reviewer`` / ``needs_human_review`` / ``needs_fix``），有时只有
    ``verdict``（``pass`` / ``needs_fix``，来自 ``agent/agent_review.json``）。
    两个都没有（review 阶段没跑过）→ ``not_run``。
    """

    #: not_run | waiting_for_reviewer | pass | needs_fix | needs_human_review
    status: str
    #: 修复轮计数（``retranslate`` / ``layout``）。
    fix_rounds: dict[str, int] = {}
    at: str | None = None


class QualityStatus(BaseModel):
    """``quality``：门禁结论 + reviewer 结论 + 能否算成功。

    ``pipeline_ok`` 只有在 check 门禁 ``pass`` **且** reviewer ``pass`` 时才为 true：
    缺任一侧结论、或任一侧非绿都按 false 报（"编译成功"不得让它变 true）。
    """

    check: QualityCheck
    reviewer: QualityReviewer
    pipeline_ok: bool


class CompileStatus(BaseModel):
    """``compile``：服务端管理的草稿编译产物（api.md §3.2）。

    W02 没有真实编译产物（草稿编译在 W09）：固定 ``status="none"`` / ``revision=0`` /
    ``stale=false`` / ``artifact=null``。``run_state`` 里 ``bdt run`` 直接产出的 PDF
    不是编译 revision，已放在 :attr:`DocumentDetail.pdf`，不在这里冒充编译结果。
    """

    status: Literal["none", "running", "ok", "failed"] = "none"
    revision: int = 0
    stale: bool = False
    artifact: dict[str, Any] | None = None


class DocumentAvailability(BaseModel):
    """哪些 workdir 产物可用（缺失或损坏 → false，对应字段为 null）。"""

    run_state: bool
    anchors: bool
    translated: bool
    geometry: bool
    parse_snapshot: bool
    review_verdict: bool
    layout_lint: bool
    link_audit: bool


class DocumentDetail(DocumentListItem):
    """``GET /api/v1/documents/{did}``：列表项字段 + 元信息。"""

    pdf: DocumentPdf
    #: ``run_state.config`` 原样（``bdt run`` 的本次运行配置）；没有 → null。
    config: dict[str, Any] | None = None
    quality: QualityStatus
    compile: CompileStatus
    available: DocumentAvailability


class StageStateItem(BaseModel):
    """单个阶段的状态与时间。"""

    stage: str
    #: ``run_state`` / manifest 记的状态（``ok`` / ``waiting`` / ``running`` …）；
    #: 两处都没有记录 → ``not_run``。
    status: str
    #: ``run_state`` 的 ``ok`` 布尔；没有就由 ``status == "ok"`` 推导，都缺 → null。
    ok: bool | None = None
    started_at: str | None = None
    finished_at: str | None = None
    #: ``run_state.stages.<stage>.duration_s``（真实实测耗时）；没有 → null。
    duration_s: float | None = None
    #: ``started_at``/``finished_at`` 的来源：``manifest``（recorder 记录的真实起止）
    #: 或 ``run_state``（只有完成时刻 ``at``，起点由 ``at`` - 耗时反推）。
    timing_source: Literal["manifest", "run_state"] | None = None


class StageStateResponse(BaseModel):
    """``GET /api/v1/documents/{did}/stage-state``：7 个固定阶段。"""

    did: str
    #: 起止时间取自哪个 run；没有 run 归档 → null。
    run_id: str | None = None
    stages: list[StageStateItem]


class ParagraphItem(BaseModel):
    """``GET /api/v1/documents/{did}/paragraphs`` 的单个段落（多产物 join）。"""

    id: str
    #: 1 基 PDF 页码（``anchors.json`` 的 0 基页码已在视图层归一）；无来源 → null。
    page: int | None = None
    layout_label: str | None = None
    #: 原文：``anchors.json::canonical``，缺则 parse 快照的 ``attrs.unicode``。
    source: str | None = None
    #: 译文（``translated.jsonl``）；没有该段译文 → null（不是空串）。
    target: str | None = None
    #: ``layout_geometry.json`` 里该段的完整记录（PDF 原生坐标、y 向上）；
    #: 没有对应记录 → null。
    geometry: dict[str, Any] | None = None
    #: ``layout_lint.json`` 该段最重缺陷级别（``P0``/``P1``/``P2``）；
    #: 无缺陷 ``ok``；lint 产物不可用 ``not_available``。
    layout_status: str


class GeometryResponse(BaseModel):
    """``GET /api/v1/documents/{did}/geometry``：bbox 数据。

    ``kind=parse`` 用 ``run_id``/``entities``/``relations``（快照，``pdf_topleft``）；
    ``kind=layout`` 用 ``pages``/``paragraphs``/``page_info``（几何产物，``pdf_native``）。
    两类的 box 坐标系不同且**不做转换**，前端按 ``coord_system`` 自行换算。
    """

    did: str
    kind: Literal["parse", "layout"]
    coord_system: Literal["pdf_topleft", "pdf_native"]
    #: 请求里的页码过滤（1 基）；没过滤 → null。
    page: int | None = None
    #: kind=parse：实体来自哪个 run。
    run_id: str | None = None
    #: kind=parse：段落实体（``{id, kind, label, page, box:{x0,y0,x1,y1}, attrs}``）。
    entities: list[dict[str, Any]] = []
    #: kind=parse：实体关系（page 过滤时只留两端在筛后实体里的关系）。
    relations: list[dict[str, Any]] = []
    #: kind=layout：产物里的总页数。
    pages: int | None = None
    #: kind=layout：段落几何记录（``src_box``/``layout_box``/``rendered_box``/字号…）。
    paragraphs: list[dict[str, Any]] = []
    #: kind=layout：每页 cropbox / layout_regions，供前端做坐标换算。
    page_info: list[dict[str, Any]] = []


class CheckAvailability(BaseModel):
    """check 三源产物各自是否可用（旧 workdir 可能缺某项）。"""

    review: bool
    lint: bool
    link: bool


class CheckResponse(BaseModel):
    """``GET /api/v1/documents/{did}/check``：三源原样透传（不裁剪字段）。"""

    did: str
    #: ``agent/review_verdict.json``（结构审查）。
    review_verdict: dict[str, Any] | None = None
    #: ``agent/layout_lint.json``（排版 lint）。
    layout_lint: dict[str, Any] | None = None
    #: ``agent/link_audit.json``（链接审计）。
    link_audit: dict[str, Any] | None = None
    available: CheckAvailability
