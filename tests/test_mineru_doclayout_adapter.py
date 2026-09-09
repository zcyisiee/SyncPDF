import json
from pathlib import Path
from types import SimpleNamespace


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

    model = MinerUDocLayoutModel(api_token="dummy-token")
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
    model = MinerUDocLayoutModel(api_token="dummy-token")
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
    model = MinerUDocLayoutModel(api_token="dummy-token")

    monkeypatch.setattr(
        model,
        "_request_upload_urls",
        lambda client, pdf_path: ("batch-1", "https://upload.example"),
    )
    monkeypatch.setattr(model, "_upload_pdf", lambda client, upload_url, pdf_path: None)
    monkeypatch.setattr(
        model, "_poll_full_zip_url", lambda client, batch_id, translate_config: "https://zip.example"
    )
    monkeypatch.setattr(model, "_download_zip_bytes", lambda client, zip_url: b"dummy")
    monkeypatch.setattr(model, "_load_layout_json_from_zip_bytes", lambda zip_bytes: layout_json)

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
