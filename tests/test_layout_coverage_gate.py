"""Layout coverage gate：未命中 layout 区域的字符占比超阈值时解析失败。

替代已删除的字符聚类兜底（fallback_line）：漏译必须显式暴露而不是静默发生。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from babeldoc.format.pdf.document_il import il_version_1
from babeldoc.format.pdf.document_il.midend.layout_parser import LayoutParser
from babeldoc.format.pdf.document_il.midend.layout_parser import compute_layout_coverage
from babeldoc.format.pdf.document_il.utils.style_helper import GREEN
from babeldoc.format.pdf.translation_config import TranslationConfig
from babeldoc.progress_monitor import ProgressMonitor


def _char(x: float, y: float, text: str = "a"):
    box = il_version_1.Box(x=x, y=y, x2=x + 5, y2=y + 10)
    return il_version_1.PdfCharacter(
        pdf_style=SimpleNamespace(font_id="base", font_size=10, graphic_state=GREEN),
        box=box,
        visual_bbox=il_version_1.VisualBbox(box=box),
        char_unicode=text,
    )


def _layout(x: float, y: float, x2: float, y2: float, class_name: str = "text"):
    return il_version_1.PageLayout(
        box=il_version_1.Box(x=x, y=y, x2=x2, y2=y2),
        id=1,
        conf=1.0,
        class_name=class_name,
    )


def _page(page_number: int, chars, layouts=()):
    return il_version_1.Page(
        page_number=page_number,
        page_layout=list(layouts),
        pdf_character=list(chars),
    )


class _StubLayoutModel:
    """最小布局模型：不给任何区域（供门禁测试驱动 LayoutParser.process）。"""

    stride = 32

    def handle_document(self, pages, mupdf_doc, translate_config, save_debug_image):
        from babeldoc.docvision.base_doclayout import YoloResult

        for page in pages:
            yield page, YoloResult(names={}, boxes=[])


def _docs(pages):
    return il_version_1.Document(page=list(pages))


def _config(tmp_path, threshold: float) -> TranslationConfig:
    return TranslationConfig(
        input_file="fixture.pdf",
        working_dir=tmp_path,
        doc_layout_model=_StubLayoutModel(),
        layout_coverage_threshold=threshold,
        progress_monitor=ProgressMonitor([(LayoutParser.stage_name, 1.0)]),
    )


def test_coverage_counts_uncovered_chars_per_page():
    # 第 1 页：2 个字符都被覆盖；第 2 页：3 个字符，只有 1 个被覆盖。
    docs = _docs(
        [
            _page(0, [_char(0, 0, "a"), _char(10, 0, "b")], [_layout(-1, -1, 100, 100)]),
            _page(1, [_char(0, 0, "c"), _char(200, 200, "d"), _char(300, 300, "e")], [_layout(-1, -1, 50, 50)]),
        ]
    )

    report = compute_layout_coverage(docs)

    assert [page["total_chars"] for page in report["pages"]] == [2, 3]
    assert [page["uncovered_chars"] for page in report["pages"]] == [0, 2]
    assert report["pages"][0]["coverage"] == 1.0
    assert report["global"]["total_chars"] == 5
    assert report["global"]["uncovered_chars"] == 2
    assert report["global"]["uncovered_ratio"] == pytest.approx(0.4)


def test_coverage_ignores_uncovered_whitespace(tmp_path):
    docs = _docs(
        [_page(0, [_char(20, 0, " "), _char(0, 0, "a")], [_layout(-1, -1, 10, 20)])]
    )
    report = compute_layout_coverage(docs)
    LayoutParser(_config(tmp_path, threshold=0))._enforce_layout_coverage(docs, report)
    assert report["global"]["total_chars"] == 1
    assert report["global"]["uncovered_chars"] == 0
    assert report["global"]["ignored_whitespace_chars"] == 1
    assert report["pages"][0]["uncovered_text_preview"] == ""


def test_whitespace_cannot_dilute_real_missing_text(tmp_path):
    chars = [_char(0, 0, " ") for _ in range(1000)] + [_char(20, 0, "x")]
    docs = _docs([_page(0, chars, [_layout(-1, -1, 10, 20)])])
    report = compute_layout_coverage(docs)
    with pytest.raises(RuntimeError, match="layout_coverage_gate"):
        LayoutParser(_config(tmp_path, threshold=0.005))._enforce_layout_coverage(docs, report)
    assert report["global"]["uncovered_ratio"] == 1
    assert report["pages"][0]["uncovered_text_preview"] == "x"


def test_coverage_gate_raises_and_writes_artifact(tmp_path):
    # 4 个字符全部裸露 → uncovered_ratio = 1.0 > 0.005。
    # 走完整 process()：stub 模型不产出任何区域，字符全部裸露。
    docs = _docs([_page(0, [_char(i * 10, 0, "x") for i in range(4)], [])])
    parser = LayoutParser(_config(tmp_path, threshold=0.005))

    with pytest.raises(RuntimeError) as excinfo:
        parser.process(docs, mupdf_doc=None)

    message = str(excinfo.value)
    assert "layout_coverage_gate" in message
    assert "--layout-coverage-threshold" in message
    artifact = parser._coverage_report_path()
    assert artifact.exists()
    import json

    report = json.loads(artifact.read_text(encoding="utf-8"))
    assert report["passed"] is False
    assert report["threshold"] == 0.005
    assert report["global"]["uncovered_chars"] == 4
    # 未覆盖字符的文本片段（人工定位用）
    assert report["pages"][0]["uncovered_text_preview"] == "xxxx"


def test_coverage_at_exactly_threshold_does_not_trigger(tmp_path):
    # 1000 个字符里 5 个裸露 → ratio == 0.005 == threshold，用 `>` 判定，不触发。
    # 直接调门禁（process() 会用模型输出覆盖夹具的 page_layout，这里只验阈值边界）。
    chars = [_char(0, 0, "a") for _ in range(995)]
    chars += [_char(500 + i, 500, "b") for i in range(5)]
    docs = _docs([_page(0, chars, [_layout(-10, -10, 100, 100)])])
    parser = LayoutParser(_config(tmp_path, threshold=0.005))

    report = compute_layout_coverage(docs)
    parser._enforce_layout_coverage(docs, report)  # 不抛错

    assert report["global"]["uncovered_ratio"] == 0.005
    assert report["global"]["uncovered_chars"] == 5


def test_coverage_gate_passes_and_still_writes_audit_artifact(tmp_path):
    docs = _docs([_page(0, [_char(1, 1, "a")], [_layout(0, 0, 50, 50)])])
    parser = LayoutParser(_config(tmp_path, threshold=0.005))

    report = compute_layout_coverage(docs)
    parser._enforce_layout_coverage(docs, report)

    import json

    artifact_report = json.loads(
        parser._coverage_report_path().read_text(encoding="utf-8")
    )
    assert artifact_report["passed"] is True
    assert artifact_report["global"]["uncovered_chars"] == 0


def test_page_without_chars_does_not_trigger_gate(tmp_path):
    # 扫描件/OCR workaround：无字符页分母为 0，coverage 记 1.0。
    docs = _docs([_page(0, [], []), _page(1, [], [])])
    parser = LayoutParser(_config(tmp_path, threshold=0.0))

    parser.process(docs, mupdf_doc=None)  # threshold=0.0 也不触发

    report = compute_layout_coverage(docs)
    assert report["global"]["total_chars"] == 0
    assert report["global"]["coverage"] == 1.0
