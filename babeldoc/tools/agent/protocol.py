"""翻译协议校验：约束 subagent 译文的占位符与结构完整性。

BabelDOC 的翻译单元 ``paragraph.unicode`` 内嵌占位符协议：
- ``{vN}``            公式占位符（还原为公式图形）
- ``<style id='N'>``/``</style>``  富文本样式边界

subagent 译文若丢失/篡改/幻觉这些标记，重建 PDF 时会破坏公式与样式。
本模块提供纯函数校验（不依赖任何 LLM API），供 ``apply-translations``
工具在写回 IR 前把关，并生成可打回给 subagent 的修复反馈文本。

同时兼容批量 JSON 协议（ILTranslatorLLMOnly 的 "## Here is the input:"
prompt 形态），用于离线回放历史产物、测量协议违规率。
"""

import json
import re
from collections import Counter

INPUT_HEADING = "## Here is the input:"

PLACEHOLDER_PATTERNS = [
    re.compile(r"\{v\d+\}"),
    re.compile(r"<style\s+id='\d+'>"),
    re.compile(r"</style>"),
    re.compile(r"<b\d+>"),
    re.compile(r"</b\d+>"),
]

MAX_ERRORS_IN_FEEDBACK = 12
MAX_ERROR_ITEM_LEN = 200


class TranslationValidationError(Exception):
    """译文未通过协议校验（供工具层拒绝写回时使用）。"""


def extract_placeholders(text: str) -> Counter:
    """提取文本中的占位符/样式标签多重集。"""
    counter: Counter = Counter()
    for pattern in PLACEHOLDER_PATTERNS:
        counter.update(pattern.findall(text))
    return counter


def strip_json_wrappers(text: str) -> str:
    """剥掉 LLM 回复常见的 ```json / <json> 包裹（与管道 _clean_json_output 一致）。"""
    text = text.strip()
    if text.startswith("<json>"):
        text = text[6:]
    if text.endswith("</json>"):
        text = text[:-7]
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def extract_input_items(prompt: str) -> list[dict] | None:
    """从批量协议 prompt 末尾提取输入 JSON 数组；无法提取返回 None。"""
    pos = prompt.rfind(INPUT_HEADING)
    if pos == -1:
        return None
    tail = prompt[pos + len(INPUT_HEADING) :].strip()
    try:
        parsed = json.loads(strip_json_wrappers(tail))
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list) or not parsed:
        return None
    if not all(
        isinstance(item, dict) and "id" in item and "input" in item
        for item in parsed
    ):
        return None
    return parsed


def check_placeholders(item_id, input_text: str, output_text: str) -> list[str]:
    """比较单个翻译单元的占位符多重集，返回违规清单。"""
    errors = []
    expected = extract_placeholders(input_text)
    actual = extract_placeholders(output_text)
    for token, count in (expected - actual).items():
        errors.append(f"placeholder_lost: id {item_id} lost {token} (x{count})")
    for token, count in (actual - expected).items():
        errors.append(
            f"placeholder_hallucinated: id {item_id} has unexpected {token} (x{count})"
        )
    return errors


def validate_output(output: str, items: list[dict]) -> list[str]:
    """校验译文集合与输入条目（[{id, input, ...}]）的结构一致性。

    返回违规清单；空列表表示通过。检查项：
    1. 合法 JSON 数组（容忍围栏包裹、单对象回退）；
    2. 长度与 id 集合对齐，每个元素有字符串 "output"；
    3. 每个元素占位符多重集与输入一致。
    """
    errors = []
    if not isinstance(output, str):
        return [f"not_json: output is {type(output).__name__}, not text"]

    cleaned = strip_json_wrappers(output)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as e:
        preview = cleaned[:MAX_ERROR_ITEM_LEN]
        return [f"not_json: {e.msg} at pos {e.pos}, output starts with: {preview!r}"]

    if isinstance(parsed, dict) and "output" in parsed:
        parsed = [parsed]
    if not isinstance(parsed, list):
        return [f"not_array: expected JSON array, got {type(parsed).__name__}"]

    if len(parsed) != len(items):
        errors.append(
            f"length_mismatch: expected {len(items)} items, got {len(parsed)}"
        )

    input_ids = [item["id"] for item in items]
    output_ids = []
    for idx, entry in enumerate(parsed):
        if not isinstance(entry, dict) or "id" not in entry:
            errors.append(f"id_mismatch: entry {idx} has no 'id' field")
            continue
        output_ids.append(entry["id"])

    if sorted(output_ids, key=str) != sorted(input_ids, key=str):
        missing = set(map(str, input_ids)) - set(map(str, output_ids))
        extra = set(map(str, output_ids)) - set(map(str, input_ids))
        errors.append(
            f"id_mismatch: missing ids {sorted(missing)}, unexpected ids {sorted(extra)}"
        )
        return errors

    entries_by_id = {entry["id"]: entry for entry in parsed}
    for item in items:
        entry = entries_by_id.get(item["id"])
        if entry is None:
            continue
        translated = entry.get("output")
        if not isinstance(translated, str):
            errors.append(f"no_output: id {item['id']} has no string 'output' field")
            continue
        errors.extend(check_placeholders(item["id"], item["input"], translated))
    return errors


def build_repair_feedback(errors: list[str]) -> str:
    """把违规清单转成打回给 subagent 的修复反馈文本。"""
    shown = "\n".join(
        f"- {error[:MAX_ERROR_ITEM_LEN]}"
        for error in errors[:MAX_ERRORS_IN_FEEDBACK]
    )
    return (
        "Your translation was REJECTED because it violated the output "
        "protocol:\n"
        f"{shown}\n"
        "Fix ALL issues and resubmit: keep the same ids, one string "
        '"output" per id, and preserve every placeholder ({v1}, '
        "<style id='1'>, </style>, etc.) exactly as in the source text."
    )
