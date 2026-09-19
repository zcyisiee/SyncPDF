import json
from pathlib import Path
from types import SimpleNamespace

import pytest

#: 构造 MinerU 适配器只需一个不触发 API 的占位值（回放路径不用它）。
_DUMMY_MINERU_ARG = "dummy"


@pytest.mark.parametrize("mode", ["replay", "cache", "online"])
@pytest.mark.parametrize(
    ("rotation", "display_box"),
    [(0, [20, 30, 80, 50]), (90, [250, 20, 270, 80]),
     (180, [120, 250, 180, 270]), (270, [30, 120, 50, 180])],
)
def test_rotated_mineru_layout_and_provider_share_native_frame(
    tmp_path, monkeypatch, mode, rotation, display_box
):
    import pymupdf
    from babeldoc.docvision.mineru_doclayout import MinerUDocLayoutModel
    from babeldoc.format.pdf.document_il import il_version_1 as il
    from babeldoc.format.pdf.document_il.midend.layout_parser import LayoutParser
    from babeldoc.format.pdf.translation_config import TranslationConfig
    from babeldoc.progress_monitor import ProgressMonitor

    raw = {"pdf_info": [{"page_idx": 0, "para_blocks": [{
        "type": "list", "bbox": display_box, "blocks": [{
            "type": "text", "bbox": display_box, "lines": [{
                "bbox": display_box, "spans": [{
                    "type": "inline_equation", "bbox": display_box, "content": "x",
                }],
            }],
        }],
    }]}]}
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(json.dumps(raw))
    model = MinerUDocLayoutModel(api_token=_DUMMY_MINERU_ARG)
    if mode == "replay":
        monkeypatch.setenv("BABELDOC_MINERU_LAYOUT_JSON", str(layout_path))
    else:
        monkeypatch.delenv("BABELDOC_MINERU_LAYOUT_JSON", raising=False)
        monkeypatch.setattr(
            model, "_layout_cache_path",
            lambda _path: layout_path if mode == "cache" else None,
        )
        monkeypatch.setattr(model, "_fetch_layout_json", lambda *_args: raw)

    config = TranslationConfig(
        input_file="fixture.pdf", working_dir=tmp_path, doc_layout_model=model,
        layout_coverage_threshold=0,
        progress_monitor=ProgressMonitor([(LayoutParser.stage_name, 1.0)]),
    )
    char_box = il.Box(x=25, y=255, x2=30, y2=265)
    docs = il.Document(page=[il.Page(page_number=0, pdf_character=[
        il.PdfCharacter(char_unicode="x", box=char_box, visual_bbox=il.VisualBbox(box=char_box)),
    ])])
    with pymupdf.open() as pdf:
        pdf.new_page(width=200, height=300).set_rotation(rotation)
        LayoutParser(config).process(docs, pdf)
        assert pdf[0].rotation == rotation
    box = docs.page[0].page_layout[0].box
    assert (box.x, box.y, box.x2, box.y2) == (19, 249, 81, 271)
    provider = model.provider_document
    for block in provider.pages[0].iter_blocks(recursive=True):
        assert block.bbox == pytest.approx([20, 30, 80, 50])
        for line in block.lines:
            assert line.bbox == pytest.approx([20, 30, 80, 50])
            assert line.spans[0].bbox == pytest.approx([20, 30, 80, 50])
    persisted = json.loads(model._provider_ir_output_path(config).read_text())
    assert persisted == provider.to_dict()
    assert json.loads(layout_path.read_text()) == raw


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
        lambda _client, _pdf_paths: ("batch-1", ["https://upload.example"]),
    )
    monkeypatch.setattr(model, "_upload_pdf", lambda _client, _upload_url, _pdf_path: None)
    monkeypatch.setattr(
        model, "_poll_full_zip_urls", lambda _client, _batch_id, _translate_config, _chunk_paths: ["https://zip.example"]
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
    batch_id, upload_urls = model._request_upload_urls(_Client(), [Path("x.pdf")])
    assert (batch_id, upload_urls) == ("batch-1", ["https://upload.example"])
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


# --------------------------------------------------------------------- #
# 分片提交（>10 页拆 chunk、单 batch 多文件、≤50 chunks）
# --------------------------------------------------------------------- #
def _make_pdf(path: Path, pages: int) -> Path:
    import pymupdf

    with pymupdf.open() as doc:
        for i in range(pages):
            page = doc.new_page()
            page.insert_text((72, 72), f"page {i}")
        doc.save(path)
    return path


def test_mineru_split_pdf_chunks_under_threshold_is_passthrough(tmp_path):
    from babeldoc.docvision.mineru_doclayout import MinerUDocLayoutModel

    pdf = _make_pdf(tmp_path / "small.pdf", 10)
    model = MinerUDocLayoutModel(api_token=_DUMMY_MINERU_ARG)
    chunk_paths, tmpdir = model._split_pdf_chunks(pdf, 10)
    assert chunk_paths == [pdf]
    assert tmpdir is None


def test_mineru_split_pdf_chunks_over_threshold(tmp_path):
    from babeldoc.docvision.mineru_doclayout import MinerUDocLayoutModel

    pdf = _make_pdf(tmp_path / "big.pdf", 25)
    model = MinerUDocLayoutModel(api_token=_DUMMY_MINERU_ARG)
    chunk_paths, tmpdir = model._split_pdf_chunks(pdf, 25)
    try:
        assert len(chunk_paths) == 3
        import pymupdf

        assert [pymupdf.open(p).page_count for p in chunk_paths] == [10, 10, 5]
        # 分片文件名互不相同（data_id 依据文件名）
        assert len({p.name for p in chunk_paths}) == 3
        # 命名带页范围，便于服务端 file_name 对齐
        assert "p0001-0010" in chunk_paths[0].name
        assert "p0021-0025" in chunk_paths[2].name
    finally:
        tmpdir.cleanup()


def test_mineru_split_pdf_chunks_rejects_over_max_chunks(tmp_path):
    from babeldoc.docvision.mineru_doclayout import MinerUDocLayoutModel

    pdf = _make_pdf(tmp_path / "huge.pdf", 501)
    model = MinerUDocLayoutModel(api_token=_DUMMY_MINERU_ARG)
    with pytest.raises(ValueError, match="max_chunks"):
        model._split_pdf_chunks(pdf, 501)


def test_mineru_merge_layout_jsons_offsets_page_idx():
    from babeldoc.docvision.mineru_doclayout import MinerUDocLayoutModel

    def chunk(_offset_pages, idx_values):
        return {
            "_backend": "vlm",
            "pdf_info": [{"page_idx": i} for i in idx_values],
        }

    merged = MinerUDocLayoutModel._merge_layout_jsons(
        [(0, chunk(0, [0, 1, 2])), (10, chunk(10, [0, 1]))]
    )
    assert [p["page_idx"] for p in merged["pdf_info"]] == [0, 1, 2, 10, 11]
    assert merged["_backend"] == "vlm"


def test_mineru_merge_layout_jsons_single_chunk_passthrough():
    from babeldoc.docvision.mineru_doclayout import MinerUDocLayoutModel

    layout = {"_backend": "vlm", "pdf_info": [{"page_idx": 3}]}
    assert MinerUDocLayoutModel._merge_layout_jsons([(0, layout)]) is layout


def test_mineru_align_extract_results_by_file_name():
    from babeldoc.docvision.mineru_doclayout import MinerUDocLayoutModel

    results = [
        {"file_name": "b.pdf", "full_zip_url": "u-b"},
        {"file_name": "a.pdf", "full_zip_url": "u-a"},
    ]
    aligned = MinerUDocLayoutModel._align_extract_results(
        results, [Path("a.pdf"), Path("b.pdf")]
    )
    assert [item["full_zip_url"] for item in aligned] == ["u-a", "u-b"]


def test_mineru_poll_full_zip_urls_waits_for_all_chunks():
    from babeldoc.docvision.mineru_doclayout import MinerUDocLayoutModel

    model = MinerUDocLayoutModel(api_token=_DUMMY_MINERU_ARG, poll_interval_seconds=0)
    states = [
        [
            {"file_name": "a.pdf", "state": "done", "full_zip_url": "u-a"},
            {"file_name": "b.pdf", "state": "waiting"},
        ],
        [
            {"file_name": "a.pdf", "state": "done", "full_zip_url": "u-a"},
            {"file_name": "b.pdf", "state": "done", "full_zip_url": "u-b"},
        ],
    ]

    class _Response:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {
                "code": 0,
                "trace_id": "t",
                "data": {"extract_result": states.pop(0)},
            }

    class _Client:
        @staticmethod
        def get(_url, **_kwargs):
            return _Response()

    urls = model._poll_full_zip_urls(
        _Client(),
        "batch-1",
        SimpleNamespace(raise_if_cancelled=lambda: None),
        [Path("a.pdf"), Path("b.pdf")],
    )
    assert urls == ["u-a", "u-b"]


def test_mineru_poll_full_zip_urls_reports_chunk_failure():
    from babeldoc.docvision.mineru_doclayout import MinerUDocLayoutModel

    model = MinerUDocLayoutModel(api_token=_DUMMY_MINERU_ARG, poll_interval_seconds=0)

    class _Response:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {
                "code": 0,
                "trace_id": "t",
                "data": {
                    "extract_result": [
                        {"file_name": "a.pdf", "state": "done", "full_zip_url": "u-a"},
                        {
                            "file_name": "b.pdf",
                            "state": "failed",
                            "err_msg": "parsing failed",
                        },
                    ]
                },
            }

    class _Client:
        @staticmethod
        def get(_url, **_kwargs):
            return _Response()

    with pytest.raises(RuntimeError, match=r"file=b\.pdf.*err_msg=parsing failed"):
        model._poll_full_zip_urls(
            _Client(),
            "batch-1",
            SimpleNamespace(raise_if_cancelled=lambda: None),
            [Path("a.pdf"), Path("b.pdf")],
        )
