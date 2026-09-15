import json
from pathlib import Path
from types import SimpleNamespace

import pytest

#: 构造 MinerU 适配器只需一个不触发 API 的占位值（回放路径不用它）。
_DUMMY_MINERU_ARG = "dummy"


def _labels_from_result(result) -> set[str]:
    labels = set()
    for box in result.boxes:
        cls_id = int(box.cls.item() if hasattr(box.cls, "item") else box.cls)
        labels.add(result.names[cls_id])
    return labels


def test_mineru_adapter_recursively_expands_leaf_blocks_and_maps_types():
    from babeldoc.docvision.mineru_doclayout import MinerUDocLayoutModel

    fixture = Path("tests/fixtures/mineru/layout_v275_s41586_excerpt.json")
    layout_json = json.loads(fixture.read_text(encoding="utf-8"))

    model = MinerUDocLayoutModel(api_token=_DUMMY_MINERU_ARG)
    page_results = model._parse_layout_json_page_results(layout_json, total_pages=7)

    # page 1 contains image blocks with nested image_body / image_caption
    page1_labels = _labels_from_result(page_results[1])
    assert "figure" in page1_labels
    assert "figure_caption" in page1_labels

    # page 2 contains table blocks with nested table_caption / body / footnote
    page2_labels = _labels_from_result(page_results[2])
    assert "table_text" in page2_labels
    assert "table_caption" in page2_labels
    assert "table_footnote" in page2_labels
    assert "table" not in page2_labels  # leaf blocks should replace container blocks

    # page 6 contains ref list with nested ref_text leaves
    page6_labels = _labels_from_result(page_results[6])
    assert "reference" in page6_labels

    # discarded blocks should be preserved on pages
    page0_labels = _labels_from_result(page_results[0])
    assert {"header", "footer", "page_number"}.issubset(page0_labels)


def test_mineru_adapter_outputs_numpy_like_yolobox_values():
    from babeldoc.docvision.mineru_doclayout import MinerUDocLayoutModel

    fixture = Path("tests/fixtures/mineru/layout_v275_s41586_excerpt.json")
    layout_json = json.loads(fixture.read_text(encoding="utf-8"))
    model = MinerUDocLayoutModel(api_token=_DUMMY_MINERU_ARG)
    page_results = model._parse_layout_json_page_results(layout_json, total_pages=7)

    sample_box = page_results[2].boxes[0]
    # LayoutParser relies on .item() access for conf/cls and numpy-like scalars in xyxy
    assert hasattr(sample_box.conf, "item")
    assert hasattr(sample_box.cls, "item")
    assert all(hasattr(v, "item") for v in sample_box.xyxy)


def test_mineru_handle_document_allows_requested_page_subset(monkeypatch):
    from babeldoc.docvision.mineru_doclayout import MinerUDocLayoutModel

    fixture = Path("tests/fixtures/mineru/layout_v275_s41586_excerpt.json")
    layout_json = json.loads(fixture.read_text(encoding="utf-8"))
    model = MinerUDocLayoutModel(api_token=_DUMMY_MINERU_ARG)

    monkeypatch.setattr(
        model,
        "_request_upload_urls",
        lambda _client, _pdf_path: ("batch-1", "https://upload.example"),
    )
    monkeypatch.setattr(model, "_upload_pdf", lambda _client, _upload_url, _pdf_path: None)
    monkeypatch.setattr(
        model, "_poll_full_zip_url", lambda _client, _batch_id, _translate_config: "https://zip.example"
    )
    monkeypatch.setattr(model, "_download_zip_bytes", lambda _client, _zip_url: b"dummy")
    monkeypatch.setattr(model, "_load_layout_json_from_zip_bytes", lambda _zip_bytes: layout_json)

    translate_config = SimpleNamespace(
        input_file="dummy.pdf",
        raise_if_cancelled=lambda: None,
    )
    pages = [SimpleNamespace(page_number=6)]

    outputs = list(model.handle_document(pages, mupdf_doc=None, translate_config=translate_config, save_debug_image=None))
    assert len(outputs) == 1
    page, result = outputs[0]
    assert page.page_number == 6
    assert "reference" in _labels_from_result(result)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [({}, "en"), ({"language": None}, "en"), ({"language": "en"}, "en"), ({"language": "zh"}, "zh")],
)
def test_mineru_request_payload_includes_language_when_set(kwargs, expected):
    """Omitted/None language defaults to English; explicit overrides survive."""
    from babeldoc.docvision.mineru_doclayout import MinerUDocLayoutModel

    captured: dict = {}

    class _Response:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {
                "code": 0,
                "data": {
                    "batch_id": "batch-1",
                    "file_urls": ["https://upload.example"],
                },
            }

    class _Client:
        @staticmethod
        def post(_url, json=None, headers=None):
            captured["payload"] = json
            captured["headers"] = headers
            return _Response()

    model = MinerUDocLayoutModel(api_token=_DUMMY_MINERU_ARG, **kwargs)
    batch_id, upload_url = model._request_upload_urls(_Client(), Path("x.pdf"))
    assert (batch_id, upload_url) == ("batch-1", "https://upload.example")
    assert captured["payload"]["language"] == expected


@pytest.mark.parametrize("command", ["parse", "run"])
@pytest.mark.parametrize("language", [None, "zh"])
def test_mineru_cli_language_default_and_override(command, language):
    from babeldoc_tools.__main__ import _build_parser

    argv = [command, "x.pdf", "--workdir", "unused"]
    if language is not None:
        argv.extend(["--mineru-language", language])
    args = _build_parser().parse_args(argv)
    assert args.mineru_language == (language or "en")


@pytest.mark.parametrize("command", ["parse", "run"])
@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [({}, "en"), ({"mineru_language": None}, "en"), ({"mineru_language": "zh"}, "zh")],
)
def test_mineru_language_reaches_model_through_parse_and_run(
    tmp_path, monkeypatch, command, kwargs, expected
):
    from babeldoc.tools.agent import markdown_view
    from babeldoc_tools import common
    from babeldoc_tools.parse import parse_document
    from babeldoc_tools.run import run_pipeline

    captured = []
    monkeypatch.setattr(
        markdown_view.workflow, "_base_config", lambda *_args: SimpleNamespace()
    )

    def stop_before_pdf_preparation(_pdf_path, config):
        captured.append(config.doc_layout_model.language)
        raise common.ToolError("config_captured", "Stop before PDF parsing or API calls")

    monkeypatch.setattr(
        markdown_view.workflow, "_prepare_pdf", stop_before_pdf_preparation
    )
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub\n")
    workdir = tmp_path / "work"
    if command == "parse":
        with pytest.raises(common.ToolError) as exc_info:
            parse_document(
                str(pdf), str(workdir), mineru_token=_DUMMY_MINERU_ARG, **kwargs
            )
        assert exc_info.value.code == "config_captured"
    else:
        result = run_pipeline(
            str(workdir), str(pdf), mineru_token=_DUMMY_MINERU_ARG, **kwargs
        )
        assert result["error"]["code"] == "config_captured"
    assert captured == [expected]
