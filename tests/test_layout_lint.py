"""排版 lint：合成 geometry 的缺陷识别 + locate。"""

from __future__ import annotations

from babeldoc.tools.agent import layout_geometry as lg


def _para(
    pid,
    page=1,
    label="text",
    rendered=None,
    src=None,
    src_font=10.0,
    mode_font=None,
    overridable=True,
):
    return {
        "id": pid,
        "overridable": overridable,
        "page": page,
        "layout_label": label,
        "src_box": src or rendered,
        "layout_box": rendered,
        "rendered_box": rendered,
        "scale": 1.0,
        "optimal_scale": 1.0,
        "font_scale": 1.0,
        "src_font_size": src_font,
        "mode_font_size": mode_font if mode_font is not None else src_font,
        "min_font_size": mode_font if mode_font is not None else src_font,
        "max_font_size": src_font,
        "n_chars": 10,
        "n_lines": 1,
        "text": pid,
    }


def _geometry(paragraphs, regions=None, cropbox=(0, 0, 612, 792)):
    return {
        "version": 1,
        "pages": 1,
        "overrides": {"paragraphs": {}, "pages": {}},
        "page_info": [
            {
                "page": 1,
                "cropbox": list(cropbox),
                "layout_regions": regions or [],
            }
        ],
        "paragraphs": paragraphs,
    }


def _codes(result):
    return sorted(result["counts"])


def test_clean_geometry_has_no_findings():
    geometry = _geometry(
        [
            _para("P01-001", rendered=[50, 700, 300, 750]),
            _para("P01-002", rendered=[50, 600, 300, 690]),
        ]
    )
    result = lg.lint_geometry(geometry)
    assert result["findings"] == []
    assert result["counts"] == {}


def test_out_of_page():
    geometry = _geometry([_para("P01-001", rendered=[50, -20, 300, 750])])
    result = lg.lint_geometry(geometry)
    assert _codes(result) == ["out_of_page"]
    finding = result["findings"][0]
    assert finding["sev"] == "P0"
    assert finding["evidence"]["overflow_pt"] == 20.0


def test_small_overflow_is_tolerated():
    geometry = _geometry([_para("P01-001", rendered=[50, -0.5, 300, 792.5])])
    assert lg.lint_geometry(geometry)["findings"] == []


def test_paragraph_overlap_and_iou():
    geometry = _geometry(
        [
            _para("P01-001", rendered=[50, 600, 300, 700]),
            _para("P01-002", rendered=[50, 620, 300, 720]),
        ]
    )
    result = lg.lint_geometry(geometry)
    assert _codes(result) == ["paragraph_overlap"]
    finding = result["findings"][0]
    assert finding["ids"] == ["P01-001", "P01-002"]
    assert finding["evidence"]["iou"] > lg.THRESHOLDS["overlap_iou"]


def test_tiny_superscript_inside_paragraph_is_ignored():
    geometry = _geometry(
        [
            _para("P01-001", rendered=[50, 600, 300, 700]),
            _para("P01-002", rendered=[55, 690, 65, 700]),  # 面积悬殊
        ]
    )
    assert lg.lint_geometry(geometry)["findings"] == []


def test_font_shrink_uses_mode_font():
    geometry = _geometry(
        [
            _para("P01-001", rendered=[50, 600, 300, 700], src_font=10.0, mode_font=7.5),
            _para("P01-002", rendered=[50, 500, 300, 560], src_font=10.0, mode_font=9.5),
        ]
    )
    result = lg.lint_geometry(geometry)
    assert _codes(result) == ["font_shrink"]
    assert result["findings"][0]["id"] == "P01-001"
    assert result["findings"][0]["evidence"]["ratio"] == 0.75


def test_figure_overlap_ignores_captions_and_preexisting_overlap():
    regions = [
        {"label": "figure", "box": [300, 500, 550, 700]},
        {"label": "figure_caption", "box": [300, 470, 550, 495]},
    ]
    geometry = _geometry(
        [
            # 原文本就压在图上（图内文字）→ 不报
            _para("P01-001", rendered=[310, 600, 540, 690], src=[310, 600, 540, 690]),
            # caption 落在 caption 区 → 不报
            _para("P01-002", label="figure_caption", rendered=[310, 470, 540, 495]),
            # 译文压到 figure 区 → 报（与 P01-001 不重叠）
            _para("P01-003", rendered=[320, 510, 530, 570], src=[50, 100, 290, 200]),
        ],
        regions=regions,
    )
    result = lg.lint_geometry(geometry)
    assert _codes(result) == ["figure_overlap"]
    assert result["findings"][0]["id"] == "P01-003"


def test_geometry_missing_is_reported():
    result = lg.lint_geometry({})
    assert _codes(result) == ["geometry_missing"]
    assert result["findings"][0]["sev"] == "P0"


def test_overridable_pairs_are_skipped_for_both_passthrough():
    geometry = _geometry(
        [
            _para("P01-001", rendered=[50, 600, 300, 700], overridable=False),
            _para("P01-002", rendered=[50, 610, 300, 690], overridable=False),
        ]
    )
    assert lg.lint_geometry(geometry)["findings"] == []


def test_locate_by_box_and_text():
    geometry = _geometry(
        [
            _para("P01-001", rendered=[50, 600, 300, 700]),
            _para("P01-002", rendered=[350, 600, 560, 700]),
        ]
    )
    geometry["paragraphs"][0]["text"] = "合并风险"
    near = lg.locate(geometry, page=1, box=[60, 610, 290, 690])
    assert near[0]["id"] == "P01-001"
    assert 0.5 < near[0]["score"] <= 1.0

    by_text = lg.locate(geometry, page=1, text="合并风险")
    assert [item["id"] for item in by_text] == ["P01-001"]

    assert lg.locate(geometry, page=1, box=[0, 0, 10, 10]) == []
