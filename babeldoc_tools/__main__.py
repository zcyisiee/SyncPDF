"""``bdt`` CLI：文档翻译链路的唯一入口（argparse 子命令）。

用法::

    bdt parse <pdf> --workdir tmp/wd [--layout mineru|paddle]
    bdt translate --workdir tmp/wd [--ids P01-003,P01-007] [--feedback "..."]
    bdt apply --workdir tmp/wd [--markdown tmp/wd/agent/translated.md]
    bdt build --workdir tmp/wd [--output-dir tmp/wd/output] [--dual] [--render 1,2]
    bdt check --workdir tmp/wd [--skip-pdf-checks]
    bdt layout-set --workdir tmp/wd --patch '{"paragraphs": {...}}'
    bdt report --workdir tmp/wd
    bdt run <pdf> --workdir tmp/wd [--from build] [--markdown self]

约定：

- stdout 恒为**单行 JSON**：``{"ok": true, "data": {...}}`` 或
  ``{"ok": false, "error": {"code", "message"}}``；
- 日志/进度/第三方库输出统一走 stderr；
- 退出码：0 = ok，1 = 失败（含可预期错误），2 = 用法错误（argparse）。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys

from babeldoc_tools import __version__
from babeldoc_tools import common
from babeldoc_tools import layout
from babeldoc_tools import parse
from babeldoc_tools import registry
from babeldoc_tools import report
from babeldoc_tools import review
from babeldoc_tools import run as run_tool
from babeldoc_tools import translate

#: ``run`` 的续跑起点；顺序与 :data:`babeldoc_tools.run.STAGES` 一致。
RUN_STAGES = run_tool.STAGES


def _split_ids(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _add_workdir(parser: argparse.ArgumentParser, *, required: bool = True) -> None:
    parser.add_argument("--workdir", required=required, help="工作目录（含 agent/ 产物）")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bdt",
        description="BabelDOC 文档翻译工具层子命令（stdout 单行 JSON）",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    # ---- parse ----------------------------------------------------------- #
    p_parse = sub.add_parser("parse", help="解析 PDF → 连续 Markdown（带锚点）+ IR 状态")
    p_parse.add_argument("pdf", help="源 PDF 路径")
    _add_workdir(p_parse)
    p_parse.add_argument(
        "--layout",
        choices=("mineru", "paddle"),
        default="mineru",
        help="布局后端：mineru（云端 API）或 paddle（本地 PP-DocLayoutV3）",
    )
    p_parse.add_argument("--mineru-token", default=None)
    p_parse.add_argument("--mineru-json", default=None, help="回放已缓存的 MinerU layout.json")
    p_parse.add_argument(
        "--mineru-cache-key",
        default=None,
        help="按 PDF 内容 sha256 指定已缓存布局（~/.cache/babeldoc/mineru-layout.v1/<key>.json）",
    )
    p_parse.add_argument(
        "--layout-coverage-threshold",
        type=float,
        default=0.005,
        help="布局覆盖率门禁阈值（未命中 layout 区域的原生字符占比上限，默认 0.005）",
    )
    p_parse.add_argument("--pages", default=None, help="如 1,2 或 1-3")
    p_parse.add_argument("--lang-in", default="en")
    p_parse.add_argument("--lang-out", default="zh")
    p_parse.add_argument(
        "--mineru-ocr-text",
        action="store_true",
        help="实验性：把 MinerU OCR 文本回填到原生字符（仅等长 text span）",
    )

    # ---- translate ------------------------------------------------------- #
    p_translate = sub.add_parser("translate", help="整篇翻译 / 按 id 重译合并")
    _add_workdir(p_translate)
    p_translate.add_argument(
        "--ids",
        default=None,
        help="逗号分隔的段落 id（如 P01-003,P01-007）；给出则走重译合并路径",
    )
    p_translate.add_argument("--feedback", default=None, help="重译时给模型的反馈")
    p_translate.add_argument(
        "--markdown",
        default=None,
        help="已有译文 Markdown 路径：直接导入，不调用模型",
    )
    p_translate.add_argument(
        "--prompt-only",
        action="store_true",
        help="只写 agent/prompt.md，不调用模型",
    )
    p_translate.add_argument("--model", default=None)
    p_translate.add_argument("--effort", default=None, help='"none" 表示不传 --effort')
    p_translate.add_argument("--timeout", type=int, default=1800)
    p_translate.add_argument(
        "--command",
        dest="translator_command",
        default=None,
        help="翻译 CLI，默认 agy",
    )
    p_translate.add_argument("--prompt", default=None, help="提示词名，默认 translator")
    p_translate.add_argument("--repair-prompt", default=None, help="重译提示词名")
    p_translate.add_argument(
        "--no-retry-missing",
        action="store_false",
        dest="retry_missing",
        help="缺失段落不自动补译",
    )

    # ---- apply ----------------------------------------------------------- #
    p_apply = sub.add_parser("apply", help="校验译文 Markdown 并写回 IR")
    _add_workdir(p_apply)
    p_apply.add_argument(
        "--markdown",
        default=None,
        help="译文 Markdown（默认 agent/translated.md）",
    )

    # ---- build ----------------------------------------------------------- #
    p_build = sub.add_parser("build", help="从 IR 重排生成 mono/dual PDF（可选渲染页）")
    _add_workdir(p_build)
    p_build.add_argument("--output-dir", default=None)
    p_build.add_argument("--dual", action="store_true", help="同时输出拼宽双语 PDF")
    p_build.add_argument(
        "--no-latex-bbox",
        action="store_false",
        dest="latex_bbox",
        default=True,
        help="关闭 LaTeX bbox 排版（默认开启）",
    )
    p_build.add_argument(
        "--latex-bbox-mode",
        choices=("full", "repair"),
        default=None,
        help="LaTeX bbox 资格模式：full（默认）/ repair",
    )
    p_build.add_argument("--render", default=None, help="重建后渲染指定页为 PNG，如 1,2 或 1-3")
    p_build.add_argument("--watermark", action="store_true")
    p_build.add_argument(
        "--no-stats",
        action="store_false",
        dest="stats",
        help="不附带 PDF 页数/目录/链接统计",
    )

    # ---- check ----------------------------------------------------------- #
    p_check = sub.add_parser(
        "check", help="结构性审查（占位：转调 review_document，聚合逻辑后续阶段实现）"
    )
    _add_workdir(p_check)
    p_check.add_argument("--mono", default=None, help="mono PDF 路径（默认自动查找）")
    p_check.add_argument("--dual", default=None)
    p_check.add_argument("--source-pdf", default=None, help="原文 PDF（默认取 state.pkl 内路径）")
    p_check.add_argument("--skip-pdf-checks", action="store_true")

    # ---- layout-set ------------------------------------------------------ #
    p_layout = sub.add_parser("layout-set", help="写入/合并段落级排版覆盖")
    _add_workdir(p_layout)
    p_layout.add_argument(
        "--patch",
        default=None,
        help='JSON 字符串，如 {"paragraphs": {"P05-012": {"scale_cap": 0.9}}}',
    )
    p_layout.add_argument("--reason", default=None, help="改动理由（写入 history）")
    p_layout.add_argument("--clear", action="store_true", help="清空全部覆盖")

    # ---- report ---------------------------------------------------------- #
    p_report = sub.add_parser("report", help="汇总产物 → FINAL_REPORT.md")
    _add_workdir(p_report)
    p_report.add_argument("--output-dir", default=None)
    p_report.add_argument("--title", default=None)
    p_report.add_argument("--notes", default=None, help="追加备注（Markdown）")

    # ---- run ------------------------------------------------------------- #
    p_run = sub.add_parser(
        "run",
        help="串联 parse→translate→apply→build→check→report（可 --from 续跑）",
        description=(
            "端到端编排：parse → translate → apply → build → check → report。"
            "阶段产物缺失会以 missing_artifact 错误退出；上游被改动会使下游阶段"
            "失效并要求从最早受影响阶段重跑。"
        ),
    )
    _add_workdir(p_run, required=False)
    p_run.add_argument(
        "pdf",
        nargs="?",
        default=None,
        help="源 PDF 路径（--from translate 及之后可省略）",
    )
    p_run.add_argument(
        "--from",
        dest="from_stage",
        choices=RUN_STAGES,
        default="parse",
        help="从指定阶段续跑（review 等价直通；缺省 parse）",
    )
    p_run.add_argument("--output-dir", default=None)
    # 各阶段透传旗标
    p_run.add_argument("--layout", choices=("mineru", "paddle"), default="mineru")
    p_run.add_argument("--mineru-token", default=None)
    p_run.add_argument("--mineru-json", default=None)
    p_run.add_argument("--mineru-cache-key", default=None)
    p_run.add_argument("--layout-coverage-threshold", type=float, default=0.005)
    p_run.add_argument("--mineru-ocr-text", action="store_true", dest="mineru_ocr_text")
    p_run.add_argument("--pages", default=None)
    p_run.add_argument("--lang-in", default="en")
    p_run.add_argument("--lang-out", default="zh")
    p_run.add_argument("--dual", action="store_true")
    p_run.add_argument(
        "--no-latex-bbox", action="store_false", dest="latex_bbox", default=True
    )
    p_run.add_argument("--latex-bbox-mode", choices=("full", "repair"), default=None)
    p_run.add_argument("--render", default=None, help="build 后渲染页，如 1,2 或 1-3")
    p_run.add_argument("--watermark", action="store_true")
    p_run.add_argument("--no-stats", action="store_false", dest="stats")
    p_run.add_argument("--skip-pdf-checks", action="store_true")
    p_run.add_argument("--source-pdf", default=None)
    # translate 相关
    p_run.add_argument("--ids", default=None, help="重译段落 id，逗号分隔")
    p_run.add_argument("--feedback", default=None)
    p_run.add_argument(
        "--markdown",
        default=None,
        help="已有译文 Markdown；'self' = 用 agent/document.md 自译（不调模型）",
    )
    p_run.add_argument("--prompt-only", action="store_true")
    p_run.add_argument("--model", default=None)
    p_run.add_argument("--effort", default=None)
    p_run.add_argument("--timeout", type=int, default=1800)
    p_run.add_argument(
        "--translator",
        default=None,
        help="翻译 provider（U3 定义语义；本版本仅接收并存进 run 配置）",
    )
    p_run.add_argument("--prompt", default=None)
    p_run.add_argument("--repair-prompt", default=None)
    p_run.add_argument(
        "--no-retry-missing", action="store_false", dest="retry_missing"
    )
    # report 相关
    p_run.add_argument("--title", default=None)
    p_run.add_argument("--notes", default=None)

    return parser


def _dispatch(args: argparse.Namespace) -> dict:
    command = args.command
    if command == "parse":
        return registry.invoke(
            parse.parse_document,
            pdf=args.pdf,
            workdir=args.workdir,
            layout=args.layout,
            pages=args.pages,
            lang_in=args.lang_in,
            lang_out=args.lang_out,
            mineru_token=args.mineru_token,
            mineru_json=args.mineru_json,
            mineru_cache_key=args.mineru_cache_key,
            layout_coverage_threshold=args.layout_coverage_threshold,
            mineru_use_ocr_text=args.mineru_ocr_text,
        )
    if command == "translate":
        return registry.invoke(
            translate.translate_document,
            workdir=args.workdir,
            ids=_split_ids(args.ids),
            feedback=args.feedback,
            markdown=args.markdown,
            prompt_only=args.prompt_only,
            model=args.model,
            effort=args.effort,
            timeout=args.timeout,
            command=args.translator_command,
            prompt=args.prompt,
            repair_prompt=args.repair_prompt,
            retry_missing=args.retry_missing,
        )
    if command == "apply":
        return registry.invoke(
            translate.apply_translation,
            workdir=args.workdir,
            markdown=args.markdown,
        )
    if command == "build":
        return registry.invoke(
            layout.build_pdf,
            workdir=args.workdir,
            output_dir=args.output_dir,
            dual=args.dual,
            watermark=args.watermark,
            latex_bbox=args.latex_bbox,
            latex_bbox_mode=args.latex_bbox_mode,
            render=args.render,
            stats=args.stats,
        )
    if command == "check":
        return registry.invoke(
            review.review_document,
            workdir=args.workdir,
            mono=args.mono,
            dual=args.dual,
            source_pdf=args.source_pdf,
            skip_pdf_checks=args.skip_pdf_checks,
        )
    if command == "layout-set":
        patch = None
        if args.patch:
            try:
                patch = json.loads(args.patch)
            except json.JSONDecodeError as exc:
                return registry.error_payload("invalid_patch", f"--patch 不是合法 JSON: {exc}")
            if not isinstance(patch, dict):
                return registry.error_payload("invalid_patch", "--patch 必须是 JSON 对象")
        return registry.invoke(
            layout.layout_set,
            workdir=args.workdir,
            patch=patch,
            reason=args.reason,
            clear=args.clear,
        )
    if command == "report":
        return registry.invoke(
            report.report,
            workdir=args.workdir,
            output_dir=args.output_dir,
            title=args.title,
            notes=args.notes,
        )
    if command == "run":
        # run 自己返回完整信封（含 data 摘要），不能再套 invoke。
        workdir = args.workdir
        if not workdir:
            return registry.error_payload(
                "workdir_missing", "run 需要 --workdir（如 --workdir tmp/my-paper）"
            )
        try:
            return run_tool.run_pipeline(
                workdir,
                args.pdf,
                from_stage=args.from_stage,
                output_dir=args.output_dir,
                layout_backend=args.layout,
                pages=args.pages,
                lang_in=args.lang_in,
                lang_out=args.lang_out,
                mineru_token=args.mineru_token,
                mineru_json=args.mineru_json,
                mineru_cache_key=args.mineru_cache_key,
                layout_coverage_threshold=args.layout_coverage_threshold,
                mineru_use_ocr_text=args.mineru_ocr_text,
                ids=_split_ids(args.ids),
                feedback=args.feedback,
                markdown=args.markdown,
                prompt_only=args.prompt_only,
                model=args.model,
                effort=args.effort,
                timeout=args.timeout,
                translator=args.translator,
                prompt=args.prompt,
                repair_prompt=args.repair_prompt,
                retry_missing=args.retry_missing,
                dual=args.dual,
                watermark=args.watermark,
                latex_bbox=args.latex_bbox,
                latex_bbox_mode=args.latex_bbox_mode,
                render=args.render,
                stats=args.stats,
                skip_pdf_checks=args.skip_pdf_checks,
                source_pdf=args.source_pdf,
                title=args.title,
                notes=args.notes,
            )
        except common.ToolError as exc:
            return registry.error_payload(exc.code, exc.message, **exc.extra)
        except Exception as exc:  # noqa: BLE001 - 保证 stdout 恒为单行 JSON
            import traceback

            return registry.error_payload(
                "tool_exception",
                f"{type(exc).__name__}: {exc}",
                traceback=traceback.format_exc(limit=8).splitlines()[-8:],
            )
    raise SystemExit(f"未知子命令: {command}")  # pragma: no cover


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    # 实现函数/第三方库的 print 一律走 stderr，保证 stdout 只有最终 JSON。
    with contextlib.redirect_stdout(sys.stderr):
        payload = _dispatch(args)
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
