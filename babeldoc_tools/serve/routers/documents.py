"""``/api/v1/documents`` 路由：列表 / 详情 / 阶段 / 段落 / 几何 / 检查 + 上传（W08）。

W02 实现 ``docs/frontend/api.md`` §3.1 的前六个 GET 端点；W08 增加 ``POST /documents``
（multipart 上传 PDF → 建 did，见 :mod:`babeldoc_tools.serve.uploads`）。路由层只做三件事：
解析路径/查询参数、调 :mod:`babeldoc_tools.serve.views`/上传层、交给 ``response_model``
序列化；**读写文件的边界**仍是
:meth:`babeldoc_tools.serve.store.DocumentStore.resolve`（did 校验 + 根目录内约束）+ 上传层的
did 生成（目录名全由服务端拼，客户端字符串不进路径）。
"""

from __future__ import annotations

from typing import Annotated
from typing import Literal

from fastapi import APIRouter
from fastapi import File
from fastapi import Path as PathParam
from fastapi import Query
from fastapi import UploadFile

from babeldoc_tools.serve import views
from babeldoc_tools.serve.schemas import API_PREFIX
from babeldoc_tools.serve.schemas import CheckResponse
from babeldoc_tools.serve.schemas import DocumentDetail
from babeldoc_tools.serve.schemas import DocumentListItem
from babeldoc_tools.serve.schemas import DocumentUploaded
from babeldoc_tools.serve.schemas import GeometryResponse
from babeldoc_tools.serve.schemas import ParagraphItem
from babeldoc_tools.serve.schemas import StageStateResponse
from babeldoc_tools.serve.store import DocumentStore
from babeldoc_tools.serve.uploads import MAX_UPLOAD_BYTES
from babeldoc_tools.serve.uploads import SOURCE_NAME
from babeldoc_tools.serve.uploads import save_upload
from babeldoc_tools.serve.workdir import WorkdirReader

__all__ = ["documents_router"]

DOCUMENT_ID = "文档 id：workdir 目录名（单段，解析结果必须在服务根目录内）"
PAGE_QUERY = "页码过滤（1 基 PDF 页码）"
UPLOAD_FILE = (
    "multipart/form-data 的 file 字段：源 PDF（读前 5 字节必须是 %PDF-，"
    f"上限 {MAX_UPLOAD_BYTES} 字节）。文件名只用来生成 did 的 slug。"
)


def documents_router(store: DocumentStore) -> APIRouter:
    """按 store 生成只读路由（与 :func:`babeldoc_tools.serve.app.create_app` 同风格）。"""
    router = APIRouter(prefix=API_PREFIX, tags=["documents"])

    def reader(did: str) -> WorkdirReader:
        """did → workdir 的产物读取器（路径校验全在 store 里）。"""
        return WorkdirReader(store.resolve(did))

    @router.post(
        "/documents",
        response_model=DocumentUploaded,
        status_code=201,
        summary="上传 PDF（建 did）",
        description=(
            "multipart/form-data 上传源 PDF：校验 ``%PDF-`` 魔数与大小上限，"
            "在服务根目录下建 ``up-<slug>-<时间戳>`` 目录，把字节流式写进"
            "``<did>/source.pdf``（tmp + rename 原子落盘，不预建 agent/ 骨架）。"
            "did 由服务端生成（同名冲突递增 -2/-3），客户端给的文件名只贡献 slug。"
        ),
        responses={
            413: {"description": "file_too_large：超过 200MB 上限"},
            422: {"description": "invalid_pdf：缺文件名或前 5 字节不是 %PDF-"},
            409: {"description": "upload_conflict：连续 50 个候选目录名都被占用"},
        },
    )
    def upload_document(
        file: Annotated[UploadFile, File(description=UPLOAD_FILE)],
    ) -> DocumentUploaded:
        did = save_upload(store, file)
        # 字节数取盘上文件的大小：上传写入多少，响应就报多少（不信请求声明）。
        source = (store.resolve(did) / SOURCE_NAME).stat()
        return DocumentUploaded(did=did, bytes=source.st_size)

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
            "只有 check 门禁与 reviewer 都 pass 才为 true（编译成功不算质量通过）；"
            "compile 读 `.bdt-serve/compile.json`：status/revision/artifact 来自最近"
            "一次成功编译，stale = 当前草稿 revision 更大（旧 PDF 不得当成最新）。"
        ),
    )
    def get_document(
        did: Annotated[str, PathParam(description=DOCUMENT_ID)],
    ) -> DocumentDetail:
        detail = views.document_detail(reader(did), did)
        from babeldoc_tools.serve.draft import read_draft

        detail.revision = read_draft(store.resolve(did)).revision
        if (store.store_base / "app.db").is_file():
            database = store.database
            with database._lock:
                preview = database.connection.execute(
                    "SELECT asset_sha256 FROM local_previews WHERE document_id=?",
                    (did,),
                ).fetchone()
                exported = database.connection.execute(
                    "SELECT revision FROM exports WHERE document_id=? ORDER BY id DESC LIMIT 1",
                    (did,),
                ).fetchone()
            detail.preview_asset = preview[0] if preview else None
            detail.export_revision = exported[0] if exported else None
        return detail

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
        if (store.store_base / "app.db").is_file():
            from babeldoc_tools.serve.block_compile import document_blocks

            store.resolve(did)
            return [
                row
                for row in document_blocks(store, did)
                if page is None or row.page == page
            ]
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
