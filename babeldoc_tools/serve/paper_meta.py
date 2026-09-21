"""论文标题 / 一作：从 workdir 产物抽取，并落到 ``papers`` 表的两列。

抽取顺序（每条都**只读本地产物**，不联网、不调模型）：

1. 源 PDF metadata 的 ``title`` / ``author``（pymupdf 打开 ``source.pdf``）；
2. provider IR（``agent/source/{provider,mineru}/provider_ir.json``）首页：第一个
   ``type == "title"`` 块的文本 = 标题；**紧随其后**的第一个 ``type == "text"`` 块
   = 作者行（形如 ``Jack Brimberg <sup>a</sup>, Said Salhi <sup>b</sup>``）。

两个字段**各自独立回退**：metadata 给得出标题但没有作者时，作者仍从 IR 取。
抽不到就保持 ``None``（视图层再回退文件名 stem）—— **不拿文件名冒充标题**。

落库职责：``papers.title`` / ``papers.authors`` 这两个列早就建好了，但此前只有
``database.register_document`` 的 ``INSERT OR IGNORE INTO papers(id)``，两列永远是
NULL。本模块是它们**唯一**的写入者，写入口径是「只填空」：已有非空值不会被一次读不到
产物降级成 NULL（见 :meth:`MetadataDB.set_paper_meta`）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from babeldoc_tools.serve.workdir import WorkdirReader

__all__ = [
    "PaperMeta",
    "extract_paper_meta",
    "first_author_of",
    "provider_ir_meta",
    "resolve_paper_meta",
    "source_pdf_meta",
]

#: 源 PDF 的文件名（与 :mod:`babeldoc_tools.serve.uploads` 的 ``SOURCE_NAME`` 一致，
#: ``input.pdf`` 是旧版 workdir 的别名，``store.list_dids`` 也两个都认）。
SOURCE_NAMES = ("source.pdf", "input.pdf")

#: ``<sup>a</sup>`` / ``<sup>c,d,∗</sup>``：MinerU 把上标单位标记原样留在 span 文本里。
#: 连**内容**一起删（那是单位编号，不是姓名的一部分）；未成对的标签再由 ``_SUP_TAG_LOOSE`` 兜底。
_SUP_ELEMENT = re.compile(r"<sup\b[^>]*>.*?</sup>", re.I | re.S)
_SUP_TAG_LOOSE = re.compile(r"</?\s*sup\b[^>]*>", re.I)

#: 作者条目末尾的单位上标（没被 ``<sup>`` 包住时）：`` a`` / `` c,d`` / `` 12`` + 星号。
_AFFILIATION_TAIL = re.compile(
    r"\s+(?:[a-z]{1,2}(?:\s*,\s*[a-z]{1,2})*|\d{1,2}(?:\s*,\s*\d{1,2})*)[\s*∗∗†‡§¶]*$"
)

#: 作者分隔符：先 ``;``（metadata 常见 ``A; B; C``），再 ``,`` 与 `` and ``（IR 作者行）。
_SEMICOLON = re.compile(r";")
_AUTHOR_SEPARATOR = re.compile(r",|\s+and\s+")

#: 作者行判据的长度上限（再长的 ``text`` 块更可能是摘要而不是作者行）。
_AUTHOR_LINE_MAX = 220

_WHITESPACE = re.compile(r"\s+")
_NOT_ALNUM = re.compile(r"[^0-9a-z]+")


@dataclass(frozen=True)
class PaperMeta:
    """一篇论文的展示信息；两个字段都可能为 ``None``（抽不到就是抽不到）。"""

    title: str | None = None
    #: 作者整串（原样保留、分词号，便于以后按需再拆）。
    authors: str | None = None

    @property
    def first_author(self) -> str | None:
        """一作姓名（作者整串的第一个条目，剥掉上标单位标记）。"""
        return first_author_of(self.authors)


def _normalize(text: Any) -> str | None:
    """任意值 → 单行文本（空白折叠）；空/非字符串 → ``None``。"""
    if not isinstance(text, str):
        return None
    collapsed = _WHITESPACE.sub(" ", text).strip()
    return collapsed or None


def _strip_sup(text: str) -> str:
    """去掉上标单位标记（``Brimberg <sup>a</sup>, S`` → ``Brimberg, S``）并折叠多余空白。"""
    without_elements = _SUP_ELEMENT.sub(" ", text)
    return _WHITESPACE.sub(" ", _SUP_TAG_LOOSE.sub(" ", without_elements)).replace(" ,", ",").strip()


def first_author_of(authors: str | None) -> str | None:
    """作者整串 → 一作姓名。

    分隔符优先 ``;``（``Binxu Wang; Jacob Zavatone-Veth`` → ``Binxu Wang``），只有一段
    时才用 ``,`` / `` and ``（IR 作者行 ``Jack Brimberg a, Said Salhi b`` → ``Jack Brimberg``）。
    末尾的单位上标（``a`` / ``c,d`` / ``12`` / ``∗``）剥掉；剥完为空则返回 ``None``。
    """
    normalized = _normalize(authors)
    if normalized is None:
        return None
    text = _strip_sup(normalized)
    parts = [part.strip() for part in _SEMICOLON.split(text) if part.strip()]
    if len(parts) == 1:
        parts = [part.strip() for part in _AUTHOR_SEPARATOR.split(text) if part.strip()]
    if not parts:
        return None
    name = _AFFILIATION_TAIL.sub("", parts[0]).strip(" ,;.")
    return name or None


def _clean_title(text: Any) -> str | None:
    """标题：折叠空白，去掉「metadata 里塞的是文件名」这一常见导出器痕迹。"""
    return _normalize(text)


def _clean_authors(text: Any) -> str | None:
    """作者整串：折叠空白 + 去掉 ``<sup>`` 标签，保留多作者与分隔符原样。"""
    normalized = _normalize(text)
    return None if normalized is None else _strip_sup(normalized) or None


def _same_as_file_name(title: str, file_name: str) -> bool:
    """标题与源文件名同形（``Microsoft Word - paper.pdf`` 这类导出器痕迹）。"""
    stem = Path(file_name).stem
    title_key = _NOT_ALNUM.sub("", title.lower())
    if not title_key:
        return False
    return title_key in {
        _NOT_ALNUM.sub("", stem.lower()),
        _NOT_ALNUM.sub("", file_name.lower()),
    }


def source_pdf_meta(source_pdf: Path) -> tuple[str | None, str | None]:
    """源 PDF metadata → ``(title, author)``；没有 pymupdf / 打不开 → 全 ``None``。"""
    try:
        import pymupdf
    except ImportError:  # web extra 没装 pymupdf 时不影响其它端点
        return None, None
    try:
        with pymupdf.open(source_pdf) as pdf:
            metadata = pdf.metadata or {}
    except Exception:  # noqa: BLE001 - 坏 PDF/无权限只让标题缺失，不让列表 500
        return None, None
    title = _clean_title(metadata.get("title"))
    if title is not None and _same_as_file_name(title, source_pdf.name):
        title = None
    return title, _clean_authors(metadata.get("author"))


def _block_text(block: dict) -> str:
    """block → 文本（行内 span 直接相接，行与行之间留一个空格）。"""
    lines = block.get("lines")
    if not isinstance(lines, list):
        return ""
    rendered: list[str] = []
    for line in lines:
        spans = line.get("spans") if isinstance(line, dict) else None
        if not isinstance(spans, list):
            continue
        rendered.append(
            "".join(
                span.get("content", "")
                for span in spans
                if isinstance(span, dict) and isinstance(span.get("content"), str)
            )
        )
    return " ".join(part for part in rendered if part)


def _looks_like_author_line(text: str) -> bool:
    """IR 首页 title 块之后的 ``text`` 块是否像作者行（短、无句号分隔的完整句子）。"""
    return len(text) <= _AUTHOR_LINE_MAX and ". " not in text


def provider_ir_meta(reader: WorkdirReader) -> tuple[str | None, str | None]:
    """provider IR 首页 → ``(title, author)``；产物不可用/没有 title 块 → 全 ``None``。"""
    payload = reader.provider_ir()
    pages = payload.get("pages") if isinstance(payload, dict) else None
    if not isinstance(pages, list) or not pages:
        return None, None
    first_page = pages[0]
    blocks = first_page.get("blocks") if isinstance(first_page, dict) else None
    if not isinstance(blocks, list):
        return None, None
    typed = [block for block in blocks if isinstance(block, dict)]
    title_index = next(
        (index for index, block in enumerate(typed) if block.get("type") == "title"),
        None,
    )
    if title_index is None:
        return None, None
    title = _clean_title(_block_text(typed[title_index]))
    authors: str | None = None
    # 只认紧随 title 块的**第一个** text 块：再往后就是摘要了，扫下去只会认错。
    for block in typed[title_index + 1 :]:
        if block.get("type") != "text":
            continue
        candidate = _clean_authors(_block_text(block))
        if candidate is not None and _looks_like_author_line(candidate):
            authors = candidate
        break
    return title, authors


def extract_paper_meta(reader: WorkdirReader) -> PaperMeta:
    """从 workdir 产物抽取；每条路径都可能给出 ``None``，且不会抛异常。"""
    source_pdf = next(
        (reader.workdir / name for name in SOURCE_NAMES if (reader.workdir / name).is_file()),
        None,
    )
    title, authors = (None, None) if source_pdf is None else source_pdf_meta(source_pdf)
    if title is None or authors is None:
        # provider IR 可能几十 MB：只在真缺字段时才读（metadata 齐全时一次都不读）。
        ir_title, ir_authors = provider_ir_meta(reader)
        title = title or ir_title
        authors = authors or ir_authors
    return PaperMeta(title=title, authors=authors)


def resolve_paper_meta(database, reader: WorkdirReader, did: str) -> PaperMeta:
    """视图层用：先读库里已存的标题/作者，缺字段时从产物抽取并**只填空**地落库。

    ``papers`` 两列是慢变量（源 PDF 与 provider IR 定了就不再变），所以读路径上的
    懒回填是一次性成本：第一次把老文档补上，之后都命中库里已有值。
    """
    stored_title, stored_authors = database.paper_meta(did)
    if stored_title is None or stored_authors is None:
        fresh = extract_paper_meta(reader)
        if fresh.title is not None or fresh.authors is not None:
            database.set_paper_meta(did, fresh.title, fresh.authors)
            stored_title = stored_title or fresh.title
            stored_authors = stored_authors or fresh.authors
    return PaperMeta(title=stored_title, authors=stored_authors)
