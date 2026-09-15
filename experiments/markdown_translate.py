"""Markdown 整篇翻译编排器：document.md → 一次 agy 调用 → md-apply → reconstruct → render。

用法：
    python experiments/markdown_translate.py <workdir> \
        [--model gemini-3.8-flash-low] [--effort low] \
        [--output-dir <dir>] [--skip-translate] [--dry-run] \
        [--skip-reconstruct] [--latex-bbox [--latex-bbox-mode full|repair]] [--no-dual]

--skip-reconstruct：只到 md-apply 为止（验收流程分开跑 default/--latex-bbox 两次
reconstruct 时用）；--latex-bbox：reconstruct 开启 LaTeX bbox 排版（与
`babeldoc.tools.agent reconstruct --latex-bbox` 同一链路）。

产物（均在 <workdir>/agent/ 与 --output-dir）：
    agent/prompt.md        实际发给模型的完整提示词
    agent/translated.md    模型返回的译文 Markdown
    agent/translated.jsonl canonical 译文（apply 的输入）
    agent/apply_report.json
    output/*.mono.pdf / *.dual.pdf / render/*.png
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from babeldoc.tools.agent import markdown_view  # noqa: E402
from babeldoc.tools.agent import workflow  # noqa: E402

PROMPT_FILE = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "document-translate"
    / "prompts"
    / "markdown-translator.md"
)

# 新版提示词位置（tools/babeldoc_tools.common 同此口径）；不存在时回退 legacy
NEW_PROMPT_FILE = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "document-translate"
    / "agents"
    / "translator.md"
)


def load_prompt_template() -> str:
    file = PROMPT_FILE if PROMPT_FILE.exists() else NEW_PROMPT_FILE
    blocks = re.findall(
        r"```text\n(.*?)```", file.read_text(encoding="utf-8"), re.DOTALL
    )
    if not blocks:
        raise SystemExit(f"提示词文件缺少 ```text 块: {file}")
    return blocks[0]


def run_agy(prompt: str, model: str, effort: str, timeout_s: int) -> tuple[str, dict]:
    """调用 agy 一次，返回 (译文, usage)。usage 含 input/output/cache_read/total tokens。"""
    started = time.time()
    result = subprocess.run(
        [
            "agy",
            "--model",
            model,
            "--effort",
            effort,
            "--disable-slash-commands",
            "--print-timeout",
            f"{timeout_s // 60}m",
            "--output-format",
            "json",
            "--print",
            prompt,
        ],
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"agy 退出码 {result.returncode}: {result.stderr[:800]}"
        )
    elapsed = round(time.time() - started, 1)
    raw = result.stdout.strip()
    usage: dict = {}
    try:
        payload = json.loads(raw)
        response = payload.get("response", "")
        usage = payload.get("usage") or {}
        usage["duration_seconds"] = payload.get("duration_seconds", elapsed)
        usage["num_turns"] = payload.get("num_turns")
        usage["conversation_id"] = payload.get("conversation_id")
    except json.JSONDecodeError:
        response = raw
        usage = {"duration_seconds": elapsed, "parse_error": True}
    print(
        f"agy 完成：{elapsed}s, 输出 {len(response)} 字符, "
        f"usage={json.dumps(usage, ensure_ascii=False)}"
    )
    return response, usage


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workdir")
    ap.add_argument("--model", default="gemini-3.8-flash-low")
    ap.add_argument("--effort", default="low")
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--skip-translate", action="store_true", help="复用已有 translated.md")
    ap.add_argument("--dry-run", action="store_true", help="只生成 prompt.md，不调用模型")
    ap.add_argument("--render-pages", default="1,5,8,11,20")
    ap.add_argument(
        "--skip-reconstruct",
        action="store_true",
        help="只到 md-apply 为止（验收流程需要分开跑 default/latex 两次 reconstruct 时用）",
    )
    ap.add_argument(
        "--latex-bbox",
        action="store_true",
        default=True,
        help="reconstruct 开启 LaTeX bbox 排版（默认开启；缺 XeLaTeX/字体时自动回退）",
    )
    ap.add_argument(
        "--no-latex-bbox",
        action="store_false",
        dest="latex_bbox",
        help="关闭 LaTeX bbox 排版（输出与旧渲染路径逐字节一致）",
    )
    ap.add_argument(
        "--latex-bbox-mode",
        choices=("full", "repair"),
        default=None,
        help="LaTeX bbox 资格模式：full（默认）/ repair",
    )
    ap.add_argument("--no-dual", action="store_true", help="只输出 mono，不输出拼宽双语")
    args = ap.parse_args()

    workdir = Path(args.workdir)
    agent = workflow.agent_dir(workdir)
    document_md = (agent / "document.md").read_text(encoding="utf-8")
    prompt = load_prompt_template().replace("{document}", document_md)
    (agent / "prompt.md").write_text(prompt, encoding="utf-8")
    print(f"prompt: {agent / 'prompt.md'} ({len(prompt)} 字符)")

    if args.dry_run:
        return 0

    translated_md = agent / "translated.md"
    if args.skip_translate:
        if not translated_md.exists():
            raise SystemExit(f"--skip-translate 但 {translated_md} 不存在")
        print(f"复用 {translated_md}")
    else:
        response, usage = run_agy(prompt, args.model, args.effort, args.timeout)
        translated_md.write_text(response, encoding="utf-8")
        print(f"译文 Markdown: {translated_md} ({len(response)} 字符)")
        print(f"token usage: {json.dumps(usage, ensure_ascii=False)}")

    # 漏行补译：模型偶尔会合并/漏掉段落，只对缺失 id 再调用一次（复用译文时也检查）
    missing = markdown_view.missing_ids(
        workdir, translated_md.read_text(encoding="utf-8")
    )
    if missing:
        print(f"missing ids: {missing}，开始补译...")
        retry_doc = markdown_view.render_retry_markdown(workdir, missing)
        retry_prompt = load_prompt_template().replace("{document}", retry_doc)
        (agent / "prompt.retry.md").write_text(retry_prompt, encoding="utf-8")
        retry_response, _retry_usage = run_agy(
            retry_prompt, args.model, args.effort, args.timeout
        )
        (agent / "translated.retry.md").write_text(retry_response, encoding="utf-8")
        with open(translated_md, "a", encoding="utf-8") as f:
            f.write("\n\n" + retry_response)
        still_missing = markdown_view.missing_ids(
            workdir, translated_md.read_text(encoding="utf-8")
        )
        print(f"补译后仍缺失: {still_missing}")

    report = markdown_view.apply_markdown(workdir, translated_md)
    (agent / "apply_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("apply:", json.dumps(report, ensure_ascii=False)[:600])
    if not report.get("ok"):
        return 1

    if args.skip_reconstruct:
        print("--skip-reconstruct：已写回 IR，跳过 reconstruct/render")
        return 0

    output_dir = Path(args.output_dir) if args.output_dir else workdir / "output"
    recon = workflow.reconstruct(
        workdir,
        output_dir=str(output_dir),
        no_dual=args.no_dual,
        latex_bbox=args.latex_bbox,
        latex_bbox_mode=args.latex_bbox_mode,
    )
    print("reconstruct:", json.dumps(recon, ensure_ascii=False))

    mono = recon.get("mono_pdf")
    render = {}
    if mono:
        render = workflow.render(
            mono,
            args.render_pages,
            dpi=110,
            out_dir=str(output_dir / "render"),
        )
    print("render:", json.dumps(render, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
