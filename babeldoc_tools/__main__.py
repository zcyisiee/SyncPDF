"""``bdt`` CLI：文档翻译链路的唯一入口（argparse 子命令）。

用法::

    bdt parse <pdf> --workdir tmp/wd [--layout mineru|paddle]
    bdt translate --workdir tmp/wd --translator scripts/agy-translator.sh
    bdt translate --workdir tmp/wd [--glossaries tmp/wd/glossary.csv]
    bdt translate --workdir tmp/wd [--ids P01-003] [--feedback "..."]
    bdt translate --workdir tmp/wd --markdown <已有译文.md>   # 不调命令
    bdt translate --workdir tmp/wd --prompt-only              # 只写 agent/prompt.md
    bdt apply --workdir tmp/wd [--markdown tmp/wd/agent/translated.md]
    bdt build --workdir tmp/wd [--output-dir tmp/wd/output] [--dual] [--render 1,2]
    bdt check --workdir tmp/wd [--skip-pdf-checks] [--strict]
    bdt layout-set --workdir tmp/wd --patch '{"paragraphs": {...}}'
    bdt report --workdir tmp/wd
    bdt run <pdf> --workdir tmp/wd [--from build] [--markdown self] \
        [--translator <cmd>] [--reviewer <cmd>] [--glossaries <csv>]
    bdt serve [--root <dir> | --workdir <dir>] [--host 127.0.0.1] [--port 0] [--open]

约定：

- stdout 恒为**单行 JSON**：``{"ok": true, "data": {...}}`` 或
  ``{"ok": false, "error": {"code", "message"}}``；
- 日志/进度/第三方库输出统一走 stderr；
- 退出码：0 = ok，1 = 失败（含可预期错误），2 = 用法错误（argparse）。
- 翻译/审查只有一种机制：被调命令从 stdin 读提示词、把结果写到 stdout。
  ``run`` 在 ``--prompt-only`` 时停在 translate；无 ``--reviewer`` 时停在 review
  并以 ``waiting_for_reviewer``（exit 1）结束——质量门禁不把"没人审查"当成功。
- ``bdt check`` 聚合结构审查 / 排版 lint / 链接审计；``--strict`` 时 verdict
  非 pass（含子项不可用）退出码 1，``bdt run`` 的 check 步用同一语义。
- ``bdt serve`` 是长驻 HTTP 服务（需 web extra），stdout 只在**绑定端口成功后**
  打印一次启动信封（含真实端口/URL），之后日志全部走 stderr。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import traceback

from babeldoc import debug_recorder as _dr

from babeldoc_tools import __version__
from babeldoc_tools import common
from babeldoc_tools import debug_replay
from babeldoc_tools import debug_runtime
from babeldoc_tools import debug_server
from babeldoc_tools import layout
from babeldoc_tools import parse
from babeldoc_tools import registry
from babeldoc_tools import report
from babeldoc_tools import review
from babeldoc_tools import run as run_tool
from babeldoc_tools import rust_backend
from babeldoc_tools import translate
from babeldoc_tools.serve import cli as serve_cli

#: ``run`` 的续跑起点；顺序与 :data:`babeldoc_tools.run.STAGES` 一致。
RUN_STAGES = run_tool.STAGES


def _split_ids(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _add_workdir(parser: argparse.ArgumentParser, *, required: bool = True) -> None:
    parser.add_argument("--workdir", required=required, help="工作目录（含 agent/ 产物）")


def _add_debug_flags(parser: argparse.ArgumentParser, *, recompile: bool = False) -> None:
    """管线子命令共用的 debug 开关（采集 + 本地查看器）。"""
    parser.add_argument(
        "--debug",
        action="store_true",
        help="开启全链路诊断采集（<workdir>/debug/runs/）并启动本地查看器",
    )
    parser.add_argument(
        "--debug-port",
        type=int,
        default=0,
        help="debug 查看器端口（默认 0 = 自动分配空闲端口；仅监听 127.0.0.1）",
    )
    parser.add_argument(
        "--debug-no-open",
        action="store_true",
        help="debug 模式不自动打开浏览器（URL 仍打印到 stderr）",
    )
    if recompile:
        parser.add_argument(
            "--debug-recompile",
            action="store_true",
            help="debug 模式专用：绕过 stamp 持久缓存读取，强制冷编译"
            "（需同时指定 --debug；与 --no-latex-bbox 互斥）",
        )


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
    p_parse.add_argument(
        "--mineru-language",
        default="en",
        help="MinerU 请求的 language 参数（如 en/zh）；默认 en（英语）",
    )
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
    _add_debug_flags(p_parse)

    # ---- translate ------------------------------------------------------- #
    p_translate = sub.add_parser("translate", help="整篇翻译 / 按 id 重译合并")
    _add_workdir(p_translate)
    p_translate.add_argument(
        "--translator",
        default=None,
        help=(
            "翻译命令（命令行字符串，如 scripts/agy-translator.sh）：从 stdin 读提示词、"
            "把译文写到 stdout。缺省读环境变量 BDT_TRANSLATOR"
        ),
    )
    p_translate.add_argument(
        "--ids",
        default=None,
        help="逗号分隔的段落 id（如 P01-003,P01-007）；给出则走重译合并路径",
    )
    p_translate.add_argument("--feedback", default=None, help="重译时给翻译命令的反馈")
    p_translate.add_argument(
        "--markdown",
        default=None,
        help="已有译文 Markdown 路径：直接导入，不调用任何命令",
    )
    p_translate.add_argument(
        "--prompt-only",
        action="store_true",
        help="只写 agent/prompt.md 后返回，不调用任何命令",
    )
    p_translate.add_argument("--timeout", type=int, default=1800, help="命令超时秒数")
    p_translate.add_argument(
        "--glossaries",
        default=None,
        help=(
            "术语表 CSV 路径（列 source,target[,note]）：整篇翻译的提示词带上术语约束段；"
            "按 --ids 重译不注入。缺省 = 不用词表"
        ),
    )
    p_translate.add_argument("--repair-prompt", default=None, help="重译提示词名")
    p_translate.add_argument(
        "--no-retry-missing",
        action="store_false",
        dest="retry_missing",
        help="缺失段落不自动补译",
    )
    _add_debug_flags(p_translate)

    # ---- apply ----------------------------------------------------------- #
    p_apply = sub.add_parser("apply", help="校验译文 Markdown 并写回 IR")
    _add_workdir(p_apply)
    p_apply.add_argument(
        "--markdown",
        default=None,
        help="译文 Markdown（默认 agent/translated.md）",
    )
    _add_debug_flags(p_apply)

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
    p_build.add_argument(
        "--no-latex-refine",
        action="store_false",
        dest="latex_refine",
        default=True,
        help="关闭编译后按译文版面扩框（默认开启；只影响被缩字段）",
    )
    p_build.add_argument(
        "--target-layout",
        action="store_true",
        dest="target_layout",
        default=None,
        help=(
            "强制对译文 PDF 跑一次版面识别（默认在布局后端为 mineru 且 "
            "MINERU_API_TOKEN 可用时自动执行）"
        ),
    )
    p_build.add_argument(
        "--no-target-layout",
        action="store_false",
        dest="target_layout",
        help="不对译文 PDF 做版面识别（不发网络请求；前端译文框将提示产物缺失）",
    )
    p_build.add_argument("--render", default=None, help="重建后渲染指定页为 PNG，如 1,2 或 1-3")
    p_build.add_argument("--watermark", action="store_true")
    p_build.add_argument(
        "--no-stats",
        action="store_false",
        dest="stats",
        help="不附带 PDF 页数/目录/链接统计",
    )
    _add_debug_flags(p_build, recompile=True)

    # ---- check ----------------------------------------------------------- #
    p_check = sub.add_parser(
        "check",
        help="三合一聚合：结构审查 + 排版 lint + 链接审计（--strict 时非 pass 退出码 1）",
    )
    _add_workdir(p_check)
    p_check.add_argument("--mono", default=None, help="mono PDF 路径（默认自动查找）")
    p_check.add_argument("--dual", default=None)
    p_check.add_argument("--source-pdf", default=None, help="原文 PDF（默认取 state.pkl 内路径）")
    p_check.add_argument("--skip-pdf-checks", action="store_true")
    p_check.add_argument(
        "--strict",
        action="store_true",
        help="verdict != pass（含子项 not_available 导致无法确认）时退出码置 1",
    )
    _add_debug_flags(p_check)

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
    _add_debug_flags(p_report)

    # ---- debug ------------------------------------------------------------ #
    p_debug = sub.add_parser(
        "debug",
        help="启动/复用本地 debug 查看器（只读归档），或 --stop 停止它",
        description=(
            "查看 <workdir>/debug/runs/ 下的诊断归档：默认打开最新 run，"
            "--run-id 指定历史 run。查看器只读，每个 workdir 一个进程，"
            "无活跃 pipeline 且无浏览器心跳 30 分钟后自动退出。"
        ),
    )
    _add_workdir(p_debug)
    p_debug.add_argument(
        "--port", type=int, default=0, help="查看器端口（默认 0 = 自动分配）"
    )
    p_debug.add_argument(
        "--no-open", action="store_true", help="不自动打开浏览器"
    )
    p_debug.add_argument("--run-id", default=None, help="要打开的 run_id")
    p_debug.add_argument(
        "--stop", action="store_true", help="只停止该 workdir 的查看器进程"
    )
    p_debug.add_argument(
        "--source-pdf",
        default=None,
        help="为旧 workdir 回放显式绑定源 PDF（写入 debug/bindings.json）",
    )
    p_debug.add_argument(
        "--mono",
        default=None,
        help="为旧 workdir 回放显式绑定 mono PDF（写入 debug/bindings.json）",
    )

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
    p_run.add_argument(
        "--mineru-language",
        default="en",
        help="MinerU 请求的 language 参数（如 en/zh）；默认 en（英语）",
    )
    p_run.add_argument("--mineru-json", default=None)
    p_run.add_argument("--mineru-cache-key", default=None)
    p_run.add_argument("--layout-coverage-threshold", type=float, default=0.005)
    p_run.add_argument("--mineru-ocr-text", action="store_true", dest="mineru_ocr_text")
    p_run.add_argument("--pages", default=None)
    p_run.add_argument(
        "--glossaries",
        default=None,
        help=(
            "术语表 CSV 路径（列 source,target[,note]）：from=parse/translate 时整篇翻译"
            "带上术语约束段；其它阶段/重译不注入。缺省 = 不用词表"
        ),
    )
    p_run.add_argument("--lang-in", default="en")
    p_run.add_argument("--lang-out", default="zh")
    p_run.add_argument("--dual", action="store_true")
    p_run.add_argument(
        "--no-latex-bbox", action="store_false", dest="latex_bbox", default=True
    )
    p_run.add_argument("--latex-bbox-mode", choices=("full", "repair"), default=None)
    p_run.add_argument(
        "--no-latex-refine",
        action="store_false",
        dest="latex_refine",
        default=True,
        help="关闭编译后按译文版面扩框（默认开启；只影响被缩字段）",
    )
    p_run.add_argument(
        "--target-layout",
        action="store_true",
        dest="target_layout",
        default=None,
        help=(
            "强制对译文 PDF 跑一次版面识别（默认在布局后端为 mineru 且 "
            "MINERU_API_TOKEN 可用时自动执行）"
        ),
    )
    p_run.add_argument(
        "--no-target-layout",
        action="store_false",
        dest="target_layout",
        help="不对译文 PDF 做版面识别（不发网络请求；前端译文框将提示产物缺失）",
    )
    p_run.add_argument("--render", default=None, help="build 后渲染页，如 1,2 或 1-3")
    p_run.add_argument("--watermark", action="store_true")
    p_run.add_argument("--no-stats", action="store_false", dest="stats")
    p_run.add_argument("--skip-pdf-checks", action="store_true")
    p_run.add_argument("--source-pdf", default=None)
    # translate 相关（透传给 translate）
    p_run.add_argument("--ids", default=None, help="重译段落 id，逗号分隔")
    p_run.add_argument("--feedback", default=None)
    p_run.add_argument(
        "--markdown",
        default=None,
        help="已有译文 Markdown；'self' = 用 agent/document.md 自译（不调命令）",
    )
    p_run.add_argument("--prompt-only", action="store_true")
    p_run.add_argument("--timeout", type=int, default=1800)
    p_run.add_argument(
        "--translator",
        default=None,
        help=(
            "翻译命令：stdin 读提示词、stdout 出译文（缺省读 BDT_TRANSLATOR）。"
            "给了 --markdown 或 --prompt-only 时不会调用它"
        ),
    )
    p_run.add_argument("--repair-prompt", default=None, help="重译提示词名（透传 translate）")
    p_run.add_argument(
        "--no-retry-missing", action="store_false", dest="retry_missing"
    )
    # review 相关
    p_run.add_argument(
        "--reviewer",
        default=None,
        help=(
            "审查命令：stdin 读审查提示词、stdout 必须是 JSON 对象"
            "（verdict: pass|needs_fix + findings）。缺省则停在 review 等待状态"
        ),
    )
    # report 相关
    p_run.add_argument("--title", default=None)
    p_run.add_argument("--notes", default=None)
    _add_debug_flags(p_run, recompile=True)

    p_harness = sub.add_parser("harness-call", help="Call a built-in harness: stdin prompt, stdout text")
    p_harness.add_argument("--profile", required=True)
    p_harness.add_argument("--thinking", default=None)

    p_model = sub.add_parser("model-call", help="Call a saved model: stdin prompt, stdout text")
    p_model.add_argument("--store-base", required=True)
    p_model.add_argument("--model-profile", required=True)
    p_run.add_argument("--skip-ai-review", action="store_true", help="Skip optional AI review; keep local checks")

    p_rust = sub.add_parser("rust-translate", help="用 Rust 后端和 pi 翻译 PDF")
    p_rust.add_argument("pdf", help="源 PDF 路径")
    p_rust.add_argument("--workdir", required=True, help="本次运行的全新产物目录")
    p_rust.add_argument("--pages", help="页子集，如 1-3 或 1,3,5")
    p_rust.add_argument("--model", default="deepseek/deepseek-flash")
    p_rust.add_argument("--thinking", default="low")
    p_rust.add_argument("--source-lang", default="auto")
    p_rust.add_argument("--target-lang", default="zh-CN")
    p_rust.add_argument("--layout-device", choices=("auto", "cpu", "coreml"), default="auto")
    p_rust.add_argument("--cached-from", help="仅重编译已有运行目录的译文缓存，不调用模型")
    p_rust.add_argument("--engine", help="syncpdf-cli 可执行文件；默认本仓库 release 构建")

    # ---- serve ----------------------------------------------------------- #
    # Web 前端入口；只 import 标准库 + store/schemas，缺 web extra 也能 --help。
    serve_cli.add_parser(sub)

    return parser


def _invoke_with_debug(args, *, config: dict, input_pdf=None, call) -> dict:
    """``--debug`` 包装：建 run 归档 → 起查看器 → ``call(recorder)`` → 收尾。

    ``call`` 是 ``recorder -> payload`` 的闭包（内部走 registry.invoke 或
    run_pipeline 自己的信封）。成功与失败都把 ``debug`` 块附到 ``data``；
    debug 基础设施失败（锁被占 / 归档不可写 / 查看器未就绪）直接返回错误
    信封且不执行 ``call``。
    """
    if not getattr(args, "debug", False):
        return call(None)
    session = debug_runtime.DebugSession(args.workdir)
    try:
        recorder = session.create_run(config=config, input_pdf=input_pdf)
    except common.ToolError as exc:
        return registry.error_payload(exc.code, exc.message, **exc.extra)
    try:
        viewer = session.start_viewer(
            port=getattr(args, "debug_port", 0),
            no_open=getattr(args, "debug_no_open", False),
            run_id=recorder.run_id,
        )
    except common.ToolError as exc:
        session.finish_run(recorder, _dr.STATUS_ERROR)
        return debug_runtime.attach_debug(
            registry.error_payload(exc.code, exc.message, **exc.extra),
            recorder,
            None,
        )
    status = _dr.STATUS_FINISHED
    try:
        payload = call(recorder)
        if not (isinstance(payload, dict) and payload.get("ok")):
            status = _dr.STATUS_ERROR
    except KeyboardInterrupt:
        status = _dr.STATUS_INTERRUPTED
        payload = registry.error_payload(
            "interrupted", "pipeline 被中断（KeyboardInterrupt），证据保留已采集部分"
        )
    except Exception as exc:  # noqa: BLE001 - 保证 stdout 恒为单行 JSON
        status = _dr.STATUS_ERROR
        payload = registry.error_payload(
            "tool_exception",
            f"{type(exc).__name__}: {exc}",
            traceback=traceback.format_exc(limit=8).splitlines()[-8:],
        )
    finally:
        session.finish_run(recorder, status)
    return debug_runtime.attach_debug(payload, recorder, viewer)


def _dispatch(args: argparse.Namespace) -> dict:
    command = args.command
    if command == "rust-translate":
        return rust_backend.translate_pdf(
            pdf=args.pdf,
            workdir=args.workdir,
            pages=args.pages,
            model=args.model,
            thinking=args.thinking,
            source_lang=args.source_lang,
            target_lang=args.target_lang,
            layout_device=args.layout_device,
            engine=args.engine,
            cached_from=args.cached_from,
        )
    if command == "parse":
        return _invoke_with_debug(
            args,
            config={
                "stages": ["parse"],
                "options": {
                    "layout": args.layout,
                    "pages": args.pages,
                    "lang_in": args.lang_in,
                    "lang_out": args.lang_out,
                    "mineru_language": args.mineru_language,
                    "mineru_json": bool(args.mineru_json),
                    "mineru_cache_key": bool(args.mineru_cache_key),
                    "layout_coverage_threshold": args.layout_coverage_threshold,
                    "mineru_ocr_text": args.mineru_ocr_text,
                },
            },
            input_pdf=args.pdf,
            call=lambda rec: registry.invoke(
                parse.parse_document,
                pdf=args.pdf,
                workdir=args.workdir,
                layout=args.layout,
                pages=args.pages,
                lang_in=args.lang_in,
                lang_out=args.lang_out,
                mineru_token=args.mineru_token,
                mineru_language=args.mineru_language,
                mineru_json=args.mineru_json,
                mineru_cache_key=args.mineru_cache_key,
                layout_coverage_threshold=args.layout_coverage_threshold,
                mineru_use_ocr_text=args.mineru_ocr_text,
                debug_recorder=rec,
            ),
        )
    if command == "translate":
        return _invoke_with_debug(
            args,
            config={
                "stages": ["translate"],
                "options": {
                    "ids": _split_ids(args.ids),
                    "markdown": args.markdown,
                    "prompt_only": args.prompt_only,
                    "translator": args.translator,
                    "timeout": args.timeout,
                    "retry_missing": args.retry_missing,
                    "glossaries": args.glossaries,
                },
            },
            call=lambda rec: registry.invoke(
                translate.translate_document,
                workdir=args.workdir,
                ids=_split_ids(args.ids),
                feedback=args.feedback,
                markdown=args.markdown,
                prompt_only=args.prompt_only,
                translator=args.translator,
                timeout=args.timeout,
                repair_prompt=args.repair_prompt,
                retry_missing=args.retry_missing,
                glossaries=args.glossaries,
                debug_recorder=rec,
            ),
        )
    if command == "apply":
        return _invoke_with_debug(
            args,
            config={"stages": ["apply"], "options": {"markdown": args.markdown}},
            call=lambda rec: registry.invoke(
                translate.apply_translation,
                workdir=args.workdir,
                markdown=args.markdown,
                debug_recorder=rec,
            ),
        )
    if command == "build":
        return _invoke_with_debug(
            args,
            config={
                "stages": ["build"],
                "options": {
                    "dual": args.dual,
                    "watermark": args.watermark,
                    "latex_bbox": args.latex_bbox,
                    "latex_bbox_mode": args.latex_bbox_mode,
                    "latex_refine": args.latex_refine,
                    "target_layout": args.target_layout,
                    "render": args.render,
                    "debug_recompile": args.debug_recompile,
                },
            },
            call=lambda rec: registry.invoke(
                layout.build_pdf,
                workdir=args.workdir,
                output_dir=args.output_dir,
                dual=args.dual,
                watermark=args.watermark,
                latex_bbox=args.latex_bbox,
                latex_bbox_mode=args.latex_bbox_mode,
                latex_refine=args.latex_refine,
                target_layout=args.target_layout,
                render=args.render,
                stats=args.stats,
                debug_recorder=rec,
                debug_recompile=args.debug_recompile,
            ),
        )
    if command == "check":
        return _invoke_with_debug(
            args,
            config={
                "stages": ["check"],
                "options": {
                    "skip_pdf_checks": args.skip_pdf_checks,
                    "strict": args.strict,
                },
            },
            call=lambda rec: registry.invoke(
                review.check_document,
                workdir=args.workdir,
                mono=args.mono,
                dual=args.dual,
                source_pdf=args.source_pdf,
                skip_pdf_checks=args.skip_pdf_checks,
                strict=args.strict,
                debug_recorder=rec,
            ),
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
        return _invoke_with_debug(
            args,
            config={
                "stages": ["report"],
                "options": {"title": args.title},
            },
            call=lambda rec: registry.invoke(
                report.report,
                workdir=args.workdir,
                output_dir=args.output_dir,
                title=args.title,
                notes=args.notes,
                debug_recorder=rec,
            ),
        )
    if command == "debug":
        return _debug_command(args)
    if command == "run":
        # run 自己返回完整信封（含 data 摘要），不能再套 invoke。
        workdir = args.workdir
        if not workdir:
            return registry.error_payload(
                "workdir_missing", "run 需要 --workdir（如 --workdir tmp/my-paper）"
            )
        start_index = RUN_STAGES.index(args.from_stage)

        def _run_call(rec):
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
                    mineru_language=args.mineru_language,
                    mineru_json=args.mineru_json,
                    mineru_cache_key=args.mineru_cache_key,
                    layout_coverage_threshold=args.layout_coverage_threshold,
                    mineru_use_ocr_text=args.mineru_ocr_text,
                    ids=_split_ids(args.ids),
                    feedback=args.feedback,
                    markdown=args.markdown,
                    prompt_only=args.prompt_only,
                    timeout=args.timeout,
                    translator=args.translator,
                    reviewer=args.reviewer,
                    skip_ai_review=args.skip_ai_review,
                    retry_missing=args.retry_missing,
                    glossaries=args.glossaries,
                    dual=args.dual,
                    watermark=args.watermark,
                    latex_bbox=args.latex_bbox,
                    latex_bbox_mode=args.latex_bbox_mode,
                    latex_refine=args.latex_refine,
                    target_layout=args.target_layout,
                    render=args.render,
                    stats=args.stats,
                    skip_pdf_checks=args.skip_pdf_checks,
                    source_pdf=args.source_pdf,
                    title=args.title,
                    notes=args.notes,
                    debug_recorder=rec,
                    debug_recompile=args.debug_recompile,
                )
            except common.ToolError as exc:
                return registry.error_payload(exc.code, exc.message, **exc.extra)
            except Exception as exc:  # noqa: BLE001 - 保证 stdout 恒为单行 JSON
                return registry.error_payload(
                    "tool_exception",
                    f"{type(exc).__name__}: {exc}",
                    traceback=traceback.format_exc(limit=8).splitlines()[-8:],
                )

        return _invoke_with_debug(
            args,
            config={
                "stages": list(RUN_STAGES[start_index:]),
                "options": {
                    "from_stage": args.from_stage,
                    "layout": args.layout,
                    "pages": args.pages,
                    "lang_in": args.lang_in,
                    "lang_out": args.lang_out,
                    "translator": args.translator,
                    "reviewer": args.reviewer,
                    "markdown": args.markdown,
                    "prompt_only": args.prompt_only,
                    "timeout": args.timeout,
                    "glossaries": args.glossaries,
                    "dual": args.dual,
                    "watermark": args.watermark,
                    "latex_bbox": args.latex_bbox,
                    "latex_bbox_mode": args.latex_bbox_mode,
                    "latex_refine": args.latex_refine,
                    "debug_recompile": args.debug_recompile,
                },
            },
            input_pdf=args.pdf,
            call=_run_call,
        )
    raise SystemExit(f"未知子命令: {command}")  # pragma: no cover


def _debug_command(args: argparse.Namespace) -> dict:
    """``bdt debug``：启动/复用查看器、显式 PDF 绑定、``--stop``、旧目录回放。"""
    session = debug_runtime.DebugSession(args.workdir)
    try:
        bindings_path = session.record_bindings(
            source_pdf=args.source_pdf, mono=args.mono
        )
        if args.stop:
            data = session.stop_viewer()
            if bindings_path:
                data["bindings"] = str(bindings_path)
            return {"ok": True, "data": data}
        replayed = None
        want_replay = args.run_id == "replay" or (
            args.run_id is None
            and debug_replay.replay_run_needed(args.workdir)
        )
        if want_replay:
            session.acquire_write_lock()
            try:
                replayed = debug_replay.build_replay_run(
                    args.workdir,
                    source_pdf=args.source_pdf,
                    mono=args.mono,
                )
            finally:
                session.release_write_lock()
            if replayed:
                sys.stderr.write(
                    "debug: 回放模式（来自旧 workdir 产物，证据不完整）\n"
                )
            elif args.run_id == "replay":
                return registry.error_payload(
                    "replay_unavailable",
                    "workdir 中没有可回放的 agent/ 历史产物",
                )
        run_id = (
            args.run_id or replayed or debug_runtime.latest_run_id(args.workdir)
        )
        info = session.start_viewer(
            port=args.port, no_open=args.no_open, run_id=run_id
        )
    except common.ToolError as exc:
        return registry.error_payload(exc.code, exc.message, **exc.extra)
    data = {
        "url": info["url"],
        "port": info["port"],
        "run_id": run_id,
        "reused": info["reused"],
    }
    if bindings_path:
        data["bindings"] = str(bindings_path)
    return {"ok": True, "data": data}


def _serve_internal(argv: list[str]) -> int:
    """隐藏参数入口：detached 查看器服务进程（不产出 stdout JSON）。"""
    internal = argparse.ArgumentParser(prog="bdt --debug-serve-internal")
    internal.add_argument("--debug-serve-internal", action="store_true")
    internal.add_argument("--workdir", required=True)
    internal.add_argument("--port", type=int, default=0)
    internal.add_argument("--token", required=True)
    ns = internal.parse_args(argv)
    return debug_server.serve(ns.workdir, ns.port, ns.token)


def _render_internal(argv: list[str]) -> int:
    """隐藏参数入口：PDF 渲染 worker（stdin/stdout 协议，见 debug_render）。"""
    internal = argparse.ArgumentParser(prog="bdt --debug-render-internal")
    internal.add_argument("--debug-render-internal", action="store_true")
    internal.parse_args(argv)
    from babeldoc_tools import debug_render

    return debug_render.render_worker()


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    # 隐藏的内部入口：detached 查看器服务进程 / PDF 渲染 worker
    # （不进公开 --help，不占子命令）。
    if "--debug-serve-internal" in argv:
        return _serve_internal(argv)
    if "--debug-render-internal" in argv:
        return _render_internal(argv)
    parser = _build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "debug_recompile", False):
        if not getattr(args, "debug", False):
            parser.error("--debug-recompile 需要同时指定 --debug")
        if getattr(args, "latex_bbox", True) is False:
            parser.error("--debug-recompile 与 --no-latex-bbox 互斥")
    if args.command == "harness-call":
        from babeldoc_tools.harnesses import call_harness

        try:
            def emit(text):
                sys.stdout.write(text)
                sys.stdout.flush()

            call_harness(args.profile, args.thinking, sys.stdin.read(), on_text=emit)
        except common.ToolError as exc:
            sys.stderr.write(exc.code + ": " + exc.message + "\n")
            return 1
        return 0
    if args.command == "model-call":
        from babeldoc_tools.serve.models import call_model

        try:
            text = call_model(args.store_base, args.model_profile, sys.stdin.read())
        except common.ToolError as exc:
            sys.stderr.write(exc.code + ": " + exc.message + "\n")
            return 1
        except Exception:
            sys.stderr.write("model_call_failed: Model call failed\n")
            return 1
        sys.stdout.write(text)
        return 0
    if args.command == "serve":
        # 长驻服务：自己写启动信封（绑定端口后才知道真实 URL），不走单行 JSON 收尾。
        return serve_cli.run(args)
    # 实现函数/第三方库的 print 一律走 stderr，保证 stdout 只有最终 JSON。
    with contextlib.redirect_stdout(sys.stderr):
        payload = _dispatch(args)
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    if payload.get("ok"):
        # ``check --strict``：verdict != pass（含子项 not_available 导致的无法确认）
        # 时退出码置 1，供 CI / run 做质量门禁；默认仍返回 0 让审查 Agent 读 JSON。
        if getattr(args, "strict", False) and args.command == "check":
            if ((payload.get("data") or {}).get("verdict")) != "pass":
                return 1
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
