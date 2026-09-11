"""确定性质量检测：长度比 / 低中文占比 / 句末 / 疑似合并 / 空译 / 注释残留。"""

from __future__ import annotations

from babeldoc.tools.agent import quality_checks as qc


def _rows(pairs):
    return [
        {"id": pid, "page": 1, "layout_label": "text", "source": source}
        for pid, source in pairs
    ]


def _check(pairs, targets, **kwargs):
    return qc.check_document(_rows(pairs), targets, **kwargs)


def test_plain_helpers():
    assert qc.plain("<style id='1'>甲</style>{v1}乙") == "甲乙"
    assert qc.anchor_density("<style id='1'>甲</style>") > 0
    assert qc.cjk_ratio("甲乙丙") == 1.0
    assert qc.cjk_ratio("abc") == 0.0
    assert qc.ascii_ratio("abc") == 1.0


def test_length_ratio_is_normalised_by_document_median():
    source = "This is a fairly long English sentence used to test ratios."
    pairs = [(f"P01-{index:03d}", source) for index in range(1, 11)]
    targets = {pid: "这是用来测试长度比的中文段落。" for pid, _ in pairs}
    # 一个明显偏短的段落（相对中位比 < 0.5）
    targets["P01-005"] = "太短"
    result = _check(pairs, targets)
    codes = [warning["code"] for warning in result["warnings"]]
    assert codes.count("intra_paragraph_truncated") == 1
    truncated = next(
        w for w in result["warnings"] if w["code"] == "intra_paragraph_truncated"
    )
    assert truncated["id"] == "P01-005"
    assert truncated["rel_ratio"] < qc.THRESHOLDS["truncated_rel_ratio"]
    assert result["metrics"]["median_len_ratio"] > 0


def test_short_source_is_not_flagged_as_truncated():
    pairs = [("P01-001", "eGuard."), ("P01-002", "This is a fairly long sentence.")]
    targets = {"P01-001": "", "P01-002": "这是一句相当长的句子。"}
    result = _check(pairs, targets)
    assert [w["code"] for w in result["warnings"]] == []


def test_empty_target_is_blocker():
    pairs = [("P01-001", "A long enough English sentence here to be translated.")]
    result = _check(pairs, {"P01-001": "<style id='1'>  </style>"})
    assert [b["code"] for b in result["blockers"]] == ["empty_target"]
    assert result["blockers"][0]["ids"] == ["P01-001"]


def test_missing_ids_blocker_and_suspect_merge():
    source = "A long enough English sentence here to be translated properly."
    pairs = [(f"P01-{index:03d}", source) for index in range(1, 6)]
    targets = {pid: "这是一段足够长的中文译文用于测试。" for pid, _ in pairs}
    targets.pop("P01-004")
    targets["P01-003"] = "这是一段异常长的中文译文。" * 6  # 疑似吞并了邻段
    result = _check(pairs, targets, missing_ids=["P01-004"])
    assert [b["code"] for b in result["blockers"]] == ["missing_ids"]
    suspects = [w for w in result["warnings"] if w["code"] == "suspect_merge"]
    assert [s["id"] for s in suspects] == ["P01-003"]
    assert suspects[0]["missing_ids"] == ["P01-004"]


def test_suspect_merge_is_page_scoped():
    """参考文献行（原文保留 → 归一化比偏大）不应被当作吞并嫌疑。"""
    source = "A long enough English sentence here to be translated properly."
    pairs = [(f"P01-{index:03d}", source) for index in range(1, 5)]
    pairs.append(("P09-001", "B. Author. 2018. A title. In Proceedings of QDB."))
    targets = {pid: "这是一段足够长的中文译文用于测试。" for pid, _ in pairs}
    targets["P09-001"] = "B. Author. 2018. A title. In Proceedings of QDB."
    result = _check(pairs, targets, missing_ids=["P01-002"])
    assert [w for w in result["warnings"] if w["code"] == "suspect_merge"] == []


def test_low_cjk_warning_and_reference_whitelist():
    body = "We propose a method that improves robustness across many settings."
    reference = "B. Author. 2018. A title. In Proceedings of QDB. https://doi.org/x"
    pairs = [("P01-001", body), ("P01-002", reference)]
    targets = {"P01-001": body, "P01-002": reference}
    result = _check(pairs, targets)
    low = [w for w in result["warnings"] if w["code"] == "low_cjk"]
    assert [w["id"] for w in low] == ["P01-001"]


def test_low_cjk_skips_short_and_anchor_heavy():
    pairs = [
        ("P01-001", "pip install -r requirements.txt"),
        ("P01-002", "Hierarchical Density-Based Clustering{v1}{v2}{v3}{v4}{v5}"),
    ]
    targets = dict(pairs)
    result = _check(pairs, targets)
    assert [w for w in result["warnings"] if w["code"] == "low_cjk"] == []


def test_sentence_end_mismatch():
    pairs = [
        ("P01-001", "This is a reasonably long sentence that ends with a period."),
        ("P01-002", "This is a reasonably long clause that ends with a comma,"),
    ]
    targets = {
        "P01-001": "这句话同样足够长但没有以中文句号作结句尾",  # 源文句末是句号 → 报
        "P01-002": "这句话同样足够长并以逗号结尾，",  # 源文句末不是句末标点 → 不报
    }
    result = _check(pairs, targets)
    mismatches = [w for w in result["warnings"] if w["code"] == "sentence_end_mismatch"]
    assert [w["id"] for w in mismatches] == ["P01-001"]


def test_formula_splice_warning():
    """公式占位符把英文单词切断 → 源文本身破碎，标记为 P2（不当作翻译缺陷）。"""
    pairs = [
        ("P01-001", "Fig. 5 illustrates how varying{v9}influences the merged model."),
        ("P01-002", "We compare the symbol {v1} has a clear meaning in this setting."),
    ]
    targets = {
        "P01-001": "图 5 说明了改变如何影响合并后的模型。",
        "P01-002": "我们比较符号在设定下的含义。",
    }
    result = _check(pairs, targets)
    spliced = [w for w in result["warnings"] if w["code"] == "formula_splice"]
    assert [w["id"] for w in spliced] == ["P01-001"]
    assert "⟨公式⟩" in spliced[0]["samples"][0]
    assert result["metrics"]["formula_splice"] == 1


def test_markdown_comment_leak_is_blocker():
    pairs = [("P01-001", "Some English source text long enough to matter.")]
    targets = {"P01-001": "中文译文 <!-- babeldoc-markdown v1 -->"}
    result = _check(pairs, targets)
    assert "markdown_comment_leak" in [b["code"] for b in result["blockers"]]


def test_fallback_ids_surface_as_warning():
    pairs = [("P01-001", "Some English source text long enough to matter.")]
    targets = {"P01-001": "Some English source text long enough to matter."}
    result = _check(pairs, targets, fallback_ids=["P01-001"])
    assert result["blockers"] == []
    assert "fallback_to_source" in [w["code"] for w in result["warnings"]]
