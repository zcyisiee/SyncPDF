"""工具链门禁聚合：把各阶段落盘的审计产物收敛成一份机读 JSON。

用法：
    python experiments/toolchain_gates.py <workdir> [--pdf <源pdf>] [--json]

设计：
- 只读 `<workdir>` 下已落盘的审计产物，不重跑任何阶段；
- 每个门禁独立判定 ``pass`` / ``fail`` / ``not_available`` / ``not_reconstructed``；
- **硬门禁**（hard=true）失败 → 退出码 1 且 ``"ok": false``；软门禁只给警告；
- 产物缺失时降级为 ``not_available``（不算失败），方便「只解析还没重建」的中间态。

产物来源（全部由前序阶段写出，路径见 docs/reference/pipeline.md）：

======================  ==========================================================
门禁                    数据源
======================  ==========================================================
``layout_coverage``     ``<workdir>/<pdf名>/layout_coverage.json``（LayoutParser）
``link_integrity``      ``<workdir>/agent/{reconstruct_report.json,source/links.json}``
``toc_integrity``       ``<workdir>/agent/source/{toc.json,bookmarks.json}`` + anchors.json
``protected_tokens``    ``<workdir>/agent/{anchors.json,source/mineru/alignment.json}``
``protocol``            ``<workdir>/agent/apply_report.json``
======================  ==========================================================
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PLACEHOLDER_RE = re.compile(r"\{v\d+\}")
STYLE_RE = re.compile(r"<style\b[^>]*>|</style>|\{v\d+\}")


# --------------------------------------------------------------------------- #
# 读取助手
# --------------------------------------------------------------------------- #
def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        return {"__error__": f"{type(exc).__name__}: {exc}"}


def _coverage_path(workdir: Path) -> Path | None:
    """layout_coverage.json 落在 ``<working_dir>/<pdf名>/`` 下，名字随输入 PDF。"""
    direct = workdir / "layout_coverage.json"
    if direct.is_file():
        return direct
    candidates = sorted(workdir.glob("*/layout_coverage.json"))
    return candidates[0] if candidates else None


def _comparable(text: str) -> str:
    """去掉样式标记/锚点/全部空白，得到可跨表示比较的目录条目文本。

    anchors 的 canonical 含 ``<style>`` 标记与锚点、字间可能多出空格
    （``get_char_unicode_string`` 按字符间距插空格，虽然 toc.json 的
    ``heading_text`` 走同一函数，但两者对 ``<style>`` 边界的处理不完全一致）；
    因此两侧都做「去标记 + 去空白」再比。
    """
    text = STYLE_RE.sub("", text or "")
    text = re.sub(r"\[\[\s*/?\s*[SFsf]\s*\d*\s*\]\]", "", text)
    return "".join(text.split())


# --------------------------------------------------------------------------- #
# 门禁 1：布局覆盖率（硬）
# --------------------------------------------------------------------------- #
def gate_layout_coverage(workdir: Path) -> dict:
    path = _coverage_path(workdir)
    if path is None:
        return {
            "status": "not_available",
            "hard": True,
            "reason": "layout_coverage.json 未找到（需要先跑 bdt parse）",
            "artifact": None,
        }
    report = _read_json(path)
    if not isinstance(report, dict) or "__error__" in report:
        return {
            "status": "not_available",
            "hard": True,
            "reason": f"layout_coverage.json 不可读: {report}",
            "artifact": str(path),
        }
    global_stats = report.get("global") or {}
    threshold = report.get("threshold")
    ratio = global_stats.get("uncovered_ratio")
    passed = report.get("passed")
    if passed is None and ratio is not None and threshold is not None:
        passed = ratio <= threshold
    unhealthy_pages = [
        {
            "page_index": page.get("page_index"),
            "uncovered_chars": page.get("uncovered_chars"),
            "coverage": page.get("coverage"),
        }
        for page in (report.get("pages") or [])
        if (page.get("coverage") or 1.0) < 1.0 - (threshold or 0.0)
    ]
    return {
        "status": "pass" if passed else "fail",
        "hard": True,
        "artifact": str(path),
        "threshold": threshold,
        "uncovered_ratio": ratio,
        "coverage": global_stats.get("coverage"),
        "total_chars": global_stats.get("total_chars"),
        "uncovered_chars": global_stats.get("uncovered_chars"),
        "unhealthy_pages": unhealthy_pages[:10],
        "detail": (
            f"未覆盖 {global_stats.get('uncovered_chars')}/"
            f"{global_stats.get('total_chars')} 字符，"
            f"占比 {ratio:.4%}（阈值 {threshold:.4%}）"
            if ratio is not None and threshold is not None
            else None
        ),
    }


# --------------------------------------------------------------------------- #
# 门禁 2：链接完整性（URI 集合硬；单条 unresolved 软）
# --------------------------------------------------------------------------- #
def _find_mono_pdf(workdir: Path) -> Path | None:
    """定位 reconstruct 输出的 mono PDF（``<workdir>/output/*.mono.pdf``）。"""
    for pattern in ("output/*.mono.pdf", "**/*.mono.pdf"):
        hits = sorted(workdir.glob(pattern))
        if hits:
            return hits[0]
    return None


def _pdf_uris(pdf_path: Path) -> set[str]:
    import pymupdf

    doc = pymupdf.open(str(pdf_path))
    try:
        return {
            link["uri"]
            for page in doc
            for link in page.get_links()
            if link.get("uri")
        }
    finally:
        doc.close()


def gate_link_integrity(workdir: Path) -> dict:
    agent = workdir / "agent"
    recon = _read_json(agent / "reconstruct_report.json")
    snapshot = _read_json(agent / "source" / "links.json")

    snapshot_total = None
    snapshot_uris = None
    if isinstance(snapshot, dict):
        snapshot_total = sum(len(v) for v in snapshot.values() if isinstance(v, list))
        snapshot_uris = sorted(
            {
                entry["uri"]
                for entries in snapshot.values()
                if isinstance(entries, list)
                for entry in entries
                if isinstance(entry, dict) and entry.get("uri")
            }
        )

    # 优先用 reconstruct 阶段落的报告（含三级回退计数）；缺失时用
    # 「mono PDF + links.json 快照」直接重算 URI 集合门禁（早期 CLI 不落报告）。
    if not isinstance(recon, dict) or "link_total" not in recon:
        mono = _find_mono_pdf(workdir)
        if mono is None or snapshot_uris is None:
            missing: list[str] = []
            if mono is None:
                missing.append("output/*.mono.pdf")
            if snapshot_uris is None:
                missing.append("agent/source/links.json")
            return {
                "status": "not_reconstructed",
                "hard": False,
                "reason": (
                    "无 reconstruct_report.json（含链接指标），且缺少重算所需产物："
                    + "、".join(missing)
                    + "（先跑 reconstruct，或确认 parse 已生成 links.json）"
                ),
                "snapshot_total": snapshot_total,
            }
        actual = sorted(_pdf_uris(mono))
        missing = sorted(set(snapshot_uris) - set(actual))
        extra = sorted(set(actual) - set(snapshot_uris))
        return {
            "status": "pass" if not missing and not extra else "fail",
            "hard": True,
            "artifact": str(mono),
            "source": "recomputed_from_pdf",
            "snapshot_total": snapshot_total,
            "snapshot_uri_count": len(snapshot_uris),
            "mono_uri_count": len(actual),
            "uri_set_match": not missing and not extra,
            "missing_uris": missing[:5],
            "extra_uris": extra[:5],
            "detail": (
                f"URI 集合：源 {len(snapshot_uris)} 条 / mono {len(actual)} 条；"
                f"缺失 {len(missing)}，新增 {len(extra)}"
            ),
        }

    unresolved = recon.get("link_unresolved") or []
    uri_match = recon.get("link_uri_set_match")
    total = recon.get("link_total") or 0
    remapped = recon.get("link_remapped") or 0
    # URI 集合门禁是硬门禁；旧调用方（无 link_remap_state）uri_match 为 None → 不判失败。
    status = "pass" if uri_match is not False else "fail"
    return {
        "status": status,
        "hard": uri_match is not None,
        "artifact": str(agent / "reconstruct_report.json"),
        "source": "reconstruct_report",
        "total": total,
        "remapped": remapped,
        "fallback_paragraph": recon.get("link_fallback_paragraph") or 0,
        "unresolved_count": len(unresolved),
        "unresolved": unresolved[:10],
        "uri_set_match": uri_match,
        "snapshot_total": snapshot_total,
        "snapshot_uri_count": len(snapshot_uris) if snapshot_uris is not None else None,
        "detail": (
            f"remapped {remapped}/{total}，unresolved {len(unresolved)}，"
            f"URI 集合一致={uri_match}"
        ),
    }


# --------------------------------------------------------------------------- #
# 门禁 3：目录完整性（条目数/顺序，硬）
# --------------------------------------------------------------------------- #
def gate_toc_integrity(workdir: Path) -> dict:
    agent = workdir / "agent"
    toc = _read_json(agent / "source" / "toc.json")
    bookmarks = _read_json(agent / "source" / "bookmarks.json")
    anchors = _read_json(agent / "anchors.json")
    if not isinstance(toc, dict) or not isinstance(anchors, dict):
        return {
            "status": "not_available",
            "hard": True,
            "reason": "toc.json 或 anchors.json 缺失（需要先跑 bdt parse）",
        }

    entries = [
        entry
        for page in toc.get("pages") or []
        for entry in (page.get("entries") or [])
    ]
    rows = [
        row
        for row in anchors.get("rows") or []
        if (row.get("layout_label") or "") == "toc_entry"
    ]
    problems: list[str] = []
    if len(entries) != len(rows):
        problems.append(f"toc.json 条目 {len(entries)} != anchors toc_entry {len(rows)}")
    # 顺序与文本一致性：anchors 的 canonical 含样式锚点，toc.json 是纯文本，比对去锚点正文。
    compared = min(len(entries), len(rows))
    order_mismatch = 0
    mismatch_samples: list[dict] = []
    for entry, row in zip(entries[:compared], rows[:compared], strict=False):
        expected = _comparable(entry.get("heading_text") or "")
        actual = _comparable(row.get("canonical") or "")
        if expected != actual:
            order_mismatch += 1
            if len(mismatch_samples) < 5:
                mismatch_samples.append(
                    {
                        "entry_index": entry.get("entry_index"),
                        "toc": entry.get("heading_text"),
                        "anchor": row.get("canonical"),
                    }
                )
    if order_mismatch:
        problems.append(f"有 {order_mismatch} 条目录条目顺序/文本不一致")

    skipped_pages = (toc.get("summary") or {}).get("skipped_pages") or []
    low_confidence = (toc.get("summary") or {}).get("low_confidence_pages") or []
    bookmark_count = len(bookmarks) if isinstance(bookmarks, list) else None

    return {
        "status": "fail" if problems else "pass",
        "hard": True,
        "artifact": str(agent / "source" / "toc.json"),
        "toc_entries": len(entries),
        "anchor_rows": len(rows),
        "order_mismatch": order_mismatch,
        "mismatch_samples": mismatch_samples,
        "bookmark_count": bookmark_count,
        "toc_pages": (toc.get("summary") or {}).get("toc_pages"),
        "low_confidence_pages": low_confidence,
        "skipped_pages": skipped_pages,
        "problems": problems,
        "detail": (
            f"目录条目 {len(entries)}（anchors {len(rows)}，书签 {bookmark_count}），"
            f"顺序不一致 {order_mismatch}"
        ),
    }


# --------------------------------------------------------------------------- #
# 门禁 4：受保护 token（软，信息性）
# --------------------------------------------------------------------------- #
def gate_protected_tokens(workdir: Path) -> dict:
    agent = workdir / "agent"
    anchors = _read_json(agent / "anchors.json")
    alignment = _read_json(agent / "source" / "mineru" / "alignment.json")
    if not isinstance(anchors, dict):
        return {
            "status": "not_available",
            "hard": False,
            "reason": "anchors.json 缺失（需要先跑 bdt parse）",
        }

    rows = anchors.get("rows") or []
    placeholder_count = sum(
        len(PLACEHOLDER_RE.findall(row.get("canonical") or "")) for row in rows
    )
    rows_with_placeholder = sum(
        1 for row in rows if PLACEHOLDER_RE.search(row.get("canonical") or "")
    )
    out: dict = {
        "status": "pass",
        "hard": False,
        "artifact": str(agent / "anchors.json"),
        "placeholder_count": placeholder_count,
        "rows_with_placeholder": rows_with_placeholder,
    }
    if isinstance(alignment, dict):
        summary = alignment.get("summary") or {}
        matched = summary.get("inline_equation_matched")
        total = summary.get("inline_equation_total")
        out.update(
            {
                "alignment_artifact": str(
                    agent / "source" / "mineru" / "alignment.json"
                ),
                "inline_equation_total": total,
                "inline_equation_matched": matched,
                "protected_inline_math": summary.get("protected_inline_math"),
                "native_char_coverage": summary.get("native_char_coverage"),
                "span_text_mismatch": summary.get("span_text_mismatch"),
                "detail": (
                    f"翻译输入含 {placeholder_count} 个 {{vN}} 占位符（"
                    f"{rows_with_placeholder} 段）；MinerU inline_equation "
                    f"{matched}/{total} 已对齐为保护区域"
                ),
            }
        )
    else:
        out["detail"] = (
            f"翻译输入含 {placeholder_count} 个 {{vN}} 占位符；"
            "alignment.json 缺失（无 MinerU 对齐审计）"
        )
    return out


# --------------------------------------------------------------------------- #
# 门禁 5：翻译协议（硬）
# --------------------------------------------------------------------------- #
def gate_protocol(workdir: Path) -> dict:
    agent = workdir / "agent"
    report = _read_json(agent / "apply_report.json")
    if not isinstance(report, dict):
        protocol = _read_json(agent / "protocol_report.json")
        if isinstance(protocol, dict):
            return {
                "status": "pass" if protocol.get("ok") else "fail",
                "hard": True,
                "artifact": str(agent / "protocol_report.json"),
                "violations": protocol.get("violations") or [],
                "warnings": [],
            }
        # 早期 CLI 只在 stdout 报报告、不落盘；用 translated.jsonl
        # 与 anchors.json 做等价确定性校验：每个应翻译段落都有 target。
        fallback = _protocol_from_translated(agent)
        if fallback is not None:
            return fallback
        return {
            "status": "not_available",
            "hard": True,
            "reason": (
                "apply_report.json / protocol_report.json / translated.jsonl "
                "均缺失（需要先 apply）"
            ),
        }
    violations = report.get("violations") or []
    ok = report.get("ok", not violations)
    return {
        "status": "pass" if ok else "fail",
        "hard": True,
        "artifact": str(agent / "apply_report.json"),
        "source": "apply_report",
        "applied": report.get("applied"),
        "violations": violations[:10],
        "violation_count": len(violations) if isinstance(violations, list) else None,
        "repaired": len(report.get("repaired") or []),
        "fallback_ids": len(report.get("fallback_ids") or []),
        "warnings": (report.get("warnings") or [])[:10],
    }


def _protocol_from_translated(agent: Path) -> dict | None:
    """无 apply 报告时的等价校验：translated.jsonl 覆盖 anchors.json 全部 id。"""
    translated_path = agent / "translated.jsonl"
    anchors = _read_json(agent / "anchors.json")
    if not translated_path.is_file() or not isinstance(anchors, dict):
        return None
    ids: list[str] = []
    for line in translated_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and entry.get("id"):
            ids.append(entry["id"])
    expected = [row.get("id") for row in anchors.get("rows") or [] if row.get("id")]
    missing = [i for i in expected if i not in set(ids)]
    extra = [i for i in ids if i not in set(expected)]
    return {
        "status": "pass" if not missing and not extra else "fail",
        "hard": True,
        "artifact": str(translated_path),
        "source": "recomputed_from_translated_jsonl",
        "applied": len(ids),
        "expected": len(expected),
        "missing_ids": missing[:10],
        "extra_ids": extra[:10],
        "detail": (
            f"translated.jsonl {len(ids)} 条 / anchors {len(expected)} 条；"
            f"缺失 {len(missing)}，多余 {len(extra)}"
        ),
    }


# --------------------------------------------------------------------------- #
# 聚合
# --------------------------------------------------------------------------- #
def evaluate(workdir: Path, pdf: str | None = None) -> dict:
    gates = {
        "layout_coverage": gate_layout_coverage(workdir),
        "link_integrity": gate_link_integrity(workdir),
        "toc_integrity": gate_toc_integrity(workdir),
        "protected_tokens": gate_protected_tokens(workdir),
        "protocol": gate_protocol(workdir),
    }
    hard_failures = [
        name
        for name, gate in gates.items()
        if gate.get("hard") and gate.get("status") == "fail"
    ]
    unresolved_gates = [
        name
        for name, gate in gates.items()
        if gate.get("status") in ("not_available", "not_reconstructed")
    ]
    return {
        "workdir": str(workdir),
        "pdf": pdf,
        "ok": not hard_failures,
        "hard_failures": hard_failures,
        "not_evaluated": unresolved_gates,
        "gates": gates,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workdir")
    parser.add_argument("--pdf", default=None, help="源 PDF（仅用于报告标注）")
    parser.add_argument("--json", action="store_true", help="机读输出（默认也是 JSON）")
    args = parser.parse_args(argv)

    workdir = Path(args.workdir)
    if not workdir.is_dir():
        print(
            json.dumps(
                {"ok": False, "error": {"code": "workdir_missing", "message": str(workdir)}},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    result = evaluate(workdir, args.pdf)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
