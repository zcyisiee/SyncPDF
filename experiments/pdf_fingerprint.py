"""PDF 指纹：页数 / 目录条目 / 链接数 / 文本层哈希（回归比对用）。

用法：
    python experiments/pdf_fingerprint.py <pdf> [<pdf> ...]
    python experiments/pdf_fingerprint.py <pdf> --json          # 机读输出
    python experiments/pdf_fingerprint.py <a.pdf> --compare <b.pdf>

哈希口径：按页提取文本（pymupdf ``get_text("text")``，NFKC 归一化后去空白），
逐页 sha256 再整体 sha256 —— 文本层一致则哈希一致（坐标/字体差异不影响）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import unicodedata
from pathlib import Path

import pymupdf


def fingerprint(pdf_path) -> dict:
    doc = pymupdf.open(pdf_path)
    page_hashes = []
    per_page = []
    links = 0
    for page in doc:
        text = unicodedata.normalize("NFKC", page.get_text("text"))
        normalized = "".join(text.split())
        page_hashes.append(hashlib.sha256(normalized.encode("utf-8")).hexdigest())
        page_links = page.get_links()
        links += len(page_links)
        per_page.append(len(page_links))
    toc = doc.get_toc()
    result = {
        "pdf": str(pdf_path),
        "pages": len(doc),
        "toc_entries": len(toc),
        "links": links,
        "links_per_page": per_page,
        "text_hash": hashlib.sha256("".join(page_hashes).encode("utf-8")).hexdigest()[
            :16
        ],
        "page_hashes": [h[:12] for h in page_hashes],
        "size_bytes": Path(pdf_path).stat().st_size,
    }
    doc.close()
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="PDF 指纹/回归比对")
    parser.add_argument("pdfs", nargs="+")
    parser.add_argument("--json", action="store_true", help="机读输出")
    parser.add_argument("--compare", default=None, help="与基线 PDF 比对")
    args = parser.parse_args(argv)

    results = [fingerprint(path) for path in args.pdfs]
    if args.compare:
        baseline = fingerprint(args.compare)
        for result in results:
            diff = {
                "pdf": result["pdf"],
                "vs": baseline["pdf"],
                "pages": [baseline["pages"], result["pages"]],
                "toc_entries": [baseline["toc_entries"], result["toc_entries"]],
                "links": [baseline["links"], result["links"]],
                "text_hash_equal": baseline["text_hash"] == result["text_hash"],
                "pages_with_different_text": [
                    index + 1
                    for index, (a, b) in enumerate(
                        zip(baseline["page_hashes"], result["page_hashes"], strict=False)
                    )
                    if a != b
                ][:20],
            }
            print(json.dumps(diff, ensure_ascii=False, indent=2))
        return 0
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for result in results:
            print(
                f"{result['pdf']}: pages={result['pages']} "
                f"toc={result['toc_entries']} links={result['links']} "
                f"text_hash={result['text_hash']} size={result['size_bytes']}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
