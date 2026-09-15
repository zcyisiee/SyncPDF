import json

import pytest
from babeldoc.tools.agent.protocol import TranslationValidationError
from babeldoc.tools.agent.protocol import build_repair_feedback
from babeldoc.tools.agent.protocol import check_placeholders
from babeldoc.tools.agent.protocol import extract_input_items
from babeldoc.tools.agent.protocol import extract_placeholders
from babeldoc.tools.agent.protocol import validate_output

PROMPT_TEMPLATE = "You are a translator.\n\n## Here is the input:\n\n{payload}"

ITEMS = [
    {"id": 0, "input": "{v1}<style id='2'>hello</style>, world!", "layout_label": "text"},
    {"id": 1, "input": "Plain paragraph without placeholders.", "layout_label": "text"},
]


def _reply(entries):
    return json.dumps(entries, ensure_ascii=False)


def test_extract_placeholders_counts_multiset():
    text = "{v1} and {v1} plus <style id='3'>x</style> and <b4>y</b4>"
    counter = extract_placeholders(text)
    assert counter["{v1}"] == 2
    assert counter["<style id='3'>"] == 1
    assert counter["</style>"] == 1
    assert counter["<b4>"] == 1


def test_check_placeholders_lost_and_hallucinated():
    errors = check_placeholders(
        7, "{v1}<style id='1'>a</style>", "<style id='1'>甲</style>{v9}"
    )
    assert any("placeholder_lost" in e and "{v1}" in e for e in errors)
    assert any("placeholder_hallucinated" in e and "{v9}" in e for e in errors)


def test_validate_output_accepts_clean_reply():
    output = _reply(
        [
            {"id": 0, "output": "{v1}<style id='2'>你好</style>，世界！"},
            {"id": 1, "output": "没有占位符的普通段落。"},
        ]
    )
    assert validate_output(output, ITEMS) == []


def test_validate_output_accepts_fenced_json():
    output = (
        "```json\n"
        + _reply(
            [
                {"id": 0, "output": "{v1}<style id='2'>你好</style>，世界！"},
                {"id": 1, "output": "普通段落。"},
            ]
        )
        + "\n```"
    )
    assert validate_output(output, ITEMS) == []


def test_validate_output_detects_placeholder_violations():
    output = _reply(
        [
            {"id": 0, "output": "<style id='2'>你好</style>，世界！"},  # {v1} 丢失
            {"id": 1, "output": "普通段落。"},
        ]
    )
    errors = validate_output(output, ITEMS)
    assert any(e.startswith("placeholder_lost") and "{v1}" in e for e in errors)


def test_validate_output_detects_id_length_and_format_errors():
    assert any(
        e.startswith("length_mismatch")
        for e in validate_output(_reply([{"id": 0, "output": "a"}]), ITEMS)
    )
    assert any(
        e.startswith("id_mismatch")
        for e in validate_output(
            _reply([{"id": 0, "output": "a"}, {"id": 5, "output": "b"}]), ITEMS
        )
    )
    assert validate_output("这不是 JSON", ITEMS)[0].startswith("not_json")
    assert any(
        e.startswith("no_output")
        for e in validate_output(
            _reply([{"id": 0, "output": 123}, {"id": 1, "output": "b"}]), ITEMS
        )
    )


def test_extract_input_items_parses_prompt_tail():
    prompt = PROMPT_TEMPLATE.format(payload=_reply(ITEMS))
    assert extract_input_items(prompt) == ITEMS
    assert extract_input_items("no heading here") is None
    assert extract_input_items("## Here is the input:\n\n[]") is None


def test_build_repair_feedback_lists_errors_and_rules():
    feedback = build_repair_feedback(
        ["placeholder_lost: id 0 lost {v1} (x1)", "not_json: bad"]
    )
    assert "placeholder_lost" in feedback
    assert "{v1}" in feedback
    assert "preserve every placeholder" in feedback


def test_validation_error_is_importable_and_raisable():
    with pytest.raises(TranslationValidationError):
        raise TranslationValidationError("rejected")
