"""Test whether real MinerU equation spans compile as LaTeX without repair.

The hypothesis says "feed MinerU OCR text (with inline LaTeX) to LaTeX". This
script isolates the *content* risk from the *layout* risk: it takes every
``interline_equation`` and ``inline_equation`` span from the real MinerU caches,
wraps each in a minimal document, and records which ones fail.

Failures fall into two classes worth distinguishing:
  * structurally invalid LaTeX (unbalanced braces/environments);
  * constructs that need packages/repair (``\\tag``, ``\\textstyle``, ``$`` inside,
    spaced ``\\mathrm { ... }`` tokens that MinerU emits).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


ROOT = HERE.parents[2]
OUT = ROOT / "docs" / "layout-hypothesis" / "out"
WORK = ROOT / "docs" / "layout-hypothesis" / "work" / "eqcheck"

CACHES = {
    "f15a0e00-2026-f1872": Path.home()
    / ".cache/babeldoc/mineru-layout.v1/f15a0e00eed725df8ed53c5da37250bb2a09ac1dd4c7710013d6b0b8d5ac062e.json",
    "773068fa-2312-04432": Path.home()
    / ".cache/babeldoc/mineru-layout.v1/773068fa427a7c594c0bd97361aea23eb5bc68dc513548604c628029c3d0c4e9.json",
    "ebdca8e7-ccs2026b": Path.home()
    / ".cache/babeldoc/mineru-layout.v1/ebdca8e7e579450770eafdf98243f197b8e60fc115fe07ab15870a31812a326b.json",
    "ba68e2e4-deepseek": Path.home()
    / ".cache/babeldoc/mineru-layout.v1/ba68e2e40408125ae6d2f63a9a241b61c73910691c74ec1a2a7023c851eac08d.json",
}

TEX = r"""\documentclass{article}
\usepackage{amsmath}
\usepackage{amssymb}
\usepackage{bm}
\pagestyle{empty}
\begin{document}
\begin{displaymath}
%s
\end{displaymath}
\end{document}
"""


def collect_spans(cache: Path, limit: int | None = None) -> list[dict]:
    doc = json.loads(cache.read_text())
    spans = []
    for page in doc["pdf_info"]:
        for block in page["para_blocks"]:
            for line in block.get("lines") or []:
                for span in line["spans"]:
                    if span["type"] != "text":
                        spans.append(
                            {
                                "page": page["page_idx"],
                                "type": span["type"],
                                "content": span["content"],
                                "score": span.get("score"),
                            }
                        )
        if limit and len(spans) >= limit:
            break
    return spans[:limit] if limit else spans


def try_compile(content: str, idx: int, timeout: float = 20.0) -> dict:
    WORK.mkdir(parents=True, exist_ok=True)
    stem = f"eq{idx:04d}"
    tex_path = WORK / f"{stem}.tex"
    tex_path.write_text(TEX % content, encoding="utf-8")
    try:
        proc = subprocess.run(
            [
                "xelatex",
                "-interaction=nonstopmode",
                "-halt-on-error",
                f"-output-directory={WORK}",
                str(tex_path),
            ],
            cwd=WORK,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "timeout"}
    log = proc.stdout + proc.stderr
    if proc.returncode == 0:
        return {"ok": True}
    errors = re.findall(r"^! (.+)$", log, flags=re.M)
    return {"ok": False, "reason": errors[0][:120] if errors else "unknown"}


def main() -> None:
    summary = {}
    for name, cache in CACHES.items():
        if not cache.exists():
            summary[name] = {"skipped": "cache missing"}
            continue
        spans = collect_spans(cache)
        results = []
        for idx, span in enumerate(spans):
            out = try_compile(span["content"], idx)
            results.append({**span, "compiles_as_is": out["ok"], "error": out.get("reason", "")})
        n_ok = sum(1 for r in results if r["compiles_as_is"])
        error_kinds: dict[str, int] = {}
        for r in results:
            if not r["compiles_as_is"]:
                key = r["error"][:60]
                error_kinds[key] = error_kinds.get(key, 0) + 1
        by_type: dict[str, dict] = {}
        for r in results:
            b = by_type.setdefault(r["type"], {"n": 0, "ok": 0})
            b["n"] += 1
            b["ok"] += 1 if r["compiles_as_is"] else 0
        summary[name] = {
            "n_spans": len(results),
            "n_compile_as_is": n_ok,
            "compile_rate": round(n_ok / len(results), 3) if results else None,
            "by_type": by_type,
            "top_errors": dict(sorted(error_kinds.items(), key=lambda kv: -kv[1])[:5]),
            "samples_failed": [r for r in results if not r["compiles_as_is"]][:3],
        }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "mineru-latex-compilability.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for name, s in summary.items():
        if "skipped" in s:
            print(f"{name}: skipped ({s['skipped']})")
            continue
        print(
            f"{name}: {s['n_compile_as_is']}/{s['n_spans']} compile as-is "
            f"({s['compile_rate']}) by_type={s['by_type']}"
        )
        for k, v in s["top_errors"].items():
            print(f"    {v:3d}x {k}")


if __name__ == "__main__":
    main()
