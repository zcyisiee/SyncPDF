"""bbox 渲染可选的「段落级中文字体族」注册表。

latex_bbox 模板按段落声明中文主字体（``\\setCJKmainfont``）：默认按 ``serif``
标志选 Source Han Serif/Sans CN（见 :mod:`capability`）。本模块登记**额外可选**
的字体族，调用方只传 id；字体文件是否可用由探测决定（缺文件的族不进
:attr:`LatexCapability.cjk_family_fonts`，请求回落默认族，绝不因字体缺失报错）。

``serif`` 表示该族是否衬线：拉丁字形仍按它挑 Noto Serif/Sans（与中文字形观感
一致），字号以外的排版语义（行距/``\\topskip`` 的 ascender）照旧按实际字体文件算。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 字体族 id 形状：小写字母/数字/连字符（对外接口按此校验）。
FONT_FAMILY_ID_PATTERN = re.compile(r"[a-z0-9-]+")

#: 字体文件名（相对字体查找目录，见 ``capability._font_dirs``）。
_SOURCE_HAN_SERIF = "SourceHanSerifCN-Regular.ttf"
_SOURCE_HAN_SERIF_BOLD = "SourceHanSerifCN-Bold.ttf"
_SOURCE_HAN_SANS = "SourceHanSansCN-Regular.ttf"
_SOURCE_HAN_SANS_BOLD = "SourceHanSansCN-Bold.ttf"


@dataclass(frozen=True)
class FontFamilySpec:
    """一个可选中文字体族。

    ``bold=None`` 表示该族没有独立粗体文件：模板只声明 UprightFont（``\\bfseries``
    由 fontspec 按同一文件的合成粗体处理）。
    """

    id: str
    label: str
    serif: bool
    regular: str
    bold: str | None = None


#: 注册表：顺序只影响探测与展示，不影响语义。
FONT_FAMILIES: tuple[FontFamilySpec, ...] = (
    FontFamilySpec(
        id="source-han-serif",
        label="思源宋体",
        serif=True,
        regular=_SOURCE_HAN_SERIF,
        bold=_SOURCE_HAN_SERIF_BOLD,
    ),
    FontFamilySpec(
        id="source-han-sans",
        label="思源黑体",
        serif=False,
        regular=_SOURCE_HAN_SANS,
        bold=_SOURCE_HAN_SANS_BOLD,
    ),
    FontFamilySpec(
        id="lxgw-wenkai",
        label="霞鹜文楷",
        serif=True,
        regular="LXGWWenKaiGB-Regular.1.520.ttf",
    ),
    FontFamilySpec(
        id="klee-one",
        label="Klee One",
        serif=False,
        regular="KleeOne-Regular.ttf",
    ),
    FontFamilySpec(
        id="maru-buri",
        label="MaruBuri",
        serif=True,
        regular="MaruBuri-Regular.ttf",
    ),
)

#: 合法 id 集合。
FONT_FAMILY_IDS: frozenset[str] = frozenset(spec.id for spec in FONT_FAMILIES)

_FONT_FAMILY_BY_ID: dict[str, FontFamilySpec] = {
    spec.id: spec for spec in FONT_FAMILIES
}


def font_family_spec(family_id: str | None) -> FontFamilySpec | None:
    """按 id 取字体族；未知 id 或 None 返回 None（调用方回落默认族）。"""
    if not family_id:
        return None
    return _FONT_FAMILY_BY_ID.get(family_id)


def is_valid_font_family(value: object) -> bool:
    """是否为注册表里的字体族 id（非字符串一律 False）。"""
    return isinstance(value, str) and value in FONT_FAMILY_IDS
