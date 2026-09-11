"""排版几何 dump / lint / locate。

数据源：
- ``layout_geometry.json``：``reconstruct`` 在 Typesetting 之后 dump 的 IR 几何
  （每个段落的源框/渲染框/最终缩放/字号/文本片段），id 与 ``layout_overrides.json``
  一一对应，是排微调的定位真源。
- 输出 PDF（可选，pymupdf）：文本层兼容字计数、超链接矩形对齐（P2 级）。

lint 阈值（默认值，集中在 ``THRESHOLDS``）：
- ``out_of_page``        渲染框越出页面 > 1pt
- ``paragraph_overlap``  两段渲染框 IoU > 0.15，或单向包含 > 30%
- ``font_shrink``        渲染色号 / 源字号 < 0.85
- ``figure_overlap``     与 figure/table*/formula 布局区重叠 > 30%
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

GEOMETRY_FILE = "layout_geometry.json"

THRESHOLDS = {
    "out_of_page_pt": 1.0,
    "overlap_iou": 0.15,
    "overlap_containment": 0.30,
    "font_shrink_ratio": 0.85,
    "figure_overlap_ratio": 0.30,
}
SKIP_LABEL_PREFIXES = ("figure", "table")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
COMPAT_IDEOGRAPH_RE = re.compile(r"[\uf900-\ufaff]")
ANCHOR_STRIP_RE = re.compile(r"<style id='\d+'>|</style>|\{v\d+\}")


# --------------------------------------------------------------------------- #
# 几何采集
# --------------------------------------------------------------------------- #
def _box_to_list(box) -> list[float] | None:
    if box is None:
        return None
    try:
        return [
            round(float(box.x), 3),
            round(float(box.y), 3),
            round(float(box.x2), 3),
            round(float(box.y2), 3),
        ]
    except (TypeError, ValueError):
        return None


def capture_source_state(
    doc, overrides: dict | None = None, state: dict | None = None
) -> dict:
    """采集 "Typesetting 之前" 的参照值。

    - 第一次调用（``state=None``）在**应用覆盖之前**：记录原始字号 ``src_font_size``
      与将施加的 ``font_scale``（排版 lint 的 "字号塔缩" 基准就是原字号）。
    - 第二次调用（传入 ``state``）在**应用覆盖之后**：只刷新 ``src_box``
      （它是 Typesetting 的输入框，含 box/box_scale 覆盖）。
    """
    from babeldoc.tools.agent import layout_overrides

    boxes_only = state is not None
    state = state if state is not None else {}
    for page in doc.page:
        page_number = page.page_number + 1
        page_scale = layout_overrides.page_font_scale(overrides, page_number) or 1.0
        for paragraph in page.pdf_paragraph:
            if paragraph.box is None:
                continue
            patch = layout_overrides.paragraph_override(overrides, paragraph.debug_id)
            key = paragraph.debug_id or _synthetic_id(page_number, paragraph)
            entry = state.get(key) or {}
            entry["src_box"] = _box_to_list(paragraph.box)
            if not boxes_only:
                entry["src_font_size"] = (
                    round(float(paragraph.pdf_style.font_size), 3)
                    if paragraph.pdf_style and paragraph.pdf_style.font_size
                    else None
                )
                entry["font_scale"] = round(
                    page_scale * float(patch.get("font_scale") or 1.0), 4
                )
            state[key] = entry
    return state


def _synthetic_id(page_number: int, paragraph) -> str:
    """跳过翻译的段落没有 debug_id，用 ``P01-S003`` 占位（不可被覆盖命中）。"""
    return f"P{page_number:02d}-S{abs(id(paragraph)) % 100000:05d}"


def _composition_chars(paragraph):
    """→ (char, kind)，kind ∈ {text, formula}。"""
    for composition in paragraph.pdf_paragraph_composition or []:
        if composition is None:
            continue
        if composition.pdf_line:
            for char in composition.pdf_line.pdf_character or []:
                yield char, "text"
        if composition.pdf_character:
            yield composition.pdf_character, "text"
        if composition.pdf_same_style_characters:
            for char in composition.pdf_same_style_characters.pdf_character or []:
                yield char, "text"
        if composition.pdf_formula:
            for char in composition.pdf_formula.pdf_character or []:
                yield char, "formula"


def _count_lines(rows: set[float], font_size: float | None) -> int | None:
    """把 y 值聚成行：行内可能带上下标（同"行"有多个基线）。"""
    if not rows:
        return None
    gap = 0.6 * (font_size or 10.0)
    ordered = sorted(rows, reverse=True)
    lines = 1
    previous = ordered[0]
    for value in ordered[1:]:
        if previous - value > gap:
            lines += 1
        previous = value
    return lines


def _mode(values: list[float]) -> float | None:
    if not values:
        return None
    counts: dict[float, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return max(counts.items(), key=lambda item: (item[1], item[0]))[0]


def _unicode_len(paragraph) -> int:
    text = paragraph.unicode
    if not text:
        for composition in paragraph.pdf_paragraph_composition or []:
            if composition and composition.pdf_same_style_unicode_characters:
                text = (text or "") + (
                    composition.pdf_same_style_unicode_characters.unicode or ""
                )
    return len(ANCHOR_STRIP_RE.sub("", text or ""))


def build_geometry(
    doc, source_state: dict | None = None, overrides: dict | None = None
) -> dict:
    """Typesetting **之后**调用：汇总渲染框/字号 → geometry dict。"""
    source_state = source_state or {}
    paragraphs: list[dict] = []
    pages_meta: list[dict] = []
    for page in doc.page:
        page_number = page.page_number + 1
        crop = getattr(page, "cropbox", None)
        crop_box = crop.box if crop else None
        layout_regions = []
        for region in page.page_layout or []:
            name = (region.class_name or "").lower()
            if name.startswith(("figure", "table", "formula")):
                layout_regions.append({"label": name, "box": _box_to_list(region.box)})
        pages_meta.append(
            {
                "page": page_number,
                "cropbox": _box_to_list(crop_box),
                "layout_regions": layout_regions,
            }
        )
        for paragraph in page.pdf_paragraph:
            if paragraph.box is None and not paragraph.pdf_paragraph_composition:
                continue
            key = paragraph.debug_id or _synthetic_id(page_number, paragraph)
            src = source_state.get(key) or {}
            chars = [
                (char, kind)
                for char, kind in _composition_chars(paragraph)
                if char is not None and _box_to_list(char.box)
            ]
            rendered = None
            font_sizes: list[float] = []
            formula_chars = 0
            text_rows: set[float] = set()
            for char, kind in chars:
                box = char.box
                visual = getattr(char, "visual_bbox", None)
                if visual is not None and visual.box is not None:
                    box = visual.box
                values = _box_to_list(box)
                rendered = _union(rendered, values)
                size = char.pdf_style.font_size if char.pdf_style else None
                if kind == "formula":
                    # 公式字符保留原字号，不参与段级字号统计
                    formula_chars += 1
                else:
                    if size:
                        font_sizes.append(round(float(size), 3))
                    # 行数用 em 框底边（同一行恒定），不能用视觉框（逐个字形不同）
                    em_box = char.box
                    if em_box is not None and em_box.y is not None:
                        text_rows.add(round(float(em_box.y), 2))
            text = ANCHOR_STRIP_RE.sub("", paragraph.unicode or "")
            paragraphs.append(
                {
                    "id": key,
                    "overridable": bool(paragraph.debug_id),
                    "page": page_number,
                    "layout_label": paragraph.layout_label or "text",
                    "src_box": src.get("src_box") or _box_to_list(paragraph.box),
                    "layout_box": _box_to_list(paragraph.box),
                    "rendered_box": rendered,
                    "scale": _round(paragraph.scale),
                    "optimal_scale": _round(paragraph.optimal_scale),
                    "font_scale": src.get("font_scale", 1.0),
                    "src_font_size": src.get("src_font_size"),
                    "min_font_size": min(font_sizes) if font_sizes else None,
                    "max_font_size": max(font_sizes) if font_sizes else None,
                    "mode_font_size": _mode(font_sizes),
                    "n_chars": len(chars),
                    "n_formula_chars": formula_chars,
                    "n_lines": _count_lines(text_rows, _mode(font_sizes)),
                    "n_unicode": _unicode_len(paragraph),
                    "text": text[:60],
                }
            )
    return {
        "version": 1,
        "pages": len(doc.page),
        "overrides": _overrides_digest(overrides),
        "page_info": pages_meta,
        "paragraphs": _annotate_space(paragraphs),
    }


def _annotate_space(paragraphs: list[dict]) -> list[dict]:
    """给每个段落算 ``space_below_pt``（同列下方到下一个障碍的净空）。

    决策用：净空大 → 可以用 ``box_scale`` 放宽框让 Typesetting 少缩字号；
    净空小 → 只能降 ``scale_cap``/字号或强制换行。
    """
    by_page: dict[int, list[dict]] = {}
    for para in paragraphs:
        by_page.setdefault(para["page"], []).append(para)
    for items in by_page.values():
        for para in items:
            box = para.get("layout_box") or para.get("rendered_box")
            if not box:
                continue
            limit = 0.0
            for other in items:
                if other is para:
                    continue
                other_box = other.get("layout_box") or other.get("rendered_box")
                if not other_box:
                    continue
                if other_box[3] > box[1] + 0.5:  # 只看下方（顶部低于本框底部）
                    continue
                x_overlap = min(box[2], other_box[2]) - max(box[0], other_box[0])
                if x_overlap <= 1:
                    continue
                limit = max(limit, other_box[3])
            para["space_below_pt"] = round(max(0.0, box[1] - limit), 2)
    return paragraphs


def _overrides_digest(overrides: dict | None) -> dict:
    if not overrides:
        return {"paragraphs": {}, "pages": {}}
    return {
        "paragraphs": overrides.get("paragraphs") or {},
        "pages": overrides.get("pages") or {},
    }


def _round(value) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def _union(a, b):
    if a is None:
        return list(b) if b else None
    if b is None:
        return list(a)
    return [
        min(a[0], b[0]),
        min(a[1], b[1]),
        max(a[2], b[2]),
        max(a[3], b[3]),
    ]


def geometry_path(workdir) -> Path:
    return Path(workdir) / "agent" / GEOMETRY_FILE


def write_geometry(workdir, geometry: dict) -> Path:
    path = geometry_path(workdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(geometry, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    return path


def load_geometry(workdir) -> dict:
    path = geometry_path(workdir)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 几何工具
# --------------------------------------------------------------------------- #
def box_area(box) -> float:
    if not box:
        return 0.0
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def box_intersection(a, b) -> float:
    if not a or not b:
        return 0.0
    dx = min(a[2], b[2]) - max(a[0], b[0])
    dy = min(a[3], b[3]) - max(a[1], b[1])
    if dx <= 0 or dy <= 0:
        return 0.0
    return dx * dy


def box_iou(a, b) -> float:
    inter = box_intersection(a, b)
    if not inter:
        return 0.0
    union = box_area(a) + box_area(b) - inter
    return inter / union if union > 0 else 0.0


def containment(a, b) -> float:
    """a 被 b 覆盖的比例（a 的面积占比）。"""
    area = box_area(a)
    if area <= 0:
        return 0.0
    return box_intersection(a, b) / area


# --------------------------------------------------------------------------- #
# lint
# --------------------------------------------------------------------------- #
def lint_geometry(geometry: dict, pdf_path=None) -> dict:
    if not geometry or not geometry.get("paragraphs"):
        return {
            "findings": [
                {
                    "code": "geometry_missing",
                    "sev": "P0",
                    "message": "layout_geometry.json 不存在或为空，请先 reconstruct",
                }
            ],
            "counts": {"geometry_missing": 1},
            "metrics": {},
        }

    thresholds = THRESHOLDS
    findings: list[dict] = []
    page_info = {p["page"]: p for p in geometry.get("page_info") or []}
    by_page: dict[int, list[dict]] = {}
    for para in geometry["paragraphs"]:
        by_page.setdefault(para["page"], []).append(para)

    for page, paragraphs in sorted(by_page.items()):
        crop = (page_info.get(page) or {}).get("cropbox")
        for para in paragraphs:
            rendered = para.get("rendered_box") or para.get("layout_box")
            box = para.get("layout_box") or para.get("rendered_box")
            # 1) 越界
            if rendered and crop:
                over = _overflow(rendered, crop)
                if over > thresholds["out_of_page_pt"]:
                    findings.append(
                        {
                            "code": "out_of_page",
                            "sev": "P0",
                            "id": para["id"],
                            "page": page,
                            "evidence": {
                                "rendered_box": rendered,
                                "mediabox": crop,
                                "overflow_pt": round(over, 2),
                            },
                            "hint": "scale_cap 下调，或 box_scale 收窄，或 force_break 分行",
                        }
                    )
            # 2) 字号塌缩（用众数字号：段内混排小字号/上下标不算塌缩）
            src = para.get("src_font_size")
            rendered_font = para.get("mode_font_size") or para.get("min_font_size")
            if src and rendered_font:
                ratio = rendered_font / src
                if ratio < thresholds["font_shrink_ratio"]:
                    findings.append(
                        {
                            "code": "font_shrink",
                            "sev": "P1",
                            "id": para["id"],
                            "page": page,
                            "evidence": {
                                "src_font_size": src,
                                "mode_font_size": rendered_font,
                                "min_font_size": para.get("min_font_size"),
                                "max_font_size": para.get("max_font_size"),
                                "ratio": round(ratio, 3),
                                "font_scale": para.get("font_scale"),
                                "optimal_scale": para.get("optimal_scale"),
                            },
                            "hint": "若为主动 font_scale 收缩可忽略；否则提高 scale_cap 或放宽 box",
                        }
                    )
            # 3) 与图/表/公式区重叠（原文已在该区域内 → 不算新缺陷）
            if rendered and not _is_caption(para.get("layout_label")):
                for region in (page_info.get(page) or {}).get("layout_regions") or []:
                    if not region.get("box") or _is_caption(region.get("label")):
                        continue
                    if containment(para.get("src_box"), region["box"]) > thresholds[
                        "figure_overlap_ratio"
                    ]:
                        continue
                    ratio = containment(rendered, region["box"])
                    if ratio > thresholds["figure_overlap_ratio"]:
                        findings.append(
                            {
                                "code": "figure_overlap",
                                "sev": "P1",
                                "id": para["id"],
                                "page": page,
                                "layout_label": para.get("layout_label"),
                                "evidence": {
                                    "rendered_box": rendered,
                                    "src_box": para.get("src_box"),
                                    "region": region["box"],
                                    "region_label": region["label"],
                                    "overlap_ratio": round(ratio, 3),
                                },
                                "hint": "段落压到图/表区域：scale_cap 下调或收窄 box",
                            }
                        )

        # 4) 段落相互重叠
        for i, first in enumerate(paragraphs):
            box_a = first.get("rendered_box") or first.get("layout_box")
            if not box_a:
                continue
            for second in paragraphs[i + 1 :]:
                box_b = second.get("rendered_box") or second.get("layout_box")
                if not box_b:
                    continue
                if _skip_pair(first, second):
                    continue
                # 面积悬殊（如小上下标落在整段大框内）不算重叠缺陷
                area_a, area_b = box_area(box_a), box_area(box_b)
                if min(area_a, area_b) / max(area_a, area_b, 1e-6) < 0.2:
                    continue
                iou = box_iou(box_a, box_b)
                contain = max(containment(box_a, box_b), containment(box_b, box_a))
                if (
                    iou > thresholds["overlap_iou"]
                    or contain > thresholds["overlap_containment"]
                ):
                    findings.append(
                        {
                            "code": "paragraph_overlap",
                            "sev": "P1",
                            "ids": [first["id"], second["id"]],
                            "page": page,
                            "labels": [
                                first.get("layout_label"),
                                second.get("layout_label"),
                            ],
                            "evidence": {
                                "iou": round(iou, 3),
                                "containment": round(contain, 3),
                                "boxes": [box_a, box_b],
                            },
                            "hint": "下调其中一段的 scale_cap / box_scale，或错开 box",
                        }
                    )

    metrics: dict = {"paragraphs": len(geometry["paragraphs"]), "pages": geometry.get("pages")}
    if pdf_path:
        findings.extend(_pdf_findings(pdf_path, metrics))

    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding["code"]] = counts.get(finding["code"], 0) + 1
    findings.sort(key=lambda f: (f.get("page") or 0, f["code"]))
    return {"findings": findings, "counts": counts, "metrics": metrics}


def _is_caption(label: str | None) -> bool:
    label = (label or "").lower()
    return label.endswith(("_caption", "_footnote", "_text"))


def _skip_pair(first: dict, second: dict) -> bool:
    """表格/图内部段落彼此重叠属正常（原文版式即如此），不报。"""
    labels = [
        (first.get("layout_label") or "").lower(),
        (second.get("layout_label") or "").lower(),
    ]
    if all(label.startswith(SKIP_LABEL_PREFIXES) for label in labels):
        return True
    # 两段都不可覆盖（跳过翻译的原文段）→ 属原文版式
    return not first.get("overridable") and not second.get("overridable")


def _overflow(rendered, crop) -> float:
    return max(
        0.0,
        crop[0] - rendered[0],
        crop[1] - rendered[1],
        rendered[2] - crop[2],
        rendered[3] - crop[3],
    )


def _pdf_findings(pdf_path, metrics: dict) -> list[dict]:
    """P2：文本层兼容字 + 超链接矩形对齐。"""
    try:
        import pymupdf
    except ImportError:  # pragma: no cover
        return []

    findings: list[dict] = []
    doc = pymupdf.open(pdf_path)
    compat_total = 0
    link_total = 0
    misaligned_total = 0
    for index in range(len(doc)):
        page = doc[index]
        text = page.get_text()
        compat = COMPAT_IDEOGRAPH_RE.findall(text)
        if compat:
            compat_total += len(compat)
            findings.append(
                {
                    "code": "text_layer_compat_ideograph",
                    "sev": "P2",
                    "page": index + 1,
                    "evidence": {
                        "count": len(compat),
                        "samples": [
                            {
                                "compat": ch,
                                "canonical": unicodedata.normalize("NFKC", ch),
                            }
                            for ch in dict.fromkeys(compat)
                        ][:10],
                    },
                    "hint": "字体缺该字形时回退到 CJK 兼容表意文字：视觉正确，"
                    "文本层复制/检索会得到兼容码位（可用 NFKC 归一化还原）",
                }
            )
        blocks = [pymupdf.Rect(block[:4]) for block in page.get_text("blocks")]
        misaligned = []
        for link in page.get_links():
            link_total += 1
            rect = link.get("from")
            if rect is None:
                continue
            if not any(rect.intersects(block) for block in blocks):
                misaligned.append(
                    [
                        round(rect.x0, 1),
                        round(rect.y0, 1),
                        round(rect.x1, 1),
                        round(rect.y1, 1),
                    ]
                )
        if misaligned:
            misaligned_total += len(misaligned)
            findings.append(
                {
                    "code": "link_misaligned",
                    "sev": "P2",
                    "page": index + 1,
                    "evidence": {"count": len(misaligned), "rects": misaligned[:10]},
                    "hint": "链接矩形下方没有文本块：多为译文重排后引用位置漂移",
                }
            )
    doc.close()
    metrics["compat_ideographs"] = compat_total
    metrics["links"] = link_total
    metrics["links_misaligned"] = misaligned_total
    return findings


# --------------------------------------------------------------------------- #
# locate
# --------------------------------------------------------------------------- #
def locate(
    geometry: dict,
    page: int | None = None,
    box=None,
    text: str | None = None,
    limit: int = 8,
) -> list[dict]:
    """按 box（IoU 排序）或 text（子串命中）定位段落 id。"""
    candidates = []
    for para in geometry.get("paragraphs") or []:
        if page is not None and para.get("page") != page:
            continue
        if box is not None:
            rendered = para.get("rendered_box") or para.get("layout_box")
            score = box_iou(box, rendered) if rendered else 0.0
            if score <= 0:
                continue
            candidates.append(
                {
                    "id": para["id"],
                    "page": para["page"],
                    "layout_label": para.get("layout_label"),
                    "score": round(score, 4),
                    "rendered_box": rendered,
                    "layout_box": para.get("layout_box"),
                    "overridable": para.get("overridable"),
                    "text": para.get("text"),
                }
            )
        elif text:
            haystack = para.get("text") or ""
            if text in haystack:
                candidates.append(
                    {
                        "id": para["id"],
                        "page": para["page"],
                        "layout_label": para.get("layout_label"),
                        "score": 1.0,
                        "rendered_box": para.get("rendered_box"),
                        "layout_box": para.get("layout_box"),
                        "overridable": para.get("overridable"),
                        "text": haystack,
                    }
                )
        else:
            candidates.append(
                {
                    "id": para["id"],
                    "page": para["page"],
                    "layout_label": para.get("layout_label"),
                    "score": 0.0,
                    "rendered_box": para.get("rendered_box"),
                    "layout_box": para.get("layout_box"),
                    "overridable": para.get("overridable"),
                    "text": para.get("text"),
                }
            )
    candidates.sort(key=lambda c: (-c["score"], c["page"], c["id"]))
    return candidates[:limit]
