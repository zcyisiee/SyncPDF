"""``/api/v1/fonts``：可用中文字体族清单（``font_families.FONT_FAMILIES``）。

只读端点，前端用它渲染"中文字体"下拉并把选中的 id 写进草稿
（``PATCH D/draft`` 的 ``layout.font_family``；仅局部块编译消费，见
``docs/reference/http-api.md``）。

``available`` 先看 LaTeX 能力，再看具体族：能力不可用（缺 xelatex/宏包）时全部族
``false``（本机出不了图）；能力可用时看该族的中文字体文件在不在。探测走
:meth:`babeldoc_tools.serve.block_compile.BlockCompiler.capability`
（按需探测一次并缓存，与块编译用同一个实例，不重复起子进程）；探测失败或
LaTeX 不可用时全部族 ``available=false`` —— **接口不报错**：清单本身（id/label/
serif）不依赖本机环境，缺字体只影响能不能真的用它出图（渲染侧回落默认族）。
"""

from __future__ import annotations

from babeldoc.format.pdf.document_il.backend.latex_bbox.font_families import (
    FONT_FAMILIES,
)
from fastapi import APIRouter

from babeldoc_tools.serve.schemas import API_PREFIX
from babeldoc_tools.serve.schemas import FontFamilyItem

__all__ = ["font_family_items", "fonts_router"]


def font_family_items(capability) -> list[FontFamilyItem]:
    """注册表 → 响应条目（顺序同 ``FONT_FAMILIES``）。

    ``capability`` 为 None（探测失败）或 LaTeX 整体不可用时全部 ``available=false``：
    本机都出不了图，单族的字体文件在不在没有意义。
    """
    latex_ok = capability is not None and capability.available
    items: list[FontFamilyItem] = []
    for spec in FONT_FAMILIES:
        available = latex_ok and capability.cjk_family(spec.id) is not None
        items.append(
            FontFamilyItem(
                id=spec.id, label=spec.label, serif=spec.serif, available=available
            )
        )
    return items


def fonts_router(runner) -> APIRouter:
    """按 job runner 生成字体路由（能力探测复用它的块编译器缓存）。"""
    router = APIRouter(prefix=API_PREFIX, tags=["fonts"])

    def capability():
        """本机 LaTeX 能力（缺块编译器/探测异常 → None，清单仍 200）。"""
        compiler = getattr(runner, "block_compiler", None)
        if compiler is None:
            return None
        try:
            return compiler.capability()
        except Exception:  # noqa: BLE001 - 探测失败只该让 available 全 false
            return None

    @router.get(
        "/fonts",
        response_model=list[FontFamilyItem],
        summary="可用中文字体族清单",
        description=(
            "服务端登记的段落级中文字体族（顺序固定）：`id` 是写进草稿 "
            "`layout.font_family` 的值，`serif` 是该族是否衬线（拉丁字形跟随它），"
            "`available` 表示本机能否真用它出图：LaTeX 能力不可用、或该族字体文件"
            "没探测到，都是 `false`（请求该族时渲染回落默认族，不是错误）。"
            "LaTeX 不可用/探测失败时全部 `available=false`，接口仍返回 200。"
        ),
    )
    def list_fonts() -> list[FontFamilyItem]:
        return font_family_items(capability())

    return router
