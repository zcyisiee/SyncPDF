"""MinerU provider IR：完整保留 layout.json 的 block/line/span 层级与阅读顺序。

背景：`MinerUDocLayoutModel` 需要把 MinerU 结果压成 `YoloResult`（bbox + 类别）
交给现有 LayoutParser 消费，但那个视图会丢弃 lines / spans / index / level /
sub_type / score。本模块把同一份 layout.json 规范化成结构化 IR，供字符对齐、
目录识别、超链接映射等下游消费者使用（对应 .plan/minerU深度融合.md 板块 1）。

坐标约定：MinerU bbox 为 ``[x0, y0, x1, y1]``，页面坐标系（左上原点，y 向下），
与 BabelDOC IL 坐标（左下原点，y 向上）不同；本模块**原样保留** MinerU 坐标，
坐标转换由消费者按页高自行完成。

id 规则（跨文档唯一，页内按 JSON 出现顺序的深度优先序号）：

- ``p{page}-b{n}``   block（page 为 0-based page_idx）
- ``p{page}-b{n}-l{m}`` line
- ``p{page}-b{n}-l{m}-s{k}`` span
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from dataclasses import field
from typing import Any

# 已知 block 类型：来源为 `mineru_doclayout._map_mineru_block_to_layout_label`
# 的显式分支（含容器类型 table / image / list / chart）。
# 与映射函数保持同步由 tests/test_provider_ir.py 的同步断言守门：
# 对集合内每个类型调用映射函数都不应触发 "Unknown MinerU block type" 警告。
KNOWN_BLOCK_TYPES = frozenset(
    {
        "text",
        "title",
        "interline_equation",
        "equation",
        "ref_text",
        "table_caption",
        "table_body",
        "table_footnote",
        "image_caption",
        "chart_caption",
        "image_footnote",
        "image_body",
        "chart_body",
        "chart",
        "header",
        "footer",
        "page_number",
        "page_footnote",
        "aside_text",
        "code",
        "algorithm",
        "code_body",
        "code_caption",
        "phonetic",
        "table",
        "image",
        "list",
    }
)

# 已知 span 类型：MinerU 的三种文本型 kind + 三种图形型 kind。
# image/table/chart 是 MinerU 给「图内 / 表内 / 图表正文」span 的标记，
# 与上面 image/table/chart block 类型同源，因此同样视为已知
# （否则 5 份样本会产生 200+ 条假 unknown 记录）。
KNOWN_SPAN_KINDS = frozenset(
    {
        "text",
        "inline_equation",
        "interline_equation",
        "image",
        "table",
        "chart",
    }
)

# 嵌套子块的键名：MinerU 不同版本分别用 children / blocks（与
# mineru_doclayout._iter_leaf_blocks 的兼容口径一致）。
_CHILD_KEYS = ("children", "blocks")

# 缺 index 的 block 在阅读顺序里排到有 index 的之后（稳定排序，保持出现顺序）。
_MISSING_INDEX_ORDER = 1 << 30


def _normalize_bbox(value: Any) -> list[float] | None:
    """bbox → list[float]；非法值返回 None（不抛错，便于回放脏数据）。"""
    if not isinstance(value, list | tuple) or len(value) != 4:
        return None
    try:
        return [float(v) for v in value]
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _as_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


@dataclass(slots=True)
class ProviderSpan:
    """MinerU 的最小文本/公式单元。"""

    span_id: str
    bbox: list[float] | None
    kind: str
    content: str
    score: float | None
    page_index: int
    # Provider-specific evidence (for example PaddleOCR-VL recognition
    # status).  Keeping this optional preserves compatibility with historical
    # MinerU provider artifacts while allowing local backends to expose
    # provenance without changing the canonical span fields.
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "bbox": self.bbox,
            "kind": self.kind,
            "content": self.content,
            "score": self.score,
            "page_index": self.page_index,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProviderSpan:
        return cls(
            span_id=str(data["span_id"]),
            bbox=_normalize_bbox(data.get("bbox")),
            kind=str(data.get("kind") or "text"),
            content=str(data.get("content") or ""),
            score=_as_float(data.get("score")),
            page_index=int(data.get("page_index") or 0),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(slots=True)
class ProviderLine:
    """MinerU 的一行（bbox + span 序列）。"""

    line_id: str
    bbox: list[float] | None
    spans: list[ProviderSpan] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "line_id": self.line_id,
            "bbox": self.bbox,
            "spans": [span.to_dict() for span in self.spans],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProviderLine:
        return cls(
            line_id=str(data["line_id"]),
            bbox=_normalize_bbox(data.get("bbox")),
            spans=[ProviderSpan.from_dict(item) for item in (data.get("spans") or [])],
        )


@dataclass(slots=True)
class ProviderBlock:
    """MinerU 的 block（含容器 block 的嵌套 children）。"""

    block_id: str
    type: str
    sub_type: str | None
    bbox: list[float] | None
    level: int | None
    index: int | None
    angle: float | None
    lines: list[ProviderLine] = field(default_factory=list)
    children: list[ProviderBlock] = field(default_factory=list)
    parent_block_id: str | None = None
    merge_prev: bool | None = None
    source: str = "para_blocks"

    def iter_blocks(self) -> Iterator[ProviderBlock]:
        """深度优先迭代自身与所有嵌套子块。"""
        yield self
        for child in self.children:
            yield from child.iter_blocks()

    def iter_lines(self) -> Iterator[ProviderLine]:
        """深度优先迭代自身与所有嵌套子块的行。"""
        for block in self.iter_blocks():
            yield from block.lines

    def iter_spans(self) -> Iterator[ProviderSpan]:
        """深度优先迭代自身与所有嵌套子块的 span。"""
        for line in self.iter_lines():
            yield from line.spans

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "type": self.type,
            "sub_type": self.sub_type,
            "bbox": self.bbox,
            "level": self.level,
            "index": self.index,
            "angle": self.angle,
            "lines": [line.to_dict() for line in self.lines],
            "children": [child.to_dict() for child in self.children],
            "parent_block_id": self.parent_block_id,
            "merge_prev": self.merge_prev,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProviderBlock:
        return cls(
            block_id=str(data["block_id"]),
            type=str(data.get("type") or "unknown"),
            sub_type=data.get("sub_type"),
            bbox=_normalize_bbox(data.get("bbox")),
            level=_as_int(data.get("level")),
            index=_as_int(data.get("index")),
            angle=_as_float(data.get("angle")),
            lines=[ProviderLine.from_dict(item) for item in (data.get("lines") or [])],
            children=[
                ProviderBlock.from_dict(item) for item in (data.get("children") or [])
            ],
            parent_block_id=data.get("parent_block_id"),
            merge_prev=_as_bool(data.get("merge_prev")),
            source=str(data.get("source") or "para_blocks"),
        )


@dataclass(slots=True)
class ProviderPage:
    """MinerU 的一页：顶层 block（正文在前、discarded 在后）+ 阅读顺序。"""

    page_index: int
    blocks: list[ProviderBlock] = field(default_factory=list)
    reading_order: list[str] = field(default_factory=list)

    def iter_blocks(self, *, recursive: bool = False) -> Iterator[ProviderBlock]:
        """按页内顺序迭代 block。

        ``recursive=False``（默认）只给顶层 block —— 与 ``reading_order`` 口径一致；
        需要图内/表内/图表内的嵌套块时传 ``recursive=True``。
        """
        for block in self.blocks:
            if recursive:
                yield from block.iter_blocks()
            else:
                yield block

    def block_by_id(self, block_id: str) -> ProviderBlock | None:
        for block in self.iter_blocks(recursive=True):
            if block.block_id == block_id:
                return block
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_index": self.page_index,
            "blocks": [block.to_dict() for block in self.blocks],
            "reading_order": list(self.reading_order),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProviderPage:
        return cls(
            page_index=int(data.get("page_index") or 0),
            blocks=[
                ProviderBlock.from_dict(item) for item in (data.get("blocks") or [])
            ],
            reading_order=[str(item) for item in (data.get("reading_order") or [])],
        )


class _LayoutJsonBuilder:
    """layout.json → provider IR 的逐页/逐块构造器。

    把构造逻辑放在类里而非函数闭包：递归构造嵌套 block 时需要共享「当前页
    block 序号」与「未知类型聚合」状态，用实例属性比闭包捕获更清晰。
    """

    def __init__(self) -> None:
        self.pages: list[ProviderPage] = []
        # 未知类型聚合：(type, where, page_index) → count
        self._unknown: dict[tuple[str, str, int], int] = {}
        # 当前页的 block 序号（build_page 时重置）。
        self._block_counter = 0

    # ------------------------------------------------------------------ #
    # 公共入口
    # ------------------------------------------------------------------ #
    def build_page(self, page_index: int, page_info: dict[str, Any]) -> None:
        """构造一页：para_blocks 进阅读顺序，discarded_blocks 排在后面。"""
        self._block_counter = 0
        para_blocks = self._build_blocks(
            page_info.get("para_blocks"), "para_blocks", page_index
        )
        discarded_blocks = self._build_blocks(
            page_info.get("discarded_blocks"), "discarded_blocks", page_index
        )

        # 阅读顺序：para_blocks 按 MinerU 的 index 升序（缺 index 的排最后，
        # 同类保持 JSON 出现顺序）。discarded_blocks 是页眉/页脚/页码等非正文
        # 内容，不进阅读顺序，但仍保存在 blocks 里并带 source 标记。
        para_blocks.sort(
            key=lambda block: (
                block.index is None,
                block.index if block.index is not None else _MISSING_INDEX_ORDER,
            )
        )

        self.pages.append(
            ProviderPage(
                page_index=page_index,
                blocks=para_blocks + discarded_blocks,
                reading_order=[block.block_id for block in para_blocks],
            )
        )

    def unknown_types(self) -> list[dict[str, Any]]:
        """未知 block/span 类型聚合（按页序 → where → type 稳定排序）。"""
        return [
            {"type": kind, "where": where, "page_index": page, "count": count}
            for (kind, where, page), count in sorted(
                self._unknown.items(),
                key=lambda item: (item[0][2], item[0][1], item[0][0]),
            )
        ]

    # ------------------------------------------------------------------ #
    # 内部构造
    # ------------------------------------------------------------------ #
    def _record_unknown(self, kind: str, where: str, page_index: int) -> None:
        key = (kind, where, page_index)
        self._unknown[key] = self._unknown.get(key, 0) + 1

    def _build_blocks(
        self, items: Any, source: str, page_index: int
    ) -> list[ProviderBlock]:
        built: list[ProviderBlock] = []
        for raw in items or []:
            block = self._build_block(raw, None, source, page_index)
            if block is not None:
                built.append(block)
        return built

    def _build_block(
        self,
        raw: Any,
        parent_block_id: str | None,
        source: str,
        page_index: int,
    ) -> ProviderBlock | None:
        if not isinstance(raw, dict):
            return None
        # 页内 block 序号：跨顶层与嵌套统一递增，保证 id 唯一且可复现。
        block_id = f"p{page_index}-b{self._block_counter}"
        self._block_counter += 1

        block_type = str(raw.get("type") or "").strip().lower()
        if block_type not in KNOWN_BLOCK_TYPES:
            self._record_unknown(block_type or "<missing>", "block", page_index)

        sub_type = raw.get("sub_type")
        return ProviderBlock(
            block_id=block_id,
            type=block_type or "unknown",
            sub_type=str(sub_type).strip().lower() if sub_type else None,
            bbox=_normalize_bbox(raw.get("bbox")),
            level=_as_int(raw.get("level")),
            index=_as_int(raw.get("index")),
            angle=_as_float(raw.get("angle")),
            lines=self._build_lines(block_id, raw, page_index),
            children=self._build_children(block_id, raw, source, page_index),
            parent_block_id=parent_block_id,
            merge_prev=_as_bool(raw.get("merge_prev")),
            source=source,
        )

    def _build_lines(
        self, block_id: str, raw: dict[str, Any], page_index: int
    ) -> list[ProviderLine]:
        lines: list[ProviderLine] = []
        for line_index, raw_line in enumerate(raw.get("lines") or []):
            if not isinstance(raw_line, dict):
                continue
            line_id = f"{block_id}-l{line_index}"
            lines.append(
                ProviderLine(
                    line_id=line_id,
                    bbox=_normalize_bbox(raw_line.get("bbox")),
                    spans=self._build_spans(line_id, raw_line, page_index),
                )
            )
        return lines

    def _build_spans(
        self, line_id: str, raw_line: dict[str, Any], page_index: int
    ) -> list[ProviderSpan]:
        spans: list[ProviderSpan] = []
        for span_index, raw_span in enumerate(raw_line.get("spans") or []):
            if not isinstance(raw_span, dict):
                continue
            kind = str(raw_span.get("type") or "text").strip().lower()
            if kind not in KNOWN_SPAN_KINDS:
                self._record_unknown(kind, "span", page_index)
            spans.append(
                ProviderSpan(
                    span_id=f"{line_id}-s{span_index}",
                    bbox=_normalize_bbox(raw_span.get("bbox")),
                    kind=kind,
                    content=str(raw_span.get("content") or ""),
                    score=_as_float(raw_span.get("score")),
                    page_index=page_index,
                )
            )
        return spans

    def _build_children(
        self,
        block_id: str,
        raw: dict[str, Any],
        source: str,
        page_index: int,
    ) -> list[ProviderBlock]:
        children: list[ProviderBlock] = []
        for key in _CHILD_KEYS:
            raw_children = raw.get(key)
            if not isinstance(raw_children, list):
                continue
            for raw_child in raw_children:
                child = self._build_block(raw_child, block_id, source, page_index)
                if child is not None:
                    children.append(child)
        return children


@dataclass(slots=True)
class ProviderDocument:
    """整篇文档的 MinerU 结构层（规范化产物，可 JSON 序列化落盘）。"""

    version_name: str | None
    backend: str | None
    page_count: int
    pages: list[ProviderPage] = field(default_factory=list)
    unknown_types: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    # 构造
    # ------------------------------------------------------------------ #
    @classmethod
    def from_layout_json(cls, layout_json: dict[str, Any]) -> ProviderDocument:
        """MinerU layout.json → ProviderDocument。

        只导入 ``pdf_info`` 的 ``para_blocks`` 与 ``discarded_blocks``；
        ``preproc_blocks`` 是 MinerU 的中间预处理结果（与 para_blocks 重复），
        不进入 IR，避免同页内容出现两份。
        """
        pdf_info = layout_json.get("pdf_info")
        if not isinstance(pdf_info, list):
            raise ValueError("Invalid MinerU layout.json: missing pdf_info list")

        builder = _LayoutJsonBuilder()
        for page_info in pdf_info:
            if not isinstance(page_info, dict):
                continue
            page_index = page_info.get("page_idx")
            if not isinstance(page_index, int):
                continue
            builder.build_page(page_index, page_info)

        unknown_types = builder.unknown_types()
        pages = builder.pages

        # page_count 取文档页数（max(page_idx)+1）而非 len(pages)：MinerU 可能只
        # 返回部分页（如 tests/fixtures 的节选 page_idx=0,1,2,6），此时 len(pages)
        # 是节选页数、page_count 才是文档页数；缺失页用 page(i) is None 判断。
        page_count = (max(page.page_index for page in pages) + 1) if pages else 0

        return cls(
            version_name=layout_json.get("_version_name"),
            backend=layout_json.get("_backend"),
            page_count=page_count,
            pages=pages,
            unknown_types=unknown_types,
        )

    # ------------------------------------------------------------------ #
    # 序列化
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict[str, Any]:
        return {
            "version_name": self.version_name,
            "backend": self.backend,
            "page_count": self.page_count,
            "pages": [page.to_dict() for page in self.pages],
            "unknown_types": [dict(item) for item in self.unknown_types],
            "metadata": self.metadata,
        }

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProviderDocument:
        pages = [ProviderPage.from_dict(item) for item in (data.get("pages") or [])]
        raw_page_count = data.get("page_count")
        page_count = (
            int(raw_page_count)
            if isinstance(raw_page_count, int)
            else (max(page.page_index for page in pages) + 1 if pages else 0)
        )
        return cls(
            version_name=data.get("version_name"),
            backend=data.get("backend"),
            page_count=page_count,
            pages=pages,
            unknown_types=[dict(item) for item in (data.get("unknown_types") or [])],
            metadata=dict(data.get("metadata") or {}),
        )

    @classmethod
    def from_json(cls, text: str) -> ProviderDocument:
        return cls.from_dict(json.loads(text))

    # ------------------------------------------------------------------ #
    # 便捷访问
    # ------------------------------------------------------------------ #
    def page(self, page_index: int) -> ProviderPage | None:
        for candidate in self.pages:
            if candidate.page_index == page_index:
                return candidate
        return None

    def iter_spans(self) -> Iterator[ProviderSpan]:
        """深度优先迭代整篇文档的全部 span（含容器块内的嵌套）。"""
        for page in self.pages:
            for block in page.iter_blocks(recursive=True):
                yield from block.iter_spans()
