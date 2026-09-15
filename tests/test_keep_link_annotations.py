"""_keep_link_annotations 对内联/间接两种 /Annots 形态的回归测试。

背景：hyperref 编译的 PDF 常把每页 /Annots 存成指向数组对象的间接引用
（``/Annots 176 0 R``）。历史实现只认内联数组，间接形态被整体置 null，
源 PDF 的全部超链接在 `_prepare_pdf` 阶段就被剥除（samples/main.pdf
实测 37 条链接全灭），后续快照/重映射/贴章全部拿到空输入。
"""

import pymupdf
from babeldoc.format.pdf import high_level as hl


def _page_link_count(page: pymupdf.Page) -> int:
    return len(page.get_links())


def _make_pdf_with_indirect_annots() -> pymupdf.Document:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_link(
        {
            "kind": pymupdf.LINK_URI,
            "from": pymupdf.Rect(10, 10, 80, 24),
            "uri": "https://example.com",
        }
    )
    page.insert_link(
        {
            "kind": pymupdf.LINK_GOTO,
            "from": pymupdf.Rect(10, 30, 80, 44),
            "page": 0,
            "to": pymupdf.Point(0, 0),
        }
    )
    page.add_text_annot(pymupdf.Point(50, 60), "a non-link note")

    kind, value = doc.xref_get_key(page.xref, "Annots")
    assert kind == "array", f"前提失败：新建 PDF 的 Annots 应为内联数组， got {kind}"
    array_xref = doc.get_new_xref()
    doc.update_object(array_xref, value)
    doc.xref_set_key(page.xref, "Annots", f"{array_xref} 0 R")
    kind, value = doc.xref_get_key(page.xref, "Annots")
    assert kind == "xref", f"前提失败：应为间接引用, got {kind}"
    return doc


def test_fix_null_xref_keeps_links_with_indirect_annots():
    doc = _make_pdf_with_indirect_annots()
    page = doc[0]
    assert _page_link_count(page) == 2

    hl.fix_null_xref(doc)

    links = page.get_links()
    assert len(links) == 2, "间接 /Annots 形态下 Link 注释不应被剥除"
    kinds = {link["kind"] for link in links}
    assert pymupdf.LINK_URI in kinds
    assert pymupdf.LINK_GOTO in kinds


def test_fix_null_xref_still_drops_non_link_annots_with_indirect_annots():
    doc = _make_pdf_with_indirect_annots()
    page = doc[0]
    assert len(list(page.annots())) == 1

    hl.fix_null_xref(doc)

    assert len(list(page.annots())) == 0, "非 Link 注释仍应被清除"
    assert _page_link_count(page) == 2


def test_fix_null_xref_keeps_links_with_inline_annots():
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_link(
        {
            "kind": pymupdf.LINK_URI,
            "from": pymupdf.Rect(10, 10, 80, 24),
            "uri": "https://example.com",
        }
    )
    # 新建未保存文档的 get_links 读缓存看不到刚插入的链接，tobytes 重开刷新
    doc = pymupdf.open("pdf", doc.tobytes())
    page = doc[0]
    kind, _value = doc.xref_get_key(page.xref, "Annots")
    assert kind == "array", f"前提失败：tobytes 重开后应保持内联数组, got {kind}"
    assert _page_link_count(page) == 1

    hl.fix_null_xref(doc)

    assert _page_link_count(page) == 1, "内联 /Annots 行为不应回归"


def test_prepare_pdf_end_to_end_with_indirect_annots(tmp_path):
    """samples 场景回归：完整 _prepare_pdf 后链接仍然存活。"""
    src = tmp_path / "indirect.pdf"
    doc = _make_pdf_with_indirect_annots()
    doc.save(str(src))
    doc.close()

    reopened = pymupdf.open(str(src))
    assert sum(_page_link_count(p) for p in reopened) == 2

    hl.fix_null_page_content(reopened)
    hl.fix_filter(reopened)
    hl.fix_null_xref(reopened)
    hl.fix_media_box(reopened)
    out = tmp_path / "prepared.pdf"
    reopened.save(str(out))
    reopened.close()

    prepared = pymupdf.open(str(out))
    assert sum(_page_link_count(p) for p in prepared) == 2
