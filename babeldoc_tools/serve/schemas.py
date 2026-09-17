"""``bdt serve`` 的响应模型与公共契约词表。

只有**已实现**端点才在这里建模（W01 ``/api/v1/health``；W02 ``/api/v1/documents``
六个只读端点；W03 事件分页与产物清单）；后续端点的字段形状冻结在
``docs/frontend/api.md``，实现时再落到本模块 —— 不预置假 stub。SSE 帧是
``text/event-stream`` 文本行，不走 pydantic（见
:mod:`babeldoc_tools.serve.routers.events`）。模型里的 ``dict`` 字段承载产物内部
结构：产物是信任边界内的本地文件，不在这里逐字段复刻它们的 schema。

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
from pydantic import Field
from pydantic import model_validator

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

# --------------------------------------------------------------------------- #
# W07：jobs（api.md §3.4）
# --------------------------------------------------------------------------- #
#: action 的**固定四值**（api.md §3.4）。
JOB_ACTIONS = ("run", "retranslate", "compile", "check")
#: W07 真正实现的 action；另外两个诚实报 422 ``action_not_available``，不冒充。
JOB_ACTIONS_IMPLEMENTED = ("run", "check")
#: 未实现 action → 归属任务（写进 422 的 detail，前端好排期）。
JOB_ACTION_PHASE = {"retranslate": "W11", "compile": "W09"}

#: ``from`` 取值 = 7 个阶段（只有一个来源 :data:`STAGES`）。
JOB_FROM_PATTERN = "^(" + "|".join(STAGES) + ")$"
#: ``pages`` 形状（``bdt run --pages`` 接受的 ``"1-3,5"``；真实解析仍是 CLI 自己的事）。
JOB_PAGES_PATTERN = r"^[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*$"
#: profile id 形状（api.md §3.4：只接 id，不接命令/密钥）。
JOB_PROFILE_PATTERN = r"^[a-z0-9-]{1,64}$"

__all__ = [
    "API_PREFIX",
    "ArtifactItem",
    "ArtifactsResponse",
    "COORD_SYSTEM_LAYOUT",
    "COORD_SYSTEM_PARSE",
    "DocumentUploaded",
    "JOB_ACTIONS",
    "JOB_ACTIONS_IMPLEMENTED",
    "JOB_ACTION_PHASE",
    "JOB_FROM_PATTERN",
    "JOB_PAGES_PATTERN",
    "JOB_PROFILE_PATTERN",
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
    "EventsPage",
    "GeometryResponse",
    "HealthResponse",
    "JobAccepted",
    "JobCreateRequest",
    "PROFILE_LABEL_MAX_LENGTH",
    "ParagraphItem",
    "PdfOutput",
    "ProfileListItem",
    "ProfileUpdateRequest",
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


# --------------------------------------------------------------------------- #
# W03：事件分页与产物清单（api.md §1.3 / §1.5）
# --------------------------------------------------------------------------- #
class EventsPage(BaseModel):
    """``GET /api/v1/documents/{did}/events``：一页事件 + 续传游标（api.md §1.3）。

    ``run_id`` 是本页事件**实际来源**的 run。``seq`` 只在单 run 内单调递增，游标语义
    是 ``(run_id, seq)``（api.md §1.3），前端要靠它组 SSE 的 ``Last-Event-ID``；
    这是对 §1.3 响应形状的 additive 扩展（三个原键不变）。

    ``next_after_seq`` 是**扫描位置**而不是匹配位置：带 ``stage``/``kind`` 过滤时
    即使本页 0 条匹配也必须推进，否则过滤条件会卡住轮询。``events`` 是事件原样
    透传（``{seq, at, stage, kind, data}``，不裁剪 ``data``）。
    """

    run_id: str
    events: list[dict[str, Any]]
    next_after_seq: int
    has_more: bool


class ArtifactItem(BaseModel):
    """``GET /api/v1/documents/{did}/artifacts`` 的一件产物（api.md §1.5）。

    ``name`` 既是稳定短名也是下载键（``artifacts/{name}`` 的路径参数）：workdir
    相对路径，如 ``output/paper.mono.pdf`` / ``agent/translated.md`` / ``source.pdf``。
    ``path`` 与 ``name`` 同值（沿用 W02 ``PdfOutput.path`` 的"相对 workdir"约定，
    不是服务端绝对路径）。

    W03 没有草稿编译产物（草稿与编译修订号按 §3.2 在 W09 落地），所以 §1.5 提到的
    ``revision`` 字段在这里暂不出现 —— 没有真实来源就不塞假值。
    """

    name: str
    path: str
    kind: Literal["pdf", "markdown", "json", "report", "source"]
    size: int
    #: 文件 mtime，UTC ISO8601（毫秒 + ``Z``）。
    mtime: str


#: ``GET /api/v1/documents/{did}/artifacts`` 的响应形状：**JSON 数组**（与
#: ``/paragraphs`` 同风格，不额外套一层对象）。白名单与 kind 规则见
#: :mod:`babeldoc_tools.serve.artifacts`。
ArtifactsResponse = list[ArtifactItem]


# --------------------------------------------------------------------------- #
# W07：jobs（api.md §3.4）
# --------------------------------------------------------------------------- #
class JobCreateRequest(BaseModel):
    """``POST /documents/{did}/jobs`` 的请求体（api.md §3.4）。

    客户端只能给这些字段：``action``/``from``/``pages``/``dual``/``profile``；
    ``profile`` 只接 **id**。translator/reviewer/timeout 之类命令与密钥字段一律由
    服务端从 profile 解析，带了就 422 ``forbidden_field``（见
    :data:`babeldoc_tools.serve.routers.jobs.FORBIDDEN_JOB_FIELDS`）—— 这些字段**不**
    出现在下面的模型里，避免被前端代码生成当成可用参数。

    ``from`` 是 Python 关键字，用别名叫回来；OpenAPI 里仍是 ``from``。
    """

    action: Literal["run", "retranslate", "compile", "check"]
    from_stage: str | None = Field(
        default=None,
        alias="from",
        pattern=JOB_FROM_PATTERN,
        description="起点阶段（只对 action=run 有效），取值 = §3.1 的 7 个阶段",
    )
    pages: str | None = Field(
        default=None,
        pattern=JOB_PAGES_PATTERN,
        description='页码范围（只对 action=run 有效），如 "1-3,5"',
    )
    dual: bool = Field(default=False, description="是否生成 dual（双语）PDF")
    profile: str = Field(
        pattern=JOB_PROFILE_PATTERN,
        description="provider profile id（命令由服务端解析，永不回传）",
    )


class JobAccepted(BaseModel):
    """``POST /documents/{did}/jobs`` 的 202 响应（api.md §3.4 冻结形状）。

    ``status`` 恒为 ``queued``（契约形状）：真正状态以 ``GET /jobs/{jid}`` 为准 ——
    有空槽位时 job 在响应发出前就已经 ``running`` 了。
    """

    job_id: str
    status: Literal["queued"] = "queued"
    action: Literal["run", "check"]


# --------------------------------------------------------------------------- #
# W08：上传与 profiles（api.md §3.5）
# --------------------------------------------------------------------------- #
#: ``PUT /profiles`` 的 ``label`` 长度上限（显示名，不是命令）。
PROFILE_LABEL_MAX_LENGTH = 80


class DocumentUploaded(BaseModel):
    """``POST /documents`` 的 201 响应（api.md §3.5 冻结形状）。

    ``did`` 由服务端生成（``up-<slug>-<yyyymmdd-hhmmss>``，同名冲突递增后缀）；
    ``source`` 恒为 ``source.pdf`` —— 上传只落这一个文件（W03 产物白名单里的
    ``kind=source``），不建任何 ``agent/`` 骨架。
    """

    did: str
    #: 落盘字节数（盘上 ``source.pdf`` 的真实大小，不是请求声明的大小）。
    bytes: int
    source: Literal["source.pdf"] = "source.pdf"


class ProfileListItem(BaseModel):
    """``GET /profiles`` 的条目 / ``PUT /profiles`` 的响应（api.md §3.5）。

    **没有命令字段**：translator/reviewer 命令字符串只存在于服务端（可能内嵌密钥），
    前端只用 ``id`` 提交 job、用 ``label`` 显示。
    """

    id: str
    #: 显示名：profiles.json 的 ``label``，没有则 id 的人性化形式（``deepseek-flash``
    #: → ``Deepseek Flash``）。
    label: str
    #: 配没配 translator/reviewer（不说明配的是什么）。
    has_translator: bool
    has_reviewer: bool


class ProfileUpdateRequest(BaseModel):
    """``PUT /profiles`` 的请求体（api.md §3.5）。

    字段**缺席**与显式 ``null`` 不同：缺席 = 不动这个字段，``null``/空串 = 删掉它
    （删成空条目就是删整个 profile）。``translator``/``reviewer``/``api_key`` 之类字段
    **不在**模型里：收到了由路由层 422 ``forbidden_field`` 拒掉，不静默忽略。
    """

    id: str = Field(
        pattern=JOB_PROFILE_PATTERN, description="profile id（[a-z0-9-]{1,64}）"
    )
    label: str | None = Field(
        default=None,
        max_length=PROFILE_LABEL_MAX_LENGTH,
        description="显示名；null/空串 = 清掉（显示时回退 id 的人性化形式）",
    )
    translator_script: str | None = Field(
        default=None,
        description=(
            "translator 脚本路径引用（scripts/<name>，必须在白名单目录内）；null = 删该字段"
        ),
    )
    reviewer_script: str | None = Field(
        default=None,
        description=(
            "reviewer 脚本路径引用（scripts/<name>，必须在白名单目录内）；null = 删该字段"
        ),
    )

    @model_validator(mode="after")
    def _require_a_change(self) -> ProfileUpdateRequest:
        """只给 ``id`` 的请求没有意义（既没建也没改）→ 422 ``validation_error``。"""
        if not (
            self.model_fields_set & {"label", "translator_script", "reviewer_script"}
        ):
            raise ValueError(
                "至少要给出 label / translator_script / reviewer_script 之一"
            )
        return self
