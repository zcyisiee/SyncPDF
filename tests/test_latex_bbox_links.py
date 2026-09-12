"""LaTeX bbox overlay 的链接生命周期与 PDF 结构保真测试（实施计划大任务三）。

覆盖：
- URI 集合门禁：overlay 后 mono 的 URI 集合与源完全相同（落盘 reload 后仍相同）；
- 参考文献矩形落位：overlay 后经 ``_remap_links_by_char_identity`` 重定位，
  链接落在重排段落 box 内、目标不变、unresolved 不增加；
- 禁止 draw 假擦除：``draw_rect`` 被禁用时 overlay 仍成功且无双层文本；
- dual 链接搬运：并排 + 交替两种模式，译文侧链接数与 mono 一致、TOC 保留；
- overlay 失败不伤链接：渲染全失败时走回退路径，链接与 URI 集合不变；
- NAMED/GOTO reload 存活：保存后重开仍可解析命名目的地、GOTO 页码正确；
- 交替 dual 的 TOC 页码在页范围内且指向原文页（回归修复的缺陷）。
"""

from __future__ import annotations

import collections
import pathlib

import pymupdf
import pytest
from babeldoc.format.pdf.document_il import il_version_1
from babeldoc.format.pdf.document_il.backend import latex_bbox
from babeldoc.format.pdf.document_il.backend import link_remap
from babeldoc.format.pdf.document_il.backend.latex_bbox import (
    capability as capability_mod,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox import renderer as renderer_mod
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import (
    BboxStampRenderer,
)
from babeldoc.format.pdf.document_il.backend.pdf_creater import PDFCreater
from babeldoc.format.pdf.high_level import get_translation_stage
from babeldoc.format.pdf.translation_config import TranslationConfig
from babeldoc.format.pdf.translation_config import WatermarkOutputMode
from babeldoc.progress_monitor import ProgressMonitor

_CAPABILITY = capability_mod.probe_latex_capability()
requires_latex = pytest.mark.skipif(
    not _CAPABILITY.available, reason="需要可用的 XeLaTeX + 中文字体"
)

#: overlay 目标 bbox（IL y-up，页高 300）：对应 mupdf (30,180)-(300,230)。
_BOX_IL = [30.0, 70.0, 300.0, 120.0]
_BOX_MUPDF = pymupdf.Rect(30, 180, 300, 230)
#: 覆盖译文内容的链接矩形（mupdf）。
_IN_BOX_LINK = pymupdf.Rect(30, 180, 300, 200)
#: 段落外（下方）的链接矩形（mupdf）。
_OUT_BOX_LINK = pymupdf.Rect(30, 240, 300, 256)

#: 贴片文本：有中文字体时用中文（验证文本层可抽取），否则退化为 ASCII 标记。
_STAMP_TEXT = (
    "中文贴片内容" if _CAPABILITY.font_path is not None else "STAMP-MARKER"
)

_URI_IN_BOX = "https://dx.doi.org/10.14722/ndss.2026.241872"
_URI_OUT_BOX = "https://arxiv.org/abs/2601.00001"


# --------------------------------------------------------------------------- #
# fixture
# --------------------------------------------------------------------------- #
def _style(size: float = 9.0):
    return il_version_1.PdfStyle(
        font_id="F1",
        font_size=size,
        graphic_state=il_version_1.GraphicState(passthrough_per_char_instruction=""),
    )


def _translated_paragraph(debug_id: str, text: str, box_il, size: float = 9.0):
    style = _style(size)
    return il_version_1.PdfParagraph(
        box=il_version_1.Box(*box_il),
        pdf_style=style,
        pdf_paragraph_composition=[
            il_version_1.PdfParagraphComposition(
                pdf_same_style_unicode_characters=il_version_1.PdfSameStyleUnicodeCharacters(
                    unicode=text, pdf_style=style
                )
            )
        ],
        unicode=text,
        debug_id=debug_id,
        # 正文本体标签：full 模式（默认）只重排 body 标签的段落。
        layout_label="text",
        xobj_id=0,
        first_line_indent=False,
    )


def _il_doc(paragraphs, page_number=0, width=400, height=300):
    page = il_version_1.Page(
        page_number=page_number,
        pdf_paragraph=list(paragraphs),
        page_layout=[],
        cropbox=il_version_1.Cropbox(box=il_version_1.Box(0, 0, width, height)),
        mediabox=il_version_1.Mediabox(box=il_version_1.Box(0, 0, width, height)),
    )
    return il_version_1.Document(page=[page], total_pages=1)


class _FakeConfig:
    """最小 config 替身（overlay 只读取少量属性）。"""

    def __init__(self, **kwargs):
        self.latex_bbox_state = kwargs.get("latex_bbox_state", {})
        self.enable_latex_bbox_layout = kwargs.get("enable_latex_bbox_layout", True)
        self.latex_xelatex_path = kwargs.get(
            "latex_xelatex_path", _CAPABILITY.xelatex_path
        )
        self.latex_cjk_font_path = kwargs.get(
            "latex_cjk_font_path", _CAPABILITY.font_path
        )
        self.latex_compile_timeout_seconds = kwargs.get(
            "latex_compile_timeout_seconds", 45.0
        )
        self.latex_max_compile_workers = kwargs.get("latex_max_compile_workers", 2)
        self.latex_min_line_fill = kwargs.get("latex_min_line_fill", 1.01)
        self.latex_bbox_mode = kwargs.get("latex_bbox_mode", "full")
        self.primary_font_family = kwargs.get("primary_font_family", None)
        self.working_dir = kwargs.get("working_dir", None)
        self.latex_bbox_stats = {}


def _bbox_state(debug_id="P01-001", text="这是一段中文参考文献译文内容。"):
    return {
        "paragraphs": {
            debug_id: {
                "page": 0,
                "box": list(_BOX_IL),
                "font_size": 9.0,
                "layout_label": "text",
                "has_formula": True,
            }
        },
        "bodies": {debug_id: text},
        "plain_texts": {debug_id: text},
        "fusion_failures": {},
        "provider_inline_spans": 1,
    }


def _make_stamp_pdf(path: pathlib.Path, width=270.0, height=50.0):
    """构造一张可用于贴片的 stamp PDF（有中文字体时嵌入 CJK 字体）。"""
    doc = pymupdf.open()
    page = doc.new_page(width=width, height=height)
    if _CAPABILITY.font_path is not None:
        writer = pymupdf.TextWriter(page.rect)
        writer.append(
            (2, min(12, height - 2)),
            _STAMP_TEXT,
            font=pymupdf.Font(fontfile=_CAPABILITY.font_path),
            fontsize=8,
        )
        writer.write_text(page)
    else:
        page.insert_text((2, min(12, height - 2)), _STAMP_TEXT, fontsize=8)
    doc.save(path)
    doc.close()
    return str(path)


def _fake_render_many(stamp_path: str):
    def _impl(self, requests):
        _ = self
        return {
            request.key: renderer_mod.StampResult(
                key=request.key,
                ok=True,
                pdf_path=stamp_path,
                font_size=9.0,
                scale=1.0,
            )
            for request, _workdir in requests
        }

    return _impl


def _all_failing_render_many(self, requests):
    _ = self
    return {
        request.key: renderer_mod.StampResult(
            key=request.key, ok=False, reason="simulated-compile-failure"
        )
        for request, _workdir in requests
    }


def _link_rows(doc: pymupdf.Document):
    """(页号, 矩形, kind, uri, 目标页, 命名目的地) 元组列表（排序便于比较）。"""
    return sorted(
        (
            page.number,
            tuple(round(v, 2) for v in link["from"]),
            link.get("kind"),
            link.get("uri"),
            link.get("page"),
            link.get("nameddest"),
        )
        for page in doc
        for link in page.get_links()
    )


def _source_pdf(tmp_path: pathlib.Path, name="ref.pdf", npages=1):
    """参考文献页：段内 URI（DOI）+ 段外 URI（arXiv）。"""
    doc = pymupdf.open()
    for _ in range(npages):
        doc.new_page(width=400, height=300)
    page = doc[0]
    page.insert_text((30, 200), "Original reference text line one", fontsize=9)
    page.insert_text((30, 220), "Original reference text line two", fontsize=9)
    page.insert_link({"kind": pymupdf.LINK_URI, "from": _IN_BOX_LINK, "uri": _URI_IN_BOX})
    page.insert_link(
        {"kind": pymupdf.LINK_URI, "from": _OUT_BOX_LINK, "uri": _URI_OUT_BOX}
    )
    doc.set_toc([[1, "References", 1]])
    path = tmp_path / name
    doc.save(path)
    doc.close()
    return path


def _overlay(tmp_path, monkeypatch, *, stamp=None, renderer_error=None, **cfg_kwargs):
    src = _source_pdf(tmp_path)
    pdf = pymupdf.open(src)
    state = _bbox_state()
    docs = _il_doc([_translated_paragraph("P01-001", state["bodies"]["P01-001"], _BOX_IL)])
    config = _FakeConfig(latex_bbox_state=state, working_dir=tmp_path, **cfg_kwargs)
    if renderer_error is not None:
        monkeypatch.setattr(BboxStampRenderer, "render_many", renderer_error)
    else:
        stamp_path = stamp or _make_stamp_pdf(tmp_path / "stamp.pdf")
        monkeypatch.setattr(
            BboxStampRenderer, "render_many", _fake_render_many(stamp_path)
        )
    return src, pdf, docs, config


def _uri_set_of(doc: pymupdf.Document) -> set[str]:
    return {link["uri"] for page in doc for link in page.get_links() if link.get("uri")}


# --------------------------------------------------------------------------- #
# 1. URI 集合门禁
# --------------------------------------------------------------------------- #
def test_uri_set_identical_after_overlay_and_reload(tmp_path, monkeypatch):
    """overlay 后 URI 集合与源完全相同；落盘 reload 后仍相同。"""
    src, pdf, docs, config = _overlay(tmp_path, monkeypatch)
    source_uris = _uri_set_of(pymupdf.open(src))

    result_pdf, stats = latex_bbox.apply_latex_bbox_overlay(pdf, docs, config)
    out = tmp_path / "mono.pdf"
    result_pdf.save(out)

    assert stats["applied"] == 1
    assert stats["reverted"] is False
    assert stats["links"]["uri_set_match"] is True
    reloaded = pymupdf.open(out)
    try:
        assert _uri_set_of(reloaded) == source_uris
        assert _uri_set_of(reloaded) == {_URI_IN_BOX, _URI_OUT_BOX}
    finally:
        reloaded.close()


@requires_latex
def test_uri_set_identical_with_real_compile(tmp_path):
    """真实编译路径（无 monkeypatch）：URI 集合与链接矩形都必须不变。"""
    src = _source_pdf(tmp_path, name="real.pdf")
    pdf = pymupdf.open(src)
    before = _link_rows(pymupdf.open(src))
    state = _bbox_state()
    docs = _il_doc(
        [_translated_paragraph("P01-001", state["bodies"]["P01-001"], _BOX_IL)]
    )
    config = _FakeConfig(latex_bbox_state=state, working_dir=tmp_path)

    result_pdf, stats = latex_bbox.apply_latex_bbox_overlay(pdf, docs, config)
    out = tmp_path / "real-mono.pdf"
    result_pdf.save(out)

    assert stats["available"] is True
    assert stats["applied"] == 1
    assert stats["links"]["uri_set_match"] is True
    reloaded = pymupdf.open(out)
    try:
        assert _link_rows(reloaded) == before
    finally:
        reloaded.close()


# --------------------------------------------------------------------------- #
# 2. 参考文献矩形落位（overlay + link_remap）
# --------------------------------------------------------------------------- #
def _snapshot_for_remap(paragraph_ids, *, uri, from_rect, ratio=None):
    return {
        "link_index": 0,
        "uri": uri,
        "from": [from_rect.x0, from_rect.y0, from_rect.x1, from_rect.y1],
        "char_indices": [],
        "paragraph_ids": list(paragraph_ids),
        "src_rect_ratio": ratio,
    }


def test_reference_link_rect_lands_inside_reflowed_paragraph(tmp_path, monkeypatch):
    """overlay 后 remap：段内链接被重定位到重排段落 box 内，段外链接保持原位。"""
    src, pdf, docs, config = _overlay(tmp_path, monkeypatch)
    paragraph = docs.page[0].pdf_paragraph[0]

    result_pdf, _stats = latex_bbox.apply_latex_bbox_overlay(pdf, docs, config)

    # 段内链接（源字符不存活 → 段落 box 回退），段外链接（无段落归属 → unresolved 保持原位）
    entries = [
        _snapshot_for_remap(
            ["P01-001"],
            uri=_URI_IN_BOX,
            from_rect=_IN_BOX_LINK,
            ratio=[0.0, 0.0, 1.0, 0.5],
        ),
        _snapshot_for_remap([], uri=_URI_OUT_BOX, from_rect=_OUT_BOX_LINK),
    ]
    result = link_remap.remap_page_links(
        result_pdf[0],
        entries,
        [],
        {"P01-001": paragraph},
        300.0,
        alive_ids=set(),
    )

    assert result.total == 2
    assert result.remapped == 1
    assert result.fallback_paragraph == 1
    assert len(result.unresolved) == 1  # 段外链接不因 overlay 新增 unresolved

    out = tmp_path / "remapped.pdf"
    result_pdf.save(out)
    reloaded = pymupdf.open(out)
    try:
        links = {link.get("uri"): link for link in reloaded[0].get_links()}
        assert set(links) == {_URI_IN_BOX, _URI_OUT_BOX}
        moved = links[_URI_IN_BOX]["from"]
        # 段落 box（mupdf）：(30,180)-(300,230)
        assert moved.x0 >= 30 - 0.5 and moved.x1 <= 300 + 0.5
        assert moved.y0 >= 180 - 0.5 and moved.y1 <= 230 + 0.5
        # 目标/URI 不变
        assert links[_URI_IN_BOX]["uri"] == _URI_IN_BOX
        # 段外链接矩形未被 overlay 改动
        assert links[_URI_OUT_BOX]["from"] == _OUT_BOX_LINK
    finally:
        reloaded.close()


def test_remap_char_union_still_preferred_when_chars_alive(tmp_path, monkeypatch):
    """字符仍存活时仍走 char_union（overlay 未退化三级回退的优先级）。"""
    src, pdf, docs, config = _overlay(tmp_path, monkeypatch)
    box = il_version_1.Box(30, 700, 35, 710)
    char = il_version_1.PdfCharacter(
        pdf_style=_style(),
        box=box,
        visual_bbox=il_version_1.VisualBbox(box=box),
        char_unicode="h",
        advance=5.0,
        xobj_id=0,
    )
    paragraph = il_version_1.PdfParagraph(
        box=il_version_1.Box(30, 700, 35, 710),
        pdf_style=_style(),
        pdf_paragraph_composition=[
            il_version_1.PdfParagraphComposition(
                pdf_line=il_version_1.PdfLine(pdf_character=[char])
            )
        ],
        unicode="h",
        debug_id="P01-001",
        layout_label="text",
        xobj_id=0,
    )
    docs = _il_doc([paragraph])
    config.latex_bbox_state = _bbox_state()
    result_pdf, _stats = latex_bbox.apply_latex_bbox_overlay(pdf, docs, config)
    rect, method = link_remap.resolve_link_rect(
        {"char_indices": [0], "paragraph_ids": ["P01-001"]},
        [char],
        {"P01-001": paragraph},
        {id(char)},
    )
    assert method == "char_union"
    assert rect == pymupdf.Rect(30, 700, 35, 710)


# --------------------------------------------------------------------------- #
# 3. 禁止 draw 假擦除
# --------------------------------------------------------------------------- #
def test_overlay_never_uses_draw_rect_erase(tmp_path, monkeypatch):
    """``draw_rect`` 被禁用时 overlay 仍成功 → 证明没有用白矩形假擦除。"""
    src, pdf, docs, config = _overlay(tmp_path, monkeypatch)

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("overlay 不得使用 draw_rect 作为擦除机制")

    monkeypatch.setattr(pymupdf.Page, "draw_rect", _forbidden)
    result_pdf, stats = latex_bbox.apply_latex_bbox_overlay(pdf, docs, config)
    out = tmp_path / "no-draw.pdf"
    result_pdf.save(out)

    assert stats["applied"] == 1
    reloaded = pymupdf.open(out)
    try:
        in_box = reloaded[0].get_text(clip=_BOX_MUPDF)
        assert _STAMP_TEXT[:2] in in_box
        assert "Original reference text" not in in_box
    finally:
        reloaded.close()


def test_redaction_path_physically_removes_source_text(tmp_path, monkeypatch):
    """文本层必须被物理移除（非叠加遮盖）：页级抽取中不再含原英文。"""
    src, pdf, docs, config = _overlay(tmp_path, monkeypatch)
    result_pdf, _stats = latex_bbox.apply_latex_bbox_overlay(pdf, docs, config)
    out = tmp_path / "clean.pdf"
    result_pdf.save(out)

    reloaded = pymupdf.open(out)
    try:
        full = reloaded[0].get_text()
        assert "Original reference text line one" not in full
        assert "Original reference text line two" not in full
        assert _STAMP_TEXT[:2] in full
    finally:
        reloaded.close()


# --------------------------------------------------------------------------- #
# 4. dual 链接搬运（并排 + 交替）
# --------------------------------------------------------------------------- #
def _dual_config(tmp_path, *, alternating: bool, translate_first: bool = False):
    src = tmp_path / "dual-src.pdf"
    doc = pymupdf.open()
    doc.new_page(width=400, height=300)
    doc.new_page(width=400, height=300)
    doc.new_page(width=400, height=300)
    doc[0].insert_text((30, 70), "page zero", fontsize=9)
    doc[1].insert_text((30, 70), "page one", fontsize=9)
    doc[2].insert_text((30, 70), "page two", fontsize=9)
    doc[0].insert_link(
        {"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(30, 50, 300, 66), "uri": _URI_IN_BOX}
    )
    doc[1].insert_link(
        {
            "kind": pymupdf.LINK_GOTO,
            "from": pymupdf.Rect(30, 50, 300, 66),
            "page": 2,
            "to": pymupdf.Point(10, 20),
        }
    )
    # 命名目的地 + 只带 nameddest 的 NAMED 链接（真实论文引用形态）
    p2_ref = doc[2].xref
    arr = doc.get_new_xref()
    doc.update_object(arr, f"[{p2_ref} 0 R /XYZ 0 700 0]")
    names_arr = doc.get_new_xref()
    doc.update_object(names_arr, f"[(sec1) {arr} 0 R]")
    dests = doc.get_new_xref()
    doc.update_object(dests, f"<< /Names {names_arr} 0 R >>")
    names = doc.get_new_xref()
    doc.update_object(names, f"<< /Dests {dests} 0 R >>")
    doc.xref_set_key(doc.pdf_catalog(), "Names", f"{names} 0 R")
    doc[0].insert_link(
        {"kind": pymupdf.LINK_NAMED, "from": pymupdf.Rect(30, 70, 300, 86), "nameddest": "sec1"}
    )
    doc[0].insert_link(
        {
            "kind": pymupdf.LINK_NAMED,
            "from": pymupdf.Rect(30, 90, 300, 106),
            "page": 1,
            "to": pymupdf.Point(10, 20),
            "nameddest": "sec1",
        }
    )
    # 真实论文里的外部 URL 常是 LAUNCH/GOTOR（file 目标），搬运时不能丢。
    doc[1].insert_link(
        {
            "kind": pymupdf.LINK_LAUNCH,
            "from": pymupdf.Rect(30, 90, 300, 106),
            "file": "www.ndss-symposium.org",
        }
    )
    doc.set_toc([[1, "First", 1], [1, "Second", 2], [1, "Third", 3]])
    doc.save(src, garbage=0)
    doc.close()

    config = TranslationConfig(
        input_file=str(src),
        lang_in="en",
        lang_out="zh",
        doc_layout_model=object(),
        output_dir=str(tmp_path),
        working_dir=str(tmp_path / f"wd-{alternating}-{translate_first}"),
        watermark_output_mode=WatermarkOutputMode.NoWatermark,
    )
    config.progress_monitor = ProgressMonitor(get_translation_stage(config))
    config.use_alternating_pages_dual = alternating
    config.dual_translate_first = translate_first
    pages = [
        il_version_1.Page(
            page_number=index,
            cropbox=il_version_1.Cropbox(box=il_version_1.Box(0, 0, 400, 300)),
            mediabox=il_version_1.Mediabox(box=il_version_1.Box(0, 0, 400, 300)),
        )
        for index in range(3)
    ]
    docs = il_version_1.Document(page=pages, total_pages=3)
    return src, config, docs


def _make_dual(tmp_path, *, alternating, translate_first=False):
    src, config, docs = _dual_config(
        tmp_path, alternating=alternating, translate_first=translate_first
    )
    creater = PDFCreater(src, docs, config, {})
    original = pymupdf.open(src)
    translated = pymupdf.open(src)
    if alternating:
        dual = creater.create_alternating_pages_dual_pdf(original, translated, config)
    else:
        dual = creater.create_side_by_side_dual_pdf(
            original, translated, str(tmp_path / "dual.pdf"), config
        )
    out = tmp_path / f"dual-out-{alternating}-{translate_first}.pdf"
    dual.save(out)
    dual.close()
    config.cleanup_temp_files()
    return src, pymupdf.open(out)


def _link_targets(doc: pymupdf.Document, indices=None, *, alternating: bool = False):
    """链接**语义目标**计数（统一 kind 差异，可跨双页布局比较）。

    - ``uri``/``file`` 原样计数；
    - 只带 ``nameddest`` 的 NAMED 计为 ``("named", 目的地)``；
    - 带 ``page`` 的 GOTO/NAMED 归一到 ``("page", mono 页索引)``：产品搬运
      时会有意把带 page 的 NAMED 转成 GOTO（不依赖命名目的地），且交替布局
      下目标页号会按 ``dual 索引 // 2`` 重映射，这里逆映射回 mono 页索引。
    """
    counter: collections.Counter = collections.Counter()
    for index in indices if indices is not None else range(doc.page_count):
        for link in doc[index].get_links():
            if link.get("uri"):
                counter[("uri", link["uri"])] += 1
            elif link.get("file"):
                counter[("file", link["file"])] += 1
            elif link.get("nameddest") and link.get("page") is None:
                counter[("named", link["nameddest"])] += 1
            elif link.get("page") is not None:
                target = int(link["page"])
                if alternating:
                    target //= 2
                counter[("page", target)] += 1
    return counter


def test_side_by_side_dual_copies_links_and_toc(tmp_path):
    """并排 dual：左右两侧链接都被搬运，URI/GOTO/NAMED 目标不变，TOC 保留。"""
    src, dual = _make_dual(tmp_path, alternating=False)
    try:
        source = pymupdf.open(src)
        assert dual.page_count == source.page_count
        assert dual.get_toc() == source.get_toc()
        # 并排模式下每页 = 源页链接 + mono 页链接（stand-in 与源同）→ 双份
        for index in range(source.page_count):
            expected = collections.Counter(
                {
                    key: count * 2
                    for key, count in _link_targets(source, [index]).items()
                }
            )
            assert _link_targets(dual, [index]) == expected
        assert _uri_set_of(dual) == _uri_set_of(source)
        # 右侧（译文侧）矩形被平移 +400，左侧与源一致
        page0 = dual[0].get_links()
        assert any(link["from"].x0 >= 400 for link in page0)
        assert any(link["from"].x1 <= 400 for link in page0)
        # 不允许出现“悬空”链接：每条链接必须有可解析目标（命名目的地可解析，
        # 或已带显式页目标/URI/file）——搬运不得丢目标。
        names = dual.resolve_names()
        for page in dual:
            for link in page.get_links():
                if link.get("uri") or link.get("file"):
                    continue
                has_page = link.get("page") is not None
                nameddest = link.get("nameddest")
                resolves = bool(nameddest) and nameddest in names
                assert has_page or resolves, f"悬空链接: {link}"
        source.close()
    finally:
        dual.close()


@pytest.mark.parametrize("translate_first", [False, True])
def test_alternating_dual_preserves_translated_side_links(
    tmp_path, translate_first
):
    """交替 dual：译文侧与原文侧链接目标都与 mono 侧一致（NAMED 不再丢失）。"""
    src, dual = _make_dual(
        tmp_path, alternating=True, translate_first=translate_first
    )
    try:
        source = pymupdf.open(src)
        page_count = source.page_count
        assert dual.page_count == page_count * 2
        for index in range(page_count):
            expected = _link_targets(source, [index])
            if not expected:
                continue
            for target in (index * 2, index * 2 + 1):
                assert (
                    _link_targets(dual, [target], alternating=True) == expected
                ), f"交替页 {target} 的链接目标与 mono 第 {index + 1} 页不一致"
        assert _uri_set_of(dual) == _uri_set_of(source)
        source.close()
    finally:
        dual.close()


@pytest.mark.parametrize("translate_first", [False, True])
def test_alternating_dual_toc_pages_in_range_and_point_to_original(
    tmp_path, translate_first
):
    """交替 dual 的 TOC 页码必须在页范围内（回归：原实现会越界丢目录）。"""
    src, dual = _make_dual(
        tmp_path, alternating=True, translate_first=translate_first
    )
    try:
        source = pymupdf.open(src)
        toc = dual.get_toc()
        assert toc, "交替 dual 丢失了全部目录"
        assert len(toc) == len(source.get_toc())
        for _level, _title, page in toc:
            assert 1 <= page <= dual.page_count
        # 第一条目录应指向原文第 1 页所在的交替页
        expected = 2 if translate_first else 1
        assert toc[0][2] == expected
        # 该页内容确实来自源第 1 页
        assert dual[toc[0][2] - 1].get_text() == source[0].get_text()
        source.close()
    finally:
        dual.close()


def test_alternating_dual_goto_targets_point_to_same_side(tmp_path):
    """交错 dual：译文侧 GOTO 必须落在译文侧目标页（内容与 mono 目标页一致）。"""
    src = tmp_path / "orig.pdf"
    trans_path = tmp_path / "trans.pdf"
    for path, tag in ((src, "ORIG"), (trans_path, "TRANS")):
        doc = pymupdf.open()
        for index in range(3):
            doc.new_page(width=400, height=300)
            doc[index].insert_text((10, 40), f"{tag}{index}", fontsize=12)
        doc.save(path)
        doc.close()

    mono = pymupdf.open(trans_path)
    mono[0].insert_link(
        {
            "kind": pymupdf.LINK_GOTO,
            "from": pymupdf.Rect(10, 10, 100, 26),
            "page": 2,
            "to": pymupdf.Point(5, 6),
        }
    )
    mono_path = tmp_path / "mono.pdf"
    mono.save(mono_path)
    mono.close()
    mono = pymupdf.open(mono_path)

    config = TranslationConfig(
        input_file=str(src),
        lang_in="en",
        lang_out="zh",
        doc_layout_model=object(),
        output_dir=str(tmp_path),
        working_dir=str(tmp_path / "wd"),
        watermark_output_mode=WatermarkOutputMode.NoWatermark,
    )
    config.progress_monitor = ProgressMonitor(get_translation_stage(config))
    config.use_alternating_pages_dual = True
    config.dual_translate_first = False
    pages = [
        il_version_1.Page(
            page_number=index,
            cropbox=il_version_1.Cropbox(box=il_version_1.Box(0, 0, 400, 300)),
            mediabox=il_version_1.Mediabox(box=il_version_1.Box(0, 0, 400, 300)),
        )
        for index in range(3)
    ]
    docs = il_version_1.Document(page=pages, total_pages=3)
    creater = PDFCreater(str(src), docs, config, {})
    dual = creater.create_alternating_pages_dual_pdf(
        pymupdf.open(src), mono, config
    )
    out = tmp_path / "dual-goto.pdf"
    dual.save(out)
    dual.close()
    config.cleanup_temp_files()

    reloaded = pymupdf.open(out)
    try:
        # 译文页 idx1（TRANS0 内容）上的 GOTO 必须指向译文页 idx5（TRANS2）
        assert reloaded[1].get_text().strip() == "TRANS0"
        gotos = [
            link
            for link in reloaded[1].get_links()
            if link.get("kind") == pymupdf.LINK_GOTO
        ]
        assert gotos, "译文侧 GOTO 丢失"
        assert gotos[0]["page"] == 5
        assert reloaded[gotos[0]["page"]].get_text().strip() == "TRANS2"
    finally:
        reloaded.close()


def test_dual_from_overlay_output_keeps_all_links(tmp_path, monkeypatch):
    """端到端：overlay 后的 mono 作为 dual 输入，两侧链接/URI 集合仍与源一致。"""
    src, pdf, docs, config = _overlay(tmp_path, monkeypatch)
    overlay_pdf, stats = latex_bbox.apply_latex_bbox_overlay(pdf, docs, config)
    assert stats["applied"] == 1
    mono_path = tmp_path / "overlay-mono.pdf"
    overlay_pdf.save(mono_path)
    overlay_pdf.close()

    dual_config = TranslationConfig(
        input_file=str(src),
        lang_in="en",
        lang_out="zh",
        doc_layout_model=object(),
        output_dir=str(tmp_path),
        working_dir=str(tmp_path / "dual-wd"),
        watermark_output_mode=WatermarkOutputMode.NoWatermark,
    )
    dual_config.progress_monitor = ProgressMonitor(get_translation_stage(dual_config))
    creater = PDFCreater(str(src), docs, dual_config, {})
    original = pymupdf.open(src)
    mono_doc = pymupdf.open(mono_path)
    dual = creater.create_side_by_side_dual_pdf(
        original, mono_doc, str(tmp_path / "overlay-dual.pdf"), dual_config
    )
    out = tmp_path / "overlay-dual.pdf"
    dual.save(out)
    dual.close()
    dual_config.cleanup_temp_files()

    reloaded = pymupdf.open(out)
    try:
        source = pymupdf.open(src)
        assert _uri_set_of(reloaded) == _uri_set_of(source)
        assert reloaded.get_toc() == source.get_toc()
        # 两侧各一份链接
        assert _link_targets(reloaded, [0]) == collections.Counter(
            {
                key: count * 2
                for key, count in _link_targets(source, [0]).items()
            }
        )
        source.close()
    finally:
        reloaded.close()
        mono_doc.close()


def test_dual_link_helper_handles_named_and_launch():
    """搬运辅助函数：NAMED(无 page) 保命名目的地，LAUNCH/GOTOR 按 file 复制。"""
    map_point = lambda x, y: pymupdf.Point(x, y)  # noqa: E731
    rect = pymupdf.Rect(1, 2, 3, 4)
    named = PDFCreater._dual_link_for_source(
        {"kind": pymupdf.LINK_NAMED, "nameddest": "sec1"}, rect, map_point
    )
    assert named == {"kind": pymupdf.LINK_NAMED, "from": rect, "nameddest": "sec1"}
    launch = PDFCreater._dual_link_for_source(
        {"kind": pymupdf.LINK_LAUNCH, "file": "www.example.org"}, rect, map_point
    )
    assert launch == {"kind": pymupdf.LINK_LAUNCH, "from": rect, "file": "www.example.org"}
    gotor = PDFCreater._dual_link_for_source(
        {"kind": pymupdf.LINK_GOTOR, "file": "www.example.org"}, rect, map_point
    )
    assert gotor and gotor["file"] == "www.example.org"
    # NAMED 带 page → GOTO（依赖页码，不依赖命名目的地）
    goto = PDFCreater._dual_link_for_source(
        {
            "kind": pymupdf.LINK_NAMED,
            "page": 3,
            "to": pymupdf.Point(5, 6),
            "nameddest": "sec1",
        },
        rect,
        map_point,
    )
    assert goto["kind"] == pymupdf.LINK_GOTO and goto["page"] == 3
    # 未知 kind → None（不插入）
    assert PDFCreater._dual_link_for_source({"kind": 99}, rect, map_point) is None


# --------------------------------------------------------------------------- #
# 5. overlay 失败不伤链接
# --------------------------------------------------------------------------- #
def test_overlay_render_failure_keeps_links_untouched(tmp_path, monkeypatch):
    """渲染全失败 → 回退路径，链接多重集与 URI 集合与 overlay 前完全一致。"""
    src, pdf, docs, config = _overlay(
        tmp_path, monkeypatch, renderer_error=_all_failing_render_many
    )
    before = _link_rows(pymupdf.open(src))

    result_pdf, stats = latex_bbox.apply_latex_bbox_overlay(pdf, docs, config)
    out = tmp_path / "fallback.pdf"
    result_pdf.save(out)

    assert stats["applied"] == 0
    assert stats["reverted"] is False
    assert stats["failed"] == 1
    reloaded = pymupdf.open(out)
    try:
        assert _link_rows(reloaded) == before
    finally:
        reloaded.close()


def test_overlay_capability_missing_keeps_links_untouched(tmp_path, monkeypatch):
    """能力缺失 → 直接回退；链接不变。"""
    src, pdf, docs, config = _overlay(tmp_path, monkeypatch)
    before = _link_rows(pymupdf.open(src))
    config.latex_xelatex_path = "/nonexistent/xelatex"
    config.latex_cjk_font_path = "/nonexistent/font.ttf"

    result_pdf, stats = latex_bbox.apply_latex_bbox_overlay(pdf, docs, config)
    out = tmp_path / "no-cap.pdf"
    result_pdf.save(out)

    assert stats["available"] is False
    assert stats["applied"] == 0
    reloaded = pymupdf.open(out)
    try:
        assert _link_rows(reloaded) == before
    finally:
        reloaded.close()


# --------------------------------------------------------------------------- #
# 6. NAMED/GOTO reload 存活
# --------------------------------------------------------------------------- #
def test_overlay_keeps_named_and_goto_resolvable_after_reload(tmp_path, monkeypatch):
    """overlay 含内部命名的文档：reload 后 NAMED 可解析、GOTO 页码不变。"""
    doc = pymupdf.open()
    doc.new_page(width=400, height=300)
    doc.new_page(width=400, height=300)
    page = doc[0]
    page.insert_text((30, 200), "Original reference text line one", fontsize=9)
    page.insert_text((30, 220), "Original reference text line two", fontsize=9)
    # 命名目的地 sec1 -> 第 2 页
    p1_ref = doc[1].xref
    arr = doc.get_new_xref()
    doc.update_object(arr, f"[{p1_ref} 0 R /XYZ 0 700 0]")
    names_arr = doc.get_new_xref()
    doc.update_object(names_arr, f"[(sec1) {arr} 0 R]")
    dests = doc.get_new_xref()
    doc.update_object(dests, f"<< /Names {names_arr} 0 R >>")
    names = doc.get_new_xref()
    doc.update_object(names, f"<< /Dests {dests} 0 R >>")
    doc.xref_set_key(doc.pdf_catalog(), "Names", f"{names} 0 R")
    page.insert_link(
        {"kind": pymupdf.LINK_NAMED, "from": _IN_BOX_LINK, "nameddest": "sec1"}
    )
    page.insert_link(
        {
            "kind": pymupdf.LINK_GOTO,
            "from": _OUT_BOX_LINK,
            "page": 1,
            "to": pymupdf.Point(10, 20),
        }
    )
    src = tmp_path / "named.pdf"
    doc.save(src, garbage=0)
    doc.close()

    pdf = pymupdf.open(src)
    state = _bbox_state()
    docs = _il_doc(
        [_translated_paragraph("P01-001", state["bodies"]["P01-001"], _BOX_IL)]
    )
    config = _FakeConfig(latex_bbox_state=state, working_dir=tmp_path)
    stamp_path = _make_stamp_pdf(tmp_path / "stamp.pdf")
    monkeypatch.setattr(
        BboxStampRenderer, "render_many", _fake_render_many(stamp_path)
    )

    result_pdf, stats = latex_bbox.apply_latex_bbox_overlay(pdf, docs, config)
    out = tmp_path / "named-out.pdf"
    result_pdf.save(out)

    assert stats["applied"] == 1
    reloaded = pymupdf.open(out)
    try:
        names_map = reloaded.resolve_names()
        links = reloaded[0].get_links()
        named = [link for link in links if link.get("kind") == pymupdf.LINK_NAMED]
        assert named, "NAMED 链接在 overlay 后丢失"
        assert named[0]["nameddest"] in names_map
        gotos = [link for link in links if link.get("kind") == pymupdf.LINK_GOTO]
        assert gotos and gotos[0]["page"] == 1
    finally:
        reloaded.close()


# --------------------------------------------------------------------------- #
# 7. 既有链接门禁回归（overlay 失败时 URI 门禁仍通过）
# --------------------------------------------------------------------------- #
def test_uri_gate_passes_on_fallback_output(tmp_path, monkeypatch):
    """回退路径的输出仍通过 ``assert_uri_set_matches``（不阻断翻译）。"""
    src, pdf, docs, config = _overlay(
        tmp_path, monkeypatch, renderer_error=_all_failing_render_many
    )
    expected = _uri_set_of(pymupdf.open(src))
    result_pdf, _stats = latex_bbox.apply_latex_bbox_overlay(pdf, docs, config)
    out = tmp_path / "gate.pdf"
    result_pdf.save(out)
    link_remap.assert_uri_set_matches(
        expected, link_remap.uri_set(pymupdf.open(out)), context="mono"
    )


def test_overlay_stats_report_links_restored(tmp_path, monkeypatch):
    """统计必须报告 overlay 前链接总数与被 redaction 删除后重插的数量。"""
    src, pdf, docs, config = _overlay(tmp_path, monkeypatch)
    _result, stats = latex_bbox.apply_latex_bbox_overlay(pdf, docs, config)
    assert stats["links"]["before_total"] == 2
    assert stats["links"]["after_total"] == 2
    assert stats["links"]["restored"] >= 1
    assert stats["links"]["uri_set_match"] is True
