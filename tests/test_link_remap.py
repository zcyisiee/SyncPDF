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

    def test_launch_and_gotor_preserve_file_target(self):
        """LAUNCH/GOTOR（外部 file 目标）不再被判为不支持的 kind。

        旧实现只认 URI/GOTO/NAMED，参考文献里的 LAUNCH/GOTOR 会直接变成
        unresolved；现在按 file 复制保留目标。
        """
        rect = pymupdf.Rect(1, 2, 3, 4)
        launch = link_remap._rebuild_link(
            {"kind": pymupdf.LINK_LAUNCH, "file": "https://x.example"}, rect
        )
        assert launch == {
            "kind": pymupdf.LINK_LAUNCH,
            "from": rect,
            "file": "https://x.example",
        }
        gotor = link_remap._rebuild_link(
            {"kind": pymupdf.LINK_GOTOR, "file": "other.pdf"}, rect
        )
        assert gotor is not None and gotor["file"] == "other.pdf"

    def test_goto_with_string_to_does_not_crash(self):
        """GOTOR 的 ``to`` 可能是命名目的地字符串，不能假设是 Point。"""
        rect = pymupdf.Rect(1, 2, 3, 4)
        out = link_remap._rebuild_link(
            {"kind": pymupdf.LINK_GOTO, "page": 2, "to": "named-dest"}, rect
        )
        assert out is not None and out["page"] == 2
        assert "to" not in out


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


# --------------------------------------------------------------------------- #
# 就地 /Rect 更新：动作保真、失败安全、跨行拆分
# --------------------------------------------------------------------------- #
class _FakeBox:
    def __init__(self, x, y, x2, y2):
        self.x, self.y, self.x2, self.y2 = x, y, x2, y2


class _FakeParagraph:
    def __init__(self, box):
        self.box = _FakeBox(*box)


def _staged_page(tmp_path, links, name="staged.pdf"):
    """插入若干链接并落盘重开（pymupdf 落盘后 xref/get_links 才真实）。"""
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    for link in links:
        page.insert_link(link)
    path = tmp_path / name
    doc.save(path)
    doc.close()
    return pymupdf.open(path)


def _entry(link_index, from_rect, *, uid=None, paragraph="p", ratio=(0.0, 0.0, 1.0, 1.0)):
    entry = {
        "link_index": link_index,
        "uri": uid,
        "from": [from_rect.x0, from_rect.y0, from_rect.x1, from_rect.y1],
        "char_indices": [],
        "paragraph_ids": [paragraph] if paragraph else [],
    }
    if ratio is not None:
        entry["src_rect_ratio"] = ratio
    return entry


def test_inplace_rect_update_preserves_launch_and_uri_actions(tmp_path):
    """原地 /Rect 更新：LAUNCH/GOTOR 的 file 与 URI 目标在落盘后都不变。"""
    src_launch = pymupdf.Rect(50, 50, 120, 70)
    src_uri = pymupdf.Rect(50, 90, 120, 110)
    doc = _staged_page(
        tmp_path,
        [
            {
                "kind": pymupdf.LINK_LAUNCH,
                "from": src_launch,
                "file": "www.example.org",
            },
            {"kind": pymupdf.LINK_URI, "from": src_uri, "uri": "https://a.example"},
        ],
    )
    page = doc[0]
    paragraph = _FakeParagraph((100, 600, 400, 660))
    entries = [
        _entry(0, src_launch, uid=None),
        _entry(1, src_uri, uid="https://a.example"),
    ]
    result = link_remap.remap_page_links(
        page, entries, [], {"p": paragraph}, 800.0
    )
    assert result.remapped == 2
    assert result.unresolved == []

    out = tmp_path / "action-out.pdf"
    doc.save(out)
    doc.close()
    reloaded = pymupdf.open(out)
    try:
        links = reloaded[0].get_links()
        assert len(links) == 2
        launch = [link for link in links if link.get("file")]
        assert launch and launch[0]["file"] == "www.example.org"
        # LAUNCH 落盘后会被 pymupdf 规范化为 GOTOR，目标语义不变即可。
        assert launch[0]["kind"] in (pymupdf.LINK_LAUNCH, pymupdf.LINK_GOTOR)
        uri = [link for link in links if link.get("uri")]
        assert uri and uri[0]["uri"] == "https://a.example"
        # 新矩形来自段落比例投影（近似方法 → 记入 fallbacks 诊断）。
        assert all(
            fallback["link_index"] in (0, 1) for fallback in result.fallbacks
        )
    finally:
        reloaded.close()


def test_inplace_update_keeps_custom_border_and_action(tmp_path):
    """原地更新只改 /Rect：自定义 /Border 与原始 /A 动作逐字节保留。"""
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    annot_xref = doc.get_new_xref()
    doc.update_object(
        annot_xref,
        "<</A<</S/Launch/F<</F(www.keep.example)/Type/Filespec>>>>"
        "/Rect[10 20 100 40]/Border[0 0 1]/Subtype/Link>>",
    )
    doc.xref_set_key(page.xref, "Annots", f"[{annot_xref} 0 R]")
    staged = tmp_path / "border.pdf"
    doc.save(staged)
    doc.close()
    doc = pymupdf.open(staged)
    page = doc[0]

    entries = [_entry(0, pymupdf.Rect(10, 760, 100, 780))]
    result = link_remap.remap_page_links(
        page, entries, [], {"p": _FakeParagraph((0, 700, 600, 760))}, 800.0
    )
    assert result.remapped == 1

    out = tmp_path / "border-out.pdf"
    doc.save(out)
    doc.close()
    reloaded = pymupdf.open(out)
    try:
        xrefs = list(
            __import__("re").findall(
                r"(\d+)\s+0\s+R",
                reloaded.xref_get_key(reloaded[0].xref, "Annots")[1],
            )
        )
        assert xrefs
        border_kind, border_value = reloaded.xref_get_key(int(xrefs[0]), "Border")
        assert border_kind == "array"
        assert "/Launch" in reloaded.xref_object(int(xrefs[0]))
        links = reloaded[0].get_links()
        assert links and links[0]["file"] == "www.keep.example"
    finally:
        reloaded.close()


def test_write_failure_keeps_source_link_unresolved(tmp_path, monkeypatch):
    """写入失败只记 unresolved，不删源链接。"""
    src_uri = pymupdf.Rect(50, 90, 120, 110)
    doc = _staged_page(
        tmp_path,
        [{"kind": pymupdf.LINK_URI, "from": src_uri, "uri": "https://safe.example"}],
    )
    page = doc[0]

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated xref write failure")

    monkeypatch.setattr(link_remap, "_set_annot_rects", _boom)
    result = link_remap.remap_page_links(
        page,
        [_entry(0, src_uri, uid="https://safe.example")],
        [],
        {"p": _FakeParagraph((10, 600, 300, 700))},
        800.0,
    )
    assert result.remapped == 0
    assert len(result.unresolved) == 1
    assert result.unresolved[0]["reason"] == "write_failed"

    out = tmp_path / "safe-out.pdf"
    doc.save(out)
    doc.close()
    reloaded = pymupdf.open(out)
    try:
        links = reloaded[0].get_links()
        assert len(links) == 1
        assert links[0]["uri"] == "https://safe.example"
    finally:
        reloaded.close()


def test_multiline_stamp_rects_split_physical_annotations(tmp_path):
    """跨行标记链接：保留逐行矩形，克隆动作，不产生 bdoclink 占位链接。"""
    src = pymupdf.Rect(50, 50, 120, 70)
    doc = _staged_page(
        tmp_path,
        [
            {
                "kind": pymupdf.LINK_LAUNCH,
                "from": src,
                "file": "www.multiline.example",
            }
        ],
    )
    page = doc[0]
    stamp = {
        0: [
            pymupdf.Rect(300, 300, 312, 314),
            pymupdf.Rect(300, 320, 360, 334),
        ]
    }
    result = link_remap.remap_page_links(
        page, [_entry(0, src, uid=None)], [], {}, 800.0, stamp_rects=stamp
    )
    assert result.remapped == 1
    assert result.stamp_resolved == 1
    assert result.split_annotations == 1
    assert result.physical_annotations == 2
    assert result.fallback_paragraph == 0
    # 同进程内刷新后就应看到两条物理注记（dual 搬运读的就是这个）。
    assert len(page.get_links()) == 2

    out = tmp_path / "multiline-out.pdf"
    doc.save(out)
    doc.close()
    reloaded = pymupdf.open(out)
    try:
        links = reloaded[0].get_links()
        assert len(links) == 2
        assert all(link.get("file") == "www.multiline.example" for link in links)
        assert not any(
            str(link.get("uri") or "").startswith("bdoclink://") for link in links
        )
        rects = sorted(
            (round(link["from"].y0, 1), round(link["from"].x0, 1)) for link in links
        )
        assert rects[0][0] != rects[1][0]
    finally:
        reloaded.close()


def test_multiline_split_preserves_indirect_annots_array(tmp_path):
    """/Annots 是间接数组时，克隆追加后仍保持间接数组结构。"""
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    page.insert_link(
        {
            "kind": pymupdf.LINK_URI,
            "from": pymupdf.Rect(50, 50, 120, 70),
            "uri": "https://indirect.example",
        }
    )
    stage = tmp_path / "indirect-stage.pdf"
    doc.save(stage)
    doc.close()

    doc = pymupdf.open(stage)
    page = doc[0]
    kind, value = doc.xref_get_key(page.xref, "Annots")
    assert kind == "array"
    array_xref = doc.get_new_xref()
    doc.update_object(array_xref, value)
    doc.xref_set_key(page.xref, "Annots", f"{array_xref} 0 R")
    indirect = tmp_path / "indirect.pdf"
    doc.save(indirect)
    doc.close()

    doc = pymupdf.open(indirect)
    page = doc[0]
    assert doc.xref_get_key(page.xref, "Annots")[0] == "xref"
    result = link_remap.remap_page_links(
        page,
        [_entry(0, pymupdf.Rect(50, 50, 120, 70))],
        [],
        {},
        800.0,
        stamp_rects={
            0: [pymupdf.Rect(100, 100, 110, 112), pymupdf.Rect(100, 120, 150, 132)]
        },
    )
    assert result.physical_annotations == 2
    out = tmp_path / "indirect-out.pdf"
    doc.save(out)
    doc.close()
    reloaded = pymupdf.open(out)
    try:
        assert reloaded.xref_get_key(reloaded[0].xref, "Annots")[0] == "xref"
        assert len(reloaded[0].get_links()) == 2
    finally:
        reloaded.close()


def test_nameddest_action_preserved_after_remap(tmp_path):
    """只带 nameddest 的 NAMED 链接重定位后仍可解析命名目的地。"""
    doc = pymupdf.open()
    doc.new_page(width=600, height=800)
    doc.new_page(width=600, height=800)
    page = doc[0]
    p1_ref = doc[1].xref
    dest = doc.get_new_xref()
    doc.update_object(dest, f"[{p1_ref} 0 R /XYZ 0 700 0]")
    names_arr = doc.get_new_xref()
    doc.update_object(names_arr, f"[(sec1) {dest} 0 R]")
    dests = doc.get_new_xref()
    doc.update_object(dests, f"<< /Names {names_arr} 0 R >>")
    names = doc.get_new_xref()
    doc.update_object(names, f"<< /Dests {dests} 0 R >>")
    doc.xref_set_key(doc.pdf_catalog(), "Names", f"{names} 0 R")
    src = pymupdf.Rect(50, 50, 120, 70)
    page.insert_link(
        {"kind": pymupdf.LINK_NAMED, "from": src, "nameddest": "sec1"}
    )
    path = tmp_path / "named.pdf"
    doc.save(path, garbage=0)
    doc.close()

    doc = pymupdf.open(path)
    page = doc[0]
    result = link_remap.remap_page_links(
        page,
        [_entry(0, src, paragraph="p")],
        [],
        {"p": _FakeParagraph((10, 600, 300, 700))},
        800.0,
    )
    assert result.remapped == 1
    out = tmp_path / "named-out.pdf"
    doc.save(out)
    doc.close()
    reloaded = pymupdf.open(out)
    try:
        links = reloaded[0].get_links()
        assert links and links[0].get("nameddest") == "sec1"
        assert "sec1" in reloaded.resolve_names()
    finally:
        reloaded.close()


# --------------------------------------------------------------------------- #
# 文本锚点匹配（数字边界 / 重复标签 / 回退诊断）
# --------------------------------------------------------------------------- #
def test_find_anchor_occurrences_numeric_boundary():
    """数字锚点带边界校验：3 不匹配 13，重复出现各自独立。"""
    compact = [
        ("1", pymupdf.Rect(0, 0, 5, 10)),
        ("3", pymupdf.Rect(5, 0, 10, 10)),
    ]
    assert link_remap._find_anchor_occurrences(compact, "3") == []

    compact = [
        ("[", pymupdf.Rect(0, 0, 3, 10)),
        ("3", pymupdf.Rect(3, 0, 8, 10)),
        ("]", pymupdf.Rect(8, 0, 11, 10)),
        ("[", pymupdf.Rect(20, 0, 23, 10)),
        ("3", pymupdf.Rect(23, 0, 28, 10)),
        ("]", pymupdf.Rect(28, 0, 31, 10)),
    ]
    occurrences = link_remap._find_anchor_occurrences(compact, "3")
    assert len(occurrences) == 2
    assert occurrences[0]["rect"].x0 == 3
    assert occurrences[1]["rect"].x0 == 23


def _text_with_citation_link(tmp_path, text, *, name="anchors.pdf"):
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((50, 400), text, fontsize=12)
    page.insert_link(
        {
            "kind": pymupdf.LINK_URI,
            "from": pymupdf.Rect(50, 380, 60, 395),
            "uri": "https://cite.example/3",
        }
    )
    path = tmp_path / name
    doc.save(path)
    doc.close()
    return pymupdf.open(path)


def test_anchor_match_avoids_numeric_substring(tmp_path):
    """段内锚点匹配避开 ``13`` 里的 ``3``，落在独立引文号上。"""
    doc = _text_with_citation_link(tmp_path, "see A[3] and B[13] here")
    page = doc[0]
    entry = _entry(0, pymupdf.Rect(50, 380, 60, 395), uid="https://cite.example/3")
    entry["source_text"] = "3"
    entry["paragraph_ids"] = ["P"]
    entry["src_rect_ratio"] = (0.0, 0.0, 0.05, 1.0)
    result = link_remap.remap_page_links(
        page, [entry], [], {"P": _FakeParagraph((0, 390, 600, 415))}, 800.0
    )
    assert result.remapped == 1
    assert result.anchor_resolved == 1
    assert result.fallback_paragraph == 0

    out = tmp_path / "anchor-out.pdf"
    doc.save(out)
    doc.close()
    reloaded = pymupdf.open(out)
    try:
        rect = reloaded[0].get_links()[0]["from"]
        x13 = reloaded[0].search_for("13")[0].x0
        assert rect.x1 <= x13 + 0.5
    finally:
        reloaded.close()


def test_repeated_labels_assigned_to_distinct_occurrences(tmp_path):
    """同一标签在段内重复：按源相对位置就近分配，不全部塞给第一个命中。"""
    doc = _text_with_citation_link(tmp_path, "n[3] m[3]", name="repeat.pdf")
    page = doc[0]
    left = pymupdf.Rect(50, 380, 60, 395)
    right = pymupdf.Rect(80, 380, 90, 395)
    # 需要两条源注记；补插一条。
    page.insert_link(
        {"kind": pymupdf.LINK_URI, "from": right, "uri": "https://cite.example/3b"}
    )
    staged = tmp_path / "repeat-staged.pdf"
    doc.save(staged)
    doc.close()
    doc = pymupdf.open(staged)
    page = doc[0]

    left_entry = _entry(0, left, uid="https://cite.example/3")
    left_entry["source_text"] = "3"
    left_entry["paragraph_ids"] = ["P"]
    left_entry["src_rect_ratio"] = (0.0, 0.0, 0.2, 1.0)
    right_entry = _entry(1, right, uid="https://cite.example/3b")
    right_entry["source_text"] = "3"
    right_entry["paragraph_ids"] = ["P"]
    right_entry["src_rect_ratio"] = (0.6, 0.0, 0.8, 1.0)

    result = link_remap.remap_page_links(
        page,
        [left_entry, right_entry],
        [],
        {"P": _FakeParagraph((0, 390, 600, 415))},
        800.0,
    )
    assert result.remapped == 2
    assert result.anchor_group_resolved == 2

    out = tmp_path / "repeat-out.pdf"
    doc.save(out)
    doc.close()
    reloaded = pymupdf.open(out)
    try:
        links = {
            link["uri"]: link["from"] for link in reloaded[0].get_links()
        }
        assert links["https://cite.example/3"].x0 < links[
            "https://cite.example/3b"
        ].x0
    finally:
        reloaded.close()


def test_anchor_match_missing_label_falls_back_diagnosed(tmp_path):
    """段内无锚点文字 → proportional 回退，并记录 fallback 原因。"""
    doc = _text_with_citation_link(tmp_path, "no digits here", name="nofall.pdf")
    page = doc[0]
    entry = _entry(0, pymupdf.Rect(50, 380, 60, 395), uid="https://cite.example/miss")
    entry["source_text"] = "99"
    entry["paragraph_ids"] = ["P"]
    entry["src_rect_ratio"] = (0.0, 0.0, 0.1, 1.0)
    result = link_remap.remap_page_links(
        page, [entry], [], {"P": _FakeParagraph((0, 390, 600, 415))}, 800.0
    )
    assert result.remapped == 1
    assert result.fallback_proportional == 1
    assert result.fallbacks and result.fallbacks[0]["method"] == "proportional"


def test_remap_result_to_dict_reports_new_fields():
    result = link_remap.LinkRemapResult(total=3, remapped=2)
    data = result.to_dict()
    for key in (
        "total",
        "remapped",
        "fallback_paragraph",
        "stamp_resolved",
        "unresolved",
        "char_union_resolved",
        "anchor_resolved",
        "anchor_group_resolved",
        "fallback_proportional",
        "physical_annotations",
        "split_annotations",
        "fallbacks",
    ):
        assert key in data


# --------------------------------------------------------------------------- #
# 快照动作元数据 / to 字符串安全
# --------------------------------------------------------------------------- #
def test_link_to_value_handles_string_and_point():
    from babeldoc.tools.agent import link_snapshot

    assert link_snapshot._link_to_value({"to": "named-dest"}) == "named-dest"
    point = link_snapshot._link_to_value({"to": pymupdf.Point(3, 4)})
    assert point == [3.0, 4.0]
    assert link_snapshot._link_to_value({}) is None


def test_snapshot_links_records_action_metadata(tmp_path):
    pdf_path = tmp_path / "meta.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((50, 400), "reference [3]", fontsize=12)
    page.insert_link(
        {
            "kind": pymupdf.LINK_LAUNCH,
            "from": pymupdf.Rect(50, 380, 120, 395),
            "file": "www.meta.example",
        }
    )
    doc.save(pdf_path)
    doc.close()

    snap = link_snapshot.snapshot_links(pdf_path)
    entry = snap["0"][0]
    assert entry["file"] == "www.meta.example"
    assert entry["kind"] in ("LAUNCH", "GOTOR")
    assert "name" in entry and "zoom" in entry and "nameddest" in entry
    assert entry["target_text"]


def test_anchor_reconstructed_from_char_indices_backward_compat(tmp_path):
    """旧缓存快照无 source_text：按 char_indices + 字符对象现场重建锚点。"""
    doc = _text_with_citation_link(tmp_path, "A[3] B[13]", name="compat.pdf")
    page = doc[0]
    entry = _entry(0, pymupdf.Rect(50, 380, 60, 395), uid="https://cite.example/3")
    entry.pop("src_rect_ratio", None)
    entry["char_indices"] = [0]
    entry["paragraph_ids"] = ["P"]
    entry["src_rect_ratio"] = (0.0, 0.0, 0.05, 1.0)
    char = _char("3", 50, 400)
    result = link_remap.remap_page_links(
        page,
        [entry],
        [char],
        {"P": _FakeParagraph((0, 390, 600, 415))},
        800.0,
        alive_ids=set(),
    )
    assert result.remapped == 1
    assert result.anchor_resolved == 1


def test_remap_refreshes_inmemory_link_cache_for_dual_copy(tmp_path):
    """就地 /Rect 写入后同一进程内 ``get_links`` 即返回新矩形。

    dual 搬运直接读 mono 页的 ``get_links``；若不刷新读缓存会把旧矩形带到
    dual 侧（落盘虽正确，但同进程读取是陈旧的）。
    """
    src = pymupdf.Rect(50, 50, 120, 70)
    doc = _staged_page(
        tmp_path,
        [{"kind": pymupdf.LINK_URI, "from": src, "uri": "https://cache.example"}],
    )
    page = doc[0]
    result = link_remap.remap_page_links(
        page,
        [_entry(0, src, uid="https://cache.example")],
        [],
        {"p": _FakeParagraph((100, 600, 400, 660))},
        800.0,
    )
    assert result.remapped == 1
    # 用文档索引重新取页（reload 后旧 wrapper 可能失效）。
    moved = doc[0].get_links()[0]["from"]
    assert moved == pymupdf.Rect(100, 140, 400, 200)
