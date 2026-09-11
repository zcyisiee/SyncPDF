"""原生字符 ↔ MinerU span 对齐（provider_alignment）与行内公式保护（inline_math_protector）测试。

覆盖（对应 .plan/minerU深度融合.md 板块 2）：

1. ``to_il_box``：MinerU 页面坐标 → IL 坐标（y 翻转）；非法 bbox 返回 None。
2. ``align_page``：中心命中 / line bbox 回退 / 完全未命中 / 多 span 命中（ambiguous）
   四类，以及 ``char_to_span`` / ``span_to_chars`` 映射；另加一个几何断言说明
   「面积覆盖率」规则被中心命中规则蕴含（实际不可达，见
   ``test_containment_rule_is_subsumed_by_center_rule``）。
3. ``span_text_mismatch``：LaTeX 表示与原生字符的宽松字母集一致性校验。
4. ``protect_page``：inline_equation span 追加为 formula 布局区域；与既有 formula
   区域重复时跳过；过小噪声区域不追加；id 接续既有最大值。
5. ``InlineMathProtector.process``：读 provider_ir.json、逐页 protect + 写
   ``alignment.json``；无 IR 时原样返回且不落盘。
6. 离线回放集成：DeepSeek 缓存 layout.json 的 inline_equation span 数 ≥ 126，
   坐标换算不越界（缓存缺失时 skip）。
7. 回归锚点：两条解析管线都必须调用 InlineMathProtector（防止未来误删）。

**已知实现缺陷（本文件只记录、不修）**：``protect_page`` 在 ``page.page_layout``
为空时执行 ``max((layout.id or 0) for layout in page.page_layout or ())``，抛
``ValueError: max() iterable argument is empty``；线上被 ``InlineMathProtector._protect``
的 ``except Exception`` 吞掉，后果是「完全没有布局区域的页面得不到行内公式保护」
（静默降级）。已在 ``test_protect_page_empty_page_layout_currently_raises``（xfail
strict）中固化；修复方式为 ``max(..., default=0)``，修后该用例会失败并提醒改成正常断言。

测试全部离线可跑：真实数据用例在缓存缺失时 skip。
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from babeldoc.docvision.provider_ir import KNOWN_SPAN_KINDS
from babeldoc.docvision.provider_ir import ProviderBlock
from babeldoc.docvision.provider_ir import ProviderDocument
from babeldoc.docvision.provider_ir import ProviderLine
from babeldoc.docvision.provider_ir import ProviderPage
from babeldoc.docvision.provider_ir import ProviderSpan
from babeldoc.format.pdf.document_il import il_version_1
from babeldoc.format.pdf.document_il.midend.inline_math_protector import (
    InlineMathProtector,
)
from babeldoc.format.pdf.document_il.midend.inline_math_protector import (
    load_provider_document,
)
from babeldoc.format.pdf.document_il.midend.inline_math_protector import protect_page
from babeldoc.format.pdf.document_il.midend.inline_math_protector import (
    provider_ir_path,
)
from babeldoc.format.pdf.document_il.utils.provider_alignment import ProviderBox
from babeldoc.format.pdf.document_il.utils.provider_alignment import _center_inside
from babeldoc.format.pdf.document_il.utils.provider_alignment import _containment
from babeldoc.format.pdf.document_il.utils.provider_alignment import align_document
from babeldoc.format.pdf.document_il.utils.provider_alignment import align_page
from babeldoc.format.pdf.document_il.utils.provider_alignment import (
    inline_equation_regions,
)
from babeldoc.format.pdf.document_il.utils.provider_alignment import to_il_box

CACHE_DIR = Path.home() / ".cache" / "babeldoc" / "mineru-layout.v1"
# DeepSeek 技术报告（51 页，126 个 inline_equation span）的内容哈希缓存文件名。
DEEPSEEK_CACHE_KEY = "ba68e2e40408125ae6d2f63a9a241b61c73910691c74ec1a2a7023c851eac08d"
# 合成用例统一页高（pt），坐标按「页面坐标 y 向下」给出。
PAGE_HEIGHT = 100.0


# --------------------------------------------------------------------------- #
# fixtures：合成 provider IR / IL 对象
# --------------------------------------------------------------------------- #
def _span(span_id, kind, bbox, content=""):
    return ProviderSpan(
        span_id=span_id,
        bbox=bbox,
        kind=kind,
        content=content,
        score=1.0,
        page_index=0,
    )


def _provider_page(spans, line_bbox=(0.0, 0.0, 100.0, 40.0), page_index=0):
    """单 block / 单 line / 给定 span 的合成 provider 页。"""
    line = ProviderLine(
        line_id=f"p{page_index}-b0-l0",
        bbox=list(line_bbox),
        spans=list(spans),
    )
    block = ProviderBlock(
        block_id=f"p{page_index}-b0",
        type="text",
        sub_type=None,
        bbox=list(line_bbox),
        level=None,
        index=0,
        angle=0.0,
        lines=[line],
    )
    return ProviderPage(
        page_index=page_index,
        blocks=[block],
        reading_order=[block.block_id],
    )


def _char(unicode_char, x0, y0, x1, y1, size=10.0):
    """构造带 box 与 visual_bbox（同值）的 IL 原生字符，坐标为 IL（左下原点）。"""
    box = il_version_1.Box(x=x0, y=y0, x2=x1, y2=y1)
    return il_version_1.PdfCharacter(
        pdf_style=il_version_1.PdfStyle(
            font_id="F1",
            font_size=size,
            graphic_state=il_version_1.GraphicState(),
        ),
        box=box,
        visual_bbox=il_version_1.VisualBbox(box=box),
        char_unicode=unicode_char,
        advance=x1 - x0,
    )


def _page(chars, page_number=0, page_height=PAGE_HEIGHT, layouts=None):
    return il_version_1.Page(
        page_number=page_number,
        pdf_character=list(chars),
        page_layout=list(layouts or []),
        cropbox=il_version_1.Cropbox(
            box=il_version_1.Box(x=0, y=0, x2=100, y2=page_height)
        ),
    )


def _docs(pages):
    return il_version_1.Document(page=list(pages))


# --------------------------------------------------------------------------- #
# 1. to_il_box
# --------------------------------------------------------------------------- #
def test_to_il_box_flips_y_axis():
    """MinerU 的 y 向下，IL 的 y 向上：y' = H - y。"""
    box = to_il_box([10.0, 20.0, 30.0, 45.0], PAGE_HEIGHT)
    assert box == ProviderBox(x=10.0, y=55.0, x2=30.0, y2=80.0)
    assert box.area == pytest.approx(20.0 * 25.0)


def test_to_il_box_accepts_tuple_and_ints():
    box = to_il_box((0, 0, 100, 100), PAGE_HEIGHT)
    assert box.y == 0.0 and box.y2 == PAGE_HEIGHT


@pytest.mark.parametrize(
    "bbox",
    [
        None,
        [],
        [1.0, 2.0, 3.0],
        [1.0, 2.0, 3.0, 4.0, 5.0],
        "1,2,3,4",
        [1.0, 2.0, 3.0, "x"],
        [None, 2.0, 3.0, 4.0],
    ],
)
def test_to_il_box_rejects_invalid_bbox(bbox):
    assert to_il_box(bbox, PAGE_HEIGHT) is None


# --------------------------------------------------------------------------- #
# 2. align_page
# --------------------------------------------------------------------------- #
def _alignment_fixture():
    """合成一页：覆盖中心命中 / line 回退 / 未命中 / ambiguous 四类。

    MinerU（页面坐标，y 向下）：
      span s0 text            x[0,30)  y[0,20)
      span s1 inline_equation x[15,35) y[0,20)   （与 s0 在 x[15,30) 重叠 → ambiguous）
      span s2 text            x[60,80) y[0,20)
      line bbox               x[0,100) y[0,40)
    IL 坐标（y' = 100 - y）：
      s0  → x[0,30)  y[80,100)
      s1  → x[15,35) y[80,100)
      s2  → x[60,80) y[80,100)
      line→ x[0,100) y[60,100)
    """
    spans = [
        _span("p0-b0-l0-s0", "text", [0.0, 0.0, 30.0, 20.0], "alpha"),
        _span("p0-b0-l0-s1", "inline_equation", [15.0, 0.0, 35.0, 20.0], "n_{Win}"),
        _span("p0-b0-l0-s2", "text", [60.0, 0.0, 80.0, 20.0], "beta"),
    ]
    provider_page = _provider_page(spans, line_bbox=(0.0, 0.0, 100.0, 40.0))

    chars = [
        # 0,1：中心 x=3/9，只落在 s0 的 x[0,30) 内（s1 从 x=15 开始）→ 只命中 s0。
        _char("a", 0.0, 85.0, 6.0, 95.0),
        _char("b", 6.0, 85.0, 12.0, 95.0),
        # 2：中心 x=21，同时落在 s0 与 s1 内 → ambiguous，取面积更小的 s1。
        _char("c", 18.0, 85.0, 24.0, 95.0),
        # 3：中心 x=29，同样同时命中 s0/s1 → ambiguous，取 s1。
        _char("d", 26.0, 85.0, 32.0, 95.0),
        # 4：中心 x=65，只落在 s2 内 → 唯一命中。
        _char("e", 62.0, 85.0, 68.0, 95.0),
        # 5：不在任何 span 内（y 在 62..70，低于所有 span 的 y[80,100)），
        #    但落在 line bbox y[60,100) 内 → line 回退。
        _char("f", 40.0, 62.0, 90.0, 70.0),
        # 6：完全在外 → unmatched。
        _char("g", 200.0, 85.0, 210.0, 95.0),
    ]
    return _page(chars), provider_page


def test_align_page_counts_and_mapping():
    page, provider_page = _alignment_fixture()
    result = align_page(page, provider_page, PAGE_HEIGHT)

    assert result.total_chars == 7
    assert result.matched_chars == 6
    assert result.unmatched_chars == 1
    # ambiguous 三个：字符 2、3 同时落在 s0/s1 重叠区；字符 5 走 line 回退时
    # 三个候选的 line bbox 都覆盖它，同覆盖率取面积更小者。
    assert result.ambiguous_chars == 3
    assert result.matched_chars + result.unmatched_chars == result.total_chars

    # char_to_span 用「页内字符下标」作 key。
    assert result.char_to_span[0] == "p0-b0-l0-s0"
    assert result.char_to_span[1] == "p0-b0-l0-s0"
    # s0（面积 600）与 s1（面积 400）同时命中时取面积更小者（更精确的盒子）。
    assert result.char_to_span[2] == "p0-b0-l0-s1"
    assert result.char_to_span[3] == "p0-b0-l0-s1"
    assert result.char_to_span[4] == "p0-b0-l0-s2"
    assert 6 not in result.char_to_span

    # 反向映射：span → 字符下标。
    assert result.span_to_chars["p0-b0-l0-s0"] == [0, 1]
    assert result.span_to_chars["p0-b0-l0-s2"] == [4]
    # 字符 5 走 line 回退，同覆盖率下取面积更小的 s1。
    assert result.span_to_chars["p0-b0-l0-s1"] == [2, 3, 5]


def test_containment_rule_is_subsumed_by_center_rule():
    """几何事实：轴对齐矩形中「字符面积 ≥60% 落在 span 内」必然蕴含「字符中心在 span 内」。

    因此 ``align_page`` 里的覆盖率分支（rank=1）实际不可达——中心判定
    （带 ±1pt 容差）总是先命中。本用例用确定性整数网格穷举把这个性质固化成
    可验证断言，避免日后误以为覆盖率分支提供了额外覆盖。
    """
    checked = 0
    for span_w, span_h in ((4, 4), (10, 10), (3, 7)):
        span = ProviderBox(x=0.0, y=0.0, x2=float(span_w), y2=float(span_h))
        for x0 in range(span_w + 1):
            for x1 in range(x0 + 1, span_w + 1):
                for y0 in range(span_h + 1):
                    for y1 in range(y0 + 1, span_h + 1):
                        char_box = il_version_1.Box(
                            x=float(x0), y=float(y0), x2=float(x1), y2=float(y1)
                        )
                        if _containment(span, char_box) < 0.6:
                            continue
                        checked += 1
                        assert _center_inside(span, char_box), (span, char_box)
    assert checked > 0, "网格样本应至少命中若干次覆盖率条件"


def test_align_page_inline_equation_matches():
    page, provider_page = _alignment_fixture()
    result = align_page(page, provider_page, PAGE_HEIGHT)

    assert len(result.inline_matches) == 1
    match = result.inline_matches[0]
    assert match.span_id == "p0-b0-l0-s1"
    assert match.kind == "inline_equation"
    assert match.matched is True
    assert match.method == "char_bbox"
    assert match.char_count == len(result.span_to_chars["p0-b0-l0-s1"])


def test_align_page_line_fallback_is_used_when_span_misses():
    """span bbox 都不命中、只有 line bbox 覆盖时，字符仍算 matched（规则 3）。"""
    page, provider_page = _alignment_fixture()
    result = align_page(page, provider_page, PAGE_HEIGHT)
    # 字符 5（x[40,90) y[62,70)）落在 line bbox 内、不在任何 span 内：
    # 走 line 回退，method 不是 char_bbox，但计入 matched。
    assert 5 in result.char_to_span
    assert result.char_to_span[5] == "p0-b0-l0-s1"
    assert result.matched_chars == 6


def test_align_page_empty_page_returns_zero_counts():
    page = _page([], page_number=0)
    result = align_page(page, _provider_page([]), PAGE_HEIGHT)
    assert result.total_chars == 0
    assert result.matched_chars == 0
    assert result.unmatched_chars == 0
    assert result.coverage == 1.0
    # 空页也要通过 to_dict 的统计自洽断言。
    assert result.to_dict()["total_chars"] == 0


def test_align_page_char_without_box_is_unmatched():
    page = _page([il_version_1.PdfCharacter(char_unicode="x")])
    result = align_page(
        page, _provider_page([_span("s0", "text", [0, 0, 50, 50])]), PAGE_HEIGHT
    )
    assert result.total_chars == 1
    assert result.unmatched_chars == 1
    assert result.matched_chars == 0


def test_align_page_falls_back_to_box_when_visual_bbox_missing():
    """visual_bbox 缺失时用 box 参与对齐。"""
    box = il_version_1.Box(x=2.0, y=85.0, x2=8.0, y2=95.0)
    char = il_version_1.PdfCharacter(
        pdf_style=il_version_1.PdfStyle(
            font_id="F1",
            font_size=10.0,
            graphic_state=il_version_1.GraphicState(),
        ),
        box=box,
        visual_bbox=None,
        char_unicode="a",
    )
    page = _page([char])
    provider = _provider_page([_span("s0", "text", [0.0, 0.0, 30.0, 20.0])])
    result = align_page(page, provider, PAGE_HEIGHT)
    assert result.matched_chars == 1
    assert result.char_to_span[0] == "s0"


def test_align_page_ambiguous_prefers_containing_smaller_span():
    """多 span 命中时取中心命中优先、同分取面积更小者（更精确的公式盒子）。"""
    big = _span("big", "text", [0.0, 0.0, 90.0, 20.0])
    small = _span("small", "inline_equation", [40.0, 0.0, 60.0, 20.0])
    page = _page([_char("m", 45.0, 85.0, 51.0, 95.0)])
    result = align_page(page, _provider_page([big, small]), PAGE_HEIGHT)
    assert result.ambiguous_chars == 1
    assert result.char_to_span[0] == "small"


def test_align_document_skips_pages_without_provider_page():
    pages = [_page([_char("a", 0.0, 85.0, 6.0, 95.0)], page_number=0)]
    provider_document = ProviderDocument(
        version_name="v",
        backend="hybrid",
        page_count=2,
        pages=[
            _provider_page([_span("s0", "text", [0.0, 0.0, 30.0, 20.0])], page_index=0)
        ],
    )
    report = align_document(_docs(pages), provider_document)
    # 第 1 页无 provider page → 只产出 1 页报告。
    assert len(report.pages) == 1
    assert report.to_dict()["summary"]["page_count"] == 1


# --------------------------------------------------------------------------- #
# 3. span_text_mismatch
# --------------------------------------------------------------------------- #
def test_span_text_mismatch_accepts_latex_vs_native_letters():
    """LaTeX 表示（n_{\\mathrm{Win}}）与原生字符（nWin）字母集一致 → 不记 mismatch。"""
    provider = _provider_page(
        [
            _span(
                "p0-b0-l0-s0",
                "inline_equation",
                [0.0, 0.0, 30.0, 20.0],
                r"n_{\mathrm{Win}}",
            )
        ]
    )
    page = _page([_char("n", 2.0, 85.0, 8.0, 95.0), _char("W", 8.0, 85.0, 14.0, 95.0)])
    result = align_page(page, provider, PAGE_HEIGHT)
    assert result.inline_matches[0].matched is True
    assert result.span_text_mismatch == 0


def test_span_text_mismatch_counts_inconsistent_content():
    """span 内容与对齐到的原生字符字母集无关 → 记 mismatch 并留样本。"""
    provider = _provider_page(
        [_span("p0-b0-l0-s0", "inline_equation", [0.0, 0.0, 30.0, 20.0], "abc")]
    )
    page = _page([_char("x", 2.0, 85.0, 8.0, 95.0), _char("y", 8.0, 85.0, 14.0, 95.0)])
    result = align_page(page, provider, PAGE_HEIGHT)
    assert result.span_text_mismatch == 1
    assert result.span_text_samples[0]["span_id"] == "p0-b0-l0-s0"
    assert result.to_dict()["span_text_mismatch"] == 1


# --------------------------------------------------------------------------- #
# 4. protect_page
# --------------------------------------------------------------------------- #
def _formula_layout(layout_id, box, class_name="formula"):
    return il_version_1.PageLayout(
        id=layout_id,
        box=il_version_1.Box(x=box[0], y=box[1], x2=box[2], y2=box[3]),
        conf=1.0,
        class_name=class_name,
    )


def test_protect_page_appends_regions_and_skips_duplicates():
    """与既有 formula 区域高度重叠的 span 不重复追加；新区域 id 接续最大值。"""
    # span A 的 IL box 与既有 formula 区域几乎重合 → 跳过。
    span_a = _span("p0-b0-l0-s0", "inline_equation", [0.0, 0.0, 30.0, 20.0])
    # span B 是独立位置 → 追加。
    span_b = _span("p0-b0-l0-s1", "inline_equation", [60.0, 0.0, 80.0, 20.0])
    page = _page(
        [],
        layouts=[_formula_layout(7, (0.0, 80.0, 30.0, 100.0))],
    )
    added = protect_page(page, _provider_page([span_a, span_b]))

    assert len(added) == 1
    assert added[0]["layout_id"] == 8
    assert added[0]["page_index"] == 0
    assert len(page.page_layout) == 2
    new_layout = page.page_layout[-1]
    assert new_layout.id == 8
    assert new_layout.class_name == "formula"
    # span B：MinerU x[60,80) y[0,20) → IL x[60,80) y[80,100)
    assert (new_layout.box.x, new_layout.box.y) == (60.0, 80.0)
    assert (new_layout.box.x2, new_layout.box.y2) == (80.0, 100.0)


# ``protect_page`` 曾在 ``page.page_layout`` 为空时抛 ``max() 空序列`` 错误
# （静默降级：没有任何布局区域的页面完全得不到行内公式保护）；
# 已修复为 ``max(..., default=0)``，下方用例验证空页也能正常追加保护区域。
def test_protect_page_empty_page_layout_appends_region():
    """page_layout 为空的页也能追加保护区域（max(default=0) 修复后）。"""
    span = _span("p0-b0-l0-s0", "inline_equation", [0.0, 0.0, 30.0, 20.0])
    page = _page([])
    added = protect_page(page, _provider_page([span]))
    assert len(added) == 1
    assert added[0]["layout_id"] == 1  # 空页 id 从 1 开始
    assert len(page.page_layout) == 1
    assert page.page_layout[0].class_name == "formula"


def test_protect_page_skips_tiny_noise_regions():
    """宽度/高度 < 1pt 的 span 视为噪声，不追加。"""
    tiny = _span("p0-b0-l0-s0", "inline_equation", [10.0, 0.0, 10.5, 20.0])
    # 给一个已存在的布局区域，绕开 page_layout 为空的已知缺陷（见上）。
    page = _page([], layouts=[_formula_layout(1, (0.0, 0.0, 5.0, 5.0), "text")])
    assert protect_page(page, _provider_page([tiny])) == []
    assert len(page.page_layout) == 1


def test_protect_page_ignores_non_inline_equation_spans():
    spans = [
        _span("s0", "text", [0.0, 0.0, 30.0, 20.0]),
        _span("s1", "interline_equation", [40.0, 0.0, 60.0, 20.0]),
    ]
    page = _page([], layouts=[_formula_layout(1, (0.0, 0.0, 5.0, 5.0), "text")])
    assert protect_page(page, _provider_page(spans)) == []
    assert len(page.page_layout) == 1


def test_protect_page_returns_empty_without_page_height():
    """cropbox 缺失（页高取不到）时不追加区域。"""
    page = il_version_1.Page(page_number=0, pdf_character=[], page_layout=[])
    span = _span("p0-b0-l0-s0", "inline_equation", [0.0, 0.0, 30.0, 20.0])
    assert protect_page(page, _provider_page([span])) == []


def test_protect_page_matches_existing_isolate_formula_region():
    """既有 isolate_formula 区域同样算「已保护」，不重复追加。"""
    span = _span("p0-b0-l0-s0", "inline_equation", [0.0, 0.0, 30.0, 20.0])
    page = _page(
        [], layouts=[_formula_layout(3, (0.0, 80.0, 30.0, 100.0), "isolate_formula")]
    )
    assert protect_page(page, _provider_page([span])) == []


def test_inline_equation_regions_filters_by_kind():
    spans = [
        _span("s0", "text", [0.0, 0.0, 30.0, 20.0]),
        _span("s1", "inline_equation", [40.0, 0.0, 60.0, 20.0]),
    ]
    regions = inline_equation_regions(_provider_page(spans), PAGE_HEIGHT)
    assert len(regions) == 1
    assert regions[0].x == 40.0 and regions[0].y == 80.0


# --------------------------------------------------------------------------- #
# 5. InlineMathProtector.process
# --------------------------------------------------------------------------- #
def _minimal_config(provider_ir_dir=None, working_dir=None):
    """最小 config 桩：只带 InlineMathProtector 需要的 provider_ir_dir/working_dir。"""
    from types import SimpleNamespace

    return SimpleNamespace(
        provider_ir_dir=provider_ir_dir,
        working_dir=working_dir,
        lang_in="en",
        lang_out="zh",
    )


def _write_provider_ir(directory: Path, page_index: int = 0, *, inline: bool = True):
    """在 ``<directory>/source/mineru/provider_ir.json`` 落盘一份最小 provider IR。"""
    spans = [_span(f"p{page_index}-b0-l0-s0", "text", [0.0, 0.0, 30.0, 20.0], "alpha")]
    if inline:
        spans.append(
            _span(
                f"p{page_index}-b0-l0-s1",
                "inline_equation",
                [40.0, 0.0, 60.0, 20.0],
                "n_{Win}",
            )
        )
    provider_page = _provider_page(spans, page_index=page_index)
    document = ProviderDocument(
        version_name="vlm-3.4.4",
        backend="hybrid",
        page_count=page_index + 1,
        pages=[provider_page],
    )
    target_dir = Path(directory) / "source" / "mineru"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "provider_ir.json"
    target.write_text(document.to_json(), encoding="utf-8")
    return target


def test_provider_ir_path_prefers_explicit_dir(tmp_path):
    explicit = tmp_path / "agent"
    _write_provider_ir(explicit)
    other = tmp_path / "other"
    _write_provider_ir(other)
    assert (
        provider_ir_path(explicit, other)
        == explicit / "source" / "mineru" / "provider_ir.json"
    )


def test_provider_ir_path_falls_back_to_working_dir(tmp_path):
    working = tmp_path / "wd"
    _write_provider_ir(working / "agent")
    assert (
        provider_ir_path(None, working)
        == working / "agent" / "source" / "mineru" / "provider_ir.json"
    )


def test_provider_ir_path_returns_none_when_missing(tmp_path):
    assert provider_ir_path(tmp_path / "nope", tmp_path / "nope2") is None


def test_load_provider_document_returns_none_on_broken_json(tmp_path):
    broken = tmp_path / "agent" / "source" / "mineru"
    broken.mkdir(parents=True)
    (broken / "provider_ir.json").write_text("{not json", encoding="utf-8")
    assert load_provider_document(tmp_path / "agent", None) is None


def test_protector_process_protects_page_and_writes_audit(tmp_path):
    agent_dir = tmp_path / "agent"
    _write_provider_ir(agent_dir)
    config = _minimal_config(provider_ir_dir=agent_dir)

    # page_layout 预置一个区域：绕开「空 page_layout 抛 ValueError」的已知缺陷。
    page0 = _page(
        [_char("a", 2.0, 85.0, 8.0, 95.0), _char("n", 45.0, 85.0, 51.0, 95.0)],
        page_number=0,
        layouts=[_formula_layout(1, (0.0, 0.0, 5.0, 5.0), "text")],
    )
    # 第二页没有对应 provider page → 不参与对齐，也不追加区域。
    page1 = _page([_char("z", 2.0, 85.0, 8.0, 95.0)], page_number=1)
    docs = _docs([page0, page1])

    returned = InlineMathProtector(config).process(docs)
    assert returned is docs
    # 第 0 页追加了 1 个 formula 区域（原有 text 区域保留），第 1 页没有。
    assert len(page0.page_layout) == 2
    assert page0.page_layout[-1].class_name == "formula"
    assert page1.page_layout == []

    audit_path = agent_dir / "source" / "mineru" / "alignment.json"
    assert audit_path.is_file()
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit["version"] == 1
    summary = audit["summary"]
    for key in (
        "page_count",
        "total_chars",
        "matched_chars",
        "unmatched_chars",
        "ambiguous_chars",
        "native_char_coverage",
        "inline_equation_total",
        "inline_equation_matched",
        "span_text_mismatch",
        "protected_inline_math",
    ):
        assert key in summary, key
    assert summary["page_count"] == 1
    assert summary["total_chars"] == 2
    assert summary["inline_equation_total"] == 1
    assert summary["protected_inline_math"] == 1
    assert len(audit["protected_inline_math"]) == 1
    assert audit["protected_inline_math"][0]["page_index"] == 0
    assert len(audit["pages"]) == 1


def test_protector_process_no_provider_ir_is_noop(tmp_path):
    config = _minimal_config(provider_ir_dir=tmp_path / "agent")
    page = _page([_char("a", 2.0, 85.0, 8.0, 95.0)], page_number=0)
    docs = _docs([page])
    returned = InlineMathProtector(config).process(docs)
    assert returned is docs
    assert page.page_layout == []
    assert not (tmp_path / "agent" / "source" / "mineru" / "alignment.json").exists()


def test_protector_process_without_output_dir_still_aligns():
    """无 provider_ir_dir/working_dir 时读不到 IR（静默跳过），不抛错。"""
    config = _minimal_config()
    docs = _docs([_page([_char("a", 2.0, 85.0, 8.0, 95.0)], page_number=0)])
    assert InlineMathProtector(config).process(docs) is docs


def test_protector_process_marks_inline_span_as_matched(tmp_path):
    """对齐审计里 inline_equation span 的 matched/char_count 应反映真实对齐结果。"""
    agent_dir = tmp_path / "agent"
    _write_provider_ir(agent_dir)
    config = _minimal_config(provider_ir_dir=agent_dir)
    page = _page(
        [_char("n", 45.0, 85.0, 51.0, 95.0)],
        page_number=0,
        layouts=[_formula_layout(1, (0.0, 0.0, 5.0, 5.0), "text")],
    )
    InlineMathProtector(config).process(_docs([page]))
    audit = json.loads(
        (agent_dir / "source" / "mineru" / "alignment.json").read_text(encoding="utf-8")
    )
    inline = audit["pages"][0]["inline_equation"]
    assert len(inline) == 1
    assert inline[0]["matched"] is True
    assert inline[0]["char_count"] == 1


# --------------------------------------------------------------------------- #
# 6. 离线回放集成（缓存缺失时 skip）
# --------------------------------------------------------------------------- #
def _deepseek_layout_json():
    path = CACHE_DIR / f"{DEEPSEEK_CACHE_KEY}.json"
    if not path.is_file():
        pytest.skip(f"MinerU layout cache missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def test_deepseek_cache_inline_equation_span_count():
    provider_document = ProviderDocument.from_layout_json(_deepseek_layout_json())
    assert provider_document.page_count == 51
    inline_spans = [
        span
        for page in provider_document.pages
        for block in page.iter_blocks(recursive=True)
        for line in block.lines
        for span in line.spans
        if span.kind == "inline_equation"
    ]
    # 板块 2 验收基准：DeepSeek 样本 126 个 inline_equation span。
    assert len(inline_spans) >= 126
    assert all(span.kind in KNOWN_SPAN_KINDS for span in inline_spans)


def test_deepseek_cache_to_il_box_stays_within_page():
    """[595, 841] 页面下所有 span 坐标换算后不越界（含 ±1pt 容差）。"""
    provider_document = ProviderDocument.from_layout_json(_deepseek_layout_json())
    checked = 0
    for page in provider_document.pages:
        for block in page.iter_blocks(recursive=True):
            for line in block.lines:
                for span in line.spans:
                    box = to_il_box(span.bbox, 841.0)
                    if box is None:
                        continue
                    checked += 1
                    assert -1.0 <= box.x <= 596.0
                    assert -1.0 <= box.y <= 842.0
                    assert box.x2 <= 596.0 and box.y2 <= 842.0
                    assert box.y <= box.y2 and box.x <= box.x2
    assert checked > 2000


# --------------------------------------------------------------------------- #
# 7. 回归锚点：两条管线必须调用 InlineMathProtector
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "module_name",
    ["babeldoc.tools.agent.markdown_view", "babeldoc.tools.agent.workflow"],
)
def test_pipelines_call_inline_math_protector(module_name):
    import importlib

    module = importlib.import_module(module_name)
    source = inspect.getsource(module)
    assert "InlineMathProtector" in source
    assert "InlineMathProtector(" in source
    # 位置约定：紧跟 LayoutParser.process 之后（那时 page.pdf_character 仍是全量）。
    layout_at = source.index("LayoutParser(config).process(docs, doc_pdf)")
    protector_at = source.index("InlineMathProtector(config).process(docs)")
    assert layout_at < protector_at
    assert "EnclosedMarkerFixer" in source[protector_at:]
