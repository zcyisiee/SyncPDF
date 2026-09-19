"""段落级排版覆盖：`<workdir>/agent/layout_overrides.json`。

设计：
- 覆盖文件是排版微调的**唯一真源**，`reconstruct` 读取后注入 Typesetting/IR，
  不写 `state.pkl`，因此可回滚（删 key / 删文件 / `restore` 快照均可）。
- 语义（全部为可选 key，缺省 = 不改变现有行为）：
  * ``scale_cap``   缩放**上限**：``optimal_scale := min(optimal_scale, cap)``。
    Typesetting 只会从 1.0 往下递减，因此 cap 只降不升（放大请用 box / font_scale）。
  * ``font_scale``  字号乘数：作用于该段 composition 内所有 ``pdf_style.font_size``
    与段级 ``pdf_style``。字形变小但排版框/字距不变（"视觉减重"杠杆）。
  * ``line_skip``   覆盖该段行距系数（默认 CJK 1.50 / 其它 1.3）。
  * ``box_scale``   布局框等比扩缩，**锚定左上角**向右侧/下侧生长（文本生长方向）。
 * ``box``         显式指定 ``[x, y, x2, y2]``（PDF 坐标，y 向上），优先于 box_scale。
 * ``bold`` / ``italic`` / ``serif``  渲染样式三态覆盖（布尔；缺省 = 跟随源文派生值）。
   由 serve 局部编译（``render_request``）消费；一次性全量编译路径暂不应用。
 * ``force_break_after_text`` / ``force_break_after_offset``
    在渲染文本的指定位置强制换行（前者子串锚定，抗文本改动；后者按字符偏移精确兜底）。
- 页级 ``pages[<n>].font_scale`` 作用于该页所有段落，段落级 font_scale 再叠乘。
"""

from __future__ import annotations

import copy
import datetime
import json
import re
from pathlib import Path

OVERRIDES_FILE = "layout_overrides.json"
VERSION = 1

PARAGRAPH_FLOAT_KEYS = {
    "scale_cap": (0.1, 5.0),
    "font_scale": (0.2, 5.0),
    "line_skip": (0.8, 3.0),
    "box_scale": (0.3, 5.0),
}
PARAGRAPH_LIST_KEYS = ("force_break_after_text", "force_break_after_offset")
#: 渲染样式布尔键（serve 局部编译消费；True/False 覆盖，缺省跟随源文）。
PARAGRAPH_BOOL_KEYS = ("bold", "italic", "serif")
PAGE_FLOAT_KEYS = {"font_scale": (0.2, 5.0)}
HISTORY_LIMIT = 200
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


# --------------------------------------------------------------------------- #
# 读写
# --------------------------------------------------------------------------- #
def empty_overrides() -> dict:
    return {"version": VERSION, "paragraphs": {}, "pages": {}, "history": []}


def overrides_path(workdir) -> Path:
    return Path(workdir) / "agent" / OVERRIDES_FILE


def normalize(data: dict | None) -> dict:
    """补齐结构；不改内容（校验交给 validate）。"""
    out = empty_overrides()
    if not isinstance(data, dict):
        return out
    if isinstance(data.get("version"), int):
        out["version"] = data["version"]
    for key in ("paragraphs", "pages"):
        value = data.get(key)
        if isinstance(value, dict):
            out[key] = copy.deepcopy(value)
    history = data.get("history")
    if isinstance(history, list):
        out["history"] = copy.deepcopy(history)[-HISTORY_LIMIT:]
    return out


def load_overrides(workdir) -> dict:
    """读取覆盖文件；缺失/损坏时返回空覆盖（并保留 raw_error 供诊断）。"""
    path = overrides_path(workdir)
    if not path.exists():
        return empty_overrides()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        out = empty_overrides()
        out["raw_error"] = f"{path}: JSON 解析失败: {exc}"
        return out
    return normalize(data)


def save_overrides(workdir, data: dict) -> Path:
    path = overrides_path(workdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = normalize(data)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


# --------------------------------------------------------------------------- #
# 校验与 patch
# --------------------------------------------------------------------------- #
def validate(data: dict, allow_none: bool = False) -> list[str]:
    """返回错误清单（空 = 合法）。

    ``allow_none=True`` 用于**待应用的 patch**：值为 ``null`` 表示删除该字段。
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["overrides 必须是 JSON 对象"]
    unknown = set(data) - {"version", "paragraphs", "pages", "history", "raw_error"}
    if unknown:
        errors.append(f"未知顶层字段: {sorted(unknown)}")
    if "version" in data and data["version"] != VERSION:
        errors.append(f"version 必须为 {VERSION}，得到 {data['version']!r}")

    paragraphs = data.get("paragraphs", {})
    if not isinstance(paragraphs, dict):
        errors.append("paragraphs 必须是对象")
        paragraphs = {}
    for pid, patch in paragraphs.items():
        if patch is None and allow_none:
            continue  # 整段删除
        if not _ID_RE.match(str(pid)):
            errors.append(f"paragraphs.{pid}: 非法的段落 id")
        if not isinstance(patch, dict):
            errors.append(f"paragraphs.{pid}: 必须是对象")
            continue
        for key, value in patch.items():
            if value is None and allow_none:
                continue
            where = f"paragraphs.{pid}.{key}"
            if key in PARAGRAPH_FLOAT_KEYS:
                low, high = PARAGRAPH_FLOAT_KEYS[key]
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    errors.append(f"{where}: 必须是数字")
                elif not (low <= float(value) <= high):
                    errors.append(f"{where}: 超出范围 [{low}, {high}]：{value}")
            elif key == "box":
                errors.extend(_validate_box(where, value))
            elif key in PARAGRAPH_BOOL_KEYS:
                if not isinstance(value, bool):
                    errors.append(f"{where}: 必须是布尔值")
            elif key == "force_break_after_text":
                if (
                    not isinstance(value, list)
                    or not value
                    or not all(isinstance(x, str) and x for x in value)
                ):
                    errors.append(f"{where}: 必须是非空字符串数组")
            elif key == "force_break_after_offset":
                if (
                    not isinstance(value, list)
                    or not value
                    or not all(
                        isinstance(x, int) and not isinstance(x, bool) and x >= 0
                        for x in value
                    )
                ):
                    errors.append(f"{where}: 必须是非负整数数组")
            else:
                errors.append(f"{where}: 未知字段")

    pages = data.get("pages", {})
    if not isinstance(pages, dict):
        errors.append("pages 必须是对象")
        pages = {}
    for key, patch in pages.items():
        if patch is None and allow_none:
            continue  # 整页删除
        if not str(key).isdigit():
            errors.append(f"pages.{key}: 页码必须是正整数（1-based）")
            continue
        if not isinstance(patch, dict):
            errors.append(f"pages.{key}: 必须是对象")
            continue
        for field, value in patch.items():
            if value is None and allow_none:
                continue
            where = f"pages.{key}.{field}"
            if field in PAGE_FLOAT_KEYS:
                low, high = PAGE_FLOAT_KEYS[field]
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    errors.append(f"{where}: 必须是数字")
                elif not (low <= float(value) <= high):
                    errors.append(f"{where}: 超出范围 [{low}, {high}]：{value}")
            else:
                errors.append(f"{where}: 未知字段")
    return errors


def _validate_box(where: str, value) -> list[str]:
    if not isinstance(value, list) or len(value) != 4:
        return [f"{where}: 必须是 [x, y, x2, y2] 四元数组"]
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
        return [f"{where}: 坐标必须是数字"]
    x, y, x2, y2 = (float(v) for v in value)
    if x2 <= x:
        return [f"{where}: 需要 x2 > x（得到 {value}）"]
    if y2 <= y:
        return [f"{where}: 需要 y2 > y（box.y2 是框顶部，y 是底部）"]
    return []


def merge_patch(current: dict, patch: dict) -> dict:
    """把 patch 合并进 current（段落/页级按 key 覆盖，None 表示删除该 key）。"""
    merged = normalize(current)
    merged.pop("raw_error", None)
    for section in ("paragraphs", "pages"):
        incoming = patch.get(section) or {}
        if not isinstance(incoming, dict):
            continue
        for key, fields in incoming.items():
            key = str(key)
            if fields is None:
                merged[section].pop(key, None)
                continue
            bucket = merged[section].setdefault(key, {})
            for field, value in (fields or {}).items():
                if value is None:
                    bucket.pop(field, None)
                else:
                    bucket[field] = value
            if not bucket:
                merged[section].pop(key, None)
    return merged


def diff_overrides(before: dict, after: dict) -> dict:
    """→ {"changed": {pid: {key: [old, new]}}, "added": [...], "removed": [...]}"""
    changed: dict[str, dict] = {}
    added: list[str] = []
    removed: list[str] = []
    for section in ("paragraphs", "pages"):
        old, new = before.get(section) or {}, after.get(section) or {}
        for key in sorted(set(old) | set(new), key=str):
            if key not in old:
                added.append(f"{section}.{key}")
                continue
            if key not in new:
                removed.append(f"{section}.{key}")
                continue
            fields = {}
            for field in sorted(set(old[key]) | set(new[key])):
                o, n = old[key].get(field), new[key].get(field)
                if o != n:
                    fields[field] = [o, n]
            if fields:
                changed[f"{section}.{key}"] = fields
    return {"changed": changed, "added": added, "removed": removed}


def apply_patch(workdir, patch: dict, reason: str | None = None) -> dict:
    """把 patch 写入覆盖文件；返回 {ok, overrides, diff, path, errors}。

    ``null`` 作为字段值表示删除该字段（便于单键回滚）。
    """
    patch = patch or {}
    errors = validate(normalize(patch), allow_none=True)
    if errors:
        return {"ok": False, "errors": errors, "patch": patch}
    current = load_overrides(workdir)
    merged = merge_patch(current, patch)
    errors = validate(merged)
    if errors:
        return {"ok": False, "errors": errors, "patch": patch}
    diff = diff_overrides(current, merged)
    history = merged.get("history") or []
    history.append(
        {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "reason": reason or "",
            "patch": copy.deepcopy(patch),
        }
    )
    merged["history"] = history[-HISTORY_LIMIT:]
    path = save_overrides(workdir, merged)
    return {
        "ok": True,
        "path": str(path),
        "diff": diff,
        "overrides": normalize(merged),
    }


def clear_overrides(workdir, reason: str | None = None) -> dict:
    """清空所有覆盖（保留 history 一条 clear 记录）。"""
    current = load_overrides(workdir)
    merged = empty_overrides()
    history = current.get("history") or []
    history.append(
        {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "reason": reason or "clear",
            "patch": {"__clear__": True},
        }
    )
    merged["history"] = history[-HISTORY_LIMIT:]
    path = save_overrides(workdir, merged)
    return {"ok": True, "path": str(path), "overrides": merged}


# --------------------------------------------------------------------------- #
# 读取侧访问器
# --------------------------------------------------------------------------- #
def paragraph_override(overrides: dict | None, debug_id: str | None) -> dict:
    if not overrides or not debug_id:
        return {}
    return (overrides.get("paragraphs") or {}).get(debug_id) or {}


def page_font_scale(overrides: dict | None, page_number: int) -> float | None:
    if not overrides:
        return None
    patch = (overrides.get("pages") or {}).get(str(page_number)) or {}
    value = patch.get("font_scale")
    return float(value) if value else None


def line_skip_override(overrides: dict | None, debug_id: str | None) -> float | None:
    value = paragraph_override(overrides, debug_id).get("line_skip")
    return float(value) if value else None


def style_flag(overrides: dict | None, debug_id: str | None, key: str) -> bool | None:
    """段落样式布尔覆盖（``bold``/``italic``/``serif``）；没有覆盖 → None。"""
    if key not in PARAGRAPH_BOOL_KEYS:
        raise ValueError(f"未知样式键：{key!r}")
    value = paragraph_override(overrides, debug_id).get(key)
    return value if isinstance(value, bool) else None


def to_config_hook(overrides: dict | None) -> dict:
    """Typesetting 用的注入结构（TranslationConfig.paragraph_layout_overrides）。"""
    return copy.deepcopy((overrides or {}).get("paragraphs") or {})


# --------------------------------------------------------------------------- #
# 应用到 IR
# --------------------------------------------------------------------------- #
def scale_box(box, factor: float):
    """锚定左上角（x, y2）等比扩缩：向右、向下生长。"""
    from babeldoc.format.pdf.document_il import il_version_1

    width = (box.x2 - box.x) * factor
    height = (box.y2 - box.y) * factor
    return il_version_1.Box(x=box.x, y=box.y2 - height, x2=box.x + width, y2=box.y2)


def _scale_style_field(obj, attr: str, factor: float, memo: dict[int, object]) -> None:
    """把 ``obj.<attr>`` 指向的 PdfStyle 字号乘 factor。

    copy-on-write + 按原对象去重：同一 style 对象（段落级与 composition 常见
    共享同一实例）只乘一次，但会把结果回填到每个持有者，不影响未覆盖段落。
    """
    style = getattr(obj, attr, None)
    if style is None:
        return
    key = id(style)
    scaled = memo.get(key)
    if scaled is None:
        scaled = copy.copy(style)
        if scaled.font_size:
            scaled.font_size = scaled.font_size * factor
        memo[key] = scaled
    if scaled is not style:
        setattr(obj, attr, scaled)


def _scale_composition(composition, factor: float, memo: dict[int, object]) -> None:
    if composition is None:
        return
    if composition.pdf_line:
        for char in composition.pdf_line.pdf_character or []:
            _scale_style_field(char, "pdf_style", factor, memo)
    if composition.pdf_character:
        _scale_style_field(composition.pdf_character, "pdf_style", factor, memo)
    if composition.pdf_same_style_characters:
        for char in composition.pdf_same_style_characters.pdf_character or []:
            _scale_style_field(char, "pdf_style", factor, memo)
    if composition.pdf_same_style_unicode_characters:
        _scale_style_field(
            composition.pdf_same_style_unicode_characters, "pdf_style", factor, memo
        )
    if composition.pdf_formula:
        for char in composition.pdf_formula.pdf_character or []:
            _scale_style_field(char, "pdf_style", factor, memo)


def apply_to_ir(doc, overrides: dict | None) -> dict:
    """把 box / font_scale 写进 IR（在 Typesetting 之前调用）。

    只改 ``pdf_style.font_size``（字形）与 ``paragraph.box``（布局框），
    不动 ``page_layout``/图形。返回统计 dict 供报告使用。
    """
    if not overrides:
        return {"paragraphs_touched": 0, "font_scaled": 0, "box_changed": 0, "clamped": 0}
    stats = {
        "paragraphs_touched": 0,
        "font_scaled": 0,
        "box_changed": 0,
        "clamped": 0,
        "unmatched_ids": [],
        "warnings": [],
    }
    seen: set[str] = set()
    for page in doc.page:
        page_number = page.page_number + 1
        page_scale = page_font_scale(overrides, page_number)
        crop = getattr(page, "cropbox", None)
        crop_box = crop.box if crop else None
        for paragraph in page.pdf_paragraph:
            patch = paragraph_override(overrides, paragraph.debug_id)
            if not paragraph.debug_id:
                continue
            seen.add(paragraph.debug_id)
            factor = 1.0
            if page_scale:
                factor *= page_scale
            para_scale = patch.get("font_scale")
            if para_scale:
                factor *= float(para_scale)
            touched = False
            if factor != 1.0:
                # 段内去重：composition 的 style 常常就是 paragraph.pdf_style
                # 本体（post_translate_paragraph 会回填），靠 memo 避免重复乘算。
                memo: dict[int, object] = {}
                _scale_style_field(paragraph, "pdf_style", factor, memo)
                for composition in paragraph.pdf_paragraph_composition or []:
                    _scale_composition(composition, factor, memo)
                stats["font_scaled"] += 1
                touched = True

            if paragraph.box is not None and (
                patch.get("box") or patch.get("box_scale")
            ):
                from babeldoc.format.pdf.document_il import il_version_1

                if patch.get("box"):
                    x, y, x2, y2 = (float(v) for v in patch["box"])
                    new_box = il_version_1.Box(x=x, y=y, x2=x2, y2=y2)
                else:
                    new_box = scale_box(paragraph.box, float(patch["box_scale"]))
                if crop_box is not None:
                    clamped_box = il_version_1.Box(
                        x=max(new_box.x, crop_box.x),
                        y=max(new_box.y, crop_box.y),
                        x2=min(new_box.x2, crop_box.x2),
                        y2=min(new_box.y2, crop_box.y2),
                    )
                    if (
                        clamped_box.x != new_box.x
                        or clamped_box.y != new_box.y
                        or clamped_box.x2 != new_box.x2
                        or clamped_box.y2 != new_box.y2
                    ):
                        stats["clamped"] += 1
                        stats["warnings"].append(
                            f"{paragraph.debug_id}: box 超出页面范围，已裁剪到 cropbox"
                        )
                    new_box = clamped_box
                if new_box.x2 <= new_box.x or new_box.y2 <= new_box.y:
                    stats["warnings"].append(
                        f"{paragraph.debug_id}: box 裁剪后无效，已忽略"
                    )
                else:
                    paragraph.box = new_box
                    stats["box_changed"] += 1
                    touched = True
            if touched:
                stats["paragraphs_touched"] += 1
    for pid in (overrides.get("paragraphs") or {}):
        if pid not in seen:
            stats["unmatched_ids"].append(pid)
    if stats["unmatched_ids"]:
        stats["warnings"].append(
            f"覆盖中的段落 id 未在文档中找到: {stats['unmatched_ids']}"
        )
    return stats
