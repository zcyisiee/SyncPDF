"""babeldoc.tools.agent CLI 入口。

用法：
    python -m babeldoc.tools.agent extract <pdf> --workdir <dir> [--lang-in en --lang-out zh] [--pages 1,2]
    python -m babeldoc.tools.agent apply <workdir> <translated.jsonl>
    python -m babeldoc.tools.agent reconstruct <workdir> [--output-dir <dir>] [--dual]
    python -m babeldoc.tools.agent render <pdf> --pages 1,2 [--dpi 110] [--out-dir <dir>]

extract 输出 <workdir>/agent/sheet.jsonl（含 id/page/layout_label/source）；
翻译方（agent subagent）产 id/target JSONL（或 JSON 数组）交 apply 校验写回。
"""

from __future__ import annotations

import argparse
import json
import sys

from babeldoc.tools.agent import workflow
from babeldoc.tools.agent import markdown_view


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="babeldoc.tools.agent",
        description="agent 文档翻译工具层（extract/apply/reconstruct/render）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_extract = sub.add_parser("extract", help="解析 PDF 导出翻译 sheet")
    p_extract.add_argument("pdf")
    p_extract.add_argument("--workdir", required=True)
    p_extract.add_argument("--lang-in", default="en")
    p_extract.add_argument("--lang-out", default="zh")
    p_extract.add_argument("--pages", default=None, help="如 1,2 或 1-3")
    p_extract.add_argument(
        "--layout",
        choices=["native", "mineru"],
        default="native",
        help="布局后端：native(本地 ONNX) 或 mineru(API，需 token，结果按内容哈希缓存)",
    )
    p_extract.add_argument("--mineru-token", default=None)
    p_extract.add_argument(
        "--skip-labels",
        default=None,
        help="追加跳过的 layout 标签（逗号分隔，如 reference,author,figure）",
    )

    p_apply = sub.add_parser("apply", help="校验译文并写回 IR")
    p_apply.add_argument("workdir")
    p_apply.add_argument("sheet", help="译文 JSONL/JSON（每行 {id, target}）")

    p_recon = sub.add_parser("reconstruct", help="从 IR 重排生成 PDF")
    p_recon.add_argument("workdir")
    p_recon.add_argument("--output-dir", default=None)
    p_recon.add_argument("--dual", action="store_true", help="同时输出拼宽双语 PDF")

    p_render = sub.add_parser("render", help="PDF 页渲染 PNG")
    p_render.add_argument("pdf")
    p_render.add_argument("--pages", required=True)
    p_render.add_argument("--dpi", type=int, default=110)
    p_render.add_argument("--out-dir", default=None)

    p_md = sub.add_parser("md-extract", help="解析 PDF 导出连续 Markdown（带行内锚点）")
    p_md.add_argument("pdf")
    p_md.add_argument("--workdir", required=True)
    p_md.add_argument("--lang-in", default="en")
    p_md.add_argument("--lang-out", default="zh")
    p_md.add_argument("--pages", default=None)
    p_md.add_argument("--layout", choices=["native", "mineru"], default="mineru")
    p_md.add_argument("--mineru-token", default=None)
    p_md.add_argument("--mineru-json", default=None, help="回放已缓存的 MinerU layout.json")

    p_mda = sub.add_parser("md-apply", help="校验译文 Markdown 并写回 IR")
    p_mda.add_argument("workdir")
    p_mda.add_argument("markdown", help="模型输出的译文 Markdown")

    args = parser.parse_args(argv)

    if args.command == "extract":
        result = workflow.extract(
            args.pdf,
            args.workdir,
            lang_in=args.lang_in,
            lang_out=args.lang_out,
            pages=args.pages,
            layout=args.layout,
            mineru_token=args.mineru_token,
            skip_labels=args.skip_labels,
        )
    elif args.command == "apply":
        result = workflow.apply(args.workdir, args.sheet)
        if not result["ok"]:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            sys.exit(1)
    elif args.command == "reconstruct":
        result = workflow.reconstruct(
            args.workdir,
            output_dir=args.output_dir,
            no_dual=not args.dual,
        )
    elif args.command == "render":
        result = workflow.render(
            args.pdf, args.pages, dpi=args.dpi, out_dir=args.out_dir
        )
    elif args.command == "md-extract":
        result = markdown_view.extract_markdown(
            args.pdf,
            args.workdir,
            lang_in=args.lang_in,
            lang_out=args.lang_out,
            pages=args.pages,
            layout=args.layout,
            mineru_token=args.mineru_token,
            mineru_json=args.mineru_json,
        )
    elif args.command == "md-apply":
        result = markdown_view.apply_markdown(args.workdir, args.markdown)
        if not result.get("ok"):
            print(json.dumps(result, ensure_ascii=False, indent=2))
            sys.exit(1)
    else:  # pragma: no cover
        parser.error(f"未知命令: {args.command}")

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
