from types import SimpleNamespace

from babeldoc.format.pdf.document_il.il_version_1 import PdfParagraph


def _mk_paragraph(layout_label: str, text: str = "hello") -> PdfParagraph:
    return PdfParagraph(
        box=None,
        pdf_style=None,
        pdf_paragraph_composition=[],
        xobj_id=None,
        unicode=text,
        vertical=False,
        first_line_indent=False,
        debug_id="dbg",
        layout_label=layout_label,
    )


def _mk_config(skip_labels: set[str]):
    return SimpleNamespace(
        min_text_length=1,
        should_skip_translate_layout_label=lambda label: label in skip_labels,
    )


def test_llm_only_translator_skips_reference_but_keeps_text():
    from babeldoc.format.pdf.document_il.midend.il_translator_llm_only import (
        ILTranslatorLLMOnly,
    )

    translator = ILTranslatorLLMOnly.__new__(ILTranslatorLLMOnly)
    translator.translation_config = _mk_config({"reference"})

    assert not translator._should_translate_paragraph(_mk_paragraph("reference"))
    assert translator._should_translate_paragraph(_mk_paragraph("text"))


def test_non_llm_translator_pre_translate_returns_none_for_code_label():
    from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator

    translator = ILTranslator.__new__(ILTranslator)
    translator.translation_config = _mk_config({"code"})
    translator.support_llm_translate = False

    text, translate_input = translator.pre_translate_paragraph(
        _mk_paragraph("code"),
        tracker=None,
        page_font_map={},
        xobj_font_map={},
    )
    assert text is None
    assert translate_input is None


def test_skip_alias_expansion_keeps_reference_and_skips_table_body_not_caption():
    from babeldoc.format.pdf.translation_config import TranslationConfig

    effective = TranslationConfig.expand_mineru_skip_translate_layout_labels(
        ("reference", "table", "image", "code", "header")
    )

    assert "reference" in effective
    assert "table_text" in effective
    # table_footnote 属于表体保护区域（数据来源等注释随表体一起跳过翻译）
    assert "table_footnote" in effective
    assert "figure" in effective
    # figure_text 是图内嵌文字，随图一起跳过
    assert "figure_text" in effective
    assert "code" in effective
    assert "header" in effective
    assert "table" not in effective
    assert "image" not in effective
    # 仅标题型 caption 默认翻译：table/figure/code 的 caption 都不被别名吞掉
    assert "table_caption" not in effective
    assert "figure_caption" not in effective
    assert "code_caption" not in effective


def test_default_skip_labels_translate_all_captions():
    from babeldoc.format.pdf.translation_config import TranslationConfig

    effective = TranslationConfig.expand_mineru_skip_translate_layout_labels(
        TranslationConfig.get_mineru_default_skip_translate_layout_labels()
    )

    for caption in ("table_caption", "figure_caption", "code_caption"):
        assert caption not in effective
    assert "table_text" in effective
    assert "figure" in effective and "figure_text" in effective
    assert "author" in effective
