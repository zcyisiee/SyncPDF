"""实验性 MinerU OCR 文本回填的离线回归测试。"""

from __future__ import annotations

import json
from types import SimpleNamespace

from babeldoc.docvision.provider_ir import ProviderBlock
from babeldoc.docvision.provider_ir import ProviderDocument
from babeldoc.docvision.provider_ir import ProviderLine
from babeldoc.docvision.provider_ir import ProviderPage
from babeldoc.docvision.provider_ir import ProviderSpan
from babeldoc.format.pdf.document_il import il_version_1
from babeldoc.format.pdf.document_il.midend.provider_ocr import ProviderOcrTextFusion


def _char(value, x0, x1):
    box = il_version_1.Box(x=x0, y=85, x2=x1, y2=95)
    return il_version_1.PdfCharacter(
        pdf_style=il_version_1.PdfStyle(
            font_id="F1", font_size=10, graphic_state=il_version_1.GraphicState()
        ),
        box=box,
        visual_bbox=il_version_1.VisualBbox(box=box),
        char_unicode=value,
        advance=x1 - x0,
    )


def _provider(tmp_path):
    # MinerU coordinates have y pointing down.  [0, 5, 30, 15] maps to the
    # native character row at y=85..95 on a 100pt page.
    spans = [
        ProviderSpan("s-text", [0, 5, 30, 15], "text", "ab", 1.0, 0),
        # Formula overlaps the broad text span; it must never be overwritten.
        ProviderSpan("s-formula", [10, 5, 20, 15], "inline_equation", "x", 1.0, 0),
    ]
    line = ProviderLine("l0", [0, 5, 30, 15], spans)
    block = ProviderBlock("b0", "text", None, [0, 5, 30, 15], None, 0, 0, [line])
    provider_page = ProviderPage(0, [block], ["b0"])
    document = ProviderDocument("vlm", "hybrid", 1, [provider_page])
    root = tmp_path / "agent"
    path = root / "source" / "mineru" / "provider_ir.json"
    path.parent.mkdir(parents=True)
    path.write_text(document.to_json(), encoding="utf-8")
    return root


def _config(root, enabled=True):
    return SimpleNamespace(
        mineru_use_ocr_text=enabled,
        provider_ir_dir=root,
        working_dir=None,
    )


def _docs():
    page = il_version_1.Page(
        page_number=0,
        pdf_character=[_char("?", 2, 8), _char("?", 12, 18)],
        page_layout=[],
        cropbox=il_version_1.Cropbox(
            box=il_version_1.Box(x=0, y=0, x2=100, y2=100)
        ),
    )
    return il_version_1.Document(page=[page])


def test_ocr_fusion_replaces_equal_length_text_and_protects_formula(tmp_path):
    root = _provider(tmp_path)
    docs = _docs()
    ProviderOcrTextFusion(_config(root)).process(docs)
    values = [char.char_unicode for char in docs.page[0].pdf_character]
    assert values == ["a", "?"]
    report = json.loads(
        (root / "source" / "mineru" / "ocr_fusion.json").read_text(encoding="utf-8")
    )
    assert report["summary"]["applied"] == 1
    assert report["summary"]["changed_chars"] == 1


def test_ocr_fusion_is_noop_by_default(tmp_path):
    root = _provider(tmp_path)
    docs = _docs()
    ProviderOcrTextFusion(_config(root, enabled=False)).process(docs)
    assert [char.char_unicode for char in docs.page[0].pdf_character] == ["?", "?"]
    assert not (root / "source" / "mineru" / "ocr_fusion.json").exists()


def test_ocr_fusion_skips_length_mismatch(tmp_path):
    root = _provider(tmp_path)
    path = root / "source" / "mineru" / "provider_ir.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["pages"][0]["blocks"][0]["lines"][0]["spans"][0]["content"] = "longer"
    path.write_text(json.dumps(data), encoding="utf-8")
    docs = _docs()
    ProviderOcrTextFusion(_config(root)).process(docs)
    assert [char.char_unicode for char in docs.page[0].pdf_character] == ["?", "?"]
    report = json.loads(
        (root / "source" / "mineru" / "ocr_fusion.json").read_text(encoding="utf-8")
    )
    assert report["summary"]["skipped"] == 1
