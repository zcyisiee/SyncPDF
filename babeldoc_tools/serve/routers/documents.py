"""``/api/v1/documents`` 只读路由：列表 / 详情 / 阶段 / 段落 / 几何 / 检查。

W02 实现 ``docs/frontend/api.md`` §3.1 的前六个 GET 端点。路由层只做三件事：
解析路径/查询参数、调 :mod:`babeldoc_tools.serve.views` 组装视图、交给
``response_model`` 序列化；**读文件的边界**仍是
:meth:`babeldoc_tools.serve.store.DocumentStore.resolve`（did 校验 + 根目录内约束）。

全部是 GET：W02 不引入任何写端点（``tests/test_serve_app.py`` 的
``test_openapi_has_no_write_routes`` 守着这一点）。
"""

from __future__ import annotations

from typing import Annotated
from typing import Literal

from fastapi import APIRouter
from fastapi import Path as PathParam
from fastapi import Query

from babeldoc_tools.serve import views
from babeldoc_tools.serve.schemas import API_PREFIX
from babeldoc_tools.serve.schemas import CheckResponse
from babeldoc_tools.serve.schemas import DocumentDetail
from babeldoc_tools.serve.schemas import DocumentListItem
from babeldoc_tools.serve.schemas import GeometryResponse
from babeldoc_tools.serve.schemas import ParagraphItem
from babeldoc_tools.serve.schemas import StageStateResponse
from babeldoc_tools.serve.store import DocumentStore
from babeldoc_tools.serve.workdir import WorkdirReader

__all__ = ["documents_router"]

DOCUMENT_ID = "文档 id：workdir 目录名（单段，解析结果必须在服务根目录内）"
PAGE_QUERY = "页码过滤（1 基 PDF 页码）"


def documents_router(store: DocumentStore) -> APIRouter:
    """按 store 生成只读路由（与 :func:`babeldoc_tools.serve.app.create_app` 同风格）。"""
    router = APIRouter(prefix=API_PREFIX, tags=["documents"])

    def reader(did: str) -> WorkdirReader:
        """did → workdir 的产物读取器（路径校验全在 store 里）。"""
        return WorkdirReader(store.resolve(did))

    @router.get(
        "/documents",
        response_model=list[DocumentListItem],
        summary="文档列表",
        description=(
            "枚举可见 workdir 的概要：阶段状态、页数/段数/已译段数、最近活动时间。"
            "产物缺失的字段为 null（不是 0），坏产物不影响其它文档。"
        ),
    )
    def list_documents() -> list[DocumentListItem]:
        return [views.document_summary(reader(did), did) for did in store.list_dids()]

    @router.get(
        "/documents/{did}",
        response_model=DocumentDetail,
        summary="文档元信息",
        description=(
            "元信息 + 质量状态 + 编译状态。质量与编译**分开**报：quality.pipeline_ok "
            "只有 check 门禁与 reviewer 都 pass 才为 true；compile 在 W02 没有真实"
            "编译产物，固定 status=none / revision=0。"
        ),
    )
    def get_document(
        did: Annotated[str, PathParam(description=DOCUMENT_ID)],
    ) -> DocumentDetail:
        return views.document_detail(reader(did), did)

    @router.get(
        "/documents/{did}/stage-state",
        response_model=StageStateResponse,
        summary="7 阶段状态与真实耗时",
        description=(
            "阶段名固定为 parse → translate → apply → build → check → review → report。"
            "起止时间优先取最新 run 的 manifest.json（token 为 manifest），"
            "manifest 缺该段时回退 run_state.json 的 at + duration_s"
            "（token 为 run_state，起点由完成时刻与实测耗时反推）。"
        ),
    )
    def get_stage_state(
        did: Annotated[str, PathParam(description=DOCUMENT_ID)],
    ) -> StageStateResponse:
        return views.stage_state(reader(did), did)

    @router.get(
        "/documents/{did}/paragraphs",
        response_model=list[ParagraphItem],
        summary="段落面板数据（原文/译文/排版 join）",
        description=(
            "以段落 id 左连接 anchors / translated.jsonl / layout_geometry / parse "
            "快照，数量不一致时缺侧为 null（不假设相等）。page 为 1 基 PDF 页码。"
        ),
    )
    def get_paragraphs(
        did: Annotated[str, PathParam(description=DOCUMENT_ID)],
        page: Annotated[int | None, Query(ge=1, description=PAGE_QUERY)] = None,
    ) -> list[ParagraphItem]:
        return views.paragraphs(reader(did), page)

    @router.get(
        "/documents/{did}/geometry",
        response_model=GeometryResponse,
        summary="bbox 几何（parse 快照 / layout 几何）",
        description=(
            "kind=parse 读最新 run 的 parse 段落实体，box 是 pdf_topleft（y 向下）；"
            "kind=layout 读 layout_geometry.json，box 是 pdf_native（y 向上）并附 "
            "page_info 的 cropbox。两套坐标系统**不做转换**，由前端按 coord_system 换算。"
            "缺对应产物时返回 404（snapshot_unavailable / geometry_unavailable），"
            "不用空数组冒充成功。"
        ),
    )
    def get_geometry(
        did: Annotated[str, PathParam(description=DOCUMENT_ID)],
        kind: Annotated[
            Literal["parse", "layout"],
            Query(description="parse = 识别/原文 bbox；layout = 译文排版 bbox"),
        ],
        page: Annotated[int | None, Query(ge=1, description=PAGE_QUERY)] = None,
    ) -> GeometryResponse:
        workdir_reader = reader(did)
        if kind == "parse":
            return views.geometry_parse(workdir_reader, did, page)
        return views.geometry_layout(workdir_reader, did, page)

    @router.get(
        "/documents/{did}/check",
        response_model=CheckResponse,
        summary="结构审查 / 排版 lint / 链接审计",
        description=(
            "三份检查产物原样透传（不裁剪字段），available 标志说明各自是否可用"
            "（旧 workdir 可能缺项）。"
        ),
    )
    def get_check(
        did: Annotated[str, PathParam(description=DOCUMENT_ID)],
    ) -> CheckResponse:
        return views.check_view(reader(did), did)

    return router
