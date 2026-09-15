"""离线协议违规报告：对历史翻译产物回放 agent 协议校验（tools/agent/protocol.py）。

两种数据源：
1. 缓存模式（默认）：~/.cache/babeldoc/cache.v1.db 里 engine=openai 的批量翻译
   prompt + 缓存回复（"## Here is the input:" 协议）。
2. tracking 模式：translate_tracking.json 中 llm_translate_trackers 的
   input（完整 prompt）/output 对。

用途：无需 LLM API 即可测量「固定管道 + 某模型」的协议违规率，作为 agent
编排实验（M3）的对照组基线。

用法：
    python experiments/protocol_report.py --cache
    python experiments/protocol_report.py --tracking tmp/mvp-full-20260909/2312.04432v2/translate_tracking.json
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from babeldoc.tools.agent.protocol import INPUT_HEADING  # noqa: E402
from babeldoc.tools.agent.protocol import extract_input_items  # noqa: E402
from babeldoc.tools.agent.protocol import validate_output  # noqa: E402


def iter_cache_rows(db_path: str):
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "SELECT original_text, translation FROM _translationcache "
        "WHERE translate_engine = 'openai' AND original_text LIKE ?",
        (f"%{INPUT_HEADING}%",),
    ).fetchall()
    yield from rows


def iter_tracking_rows(tracking_path: str):
    with Path(tracking_path).open() as f:
        data = json.load(f)

    def walk(items):
        for item in items:
            for tracker in item.get("llm_translate_trackers") or []:
                prompt, output = tracker.get("input"), tracker.get("output")
                if prompt and output:
                    yield prompt, output

    for page in data.get("page", []):
        yield from walk(page.get("paragraph") or [])
    yield from walk(data.get("cross_page") or [])
    yield from walk(data.get("cross_column") or [])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--cache", action="store_true", help="分析本地翻译缓存库")
    source.add_argument("--tracking", metavar="PATH", help="分析 translate_tracking.json")
    parser.add_argument("--db", default="~/.cache/babeldoc/cache.v1.db")
    parser.add_argument("--save", metavar="PATH", help="把 JSON 报告写入文件")
    args = parser.parse_args()

    if args.cache:
        rows = iter_cache_rows(Path(args.db).expanduser())
        source_name = f"cache:{args.db}"
    else:
        rows = iter_tracking_rows(args.tracking)
        source_name = f"tracking:{args.tracking}"

    report = {
        "source": source_name,
        "total_batch_replies": 0,
        "not_batch_protocol": 0,
        "first_try_pass": 0,
        "violation_rate": None,
        "violations": {},
        "samples": [],
    }
    for prompt, output in rows:
        items = extract_input_items(prompt)
        if items is None:
            report["not_batch_protocol"] += 1
            continue
        report["total_batch_replies"] += 1
        errors = validate_output(output, items)
        if not errors:
            report["first_try_pass"] += 1
        else:
            for error in errors:
                key = error.split(":", 1)[0]
                report["violations"][key] = report["violations"].get(key, 0) + 1
            if len(report["samples"]) < 10:
                report["samples"].append(
                    {"errors": errors[:3], "output_head": str(output)[:200]}
                )

    checked = report["total_batch_replies"]
    if checked:
        report["violation_rate"] = round(
            1 - report["first_try_pass"] / checked, 4
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.save:
        Path(args.save).parent.mkdir(parents=True, exist_ok=True)
        Path(args.save).write_text(
            json.dumps(report, ensure_ascii=False, indent=2)
        )


if __name__ == "__main__":
    main()
