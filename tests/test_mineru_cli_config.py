import pytest


def test_parser_accepts_mineru_doclayout_args():
    from babeldoc.main import create_parser

    parser = create_parser()
    args = parser.parse_args(
        [
            "--files",
            "dummy.pdf",
            "--mineru-doclayout",
            "--mineru-api-token",
            "token",
            "--mineru-model-version",
            "vlm",
            "--mineru-skip-translate-layout-labels",
            "table,image,code,reference",
        ]
    )

    assert args.mineru_doclayout is True
    assert args.mineru_api_token == "token"
    assert args.mineru_model_version == "vlm"
    assert args.mineru_skip_translate_layout_labels == "table,image,code,reference"


def test_create_doc_layout_model_uses_mineru_backend():
    from babeldoc.main import _create_doc_layout_model_from_args
    from babeldoc.main import create_parser
    from babeldoc.docvision.mineru_doclayout import MinerUDocLayoutModel

    parser = create_parser()
    args = parser.parse_args(
        [
            "--files",
            "dummy.pdf",
            "--mineru-doclayout",
            "--mineru-api-token",
            "token",
        ]
    )

    model = _create_doc_layout_model_from_args(args)
    assert isinstance(model, MinerUDocLayoutModel)


def test_create_doc_layout_model_rejects_mineru_html_for_pdf_flow():
    from babeldoc.main import _create_doc_layout_model_from_args
    from babeldoc.main import create_parser

    parser = create_parser()
    args = parser.parse_args(
        [
            "--files",
            "dummy.pdf",
            "--mineru-doclayout",
            "--mineru-api-token",
            "token",
            "--mineru-model-version",
            "MinerU-HTML",
        ]
    )

    with pytest.raises(ValueError, match="MinerU-HTML"):
        _create_doc_layout_model_from_args(args)


def test_default_mineru_skip_aliases_include_revised_entries():
    from babeldoc.format.pdf.translation_config import TranslationConfig

    defaults = set(TranslationConfig.get_mineru_default_skip_translate_layout_labels())
    assert {
        "reference",
        "table",
        "image",
        "code",
        "header",
        "footer",
        "page_number",
        "page_footnote",
        "aside_text",
    }.issubset(defaults)

