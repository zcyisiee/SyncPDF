"""A1 回归：``PdfCharacter.advance`` 必须与 ``box`` 同处 device space。

``AWLTChar.adv`` 是 **text-space** 宽度（``textwidth * fontsize * scaling``），而
``PdfCharacter.box`` 是 **device-space** 坐标（``Tm × CTM`` 已作用）。当 PDF 把
缩放烘进 ``Tm``（``Tf 1`` 型，如 ``11.12728 0 0 10.9091 Tm``）时两者单位差一个
数量级，``layout_helper._has_word_gap`` 的
``next.box.x - (prev.box.x + prev.advance)`` 虚高 → 每对字符间都假阳性插空格
（``S i n g l e``）。本文件固化「advance ∈ device space」这一 IL 约定。

IL 约定证据：``typesetting.py`` 重建字符时 ``advance * scale`` 与 ``box`` 宽
``width * scale`` 同乘；同文件对纯 unicode 单元用 ``advance=char_width``。

两个 frontend（active / legacy）都必须遵守该约定。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from babeldoc.format.pdf.awlt_char import AWLTChar
from babeldoc.format.pdf.document_il.frontend.il_creater import ILCreater
from babeldoc.format.pdf.document_il.frontend.il_creater_active import ActiveILCreater
from babeldoc.format.pdf.document_il.il_version_1 import Page
from babeldoc.format.pdf.document_il.utils.layout_helper import get_char_unicode_string
from babeldoc.format.pdf.new_parser.bridge_types import GraphicStateSnapshot
from babeldoc.format.pdf.parse_shared import _ParseOnlyDocLayoutModel
from babeldoc.format.pdf.translation_config import TranslationConfig
from babeldoc.progress_monitor import ProgressMonitor

CREATOR_CLASSES = [ActiveILCreater, ILCreater]

# 测试.pdf 的排版参数：Tf 1 + 缩放烘进 Tm（a = 11.12728）。
TF_ONE_MATRIX = (11.12728, 0.0, 0.0, 10.9091, 0.0, 0.0)
TF_ONE_FONT_SIZE = 1.0
TF_ONE_TEXT_WIDTH = 0.568  # → device 宽 0.568 × 11.12728 = 6.32

# 2512.08296v3.pdf 的排版参数：正常字号 + Tm a = 1.02。
NORMAL_MATRIX = (1.02, 0.0, 0.0, 1.0, 0.0, 0.0)
NORMAL_FONT_SIZE = 10.9091
NORMAL_TEXT_WIDTH = 0.528  # → device 宽 0.528 × 10.9091 × 1.02 = 5.875


def _stub_font(*, vertical: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        fontname="XCharter-Roman",
        is_vertical=lambda: vertical,
        get_descent=lambda: -0.2,
    )


def _awlt_char(
    matrix: tuple[float, float, float, float, float, float],
    *,
    font_size: float,
    text_width: float,
    text: str,
    vertical: bool = False,
) -> AWLTChar:
    """构造真实 ``AWLTChar``（advance 的唯一生产者）。"""
    char = AWLTChar(
        matrix,
        _stub_font(vertical=vertical),
        font_size,
        1.0,
        0.0,
        text,
        text_width,
        (None, 0.0) if vertical else 0.0,
        None,
        GraphicStateSnapshot(),
        0,
        "f1",
        0,
    )
    char.cid = 1
    char.render_mode = 0
    char.clip_paths = None
    return char


def _project(creator_cls, chars: list[AWLTChar], tmp_path) -> list:
    config = TranslationConfig(
        input_file="fixture.pdf",
        working_dir=tmp_path,
        doc_layout_model=_ParseOnlyDocLayoutModel(),
        progress_monitor=ProgressMonitor([(creator_cls.stage_name, 1.0)]),
    )
    creator = creator_cls(config)
    creator.on_page_start()
    for char in chars:
        creator.on_lt_char(char)
    assert isinstance(creator.current_page, Page)
    return creator.current_page.pdf_character


def _shifted(
    matrix: tuple[float, float, float, float, float, float],
    x: float,
) -> tuple[float, float, float, float, float, float]:
    return (matrix[0], matrix[1], matrix[2], matrix[3], x, matrix[5])


@pytest.mark.parametrize("creator_cls", CREATOR_CLASSES)
def test_tf_one_advance_is_scaled_to_device_space(creator_cls, tmp_path):
    """Tf 1 型 PDF：advance 换算后等于 box 宽（6.32），而非 text-space 的 0.568。"""
    chars = _project(
        creator_cls,
        [
            _awlt_char(
                _shifted(TF_ONE_MATRIX, 100.0),
                font_size=TF_ONE_FONT_SIZE,
                text_width=TF_ONE_TEXT_WIDTH,
                text="S",
            ),
        ],
        tmp_path,
    )

    assert len(chars) == 1
    char = chars[0]
    assert char.box.x2 - char.box.x == pytest.approx(6.32, abs=1e-3)
    assert char.advance == pytest.approx(6.32, abs=1e-3)


@pytest.mark.parametrize("creator_cls", CREATOR_CLASSES)
def test_normal_font_size_advance_matches_box_width(creator_cls, tmp_path):
    """正常字号 PDF：换算后 advance 仍等于 box 宽（+2%），不在空间之间跳跃。"""
    chars = _project(
        creator_cls,
        [
            _awlt_char(
                _shifted(NORMAL_MATRIX, 100.0),
                font_size=NORMAL_FONT_SIZE,
                text_width=NORMAL_TEXT_WIDTH,
                text="S",
            ),
        ],
        tmp_path,
    )

    char = chars[0]
    assert char.box.x2 - char.box.x == pytest.approx(5.875, abs=1e-3)
    assert char.advance == pytest.approx(5.875, abs=1e-3)


@pytest.mark.parametrize("creator_cls", CREATOR_CLASSES)
def test_tf_one_chars_get_no_false_word_gap(creator_cls, tmp_path):
    """端到端症状：紧排的两字符不再被插空格（修复前是 ``S T``）。"""
    chars = _project(
        creator_cls,
        [
            _awlt_char(
                _shifted(TF_ONE_MATRIX, 100.0),
                font_size=TF_ONE_FONT_SIZE,
                text_width=TF_ONE_TEXT_WIDTH,
                text="S",
            ),
            _awlt_char(
                _shifted(TF_ONE_MATRIX, 106.32),
                font_size=TF_ONE_FONT_SIZE,
                text_width=TF_ONE_TEXT_WIDTH,
                text="T",
            ),
        ],
        tmp_path,
    )

    assert get_char_unicode_string(chars) == "ST"


@pytest.mark.parametrize("creator_cls", CREATOR_CLASSES)
def test_missing_advance_keeps_original_value(creator_cls, tmp_path):
    """缺宽度（Type3/CID 等）时 advance 保持原值 0，不被换算成非零。"""
    chars = _project(
        creator_cls,
        [
            _awlt_char(
                _shifted(TF_ONE_MATRIX, 100.0),
                font_size=TF_ONE_FONT_SIZE,
                text_width=0.0,
                text="S",
            ),
        ],
        tmp_path,
    )

    assert chars[0].advance == 0


@pytest.mark.parametrize("creator_cls", CREATOR_CLASSES)
def test_degenerate_matrix_keeps_original_advance(creator_cls, tmp_path):
    """无缩放信息的退化矩阵不缩放 advance（保持原值，不臆造 0）。"""
    chars = _project(
        creator_cls,
        [
            _awlt_char(
                _shifted((0.0, 0.0, 1.0, 1.0, 0.0, 0.0), 100.0),
                font_size=NORMAL_FONT_SIZE,
                text_width=NORMAL_TEXT_WIDTH,
                text="S",
            ),
        ],
        tmp_path,
    )

    assert chars[0].advance == pytest.approx(NORMAL_FONT_SIZE * NORMAL_TEXT_WIDTH)


@pytest.mark.parametrize("creator_cls", CREATOR_CLASSES)
def test_vertical_advance_uses_writing_direction_scale(creator_cls, tmp_path):
    """竖排字符的 advance 沿书写方向缩放（矩阵第二列长度），同样是 device space。"""
    chars = _project(
        creator_cls,
        [
            _awlt_char(
                (0.0, 11.12728, -11.12728, 0.0, 100.0, 500.0),
                font_size=TF_ONE_FONT_SIZE,
                text_width=TF_ONE_TEXT_WIDTH,
                text="A",
                vertical=True,
            ),
        ],
        tmp_path,
    )

    char = chars[0]
    assert char.vertical is True
    assert char.advance == pytest.approx(6.32, abs=1e-3)
