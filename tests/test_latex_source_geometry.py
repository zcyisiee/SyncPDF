"""源行几何采集（P3-0）：行盒、首行缩进、baseline pitch、下方净空。

覆盖：

1. ``capture_source_line_geometry``：由 composition（``pdf_line``）字符聚类出
   源行；正缩进 / 悬挂（负 dx）；``baseline_pitch``；``ascent_top``；
2. ``space_below_pt``：同列下方最近障碍（其他段落 / figure 布局区）与页底；
3. 水印（``xobj_id=-1``）与嵌套 XObject（``xobj_id>=1``）不采集；
4. ``geometry_from_char_objects``：旧 workdir 用 ``page_char_objects`` 兜底，
   按段 box 归属行。
"""

from __future__ import annotations

from babeldoc.format.pdf.document_il import il_version_1
from babeldoc.format.pdf.document_il.backend.latex_bbox import source_geometry


def _style(size: float = 10.0, font_id: str = "F1"):
    return il_version_1.PdfStyle(
        font_id=font_id,
        font_size=size,
        graphic_state=il_version_1.GraphicState(passthrough_per_char_instruction=""),
    )


def _char(text: str, x: float, y: float, size: float = 10.0, width: float = 5.0):
    return il_version_1.PdfCharacter(
        char_unicode=text,
        box=il_version_1.Box(x, y, x + width, y + size),
        visual_bbox=il_version_1.VisualBbox(
            box=il_version_1.Box(x, y + 1.0, x + width, y + size - 1.0)
        ),
        pdf_character_id=int(x * 100 + y),
        pdf_style=_style(size),
        xobj_id=0,
    )


def _line(chars):
    return il_version_1.PdfParagraphComposition(
        pdf_line=il_version_1.PdfLine(pdf_character=list(chars))
    )


def _paragraph(debug_id, box, compositions, xobj_id=0, label="text"):
    return il_version_1.PdfParagraph(
        box=box,
        pdf_style=_style(),
        pdf_paragraph_composition=list(compositions),
        unicode="源文本",
        debug_id=debug_id,
        layout_label=label,
        xobj_id=xobj_id,
        first_line_indent=False,
    )


def _page(number: int, paragraphs, width=400.0, height=300.0, layouts=()):
    return il_version_1.Page(
        page_number=number,
        pdf_paragraph=list(paragraphs),
        page_layout=list(layouts),
        cropbox=il_version_1.Cropbox(box=il_version_1.Box(0, 0, width, height)),
        mediabox=il_version_1.Mediabox(box=il_version_1.Box(0, 0, width, height)),
    )


def _doc(pages):
    return il_version_1.Document(page=list(pages), total_pages=len(pages))


def _three_line_paragraph(debug_id="P01-001", first_x=60.0):
    """三行段落：首行 x=first_x，其余行 x=54；行顶 210/198/186（pitch 12）。"""
    compositions = []
    for index, (x, top) in enumerate(
        ((first_x, 210.0), (54.0, 198.0), (54.0, 186.0))
    ):
        compositions.append(
            _line([_char("字", x + 5 * col, top - 10.0) for col in range(6)])
        )
        _ = index
    box = il_version_1.Box(54.0, 180.0, 300.0, 214.0)
    return _paragraph(debug_id, box, compositions)


def test_capture_line_geometry_reports_indent_pitch_and_ascent():
    docs = _doc([_page(0, [_three_line_paragraph()])])

    geometry = source_geometry.capture_source_line_geometry(docs)

    meta = geometry["P01-001"]
    assert meta["n_lines"] == 3
    # 首行左缘 60、其余 54 → 正缩进 6bp。
    assert meta["first_line_dx"] == 6.0
    # 行顶 210/198/186 → pitch 12。
    assert meta["baseline_pitch"] == 12.0
    assert meta["page"] == 0
    assert len(meta["line_boxes"]) == 3
    # 首行墨迹顶（visual_bbox）到段 box 顶的距离。
    first_line_top = max(box[3] for box in meta["line_boxes"])
    assert meta["ascent_top"] == round(214.0 - first_line_top, 3)


def test_capture_hanging_indent_is_negative():
    """首行在左（悬挂）：first_line_dx 为负。"""
    docs = _doc([_page(0, [_three_line_paragraph(first_x=50.0)])])

    geometry = source_geometry.capture_source_line_geometry(docs)

    assert geometry["P01-001"]["first_line_dx"] == -4.0


def test_space_below_uses_next_paragraph_and_page_bottom():
    upper = _paragraph(
        "UPPER",
        il_version_1.Box(0.0, 200.0, 300.0, 260.0),
        [_line([_char("上", 10.0, 220.0)])],
    )
    lower = _paragraph(
        "LOWER",
        il_version_1.Box(0.0, 100.0, 300.0, 160.0),
        [_line([_char("下", 10.0, 120.0)])],
    )
    docs = _doc([_page(0, [upper, lower])])

    geometry = source_geometry.capture_source_line_geometry(docs)

    # UPPER 底部 200 → 最近障碍 LOWER 顶部 160 → 净空 40。
    assert geometry["UPPER"]["space_below_pt"] == 40.0
    # LOWER 底部 100 → 页底 0 → 净空 100。
    assert geometry["LOWER"]["space_below_pt"] == 100.0


def test_space_below_is_limited_by_layout_region():
    paragraph = _paragraph(
        "P01-002",
        il_version_1.Box(0.0, 200.0, 300.0, 260.0),
        [_line([_char("文", 10.0, 220.0)])],
    )
    figure = il_version_1.PageLayout(
        id="fig-1",
        box=il_version_1.Box(0.0, 120.0, 200.0, 190.0),
        class_name="figure",
        conf=1.0,
    )
    table = il_version_1.PageLayout(
        id="tab-1",
        box=il_version_1.Box(250.0, 100.0, 390.0, 190.0),
        class_name="table",
        conf=1.0,
    )
    docs = _doc([_page(0, [paragraph], layouts=(figure, table))])

    geometry = source_geometry.capture_source_line_geometry(docs)

    # 只有横向重叠的 figure 阻挡（table 在右侧列外）→ 200 - 190 = 10。
    assert geometry["P01-002"]["space_below_pt"] == 10.0


def test_capture_skips_watermark_and_nested_xobject():
    watermark = _paragraph(
        "WM",
        il_version_1.Box(0.0, 200.0, 300.0, 260.0),
        [_line([_char("水", 10.0, 220.0)])],
        xobj_id=-1,
    )
    nested = _paragraph(
        "NESTED",
        il_version_1.Box(0.0, 200.0, 300.0, 260.0),
        [_line([_char("内", 10.0, 220.0)])],
        xobj_id=1,
    )
    normal = _paragraph(
        "OK",
        il_version_1.Box(0.0, 100.0, 300.0, 160.0),
        [_line([_char("正", 10.0, 120.0)])],
    )
    docs = _doc([_page(0, [watermark, nested, normal])])

    geometry = source_geometry.capture_source_line_geometry(docs)

    assert set(geometry) == {"OK"}


def test_geometry_from_char_objects_assigns_lines_by_box():
    """旧 workdir 兜底：page_char_objects 的源坐标按段 box 归属。"""
    paragraph = _paragraph("P01-003", il_version_1.Box(50.0, 180.0, 300.0, 214.0), [])
    page = _page(0, [paragraph])
    docs = _doc([page])
    chars = []
    for x, top in ((60.0, 210.0), (50.0, 198.0)):
        chars.extend(_char("字", x + 5 * col, top - 10.0) for col in range(6))
    other = _char("其", 10.0, 40.0)  # 页面底部无关字符

    geometry = source_geometry.geometry_from_char_objects(
        docs, {0: [*chars, other]}
    )

    meta = geometry["P01-003"]
    assert meta["n_lines"] == 2
    assert meta["first_line_dx"] == 10.0
    assert meta["baseline_pitch"] == 12.0
    assert meta["source"] == "page-char-objects"


def test_geometry_from_char_objects_without_chars_is_empty():
    docs = _doc([_page(0, [_three_line_paragraph()])])

    assert source_geometry.geometry_from_char_objects(docs, {}) == {}
    assert source_geometry.geometry_from_char_objects(docs, {0: []}) == {}


def test_geometry_from_char_objects_keeps_columns_separate():
    """双栏页面：同一 y 上的左右栏字符不得被聚成同一行。"""
    left = _paragraph("LEFT", il_version_1.Box(50.0, 180.0, 290.0, 214.0), [])
    right = _paragraph("RIGHT", il_version_1.Box(320.0, 180.0, 560.0, 214.0), [])
    page = _page(0, [left, right], width=600.0, height=300.0)
    docs = _doc([page])
    chars = []
    for x0 in (50.0, 320.0):
        for top in (210.0, 198.0):
            chars.extend(_char("字", x0 + 5 * col, top - 10.0) for col in range(6))

    geometry = source_geometry.geometry_from_char_objects(docs, {0: chars})

    # 每段各自 2 行、首行缩进 0，而不是把两栏合并成一行、dx 为 -270。
    for debug_id in ("LEFT", "RIGHT"):
        meta = geometry[debug_id]
        assert meta["n_lines"] == 2, (debug_id, meta)
        assert meta["first_line_dx"] == 0.0, (debug_id, meta)
        assert meta["baseline_pitch"] == 12.0


def test_capture_requires_pre_translation_composition():
    """翻译前采集契约：译文回填后的 composition 已丢失源行几何。

    ``ILTranslator`` 会把 composition 换成 ``pdf_same_style_unicode_characters``
    （纯文本 run，无字符 box）：此时 ``capture_source_line_geometry`` 已拿不到
    任何行几何，只能靠 ``geometry_from_char_objects``（extract 期落盘的
    ``page_char_objects``）兜底 —— 这正是采集必须前移的原因（根因 5）。
    """
    before = _doc([_page(0, [_three_line_paragraph()])])
    assert source_geometry.capture_source_line_geometry(before)["P01-001"]["n_lines"] == 3

    translated = _paragraph(
        "P01-001",
        il_version_1.Box(54.0, 180.0, 300.0, 214.0),
        [
            il_version_1.PdfParagraphComposition(
                pdf_same_style_unicode_characters=il_version_1.PdfSameStyleUnicodeCharacters(
                    pdf_style=_style(), unicode="这是回填后的译文纯文本 run"
                )
            )
        ],
    )
    after = _doc([_page(0, [translated])])

    # 译文回填后：composition 无字符 box，采集不到几何。
    assert source_geometry.capture_source_line_geometry(after) == {}

    # 旧 workdir 兜底仍能重建（extract 期落盘的字符 box 未被回写）。
    chars = []
    for x, top in ((60.0, 210.0), (54.0, 198.0), (54.0, 186.0)):
        chars.extend(_char("字", x + 5 * col, top - 10.0) for col in range(6))
    fallback = source_geometry.geometry_from_char_objects(after, {0: chars})
    assert fallback["P01-001"]["n_lines"] == 3
    assert fallback["P01-001"]["first_line_dx"] == 6.0
