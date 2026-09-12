"""LaTeX bbox 排版验收度量：eligible 集、应用率、行填充分布、贴片文本层差异。

用法：
    python3 experiments/acceptance_latex.py <workdir> <mono_pdf> [--out-dir DIR]
        [--dual-pdf PDF] [--report JSON] [--fill-threshold 0.98]

只读输入产物，产出 ``<out_dir>/acceptance_latex.json`` 与 ``.md``（默认
``<workdir>/acceptance/``）：

- **eligible 集**（DONE 标准的分母）：正文本体标签 ∧ 已翻译且译文≠原文 ∧
  源行数 ≥2 ∧ 非旋转页 ∧ 不压水印；
- **应用率** = applied / eligible（applied 取自 ``latex_bbox_report.json``）；
- **applied 段非末行 fill 分布**：复用 ``overlay.measure_line_fills`` 从贴片
  文本层按行量测（口径与产品门禁一致）；
- **贴片文本层 vs 期望可见文本**：期望值取 overlay 报告 ``decisions[].expected_text``
  （译文去标记 + text/mineru/simple_math 片段原文；旧报告回退译文去标记）。
  逐段给出严格字符差 ``text_diff`` 与容差判定 ``text_layer_match``
  （NFKC + 同形字形折叠 + 去空白后「期望文本是抽取文本的子序列」，容忍断词
  连字符/额外字形但不容忍缺字）；零差异比率只统计非公式段
  （``mineru``/``simple_math``/``fragment`` 类段单独排除）；
- 耗时（报告里的编译秒数）与 PDF 体积。

设计约束：本脚本不重跑任何阶段、不写输入 workdir（除显式传入的 out_dir）。
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from babeldoc.format.pdf.document_il.backend.latex_bbox import (  # noqa: E402
    overlay as overlay_mod,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox import (
    renderer as renderer_mod,  # noqa: E402
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.fusion import (  # noqa: E402
    FORMULA_PLACEHOLDER,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.fusion import (  # noqa: E402
    GENERIC_TAG,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.fusion import (  # noqa: E402
    STYLE_CLOSE,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.fusion import (  # noqa: E402
    STYLE_OPEN,
)

#: DONE 标准里的正文本体标签（与 overlay._BODY_LABELS 同口径）。
BODY_LABELS = frozenset(
    {
        "text",
        "list",
        "figure_caption",
        "table_caption",
        "page_footnote",
        "table_footnote",
    }
)
#: 默认的非末行填充率达标阈值。
DEFAULT_FILL_THRESHOLD = 0.98


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def _read_jsonl(path: Path) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("id"):
            rows[obj["id"]] = obj
    return rows


def _first(pattern: str, workdir: Path) -> Path | None:
    """定位落在 ``<workdir>/`` 或 ``<workdir>/<pdf名>/`` 下的产物。"""
    direct = workdir / pattern
    if direct.is_file():
        return direct
    candidates = sorted(workdir.glob(f"*/{pattern}"))
    return candidates[0] if candidates else None


def _strip_markup(text: str) -> str:
    """去掉样式标记与 ``{vN}`` 占位符，得到「译文纯文本」。"""
    text = STYLE_OPEN.sub("", text or "")
    text = STYLE_CLOSE.sub("", text)
    text = GENERIC_TAG.sub("", text)
    return FORMULA_PLACEHOLDER.sub("", text)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _rect_from_box(box, page_height: float) -> pymupdf.Rect:
    """IL 源 box（y-up）→ MuPDF rect（y-down），与 overlay 贴片矩形同口径。"""
    x0, y0, x1, y1 = box
    return pymupdf.Rect(x0, page_height - y1, x1, page_height - y0)


def _char_diff(expected: str, extracted: str) -> int:
    """字符级差异计数（replace/delete/insert 的最长跨度之和）。"""
    if expected == extracted:
        return 0
    matcher = difflib.SequenceMatcher(a=expected, b=extracted, autojunk=False)
    return sum(
        max(i2 - i1, j2 - j1)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
        if tag != "equal"
    )


#: 「公式段」判定用的分类：包含这些类的段不参与零差异（位图/数学渲染）。
FORMULA_BEARING_CLASSES = frozenset({"mineru", "simple_math", "fragment"})
#: ``text`` 级公式就是文本，参与零差异判定。


def fuse_classes_for(pid, para, decisions: dict) -> list[str]:
    """段落的公式级分类（P2 起由 capture 写入 overlay 报告 decisions）。

    优先取 ``decisions[pid]["fuse_classes"]``；旧产物没有该字段时回退
    ``n_formula_chars`` 的粗判定（把「有公式字符」折算成非 text 类），
    保证历史产物仍可度量。
    """
    entry = decisions.get(pid) or {}
    classes = entry.get("fuse_classes")
    if classes:
        return list(classes)
    return ["simple_math"] if para.get("n_formula_chars") else []


def measure_workdir(
    workdir: Path,
    mono_pdf: Path,
    *,
    dual_pdf: Path | None = None,
    report_path: Path | None = None,
    fill_threshold: float = DEFAULT_FILL_THRESHOLD,
) -> dict:
    """对一篇回放产物做验收度量，返回可序列化的结果 dict。"""
    geometry_path = workdir / "agent" / "layout_geometry.json"
    geometry = _read_json(geometry_path) or {}
    report = _read_json(report_path) if report_path else None
    if report is None:
        found = _first("latex_bbox_report.json", workdir)
        report = _read_json(found) if found else None
    sheet = _read_jsonl(workdir / "agent" / "sheet.jsonl")
    translated = _read_jsonl(workdir / "agent" / "translated.jsonl")

    applied_ids = list((report or {}).get("applied_paragraphs") or [])
    decisions = {
        row.get("debug_id"): row for row in (report or {}).get("decisions") or []
    }
    doc = pymupdf.open(mono_pdf)
    try:
        rotated_pages = {page.number for page in doc if page.rotation != 0}
        page_heights = {page.number: page.rect.height for page in doc}
        paragraphs = geometry.get("paragraphs") or []

        eligible: list[dict] = []
        ineligible_reasons: dict[str, int] = {}
        for para in paragraphs:
            pid = para.get("id")
            label = para.get("layout_label")
            page_index = para.get("page")
            if label not in BODY_LABELS:
                ineligible_reasons["label"] = ineligible_reasons.get("label", 0) + 1
                continue
            target = (translated.get(pid) or {}).get("target")
            source = (sheet.get(pid) or {}).get("source")
            if not (target or "").strip():
                ineligible_reasons["untranslated"] = (
                    ineligible_reasons.get("untranslated", 0) + 1
                )
                continue
            if _normalize_text(target) == _normalize_text(source or ""):
                ineligible_reasons["target-equals-source"] = (
                    ineligible_reasons.get("target-equals-source", 0) + 1
                )
                continue
            if (para.get("n_lines") or 0) < 2:
                ineligible_reasons["single-line"] = (
                    ineligible_reasons.get("single-line", 0) + 1
                )
                continue
            zero_based_page = (page_index or 1) - 1
            if zero_based_page in rotated_pages:
                ineligible_reasons["rotated-page"] = (
                    ineligible_reasons.get("rotated-page", 0) + 1
                )
                continue

            box = para.get("src_box")
            rect = None
            if box and zero_based_page in page_heights:
                rect = _rect_from_box(box, page_heights[zero_based_page])
            metrics = (
                overlay_mod.measure_line_fills(doc[zero_based_page], rect)
                if rect is not None
                else {"n_lines": 0, "fills": [], "min_body_fill": None, "watermark": False}
            )
            if metrics["watermark"]:
                ineligible_reasons["watermark-overlap"] = (
                    ineligible_reasons.get("watermark-overlap", 0) + 1
                )
                continue

            eligible.append(
                {
                    "id": pid,
                    "page": zero_based_page,
                    "label": label,
                    # 注意：当前产物的 n_lines 是 Typesetting 之后的译文行数代理
                    # （layout_geometry 在回填后计算）；P3-0 源行几何落地后切换。
                    "line_basis": "rendered",
                    "n_lines_source": para.get("n_lines"),
                    "n_lines_stamped": metrics["n_lines"],
                    "src_box": box,
                    "rect": [rect.x0, rect.y0, rect.x1, rect.y1] if rect else None,
                    "fuse_classes": fuse_classes_for(pid, para, decisions),
                    "has_formula": bool(
                        set(fuse_classes_for(pid, para, decisions))
                        & FORMULA_BEARING_CLASSES
                    ),
                    "target_chars": len(_normalize_text(_strip_markup(target))),
                }
            )

        eligible_ids = {item["id"] for item in eligible}
        applied = [item for item in eligible if item["id"] in set(applied_ids)]
        applied_not_eligible = [pid for pid in applied_ids if pid not in eligible_ids]

        lines_total = 0
        lines_filled = 0
        paragraphs_all_ok = 0
        diffs: list[dict] = []
        non_formula_applied = 0
        non_formula_zero_diff = 0
        non_formula_strict = 0

        for item in applied:
            entry = decisions.get(item["id"]) or {}
            rect = pymupdf.Rect(item["rect"]) if item["rect"] else None
            fills = (
                overlay_mod.measure_line_fills(doc[item["page"]], rect)
                if rect is not None
                else {"fills": [], "min_body_fill": None, "n_lines": 0}
            )
            body_fills = fills["fills"][:-1] if len(fills["fills"]) > 1 else []
            lines_total += len(body_fills)
            lines_filled += sum(1 for value in body_fills if value >= fill_threshold)
            if body_fills and all(value >= fill_threshold for value in body_fills):
                paragraphs_all_ok += 1

            target = (translated.get(item["id"]) or {}).get("target") or ""
            # 期望可见文本：capture 写的 plain_text（译文去标记 + text/mineru/
            # simple_math 片段原文）；旧报告没有该字段时回退译文去标记。
            expected_source = entry.get("expected_text") or _strip_markup(target)
            expected = _normalize_text(expected_source)
            extracted = (
                _normalize_text(doc[item["page"]].get_text(clip=rect))
                if rect is not None
                else ""
            )
            text_diff = _char_diff(expected, extracted)
            text_ok = renderer_mod.text_layer_matches(expected_source, extracted)
            if not item["has_formula"]:
                non_formula_applied += 1
                if text_ok:
                    non_formula_zero_diff += 1
                if text_diff == 0:
                    non_formula_strict += 1
            diffs.append(
                {
                    "id": item["id"],
                    "page": item["page"],
                    "layout_label": item["label"],
                    "has_formula": item["has_formula"],
                    "fuse_classes": item["fuse_classes"],
                    "fill_before": entry.get("fill_before"),
                    "fill_after": entry.get("fill_after"),
                    "min_body_fill": fills["min_body_fill"],
                    "n_lines_stamped": fills["n_lines"],
                    "font_scale": entry.get("font_scale"),
                    "lead": entry.get("lead"),
                    "attempts": entry.get("attempts"),
                    "expected_chars": len(expected),
                    "extracted_chars": len(extracted),
                    "text_diff": text_diff,
                    "zero_diff": text_diff == 0,
                    "text_layer_match": text_ok,
                    "extracted_head": extracted[:40],
                }
            )

        mono_size = mono_pdf.stat().st_size if mono_pdf.is_file() else None
        dual_size = (
            dual_pdf.stat().st_size if dual_pdf and dual_pdf.is_file() else None
        )
        compile_info = (report or {}).get("compile") or {}
        reasons: dict[str, int] = {}
        for item in eligible:
            reason = (decisions.get(item["id"]) or {}).get("reason")
            if reason is None:
                reason = "applied" if item["id"] in set(applied_ids) else "unknown"
            reasons[reason] = reasons.get(reason, 0) + 1

        summary = {
            "workdir": str(workdir),
            "mono_pdf": str(mono_pdf),
            "dual_pdf": str(dual_pdf) if dual_pdf else None,
            "report": str(report_path) if report_path else None,
            "decisions_available": bool(decisions),
            "pages": len(doc),
            "fill_threshold": fill_threshold,
            "eligible": len(eligible),
            "applied": len(applied),
            "application_rate": (
                round(len(applied) / len(eligible), 4) if eligible else None
            ),
            "applied_not_eligible": applied_not_eligible,
            "ineligible_reasons": ineligible_reasons,
            "eligible_reasons": reasons,
            "lines_nonfinal_total": lines_total,
            "lines_nonfinal_ge_threshold": lines_filled,
            "lines_nonfinal_ge_threshold_ratio": (
                round(lines_filled / lines_total, 4) if lines_total else None
            ),
            "applied_paragraphs_all_nonfinal_ok": paragraphs_all_ok,
            "fusion_classes": (report or {}).get("fusion") or {},
            "applied_paragraphs_zero_diff_non_formula": non_formula_zero_diff,
            #: 严格字符级（text_diff==0）计数：断词连字符/同形字形会让它偏低，
            #: 只作参考；DONE 判定用上面的容差口径。
            "applied_paragraphs_strict_zero_diff_non_formula": non_formula_strict,
            "applied_paragraphs_non_formula": non_formula_applied,
            "text_diff_zero_ratio_non_formula": (
                round(non_formula_zero_diff / non_formula_applied, 4)
                if non_formula_applied
                else None
            ),
            "text_diff_max": max((row["text_diff"] for row in diffs), default=0),
            "compile": compile_info,
            "compile_seconds": compile_info.get("seconds"),
            "pdf_sizes": {"mono_bytes": mono_size, "dual_bytes": dual_size},
            "report_links": (report or {}).get("links"),
            "applied_paragraphs": diffs,
            "eligible_paragraphs": eligible,
        }
    finally:
        doc.close()
    return summary


def render_markdown(result: dict) -> str:
    """把度量结果渲染成便于目视的 Markdown 摘要。"""
    lines = [
        "# LaTeX bbox 验收度量",
        "",
        f"- workdir: `{result['workdir']}`",
        f"- mono PDF: `{result['mono_pdf']}`",
        f"- 页数: {result['pages']}",
        f"- report 含 decisions[]: {result['decisions_available']}",
        f"- eligible（DONE 分母）: **{result['eligible']}**",
        f"- applied: **{result['applied']}**（应用率 {result['application_rate']}）",
        f"- applied 非末行 fill ≥ {result['fill_threshold']} 的行占比: "
        f"**{result['lines_nonfinal_ge_threshold_ratio']}**"
        f"（{result['lines_nonfinal_ge_threshold']}/{result['lines_nonfinal_total']}）",
        f"- applied 段全部非末行达标: {result['applied_paragraphs_all_nonfinal_ok']}"
        f"/{result['applied']}",
        f"- 非公式 applied 段文本层完整（容差口径，期望文本是抽取文本的子序列）: "
        f"**{result['applied_paragraphs_zero_diff_non_formula']}"
        f"/{result['applied_paragraphs_non_formula']}**"
        f"（严格字符级 zero-diff："
        f"{result.get('applied_paragraphs_strict_zero_diff_non_formula')}"
        f"/{result['applied_paragraphs_non_formula']}，"
        f"max text_diff={result['text_diff_max']}：断词连字符与同形字形）",
        f"- 编译: attempts={result['compile'].get('attempts')} "
        f"seconds={result['compile_seconds']} "
        f"cache_hits={result['compile'].get('cache_hits')}",
        f"- 体积: {result['pdf_sizes']}",
        "",
        "## eligible 段的决策原因分布",
        "",
    ]
    for reason, count in sorted(
        result["eligible_reasons"].items(), key=lambda item: (-item[1], item[0])
    ):
        lines.append(f"- `{reason}`: {count}")
    lines += ["", "## applied 段明细", ""]
    lines.append(
        "| id | page | label | formula | fill_before | fill_after | 非末行 min fill | "
        "n_lines | font_scale | attempts | text_diff |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in result["applied_paragraphs"]:
        lines.append(
            f"| {row['id']} | {row['page'] + 1} | {row['layout_label']} | "
            f"{row['has_formula']} | {row['fill_before']} | {row['fill_after']} | "
            f"{row['min_body_fill']} | {row['n_lines_stamped']} | "
            f"{row['font_scale']} | {row['attempts']} | {row['text_diff']} |"
        )
    if result["applied_not_eligible"]:
        lines += [
            "",
            "## applied 但不在 eligible 集（需人工核查）",
            "",
            ", ".join(result["applied_not_eligible"]),
        ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workdir", type=Path, help="回放 workdir（含 agent/ 产物）")
    parser.add_argument("mono_pdf", type=Path, help="latex 模式的 mono PDF")
    parser.add_argument("--dual-pdf", type=Path, default=None, help="dual PDF（可选，仅记录体积）")
    parser.add_argument("--report", type=Path, default=None, help="latex_bbox_report.json 路径")
    parser.add_argument("--out-dir", type=Path, default=None, help="输出目录（默认 <workdir>/acceptance）")
    parser.add_argument(
        "--fill-threshold",
        type=float,
        default=DEFAULT_FILL_THRESHOLD,
        help="非末行填充率达标阈值（默认 0.98）",
    )
    args = parser.parse_args()

    if not args.workdir.is_dir():
        parser.error(f"workdir 不存在: {args.workdir}")
    if not args.mono_pdf.is_file():
        parser.error(f"mono PDF 不存在: {args.mono_pdf}")
    out_dir = args.out_dir or (args.workdir / "acceptance")
    out_dir.mkdir(parents=True, exist_ok=True)

    result = measure_workdir(
        args.workdir,
        args.mono_pdf,
        dual_pdf=args.dual_pdf,
        report_path=args.report,
        fill_threshold=args.fill_threshold,
    )
    json_path = out_dir / "acceptance_latex.json"
    md_path = out_dir / "acceptance_latex.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")

    print(
        f"eligible={result['eligible']} applied={result['applied']} "
        f"rate={result['application_rate']} "
        f"nonfinal_fill_ratio={result['lines_nonfinal_ge_threshold_ratio']} "
        f"zero_diff={result['applied_paragraphs_zero_diff_non_formula']}"
        f"/{result['applied_paragraphs_non_formula']} "
        f"max_text_diff={result['text_diff_max']} "
        f"compile_seconds={result['compile_seconds']}"
    )
    print(f"json: {json_path}")
    print(f"md:   {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
