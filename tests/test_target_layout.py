"""译文侧版面识别（``babeldoc_tools.target_layout``）：落盘、状态与失败降级。

单测**不打真实网络**：识别客户端整体用替身替换（``monkeypatch`` 掉
``MinerUDocLayoutModel``），只验「什么条件下跑、跑完写什么、失败怎么降级」。
真实 MinerU 端到端只跑一次，见任务交付的过程证据（``tmp/``），不进单测。
"""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf
import pytest
from babeldoc_tools import target_layout


def _pdf(path, pages=2):
    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as doc:
        for index in range(pages):
            page = doc.new_page(width=300, height=200)
            page.insert_text((20, 40), f"page {index + 1}")
        doc.save(path)
    return path


def _mono(workdir, name="out.mono.pdf"):
    """build 产物位置：``<workdir>/output/<name>``（与 layout.build_pdf 的缺省一致）。"""
    return _pdf(workdir / "output" / name)


def _workdir(tmp_path, *, layout="mineru", lang_out="zh"):
    """最小 workdir：只需要 ``agent/run_state.json`` 的 config（后端与目标语言）。"""
    agent = tmp_path / "wd" / "agent"
    agent.mkdir(parents=True)
    (agent / "run_state.json").write_text(
        json.dumps({"config": {"layout": layout, "lang_out": lang_out}}),
        encoding="utf-8",
    )
    return tmp_path / "wd"


#: 替身客户端拿到的 token 值（只用于断言"token 真的传下去了"，不是真凭据）。
FAKE_TOKEN = "fixture-token"  # noqa: S105 - 测试替身，不是凭据


#: 替身客户端返回的 IR（默认为最小结构；测试可换成 fixture 的真实识别结果）。
DEFAULT_IR: dict = {
    "page_count": 2,
    "pages": [
        {
            "page_index": 0,
            "blocks": [
                {
                    "block_id": "p0-b0",
                    "type": "text",
                    "bbox": [10, 10, 100, 30],
                    "lines": [],
                }
            ],
        },
        {"page_index": 1, "blocks": []},
    ],
}


class _FakeModel:
    """替身识别客户端：记录调用参数，按 ``fail`` 决定抛错还是写一份 IR。"""

    calls: list[dict] = []
    fail: str | None = None
    ir: dict = DEFAULT_IR

    def __init__(self, *, api_token=None, language=None):
        self.has_token = bool(api_token)
        self.language = language

    def recognize_pdf_provider_ir(self, pdf_path, output_path, *, translate_config=None):
        _FakeModel.calls.append(
            {
                "pdf": str(pdf_path),
                "output": str(output_path),
                "has_token": self.has_token,
                "language": self.language,
            }
        )
        if _FakeModel.fail is not None:
            raise RuntimeError(_FakeModel.fail)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(self.ir), encoding="utf-8")
        return None


@pytest.fixture
def fake_model(monkeypatch):
    import babeldoc.docvision.mineru_doclayout as mineru_mod

    _FakeModel.calls = []
    _FakeModel.fail = None
    _FakeModel.ir = DEFAULT_IR
    monkeypatch.setattr(mineru_mod, "MinerUDocLayoutModel", _FakeModel)
    monkeypatch.setenv("MINERU_API_TOKEN", FAKE_TOKEN)
    return _FakeModel


# --------------------------------------------------------------------------- #
# 成功路径
# --------------------------------------------------------------------------- #
def test_writes_ir_and_manifest_after_build(tmp_path, fake_model):
    workdir = _workdir(tmp_path)
    pdf = _mono(workdir)

    payload = target_layout.recognize_target_layout(workdir, {"mono_pdf": str(pdf)})

    assert payload["status"] == target_layout.STATUS_OK
    assert payload["reason"] is None
    assert payload["provider"] == "mineru"
    assert payload["page_count"] == 2
    # 清单里的 pdf 是 workdir 相对路径（下游/前端都要能定位它是哪份产物）
    assert payload["pdf"] == "output/out.mono.pdf"
    assert payload["provider_ir"] == target_layout.PROVIDER_IR_RELATIVE
    # IR 与清单都落在约定的位置
    assert target_layout.provider_ir_path(workdir).is_file()
    assert target_layout.manifest_path(workdir).is_file()
    assert json.loads(target_layout.manifest_path(workdir).read_text()) == payload
    # 传给客户端的语言来自 run_state 的 lang_out（译文 PDF 的语言，不是源侧 en）
    assert fake_model.calls[0]["language"] == "zh"
    assert fake_model.calls[0]["has_token"] is True


def test_mono_pdf_wins_over_dual(tmp_path, fake_model):
    workdir = _workdir(tmp_path)
    dual = _mono(workdir, "out.dual.pdf")
    mono = _mono(workdir)

    payload = target_layout.recognize_target_layout(
        workdir, {"dual_pdf": str(dual), "mono_pdf": str(mono)}
    )

    assert payload["status"] == target_layout.STATUS_OK
    assert fake_model.calls[0]["pdf"] == str(mono)


def test_dual_is_used_when_mono_missing(tmp_path, fake_model):
    workdir = _workdir(tmp_path)
    dual = _mono(workdir, "out.dual.pdf")

    payload = target_layout.recognize_target_layout(workdir, {"dual_pdf": str(dual)})

    assert payload["status"] == target_layout.STATUS_OK
    assert fake_model.calls[0]["pdf"] == str(dual)


def test_missing_pdf_file_is_skipped(tmp_path, fake_model):
    """返回值里给了路径但文件不存在 → 跳过（不把幽灵路径交给识别）。"""
    workdir = _workdir(tmp_path)

    payload = target_layout.recognize_target_layout(
        workdir, {"mono_pdf": str(workdir / "output" / "gone.mono.pdf")}
    )

    assert payload["status"] == target_layout.STATUS_SKIPPED
    assert fake_model.calls == []


# --------------------------------------------------------------------------- #
# 跳过：状态与原因都必须落盘（不谎报 ok）
# --------------------------------------------------------------------------- #
def test_missing_pdf_is_skipped(tmp_path, fake_model):
    workdir = _workdir(tmp_path)

    payload = target_layout.recognize_target_layout(workdir, {})

    assert payload["status"] == target_layout.STATUS_SKIPPED
    assert "译文 PDF" in payload["reason"]
    assert target_layout.manifest_path(workdir).is_file()
    assert fake_model.calls == []


def test_explicit_disable_is_skipped(tmp_path, fake_model):
    workdir = _workdir(tmp_path)
    pdf = _mono(workdir)

    payload = target_layout.recognize_target_layout(
        workdir, {"mono_pdf": str(pdf)}, enabled=False
    )

    assert payload["status"] == target_layout.STATUS_SKIPPED
    assert "--no-target-layout" in payload["reason"]
    assert fake_model.calls == []


def test_missing_token_is_skipped_not_failed(tmp_path, fake_model, monkeypatch):
    workdir = _workdir(tmp_path)
    pdf = _mono(workdir)
    monkeypatch.delenv("MINERU_API_TOKEN", raising=False)

    payload = target_layout.recognize_target_layout(workdir, {"mono_pdf": str(pdf)})

    assert payload["status"] == target_layout.STATUS_SKIPPED
    assert "MINERU_API_TOKEN" in payload["reason"]
    assert fake_model.calls == []


def test_paddle_backend_is_skipped(tmp_path, fake_model):
    workdir = _workdir(tmp_path, layout="paddle")
    pdf = _mono(workdir)

    payload = target_layout.recognize_target_layout(workdir, {"mono_pdf": str(pdf)})

    assert payload["status"] == target_layout.STATUS_SKIPPED
    assert "paddle" in payload["reason"]
    assert fake_model.calls == []


def test_unrecorded_backend_is_skipped(tmp_path, fake_model):
    workdir = tmp_path / "wd"
    (workdir / "agent").mkdir(parents=True)
    pdf = _mono(workdir)

    payload = target_layout.recognize_target_layout(workdir, {"mono_pdf": str(pdf)})

    assert payload["status"] == target_layout.STATUS_SKIPPED
    assert "run_state.json" in payload["reason"]
    assert fake_model.calls == []


# --------------------------------------------------------------------------- #
# 失败：写 failed + 原因，但不抛（不能反过来决定 build 成败）
# --------------------------------------------------------------------------- #
def test_recognition_failure_is_recorded_and_not_raised(tmp_path, fake_model):
    workdir = _workdir(tmp_path)
    pdf = _mono(workdir)
    fake_model.fail = "额度用尽"

    payload = target_layout.recognize_target_layout(workdir, {"mono_pdf": str(pdf)})

    assert payload["status"] == target_layout.STATUS_FAILED
    assert "额度用尽" in payload["reason"]
    assert payload["provider_ir"] is None
    # 清单落盘（现场可查），IR 不落盘（没有可信结果就不留半个产物）
    assert target_layout.manifest_path(workdir).is_file()
    assert not target_layout.provider_ir_path(workdir).exists()


@pytest.mark.usefixtures("fake_model")
def test_stale_ir_is_removed_when_recognition_skipped(tmp_path, monkeypatch):
    """上一轮的 IR 不能留给新 PDF 用：跳过时删干净，让前端报「尚未识别」。"""
    workdir = _workdir(tmp_path)
    pdf = _mono(workdir)
    target_layout.recognize_target_layout(workdir, {"mono_pdf": str(pdf)})
    assert target_layout.provider_ir_path(workdir).is_file()

    monkeypatch.delenv("MINERU_API_TOKEN", raising=False)
    payload = target_layout.recognize_target_layout(workdir, {"mono_pdf": str(pdf)})

    assert payload["status"] == target_layout.STATUS_SKIPPED
    assert not target_layout.provider_ir_path(workdir).exists()


def test_failed_recognition_removes_previous_ir(tmp_path, fake_model):
    workdir = _workdir(tmp_path)
    pdf = _mono(workdir)
    target_layout.recognize_target_layout(workdir, {"mono_pdf": str(pdf)})

    fake_model.fail = "网络超时"
    payload = target_layout.recognize_target_layout(workdir, {"mono_pdf": str(pdf)})

    assert payload["status"] == target_layout.STATUS_FAILED
    assert not target_layout.provider_ir_path(workdir).exists()


@pytest.mark.usefixtures("fake_model")
def test_pdf_sha256_matches_the_recognised_file(tmp_path):
    import hashlib

    workdir = _workdir(tmp_path)
    pdf = _mono(workdir)

    payload = target_layout.recognize_target_layout(workdir, {"mono_pdf": str(pdf)})

    assert payload["pdf_sha256"] == hashlib.sha256(pdf.read_bytes()).hexdigest()


@pytest.mark.usefixtures("fake_model")
def test_ir_keeps_mineru_topleft_coordinates_verbatim(tmp_path):
    """坐标契约：IR 原样保留 MinerU 坐标，本模块不做任何换算。"""
    workdir = _workdir(tmp_path)
    pdf = _mono(workdir)

    target_layout.recognize_target_layout(workdir, {"mono_pdf": str(pdf)})

    ir = json.loads(target_layout.provider_ir_path(workdir).read_text())
    assert ir["pages"][0]["blocks"][0]["bbox"] == [10, 10, 100, 30]


# --------------------------------------------------------------------------- #
# 端到端接缝（离线：用真实识别结果的 fixture + 一张自造的译文 PDF）
# --------------------------------------------------------------------------- #
TARGET_FIXTURE = Path("tests/fixtures/target_layout_page3.json")


def _translated_pdf(path):
    """造一页「译文 PDF」：文字落在真实识别框的位置上（MinerU 坐标 y 向下）。"""
    blocks = json.loads(TARGET_FIXTURE.read_text(encoding="utf-8"))["blocks"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as doc:
        page = doc.new_page(width=595, height=794)
        for index, block in enumerate(blocks):
            x0, y0, _x1, y1 = block["bbox"]
            # 基线落在框底附近（pymupdf 的 y 原点也在左上，可直接用）。
            # 用 ASCII 标记：默认字体不能内嵌 CJK，标记文本本身不是被测对象。
            page.insert_text((x0, y1 - 2), f"BLOCK-{index} translated", fontsize=8)
        doc.save(path)
    return path


@pytest.fixture
def fixture_ir(fake_model):
    """把替身客户端的结果换成真实识别结果的 fixture（第 3 页前 5 个块）。"""
    blocks = json.loads(TARGET_FIXTURE.read_text(encoding="utf-8"))["blocks"]
    fake_model.ir = {"page_count": 1, "pages": [{"page_index": 0, "blocks": blocks}]}
    return blocks


@pytest.mark.usefixtures("fixture_ir")
def test_recognized_boxes_land_on_translated_text(tmp_path):
    """识别产物必须描述**译文 PDF 的版面**，不是源文档的。

    用真实识别结果的框坐标当 fixture：把译文文字插在那些框里，再按框取文字，
    应当取到对应的译文。若拿的是源侧几何（本任务之前的实际行为），同一位置取到的
    是错位的别段文字 —— 这正是「译文框显示原文框」的可观察形态。
    """
    workdir = _workdir(tmp_path)
    pdf = _translated_pdf(workdir / "output" / "out.mono.pdf")

    target_layout.recognize_target_layout(workdir, {"mono_pdf": str(pdf)})

    ir = json.loads(target_layout.provider_ir_path(workdir).read_text())
    blocks = list(ir["pages"][0]["blocks"])
    assert blocks, "识别产物没有 block"
    expected = json.loads(TARGET_FIXTURE.read_text(encoding="utf-8"))["blocks"]
    # 坐标原样保留（服务端/管线不做换算），框的位置就是真实识别出来的位置
    assert [b["bbox"] for b in blocks] == [b["bbox"] for b in expected]
    with pymupdf.open(pdf) as doc:
        page = doc[0]
        hits = 0
        for block in blocks:
            x0, y0, x1, y1 = block["bbox"]
            text = page.get_text("text", clip=pymupdf.Rect(x0, y0, x1, y1)).strip()
            if text.startswith("BLOCK-"):
                hits += 1
        assert hits == len(blocks), f"{hits}/{len(blocks)} 个框落在了译文文字上"


@pytest.mark.usefixtures("fixture_ir")
def test_target_ir_is_not_source_ir(tmp_path):
    """译文侧产物里不含源侧 block 的坐标：两者是两份不同的识别。"""
    workdir = _workdir(tmp_path)
    pdf = _translated_pdf(workdir / "output" / "out.mono.pdf")
    target_layout.recognize_target_layout(workdir, {"mono_pdf": str(pdf)})

    target_boxes = {
        tuple(block["bbox"])
        for page in json.loads(target_layout.provider_ir_path(workdir).read_text())["pages"]
        for block in page["blocks"]
    }
    fixture_boxes = {
        tuple(block["bbox"])
        for block in json.loads(TARGET_FIXTURE.read_text(encoding="utf-8"))["blocks"]
    }
    assert target_boxes == fixture_boxes
    # 源侧 fixture（tests/fixtures/mineru/layout_v275_s41586_excerpt.json）是另一份文档，
    # 两者的框不可能一致 —— 说明产物确实来自「对译文 PDF 的重新识别」这条链路
    source = json.loads(
        Path("tests/fixtures/mineru/layout_v275_s41586_excerpt.json").read_text()
    )
    source_boxes = {
        tuple(block["bbox"])
        for page in source.get("pdf_info", [])
        for block in page.get("para_blocks", [])
        if isinstance(block.get("bbox"), list)
    }
    assert source_boxes
    assert target_boxes & source_boxes == set()
