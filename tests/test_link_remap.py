"""超链接源字符映射测试（板块 5）。

覆盖：
- ``link_remap`` 字符并集 / 段内比例投影 / unresolved 三级回退
- 存活判定（Typesetting 后源字符对象不再被引用 → 不能读它的过期 box）
- URI 集合门禁（缺失 URI → RuntimeError）
- ``_rebuild_link`` 的 URI / GOTO / NAMED 构造
- ``link_snapshot.snapshot_links`` / ``resolve_link_chars`` 解析侧快照
"""

from __future__ import annotations

import pymupdf
import pytest
from babeldoc.format.pdf.document_il import il_version_1
from babeldoc.format.pdf.document_il.backend import link_remap
from babeldoc.tools.agent import link_snapshot


# --------------------------------------------------------------------------- #
# 合成对象
# --------------------------------------------------------------------------- #
def _style():
    return il_version_1.PdfStyle(
        font_id="F1",
        font_size=10.0,
        graphic_state=il_version_1.GraphicState(passthrough_per_char_instruction=""),
    )


def _char(text: str, x: float, y: float, w: float = 5.0, h: float = 10.0):
    box = il_version_1.Box(x=x, y=y, x2=x + w, y2=y + h)
    return il_version_1.PdfCharacter(
        pdf_style=_style(),
        box=box,
        visual_bbox=il_version_1.VisualBbox(box=box),
        char_unicode=text,
        advance=w,
        xobj_id=0,
    )


def _paragraph(debug_id: str, chars, box=None):
    comps = [
        il_version_1.PdfParagraphComposition(
            pdf_line=il_version_1.PdfLine(pdf_character=list(chars))
        )
    ] if chars else [
        il_version_1.PdfParagraphComposition(
            pdf_same_style_characters=il_version_1.PdfSameStyleCharacters(
                pdf_character=list(chars), pdf_style=_style()
            )
        )
    ]
    return il_version_1.PdfParagraph(
        box=box or il_version_1.Box(0, 0, 100, 20),
        pdf_style=_style(),
        pdf_paragraph_composition=comps,
        unicode=debug_id,
        debug_id=debug_id,
        layout_label="text",
        xobj_id=0,
    )


def _page(chars, paragraphs=()):
    return il_version_1.Page(
        page_number=0,
        pdf_paragraph=list(paragraphs),
        page_layout=[],
        cropbox=il_version_1.Cropbox(
            box=il_version_1.Box(x=0, y=0, x2=600, y2=800)
        ),
        pdf_character=list(chars),
    )


def _mupdf_page(links, width=600, height=800):
    """新建一页并插入链接。

    注意：pymupdf 的 ``insert_link`` 要等 save/reload 后才会出现在
    ``get_links()`` 里（注释对象未回写），所以这里存到临时文件再打开。
    """
    import tempfile
    from pathlib import Path

    doc = pymupdf.open()
    page = doc.new_page(width=width, height=height)
    for link in links:
        page.insert_link(link)
    tmp = Path(tempfile.mkdtemp()) / "page.pdf"
    doc.save(str(tmp))
    doc.close()
    reloaded = pymupdf.open(str(tmp))
    return reloaded, reloaded[0]



def _reload_page(page):
    """remap 后 pymupdf 的内存链接表是陈旧的（delete+insert 不刷新），
    保存并重新打开才能观察结果。产品代码在 save 时正会读到新注释。"""
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp()) / "reloaded.pdf"
    page.parent.save(str(tmp))
    doc = pymupdf.open(str(tmp))
    return doc, doc[page.number]


# --------------------------------------------------------------------------- #
# il_box_union / 存活判定
# --------------------------------------------------------------------------- #
class TestBoxUnion:
    def test_union_of_alive_chars(self):
        chars = [_char("a", 10, 700), _char("b", 20, 700), _char("c", 30, 700)]
        rect = link_remap.il_box_union(chars)
        assert rect is not None
        assert rect.x0 == pytest.approx(10)
        assert rect.x1 == pytest.approx(35)
        assert rect.y0 == pytest.approx(700)
        assert rect.y1 == pytest.approx(710)

    def test_ignores_dead_chars(self):
        alive = _char("a", 10, 700)
        dead = _char("b", 500, 100)
        alive_ids = {id(alive)}
        rect = link_remap.il_box_union([alive, dead], alive_ids)
        assert rect is not None
        # dead 的坐标不能进并集
        assert rect.x1 == pytest.approx(15)

    def test_ignores_degenerate_boxes(self):
        flat = _char("x", 10, 700, w=0.0)
        assert link_remap.il_box_union([flat]) is None

    def test_empty_when_no_chars(self):
        assert link_remap.il_box_union([]) is None


class TestAliveIds:
    def test_build_alive_ids_follows_composition_reference(self):
        original = _char("a", 10, 700)
        replacement = _char("b", 10, 700)
        paragraph = _paragraph("P01-001", [original])
        page = _page([], [paragraph])
        doc = il_version_1.Document(page=[page])
        assert id(original) in link_remap.build_alive_char_ids(doc)
        # 模拟 Typesetting 重排：composition 换成新对象
        paragraph.pdf_paragraph_composition = [
            il_version_1.PdfParagraphComposition(
                pdf_line=il_version_1.PdfLine(pdf_character=[replacement])
            )
        ]
        alive = link_remap.build_alive_char_ids(doc)
        assert id(replacement) in alive
        assert id(original) not in alive


# --------------------------------------------------------------------------- #
# resolve_link_rect 三级回退
# --------------------------------------------------------------------------- #
class TestResolveLinkRect:
    def test_char_union_when_chars_alive(self):
        chars = [_char("h", 10, 700), _char("i", 20, 700)]
        paragraph = _paragraph("P01-001", chars)
        page = _page(chars, [paragraph])
        doc = il_version_1.Document(page=[page])
        alive = link_remap.build_alive_char_ids(doc)
        entry = {
            "char_indices": [0, 1],
            "paragraph_ids": ["P01-001"],
            "paragraph_char_ranges": {},
        }
        rect, method = link_remap.resolve_link_rect(
            entry, link_snapshot.collect_page_chars(page), {"P01-001": paragraph}, alive
        )
        assert method == "char_union"
        assert rect.x0 == pytest.approx(10)
        assert rect.x1 == pytest.approx(25)

    def test_paragraph_box_fallback_when_chars_dead(self):
        """译文重排后源字符对象不再存活 → 回退到段落 box（板块 5 第二级）。"""
        chars = [_char("a", 100, 700), _char("b", 110, 700), _char("c", 120, 700)]
        paragraph = _paragraph(
            "P01-001", chars, box=il_version_1.Box(100, 690, 130, 710)
        )
        page = _page(chars, [paragraph])
        entry = {
            "char_indices": [2],
            "paragraph_ids": ["P01-001"],
        }
        # alive_ids 不含原字符（Typesetting 已把它们从 composition 换掉）
        rect, method = link_remap.resolve_link_rect(
            entry, link_snapshot.collect_page_chars(page), {"P01-001": paragraph}, set()
        )
        assert method == "paragraph"
        assert rect == pymupdf.Rect(100, 690, 130, 710)

    def test_paragraph_box_used_directly_when_no_chars(self):
        paragraph = _paragraph("P01-001", [], box=il_version_1.Box(5, 6, 55, 26))
        entry = {"char_indices": [], "paragraph_ids": ["P01-001"]}
        rect, method = link_remap.resolve_link_rect(
            entry, [], {"P01-001": paragraph}, set()
        )
        assert method == "paragraph"
        assert rect == pymupdf.Rect(5, 6, 55, 26)

    def test_unresolved_when_nothing_available(self):
        entry = {
            "char_indices": [],
            "paragraph_ids": ["P99-999"],
            "paragraph_char_ranges": {},
        }
        rect, method = link_remap.resolve_link_rect(entry, [], {}, set())
        assert rect is None
        assert method == "unresolved"

# --------------------------------------------------------------------------- #
# remap_page_links 写回
# --------------------------------------------------------------------------- #
class TestRemapPageLinks:
    def _setup(self, page_height=800.0):
        # IL 字符在 y=700..710；翻到 pymupdf 坐标即 y=90..100
        chars = [_char("h", 10, 700), _char("i", 20, 700), _char("j", 30, 700)]
        paragraph = _paragraph(
            "P01-001", chars, box=il_version_1.Box(10, 700, 35, 710)
        )
        page = _page(chars, [paragraph])
        doc = il_version_1.Document(page=[page])
        alive = link_remap.build_alive_char_ids(doc)
        page_char_objects = link_snapshot.collect_page_chars(page)
        # 源链接矩形（pymupdf 坐标）覆盖这 3 个字符
        src_rect = pymupdf.Rect(10, 90, 35, 100)
        return chars, paragraph, page_char_objects, alive, src_rect

    def test_remaps_uri_link_to_char_union(self):
        chars, paragraph, pco, alive, src_rect = self._setup()
        doc, dst = _mupdf_page(
            [
                {
                    "kind": pymupdf.LINK_URI,
                    "from": src_rect,
                    "uri": "https://example.com/x",
                }
            ]
        )
        entry = {
            "link_index": 0,
            "uri": "https://example.com/x",
            "from": [src_rect.x0, src_rect.y0, src_rect.x1, src_rect.y1],
            "char_indices": [0, 1, 2],
            "paragraph_ids": ["P01-001"],
            "paragraph_char_ranges": {},
        }
        result = link_remap.remap_page_links(
            dst, [entry], pco, {"P01-001": paragraph}, 800.0, alive_ids=alive
        )
        assert result.remapped == 1
        assert result.unresolved == []
        _doc2, page2 = _reload_page(dst)
        links = page2.get_links()
        assert len(links) == 1
        assert links[0]["uri"] == "https://example.com/x"
        assert links[0]["from"] == pymupdf.Rect(10, 90, 35, 100)

    def test_keeps_original_rect_when_unresolved(self):
        _chars, _paragraph, pco, _alive, src_rect = self._setup()
        doc, dst = _mupdf_page(
            [{"kind": pymupdf.LINK_URI, "from": src_rect, "uri": "https://x"}]
        )
        entry = {
            "link_index": 0,
            "uri": "https://x",
            "from": [src_rect.x0, src_rect.y0, src_rect.x1, src_rect.y1],
            "char_indices": [],
            "paragraph_ids": [],
            "paragraph_char_ranges": {},
        }
        result = link_remap.remap_page_links(dst, [entry], pco, {}, 800.0, alive_ids=set())
        assert result.remapped == 0
        assert len(result.unresolved) == 1
        assert result.unresolved[0]["uri"] == "https://x"
        # 原链接仍在（不删）
        _doc2, page2 = _reload_page(dst)
        assert len(page2.get_links()) == 1

    def test_internal_goto_link_rebuilt_and_remapped(self):
        """内部跳转（LINK_GOTO）重映射后仍是 GOTO，且目标页/点保留。"""
        chars, paragraph, pco, alive, src_rect = self._setup()
        # pymupdf 要求 GOTO 的目标页存在
        doc = pymupdf.open()
        doc.new_page(width=600, height=800)
        doc.new_page(width=600, height=800)
        doc[0].insert_link(
            {
                "kind": pymupdf.LINK_GOTO,
                "from": src_rect,
                "page": 1,
                "to": pymupdf.Point(12, 100),
            }
        )
        import tempfile
        from pathlib import Path as _Path

        tmp = _Path(tempfile.mkdtemp()) / "goto.pdf"
        doc.save(str(tmp))
        doc.close()
        reloaded = pymupdf.open(str(tmp))
        dst = reloaded[0]
        entry = {
            "link_index": 0,
            "uri": None,
            "from": [src_rect.x0, src_rect.y0, src_rect.x1, src_rect.y1],
            "char_indices": [0],
            "paragraph_ids": ["P01-001"],
            "paragraph_char_ranges": {},
        }
        result = link_remap.remap_page_links(
            dst, [entry], pco, {"P01-001": paragraph}, 800.0, alive_ids=alive
        )
        assert result.remapped == 1
        _doc2, page2 = _reload_page(dst)
        links = page2.get_links()
        assert links[0]["kind"] == pymupdf.LINK_GOTO
        assert links[0]["page"] == 1
        assert links[0]["from"] == pymupdf.Rect(10, 90, 15, 100)

    def test_no_state_is_noop(self):
        doc, dst = _mupdf_page(
            [
                {
                    "kind": pymupdf.LINK_URI,
                    "from": pymupdf.Rect(1, 2, 3, 4),
                    "uri": "https://keep.example",
                }
            ]
        )
        result = link_remap.remap_page_links(dst, [], [], {}, 800.0, alive_ids=set())
        assert result.total == 0
        _doc2, page2 = _reload_page(dst)
        assert len(page2.get_links()) == 1


# --------------------------------------------------------------------------- #
# URI 集合门禁
# --------------------------------------------------------------------------- #
class TestRebuildLinkNamed:
    def test_named_without_page_keeps_nameddest(self):
        rect = pymupdf.Rect(1, 2, 3, 4)
        out = link_remap._rebuild_link(
            {"kind": pymupdf.LINK_NAMED, "nameddest": "section.1"}, rect
        )
        assert out == {
            "kind": pymupdf.LINK_NAMED,
            "from": rect,
            "nameddest": "section.1",
        }

    def test_uri_from_file_field(self):
        rect = pymupdf.Rect(1, 2, 3, 4)
        out = link_remap._rebuild_link(
            {"kind": pymupdf.LINK_LAUNCH, "file": "https://x.example"}, rect
        )
        # LAUNCH 不是支持的 kind → None（保持原链接）
        assert out is None


class TestUriGate:
    def test_matching_sets_pass(self):
        link_remap.assert_uri_set_matches(
            {"a", "b"}, {"a", "b"}, context="mono"
        )

    def test_missing_uri_raises(self):
        with pytest.raises(RuntimeError) as exc:
            link_remap.assert_uri_set_matches(
                {"a", "b"}, {"a"}, context="mono"
            )
        assert "link_uri_set_mismatch" in str(exc.value)
        assert "缺失" in str(exc.value)

    def test_extra_uri_raises(self):
        with pytest.raises(RuntimeError) as exc:
            link_remap.assert_uri_set_matches(
                {"a"}, {"a", "b"}, context="mono"
            )
        assert "新增" in str(exc.value)

    def test_uri_set_reads_document(self):
        doc, page = _mupdf_page(
            [
                {
                    "kind": pymupdf.LINK_URI,
                    "from": pymupdf.Rect(1, 2, 3, 4),
                    "uri": "https://a.example",
                },
                {
                    "kind": pymupdf.LINK_URI,
                    "from": pymupdf.Rect(5, 6, 7, 8),
                    "uri": "https://b.example",
                },
            ]
        )
        assert link_remap.uri_set(doc) == {"https://a.example", "https://b.example"}


# --------------------------------------------------------------------------- #
# _rebuild_link
# --------------------------------------------------------------------------- #
class TestRebuildLink:
    def test_uri(self):
        rect = pymupdf.Rect(1, 2, 3, 4)
        out = link_remap._rebuild_link(
            {"kind": pymupdf.LINK_URI, "uri": "https://x"}, rect
        )
        assert out == {"kind": pymupdf.LINK_URI, "from": rect, "uri": "https://x"}

    def test_named_becomes_goto(self):
        rect = pymupdf.Rect(1, 2, 3, 4)
        out = link_remap._rebuild_link(
            {"kind": pymupdf.LINK_NAMED, "page": 7, "to": pymupdf.Point(9, 10)}, rect
        )
        assert out["kind"] == pymupdf.LINK_GOTO
        assert out["page"] == 7
        assert out["to"] == pymupdf.Point(9, 10)

    def test_unknown_kind_returns_none(self):
        out = link_remap._rebuild_link({"kind": 99}, pymupdf.Rect(1, 2, 3, 4))
        assert out is None


# --------------------------------------------------------------------------- #
# link_snapshot 解析侧
# --------------------------------------------------------------------------- #
class TestSnapshotLinks:
    def test_snapshot_links_shape(self, tmp_path):
        pdf_path = tmp_path / "links.pdf"
        doc = pymupdf.open()
        page = doc.new_page(width=600, height=800)
        page.insert_link(
            {
                "kind": pymupdf.LINK_URI,
                "from": pymupdf.Rect(10, 20, 60, 30),
                "uri": "https://example.com",
            }
        )
        page.insert_link(
            {
                "kind": pymupdf.LINK_NAMED,
                "from": pymupdf.Rect(70, 20, 90, 30),
                "page": 0,
                "to": pymupdf.Point(1, 2),
                "nameddest": "section.1",
            }
        )
        doc.save(str(pdf_path))
        doc.close()

        snap = link_snapshot.snapshot_links(pdf_path)
        assert list(snap.keys()) == ["0"]
        assert len(snap["0"]) == 2
        assert snap["0"][0]["kind"] == "URI"
        assert snap["0"][0]["uri"] == "https://example.com"
        assert snap["0"][0]["from"] == [10.0, 20.0, 60.0, 30.0]
        # NAMED 链接在 pymupdf 回读后保留 kind（nameddest 需文档里真有该命名目的地）
        assert snap["0"][1]["kind"] == "NAMED"

    def test_resolve_link_chars_matches_char_boxes(self):
        # IL 字符 y=700..710；翻到 pymupdf 坐标 y=90..100
        chars = [_char("a", 10, 700), _char("b", 20, 700)]
        paragraph = _paragraph("P01-001", chars)
        # 真实 extract 中段落字符已从 page.pdf_character 移出，这里保持一致
        page = _page([], [paragraph])
        links = [
            {
                "link_index": 0,
                "page_index": 0,
                "kind": "URI",
                "uri": "https://example.com",
                "from": [10.0, 90.0, 25.0, 100.0],
            }
        ]
        out = link_snapshot.resolve_link_chars(links, page, 800.0)
        assert out[0]["char_indices"] == [0, 1]
        assert out[0]["paragraph_ids"] == ["P01-001"]

    def test_resolve_link_chars_no_overlap(self):
        chars = [_char("a", 10, 700)]
        paragraph = _paragraph("P01-001", chars)
        page = _page([], [paragraph])
        links = [
            {
                "link_index": 0,
                "page_index": 0,
                "kind": "URI",
                "uri": "https://example.com",
                "from": [400.0, 400.0, 500.0, 500.0],
            }
        ]
        out = link_snapshot.resolve_link_chars(links, page, 800.0)
        assert out[0]["char_indices"] == []
        assert out[0]["paragraph_ids"] == []

    def test_collect_page_chars_order_is_stable(self):
        para_chars = [_char("a", 10, 700), _char("b", 20, 700)]
        orphan = _char("z", 500, 700)
        paragraph = _paragraph("P01-001", para_chars)
        page = _page([orphan], [paragraph])
        collected = link_snapshot.collect_page_chars(page)
        # 段落字符在前，未入段的 page.pdf_character 在后
        assert collected[:2] == para_chars
        assert collected[2] is orphan


def test_safe_insert_link_repairs_link_substring_corruption(tmp_path):
    """pymupdf 1.27 getLinkText 的 /NM 注入缺陷回归：URI 含 "/Link" 子串。

    上游 ``annot.replace("/Link", "/Link/NM(name)")`` 会把 URI 中首次出现的
    "/Link" 一并改写（真实论文 llvm.org/docs/LinkTimeOptimization.html 被
    污染并触发 URI 集合门禁失败）。safe_insert_link 必须修复该污染。
    """
    import pymupdf
    from babeldoc.format.pdf.document_il.backend.link_remap import safe_insert_link

    doc = pymupdf.open()
    page = doc.new_page(width=300, height=100)
    uri = "https://llvm.org/docs/LinkTimeOptimization.html"
    safe_insert_link(page, {"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(10, 10, 100, 30), "uri": uri})
    # pymupdf 的 link 读缓存在 insert 后是脏的（get_links 可能暂不可见），
    # 一律用落盘 reload 验证（污染发生在对象序列化层）。
    out = tmp_path / "link-corrupt.pdf"
    doc.save(out)
    doc.close()
    doc = pymupdf.open(out)
    uris = [l["uri"] for l in doc[0].get_links()]
    assert uris == [uri]
    doc.close()


def test_remap_page_links_preserves_uri_containing_link_substring(tmp_path):
    """remap_page_links 全链路：URI 含 "/Link" 子串时重定位后目标不变。"""
    import pymupdf
    from babeldoc.format.pdf.document_il.backend.link_remap import remap_page_links

    doc = pymupdf.open()
    page = doc.new_page(width=300, height=200)
    uri = "https://example.org/docs/LinkCheck.html"
    # 暂存注记不能用 insert_link（它自身就会触发同一上游缺陷把 URI 污染），
    # 用 xref 手工构造干净的源链接。
    annot_xref = doc.get_new_xref()
    doc.update_object(
        annot_xref,
        f"<</A<</S/URI/URI({uri})>>/Rect[10 20 100 40]/Subtype/Link>>",
    )
    doc.xref_set_key(page.xref, "Annots", f"[{annot_xref} 0 R]")
    # pymupdf 的 insert_link 要等 save/reload 后才会出现在 get_links 里。
    staged = tmp_path / "staged.pdf"
    doc.save(staged)
    doc.close()
    doc = pymupdf.open(staged)
    page = doc[0]
    assert [l["uri"] for l in page.get_links()] == [uri]  # 暂存必须干净
    entries = [
        {
            "link_index": 0,
            "from": (10, 160, 100, 180),
            "uri": uri,
            "char_indices": [],
            "paragraph_ids": ["p0"],
            "src_rect_ratio": (0.0, 0.0, 1.0, 1.0),
        }
    ]

    class FakeBox:
        x, y, x2, y2 = 20, 40, 260, 60

    class FakeParagraph:
        box = FakeBox()

    result = remap_page_links(
        page, entries, [], {"p0": FakeParagraph()}, page.rect.height
    )
    assert result.remapped == 1
    out = tmp_path / "remap-corrupt.pdf"
    doc.save(out)
    doc.close()
    doc = pymupdf.open(out)
    assert [l["uri"] for l in doc[0].get_links()] == [uri]
    doc.close()
