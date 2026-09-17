"""``/api/v1/glossary``：全局词表的读写（``api.md`` §3.2，W13）。

**一个全局词表**，不属于任何文档：``<store_base>/.bdt-serve/glossary.csv``。三个端点：

- ``GET``：当前条目 + 条数（没有词表 → 空数组，不是错误）；
- ``PUT``：整表替换（``{entries: [{source, target, note?}]}``），校验在服务端
  （空 source/target、超长、超条数 → 422 ``glossary_invalid``，盘上一字不改）；
- ``DELETE``：清空（幂等）。

两条边界：

- **客户端不收也不发 CSV 文本**：CSV 的解析/导出在前端，与后端交换一律用 JSON 条目；
- **注入不在这里**：翻译 job 拿的只是**服务端构造的路径**（
  :meth:`babeldoc_tools.serve.glossary.GlossaryStore.injection_path`），客户端在
  ``POST /documents/{did}/jobs`` 里只给 ``use_glossary`` 布尔。

``GET`` 是只读的；``PUT``/``DELETE`` 是 W13 新增的写端点（已登记在
``tests/test_serve_app.py`` 的写端点白名单里）。
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Request

from babeldoc_tools.glossary import GlossaryEntry
from babeldoc_tools.serve.glossary import GlossaryStore
from babeldoc_tools.serve.schemas import API_PREFIX
from babeldoc_tools.serve.schemas import GlossaryEntryModel
from babeldoc_tools.serve.schemas import GlossaryResponse
from babeldoc_tools.serve.schemas import GlossaryUpdateRequest

__all__ = ["glossary_payload", "glossary_router"]


def glossary_payload(entries: list[GlossaryEntry]) -> GlossaryResponse:
    """条目 → 响应形状（没有备注的条目 ``note`` 为 ``null``）。"""
    return GlossaryResponse(
        entries=[
            GlossaryEntryModel(source=e.source, target=e.target, note=e.note)
            for e in entries
        ],
        count=len(entries),
    )


def glossary_router(store: GlossaryStore) -> APIRouter:
    """按 store 生成词表路由（CSV 落在 ``store_base/.bdt-serve/`` 下）。"""
    router = APIRouter(prefix=API_PREFIX, tags=["glossary"])

    @router.get(
        "/glossary",
        response_model=GlossaryResponse,
        summary="全局词表（条目 + 条数）",
        description=(
            "返回 ``<store_base>/.bdt-serve/glossary.csv`` 的内容，已按 ``source`` 排序、"
            "同 source 只留最后一条。没有词表 → ``entries=[]``/``count=0``（不是 404）。"
        ),
    )
    def get_glossary() -> GlossaryResponse:
        return glossary_payload(store.read())

    @router.put(
        "/glossary",
        response_model=GlossaryResponse,
        summary="整表替换全局词表",
        description=(
            "用 ``entries`` **整体替换**词表（本地编辑后一次保存，不做行级 patch）。"
            "校验：source/target 去空白后必须非空且在长度上限内、同 source 后者覆盖前者、"
            "按 source 排序；不合法 → 422 ``glossary_invalid``（带 ``index``/``field``）"
            "且**盘上一字不改**。空数组 = 清空（等效于 DELETE）。"
            "**词表变更不回溯**：已经翻译过的内容不会自动重译，重新跑翻译才生效。"
        ),
        responses={422: {"description": "glossary_invalid / validation_error"}},
    )
    async def put_glossary(payload: GlossaryUpdateRequest) -> GlossaryResponse:
        entries = [
            GlossaryEntry(
                source=item.source, target=item.target, note=item.note
            )
            for item in payload.entries
        ]
        return glossary_payload(store.replace(entries))

    @router.delete(
        "/glossary",
        response_model=GlossaryResponse,
        summary="清空全局词表（幂等）",
        description=(
            "删掉词表文件。之后翻译 job 不再注入（``use_glossary=true`` 也一样），"
            "``GET`` 返回空表。已经翻译过的内容不受影响（词表变更不回溯）。"
        ),
    )
    async def delete_glossary(_request: Request) -> GlossaryResponse:
        store.clear()
        return glossary_payload(store.read())

    return router
