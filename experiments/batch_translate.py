"""批量 sheet 翻译编排器：分批调用 agy CLI + 逐批协议校验 + 违规打回重试。

用法：
    python experiments/batch_translate.py <workdir> [--model gemini-3.8-flash-low]
        [--effort low] [--batch-size 40] [--max-retries 2]

读 <workdir>/agent/sheet.jsonl，写 <workdir>/agent/translated.jsonl 与
<workdir>/agent/batch_report.json。占位符校验复用 babeldoc.tools.agent.protocol。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from babeldoc.tools.agent import protocol  # noqa: E402

PROMPT_TEMPLATE = """你是专业英译中（简体中文）翻译。下面的输入是论文 PDF 逐段抽取的翻译清单（JSONL，每行一个对象）。

## 占位符协议（必须严格遵守）
- 源文中形如 {{v1}} {{v2}} 的 token 是公式占位符：原样保留，不得改写、增删、翻译。
- 源文中形如 <style id='1'>...</style> 的是富文本标记：标签原样保留，只翻译标签内/之间的自然语言文本。
- 人名、邮箱、URL 原样保留。
- 译文不要在引用占位符（如 [5],[15]）后额外添加逗号或顿号。

## 输出要求
- 逐行对应输出 JSONL，每行恰好一个对象：{{"id": "<原id>", "target": "<译文>"}}
- id 必须与输入完全一致，不得增行、漏行、改行。
- target 中占位符的集合与出现次数必须与 source 完全一致。
- 不要输出任何解释、markdown 代码块或其他文字，只输出 JSONL。

## Here is the input:
{input}"""

RETRY_TEMPLATE = """你上一批译文有协议违规，被校验器拒绝。违规清单与对应源文如下（JSONL）。
请重新翻译这些行并严格遵守占位符协议；只输出这些行的 JSONL 译文。

{feedback}

## Here is the input:
{input}"""


def run_agy(prompt: str, model: str, effort: str) -> str:
    result = subprocess.run(
        [
            "agy",
            "--model",
            model,
            "--effort",
            effort,
            "--print",
            prompt,
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(f"agy 退出码 {result.returncode}: {result.stderr[:400]}")
    return result.stdout


def parse_jsonl_output(raw: str) -> dict[str, str]:
    out = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "id" in obj and "target" in obj:
            out[str(obj["id"])] = str(obj["target"])
    return out


def translate_batch(rows, model, effort, max_retries):
    """翻译一批行；违规行打回重试。返回 (id->target, 批次统计)。"""
    ids = [r["id"] for r in rows]
    pending = rows
    translated: dict[str, str] = {}
    stats = {"rows": len(rows), "retries": 0, "fallback_source": 0}

    prompt_input = "\n".join(
        json.dumps({"id": r["id"], "source": r["source"]}, ensure_ascii=False)
        for r in pending
    )
    raw = run_agy(PROMPT_TEMPLATE.format(input=prompt_input), model, effort)
    got = parse_jsonl_output(raw)
    for r in pending:
        if r["id"] in got:
            translated[r["id"]] = got[r["id"]]

    for attempt in range(max_retries):
        pending = []
        feedback = []
        for r in rows:
            target = translated.get(r["id"])
            if target is None:
                pending.append(r)
                feedback.append(
                    {"id": r["id"], "error": "missing row (id 未出现在输出中)"}
                )
                continue
            violations = protocol.check_placeholders(r["id"], r["source"], target)
            if violations:
                pending.append(r)
                feedback.append({"id": r["id"], "error": "; ".join(violations)})
        if not pending:
            break
        stats["retries"] += 1
        fb_text = "\n".join(json.dumps(x, ensure_ascii=False) for x in feedback)
        prompt_input = "\n".join(
            json.dumps({"id": r["id"], "source": r["source"]}, ensure_ascii=False)
            for r in pending
        )
        raw = run_agy(
            RETRY_TEMPLATE.format(feedback=fb_text, input=prompt_input), model, effort
        )
        got = parse_jsonl_output(raw)
        for r in pending:
            if r["id"] in got:
                translated[r["id"]] = got[r["id"]]

    # 重试耗尽仍违规的行回退 source（恒通过校验，原文保留）
    for r in rows:
        target = translated.get(r["id"])
        if target is None or protocol.check_placeholders(r["id"], r["source"], target):
            translated[r["id"]] = r["source"]
            stats["fallback_source"] += 1

    return translated, stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("workdir")
    parser.add_argument("--model", default="gemini-3.8-flash-low")
    parser.add_argument("--effort", default="low")
    parser.add_argument("--batch-size", type=int, default=40)
    parser.add_argument("--max-retries", type=int, default=2)
    args = parser.parse_args()

    agent_dir = Path(args.workdir) / "agent"
    sheet = [
        json.loads(line)
        for line in (agent_dir / "sheet.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    merged: dict[str, str] = {}
    report = {"batches": [], "started": time.strftime("%H:%M:%S")}
    for i in range(0, len(sheet), args.batch_size):
        rows = sheet[i : i + args.batch_size]
        t0 = time.time()
        translated, stats = translate_batch(rows, args.model, args.effort, args.max_retries)
        stats["seconds"] = round(time.time() - t0, 1)
        stats["batch"] = len(report["batches"]) + 1
        report["batches"].append(stats)
        merged.update(translated)
        print(
            f"batch {stats['batch']}: rows={stats['rows']} retries={stats['retries']} "
            f"fallback={stats['fallback_source']} {stats['seconds']}s",
            flush=True,
        )

    out_path = agent_dir / "translated.jsonl"
    with open(out_path, "w", encoding="utf-8") as f:
        for row in sheet:
            f.write(
                json.dumps(
                    {"id": row["id"], "target": merged[row["id"]]}, ensure_ascii=False
                )
                + "\n"
            )
    report["total_rows"] = len(sheet)
    report["total_fallback"] = sum(b["fallback_source"] for b in report["batches"])
    report["total_retries"] = sum(b["retries"] for b in report["batches"])
    (agent_dir / "batch_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"done: {len(sheet)} rows -> {out_path} (fallback={report['total_fallback']})")


if __name__ == "__main__":
    main()
