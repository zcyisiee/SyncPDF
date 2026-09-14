import pytest

from babeldoc.format.pdf.document_il.backend.link_text import role_matches


def test_glm5_page11_footnote_not_equation():
    # role_matches('footnote','4',' following and the quality of generation','. next') must be True.
    # Current regex sees Eq inside thequalityofgeneration, wrongly treats this real GLM-5 page 11 footnote as equation.
    assert role_matches("footnote", "4", " following and the quality of generation", ". next") is True
    assert role_matches("equation", "4", " following and the quality of generation", ". next") is False


def test_english_prefix_word_boundary():
    # Avoid converting generation/equality/tableau into numbered cross references
    assert role_matches("equation", "4", "equality", "") is False
    assert role_matches("equation", "4", "high quality of generation", "") is False
    assert role_matches("table", "II", "tableau", "") is False
    assert role_matches("table", "4", "tableau", "") is False
    assert role_matches("figure", "4", "prefiguration", "") is False
    assert role_matches("appendix", "A", "appendicular", "") is False


def test_real_cross_references_preserved():
    # preserve real Eq. (4), Figure 4, 图4, Table II and citation [4] recognition
    assert role_matches("equation", "4", "as shown in Eq. ", "") is True
    assert role_matches("equation", "(4)", "as shown in Eq. ", "") is True
    assert role_matches("equation", "4", "as shown in Eq. (", ")") is True
    assert role_matches("equation", "4", "in Equation ", "") is True
    assert role_matches("equation", "(4)", "in Equation ", "") is True

    assert role_matches("figure", "4", "see Figure ", "") is True
    assert role_matches("figure", "4", "see Fig. ", "") is True
    assert role_matches("figure", "4", "see Fig.", "") is True
    assert role_matches("figure", "4", "see Fig4", "") is True
    assert role_matches("figure", "4", "见图", "") is True
    assert role_matches("figure", "4", "如图", "") is True

    assert role_matches("table", "II", "in Table ", "") is True
    assert role_matches("table", "2", "in Tab. ", "") is True
    assert role_matches("table", "2", "见表", "") is True

    assert role_matches("citation", "4", "as seen in [", "]") is True
    assert role_matches("citation", "[4]", "as seen in ", "") is True


def test_chinese_prose_prefix():
    # Chinese prefixes must still match after Chinese prose.
    assert role_matches("equation", "4", "根据公式", "") is True
    assert role_matches("equation", "(4)", "由公式", "") is True
    assert role_matches("equation", "4", "由式", "") is True
    assert role_matches("equation", "4", "由式(", ")") is True
    assert role_matches("equation", "一", "见式", "") is True
    assert role_matches("figure", "4", "在第一章如图", "") is True
    assert role_matches("table", "3", "详细对比见表", "") is True
    assert role_matches("theorem", "1", "根据定理", "") is True
    assert role_matches("appendix", "A", "参见附录", "") is True


def test_footnotes_following_prose():
    # Footnotes following ordinary English must remain eligible.
    assert role_matches("footnote", "1", "This is an interesting statement", ". Later") is True
    assert role_matches("footnote", "2", "另一个重要发现", "。接下来") is True
