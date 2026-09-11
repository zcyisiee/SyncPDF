"""确定性质量检测：段内完整性 / 漏译 / 疑似合并 / 句末完整性。

与 ``review_report.py``（experiments 里的旧审查脚本）的区别：
- 不以 ``target == source`` 判"未翻译"（缩写行、代码行、参考文献会误报）；
  "回退原文"以 ``apply_report.fallback_ids`` 为准。
- 长度比按**文档自身中位比**归一化（en→zh 本身约 0.35，绝对值阈值必然误报）。

输出结构（供 review_document 汇总成 verdict）::

    {"warnings": [{"code": "intra_paragraph_truncated", "id": "P07-012",
                   "len_ratio": 0.157, "rel_ratio": 0.42, ...}],
     "blockers": [{"code": "empty_target", "ids": ["P10-005"]}],
     "metrics": {"rows": 209, "median_len_ratio": 0.378, ...}}
"""

from __future__ import annotations

import re
import statistics

ANCHOR_RE = re.compile(r"<style id='\d+'>|</style>|\{v\d+\}")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
ASCII_WORD_RE = re.compile(r"[A-Za-z]{2,}")
# 公式占位符把"单词"从中切断（源文被切碎的强信号）
SPLICE_RE = re.compile(r"[A-Za-z]{2,}\{v\d+\}[A-Za-z]{2,}")
# 源文句末：字母/右括号后跟 .!?（排除 "and,."、"{v1}"、"(2023)" 等）
SENT_END_RE = re.compile(r"[A-Za-z)\]][.!?][\"'”’)\]]*$")
TARGET_SENT_END_RE = re.compile(r"[。！？…][\"'”’)\]]*$")
REFERENCE_LIKE_RE = re.compile(
    r"proceedings of|copyright|permission to make digital|all rights reserved|"
    r"https?://|doi\.org|\bdoi\b|arxiv:|\bisbn\b|@[a-z0-9.-]+\.[a-z]{2,}|"
    r"\.\s*(19|20)\d{2}\.\s",
    re.IGNORECASE,
)

THRESHOLDS = {
    "truncated_rel_ratio": 0.50,  # 相对文档中位比的长度比
    "truncated_min_src": 40,  # 源文短于此不判截断
    "merge_rel_ratio": 1.60,  # 相对中位比 > 1.6 → 疑似吞并邻段
    "low_cjk_ratio": 0.15,
    "low_cjk_min_src": 40,
    "low_cjk_anchor_density": 0.30,
}


def plain(text: str | None) -> str:
    """去锚点/占位符后的自然语言文本。"""
    return ANCHOR_RE.sub("", text or "")


def anchor_density(canonical: str) -> float:
    total = len(canonical or "")
    if not total:
        return 0.0
    return 1.0 - len(plain(canonical)) / total


def cjk_ratio(text: str) -> float:
    text = plain(text)
    if not text:
        return 0.0
    return len(CJK_RE.findall(text)) / len(text)


def ascii_ratio(text: str) -> float:
    text = plain(text)
    if not text:
        return 0.0
    ascii_count = sum(1 for ch in text if ord(ch) < 128)
    return ascii_count / len(text)


def _is_reference_like(source_plain: str) -> bool:
    return bool(REFERENCE_LIKE_RE.search(source_plain))


def check_document(
    rows: list[dict],
    targets: dict[str, str],
    missing_ids: list[str] | None = None,
    fallback_ids: list[str] | None = None,
    thresholds: dict | None = None,
) -> dict:
    """确定性检查。

    Args:
        rows: sheet 行 ``[{"id", "source", ...}]``（source 为 canonical 形式）
        targets: ``{id: target}``（canonical 形式，来自 translated.jsonl）
        missing_ids: apply 报告里"译文中缺失、已回退原文"的 id
        fallback_ids: 同上（``apply_report.fallback_ids``）
    """
    limits = dict(THRESHOLDS)
    if thresholds:
        limits.update(thresholds)
    missing_ids = list(missing_ids or [])
    fallback_ids = list(fallback_ids or [])

    ratios: dict[str, float] = {}
    src_lens: dict[str, int] = {}
    meta: dict[str, dict] = {}
    for row in rows:
        pid = row["id"]
        source = row.get("source")
        src_plain = plain(source).strip()
        src_lens[pid] = len(src_plain)
        meta[pid] = {
            "label": row.get("layout_label") or "text",
            "page": row.get("page"),
            "source_plain": src_plain,
            "anchor_density": anchor_density(source or ""),
        }
        if pid in targets and src_plain:
            tgt_plain = plain(targets[pid]).strip()
            ratios[pid] = len(tgt_plain) / len(src_plain)

    values = sorted(ratios.values())
    median_ratio = statistics.median(values) if values else 0.0

    blockers: list[dict] = []
    warnings: list[dict] = []

    # ---- 缺行 / 空译 / 回退原文 -------------------------------------------- #
    empty_ids = [
        pid
        for pid in targets
        if not plain(targets[pid]).strip()
        and meta.get(pid, {}).get("source_plain", "")
    ]
    if missing_ids:
        blockers.append({"code": "missing_ids", "ids": missing_ids})
    if empty_ids:
        blockers.append(
            {
                "code": "empty_target",
                "ids": empty_ids,
                "evidence": {
                    pid: {
                        "source": meta.get(pid, {}).get("source_plain", "")[:60],
                        "target": targets.get(pid, ""),
                    }
                    for pid in empty_ids[:5]
                },
            }
        )
    if fallback_ids:
        warnings.append(
            {
                "code": "fallback_to_source",
                "sev": "P1",
                "ids": fallback_ids,
                "hint": "这些段落未拿到译文，已回退原文渲染（内容未丢失，但未翻译）",
            }
        )

    # 模型偶尔把文件头注释（<!-- babeldoc-markdown v1 -->）抄进最后一段；
    # md-apply 会程序化剔除，这里兜底拦截残留（否则会渲染进 PDF）。
    comment_leaks = [
        pid for pid, target in targets.items() if "<!--" in (target or "")
    ]
    if comment_leaks:
        blockers.append(
            {
                "code": "markdown_comment_leak",
                "ids": comment_leaks,
                "hint": "译文里残留 HTML 注释（多为文件头被抄写）：重新 apply 即可程序化剔除",
            }
        )

    # ---- 段内截断 / 疑似吞并 ---------------------------------------------- #
    truncated = []
    for pid, ratio in ratios.items():
        if src_lens.get(pid, 0) < limits["truncated_min_src"]:
            continue
        rel = ratio / median_ratio if median_ratio else 0.0
        if rel < limits["truncated_rel_ratio"]:
            truncated.append(
                {
                    "code": "intra_paragraph_truncated",
                    "sev": "P1",
                    "id": pid,
                    "page": meta[pid]["page"],
                    "label": meta[pid]["label"],
                    "len_ratio": round(ratio, 3),
                    "rel_ratio": round(rel, 3),
                    "source_len": src_lens[pid],
                    "source_head": meta[pid]["source_plain"][:80],
                    "source_tail": meta[pid]["source_plain"][-60:],
                    "target_tail": plain(targets[pid]).strip()[-40:],
                    "hint": "疑似段内截断（相对全文中位长度比过小）：请对照 source_tail 复核",
                }
            )
    truncated.sort(key=lambda item: item["rel_ratio"])
    warnings.extend(truncated)

    if missing_ids or empty_ids:
        suspects = []
        # 只在缺失段落所在页（含邻近一页）找"异常变长"的段落，避免参考文献
        # 行（原文保留 → 归一化比必然偏大）造成大量误报。
        anchor_pages = {
            meta[pid]["page"]
            for pid in (missing_ids + empty_ids)
            if pid in meta and meta[pid].get("page") is not None
        }
        for pid, ratio in ratios.items():
            if src_lens.get(pid, 0) < limits["truncated_min_src"]:
                continue
            page = meta[pid].get("page")
            if anchor_pages and (
                page is None or not any(abs(page - p) <= 1 for p in anchor_pages)
            ):
                continue
            if cjk_ratio(targets[pid]) < limits["low_cjk_ratio"]:
                continue
            rel = ratio / median_ratio if median_ratio else 0.0
            if rel > limits["merge_rel_ratio"]:
                suspects.append(
                    {
                        "code": "suspect_merge",
                        "sev": "P2",
                        "id": pid,
                        "page": meta[pid]["page"],
                        "len_ratio": round(ratio, 3),
                        "rel_ratio": round(rel, 3),
                        "missing_ids": missing_ids or empty_ids,
                        "hint": "该段长度异常偏大且同时存在缺失/空译段落：疑似把邻段并入了本段",
                    }
                )
        suspects.sort(key=lambda item: -item["rel_ratio"])
        warnings.extend(suspects[:10])

    # ---- 低中文占比（疑似未翻译） ------------------------------------------ #
    low_cjk = []
    for pid, target in targets.items():
        if pid not in meta:
            continue
        src_plain = meta[pid]["source_plain"]
        tgt_plain = plain(target).strip()
        if len(src_plain) < limits["low_cjk_min_src"] or not tgt_plain:
            continue
        if meta[pid]["anchor_density"] >= limits["low_cjk_anchor_density"]:
            continue
        ratio = cjk_ratio(target)
        if ratio >= limits["low_cjk_ratio"]:
            continue
        if ascii_ratio(target) >= 0.85 and _is_reference_like(src_plain):
            continue  # 参考文献/版权/DOI 行：保留原文是有意的
        low_cjk.append(
            {
                "code": "low_cjk",
                "sev": "P1",
                "id": pid,
                "page": meta[pid]["page"],
                "label": meta[pid]["label"],
                "cjk_ratio": round(ratio, 3),
                "source_len": len(src_plain),
                "target": tgt_plain[:60],
                "hint": "中文占比过低：确认是否术语密集行，否则重译",
            }
        )
    low_cjk.sort(key=lambda item: item["cjk_ratio"])
    warnings.extend(low_cjk)

    # ---- 句末完整性 -------------------------------------------------------- #
    sentence_end = []
    for pid, target in targets.items():
        if pid not in meta:
            continue
        src_plain = meta[pid]["source_plain"]
        tgt_plain = plain(target).strip()
        if len(src_plain) < limits["truncated_min_src"] or not tgt_plain:
            continue
        if not SENT_END_RE.search(src_plain):
            continue
        if _is_reference_like(src_plain):
            continue  # 参考文献/版权行保留原文时的英文句末属正常
        if TARGET_SENT_END_RE.search(tgt_plain):
            continue
        sentence_end.append(
            {
                "code": "sentence_end_mismatch",
                "sev": "P1",
                "id": pid,
                "page": meta[pid]["page"],
                "label": meta[pid]["label"],
                "source_tail": src_plain[-40:],
                "target_tail": tgt_plain[-40:],
                "hint": "源文以句末标点结束而译文没有：疑似译文被截断",
            }
        )
    warnings.extend(sentence_end)

    # ---- 公式占位符拼接（源文被切碎） -------------------------------------- #
    # 公式被抽成 {vN} 后，原文文本流会在占位符两侧直接相连（"varying{v9}influences"），
    # 这类段落的"不完整感"是解析产物，不是翻译错误 —— 单独标出来避免误判为重译。
    spliced = []
    for pid, row in ((row["id"], row) for row in rows):
        source = row.get("source") or ""
        matches = SPLICE_RE.findall(source)
        if matches:
            spliced.append(
                {
                    "code": "formula_splice",
                    "sev": "P2",
                    "id": pid,
                    "page": row.get("page"),
                    "count": len(matches),
                    "samples": [
                        re.sub(r"\{v\d+\}", "⟨公式⟩", sample) for sample in matches[:3]
                    ],
                    "hint": "公式占位符把英文单词从中切断（如 varying⟨公式⟩influences）："
                    "该段源文本身已被切碎，译文不完整属预期；"
                    "要根治需在 IL 层合并同一公式的多个 fragment 并补回词间空格",
                }
            )
    spliced.sort(key=lambda item: -item["count"])
    warnings.extend(spliced[:20])

    metrics = {
        "rows": len(rows),
        "checked_targets": len(targets),
        "median_len_ratio": round(median_ratio, 3),
        "empty_targets": len(empty_ids),
        "truncated": len(truncated),
        "low_cjk": len(low_cjk),
        "sentence_end_mismatch": len(sentence_end),
        "formula_splice": len(spliced),
    }
    return {"blockers": blockers, "warnings": warnings, "metrics": metrics}
