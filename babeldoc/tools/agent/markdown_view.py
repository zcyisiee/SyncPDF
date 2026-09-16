"""Markdown 视图管线：整篇文档 → 连续英文 Markdown（带行内锚点）→ 译文无损写回 IR。

设计目标（相对 sheet.jsonl 分批协议）：
1. 翻译模型看到的是**一篇连续的论文**（标题/章节/图注/正文按阅读顺序），
   而不是 40 行一撮的 JSONL —— 术语与语气一致性和上下文理解都更好。
2. 排版信息不丢：富文本片段变成 ``[[S1]]...[[/S1]]`` 锚点，公式变成 ``[[F3]]``；
   锚点 1:1 映射回 BabelDOC 的 ``<style id='1'>``/``</style>``/``{v3}`` 协议，
   因此 reconstruct 仍能逐 span 还原字体/字号/公式图形。
3. 确定性段落 id（``P01-005``），跨运行可复现。
4. 文本层修复：断词连字符、缺失空格、标点后缺空格。

产物（均在 ``<workdir>/agent/``）：
- ``document.md``       给翻译模型的连续 Markdown
- ``anchors.json``      id → 源文/锚点明细（诊断用）
- ``sheet.jsonl``       与 extract 兼容的清单（id/source 为 canonical 形式）
- ``state.pkl``         与 extract 兼容的 IR 状态（供 apply/reconstruct；
                        含 LaTeX bbox 所需的 ``source_line_geometry``，
                        解析时无条件采集）
"""

from __future__ import annotations

import json
import logging
import os
import pickle
import re
from collections import Counter
from pathlib import Path

from babeldoc.format.pdf.document_il.utils.layout_helper import BULLET_POINT_PATTERN
from babeldoc.tools.agent import workflow
from babeldoc.tools.agent.translation_selection import SelectionContext
from babeldoc.tools.agent.translation_selection import normalize_label
from babeldoc.tools.agent.translation_selection import select_page_paragraphs

logger = logging.getLogger(__name__)

# 锚点：[[S1]] / [[/S1]] / [[F3]]（容忍模型写成 [[ S 1 ]]/[[/s1]]）
ANCHOR_RE = re.compile(r"\[\[\s*(/?)\s*([SFsf])\s*(\d+)?\s*\]\]")
# canonical 占位符
CANON_RE = re.compile(r"<style id='(\d+)'>|</style>|\{v(\d+)\}")
# Markdown 段落标记
# label 可能含空格（如 native 布局的 "plain text"）或斜杠，这里放宽到"不含 > 的任意字符"
ID_MARK_RE = re.compile(r"<!--\s*id\s*=\s*([A-Za-z0-9._-]+)\s*(?:label\s*=\s*([^>]*?))?\s*-->")
# 模型抄进段落正文的 HTML 注释（典型：文件头 <!-- babeldoc-markdown v1 -->）
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
HTML_COMMENT_DANGLING_RE = re.compile(r"<!--[^>]*$", re.DOTALL)
MD_HEADER = (
    "<!-- babeldoc-markdown v1 -->\n"
    "<!-- 这是一篇论文的完整正文（Markdown）。请翻译为简体中文。 -->\n"
    "<!-- 必须原样保留：样式锚点（双方括号 S+数字，开闭成对）、"
    "公式锚点（双方括号 F+数字）、以及每段上方的段落标记注释。 -->\n"
)


# --------------------------------------------------------------------------- #
# 文本层修复
# --------------------------------------------------------------------------- #
#: MinerU 回放（`--mineru-json`）时使用的占位 token：不发起 API 调用，仅满足构造签名。
_MINERU_REPLAY_ARG = "replay"

_WORDS: set[str] | None = None

_HYPHEN_RE = re.compile(r"([A-Za-z][A-Za-z0-9]*)[-\u2010\u2011]\s+([A-Za-z][A-Za-z']*)")

# 两个部分都是单词、但实际是断词粘连的常见词
_HYPHEN_EXCEPTIONS = {
    "mean-while": "meanwhile",
    "there-fore": "therefore",
    "how-ever": "however",
    "more-over": "moreover",
    "further-more": "furthermore",
    "never-theless": "nevertheless",
    "none-theless": "nonetheless",
    "al-though": "although",
    "where-as": "whereas",
    "there-by": "thereby",
    "there-in": "therein",
    "there-after": "thereafter",
    "notwith-standing": "notwithstanding",
    "some-times": "sometimes",
    "else-where": "elsewhere",
}

# 这些前缀与后面的词构成固定连字符复合词，应保留连字符
_PREFIX_KEEP = {
    "non", "opt", "pre", "post", "anti", "multi", "inter", "intra",
    "semi", "sub", "super", "ultra", "self", "cross", "micro", "macro",
    "meta", "pseudo", "quasi", "vice", "well", "high", "low", "long",
    "short", "mid", "top", "end", "all", "co",
}


def _load_words() -> set[str]:
    global _WORDS
    if _WORDS is None:
        words: set[str] = set()
        try:
            with Path("/usr/share/dict/words").open(
                encoding="utf-8", errors="ignore"
            ) as f:
                for line in f:
                    w = line.strip().lower()
                    if w.isalpha():
                        words.add(w)
        except OSError:
            pass
        _WORDS = words
    return _WORDS


def _join_hyphen(m: re.Match) -> str:
    """行末连字符：单词被断行时去连字符拼接；真复合词保留连字符。"""
    left, right = m.group(1), m.group(2)
    r_core = right.split("'")[0]
    key = f"{left.lower()}-{r_core.lower()}"
    if key in _HYPHEN_EXCEPTIONS:
        return _HYPHEN_EXCEPTIONS[key] + right[len(r_core) :]
    # 缩写/型号（ML-based、RQ3-based）：保留连字符
    if left[-1].isupper() or any(c.isdigit() for c in left):
        return f"{left}-{right}"
    if right[:1].isupper():
        return f"{left}-{right}"
    l_low, r_low = left.lower(), r_core.lower()
    # 拼接后是词典词 → 是断词（hidden / meanwhile / engineering ...）
    words = _load_words()
    if words and (l_low + r_low) in words:
        return f"{left}{right}"
    # 固定连字符前缀（non-neural / opt-out / pre-training ...）
    if l_low in _PREFIX_KEEP:
        return f"{left}-{right}"
    # 左侧是完整单词且右侧足够长 → 复合词（high-dimensional / compiler-producing）
    if words and l_low in words and len(l_low) >= 4 and len(r_low) >= 4:
        return f"{left}-{right}"
    return f"{left}{right}"  # inlin-ing -> inlining


def repair_text(s: str) -> str:
    """修复 PDF 文本层缺陷（只改空白/标点，不动锚点）。"""
    if not s:
        return s
    s = _HYPHEN_RE.sub(_join_hyphen, s)
    s = re.sub(r",([a-z])", r", \1", s)  # graphs,which
    s = re.sub(r";([a-z])", r"; \1", s)
    s = re.sub(r"([a-z]{2})\.([A-Z])", r"\1. \2", s)  # structures.These
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = s.replace("\n", " ")
    return s


# --------------------------------------------------------------------------- #
# canonical <-> 锚点
# --------------------------------------------------------------------------- #
def canonical_to_markdown(text: str) -> str:
    """canonical 占位符 → 短锚点，同时修复文本片段。"""
    out: list[str] = []
    pos = 0
    stack: list[str] = []
    for m in CANON_RE.finditer(text):
        out.append(repair_text(text[pos : m.start()]))
        if m.group(1):
            stack.append(m.group(1))
            out.append(f"[[S{m.group(1)}]]")
        elif m.group(2):
            out.append(f"[[F{m.group(2)}]]")
        else:
            sid = stack.pop() if stack else ""
            out.append(f"[[/S{sid}]]")
        pos = m.end()
    out.append(repair_text(text[pos:]))
    return "".join(out)


def markdown_to_canonical(text: str) -> str:
    """短锚点 → canonical 占位符。"""

    def rep(m: re.Match) -> str:
        slash, kind, num = m.group(1), m.group(2).upper(), m.group(3)
        if kind == "F":
            return f"{{v{num}}}"
        if slash:
            return "</style>"
        return f"<style id='{num}'>"

    return ANCHOR_RE.sub(rep, text)


def anchor_sequence(text: str) -> list[tuple[str, str, str]]:
    return [
        (m.group(1) or "", m.group(2).upper(), m.group(3) or "")
        for m in ANCHOR_RE.finditer(text)
    ]


def _split_anchors(text: str) -> tuple[list[tuple[str, str]], str]:
    """→ (tokens, plain)；tokens 为 ('a', anchor) / ('t', text)。"""
    tokens: list[tuple[str, str]] = []
    plain: list[str] = []
    pos = 0
    for m in ANCHOR_RE.finditer(text):
        if m.start() > pos:
            chunk = text[pos : m.start()]
            tokens.append(("t", chunk))
            plain.append(chunk)
        tokens.append(("a", m.group(0)))
        pos = m.end()
    if pos < len(text):
        chunk = text[pos:]
        tokens.append(("t", chunk))
        plain.append(chunk)
    return tokens, "".join(plain)


def _fix_empty_spans(s: str) -> str:
    """把 [[SN]][[/SN]]X 修成 [[SN]]X[[/SN]]（X 为普通字符）。"""
    pattern = re.compile(r"\[\[S(\d+)\]\]\[\[/S\1\]\]([^\s\[\]])")
    while True:
        m = pattern.search(s)
        if not m:
            return s
        s = (
            s[: m.start()]
            + f"[[S{m.group(1)}]]{m.group(2)}[[/S{m.group(1)}]]"
            + s[m.end() :]
        )


def repair_target(src_md: str, tgt_md: str) -> tuple[str, str]:
    """确定性地把译文的锚点修成协议合法形态（不调用模型）。

    模式 1（accepted）：锚点多重集与源文一致 —— **尊重模型语序**。中英翻译调整
    锚点位置是合法行为（"coordinates T rounds across S sub-agents" → "在 S 个子
    智能体间协调 T 轮"），按源文顺序回贴会把语义颠倒；这里只修空 span。
    模式 2（proportional）：锚点有增删/幻觉 —— 按源文各文本段长度占比，把源文
    锚点序列等比投放到译文上，保证协议合法。

    返回 ``(target, mode)``，``mode`` ∈ ``{"accepted", "proportional"}``。
    """
    if Counter(anchor_sequence(tgt_md)) == Counter(anchor_sequence(src_md)):
        return _fix_empty_spans(tgt_md), "accepted"

    src_tokens, src_plain = _split_anchors(src_md)
    _, plain = _split_anchors(tgt_md)
    total = max(1, len(src_plain))
    length = len(plain)
    out_tokens: list[str] = []
    cum = 0
    pos = 0
    for kind, val in src_tokens:
        if kind == "a":
            out_tokens.append(val)
            continue
        cum += len(val)
        target_pos = min(length, max(pos, round(cum / total * length)))
        out_tokens.append(plain[pos:target_pos])
        pos = target_pos
    out_tokens.append(plain[pos:])
    return _fix_empty_spans("".join(out_tokens)), "proportional"


# --------------------------------------------------------------------------- #
# 解析管线（与 workflow.extract 同源，但加确定性 id + Markdown 渲染）
# --------------------------------------------------------------------------- #
def _deterministic_ids(docs) -> None:
    """把随机 debug_id 换成 P<page>-<seq>（按页内顺序）。"""
    for page in docs.page:
        seq = 0
        for paragraph in page.pdf_paragraph:
            if not paragraph.debug_id:
                continue
            seq += 1
            paragraph.debug_id = f"P{page.page_number + 1:02d}-{seq:03d}"


def _resolve_mineru_json(mineru_json, mineru_cache_key):
    """解析回放路径：显式 --mineru-json 优先，否则按内容哈希找缓存。

    缓存 key 是 PDF 内容的 sha256，缓存目录与 MinerUDocLayoutModel 保持一致
    （~/.cache/babeldoc/mineru-layout.v1/）。缓存不存在时直接报错，
    避免静默回退到 API（可能无 token）。
    """
    if mineru_json:
        return str(mineru_json)
    if not mineru_cache_key:
        return None
    from babeldoc.docvision.mineru_doclayout import MinerUDocLayoutModel

    cache_path = MinerUDocLayoutModel.LAYOUT_CACHE_DIR / f"{mineru_cache_key}.json"
    if not cache_path.exists():
        raise ValueError(
            "MinerU 缓存未命中："
            f"{cache_path} 不存在。请提供 --mineru-json 或设置 MINERU_API_TOKEN "
            "让 MinerU 重新解析一次。"
        )
    return str(cache_path)


def _snapshot_bookmarks(pdf_path, workdir) -> dict:
    """把源 PDF 书签快照到 ``<workdir>/agent/source/bookmarks.json``。

    返回 ``write_bookmarks`` 的结果（``{"path", "count", "written"}``）；
    失败时返回 ``{"count": 0, "written": False}``，不阻断解析。
    """
    from babeldoc.tools.agent import link_snapshot

    try:
        return link_snapshot.write_bookmarks(
            pdf_path, link_snapshot.bookmarks_path(workflow.agent_dir(workdir))
        )
    except Exception:  # noqa: BLE001 - 快照是审计产物，不阻断解析
        logger.warning("书签快照失败", exc_info=True)
        return {"count": 0, "written": False}


def _snapshot_links(pdf_path, workdir, docs) -> dict:
    """快照超链接 → ``source/links.json`` + state 用的字符映射。

    返回 ``{"link_snapshot", "page_char_objects"}``（见 link_snapshot.build_link_state）。
    必须在 Typesetting 之前调用；失败时返回空状态，不阻断解析（此时重建阶段
    会跳过链接重映射，保持现状行为）。
    """
    from babeldoc.tools.agent import link_snapshot

    try:
        state = link_snapshot.build_link_state(pdf_path, docs)
        link_snapshot.write_links(
            state.get("link_snapshot", {}),
            link_snapshot.links_path(workflow.agent_dir(workdir)),
        )
        return state
    except Exception:  # noqa: BLE001 - 链接快照是审计产物，不阻断解析
        logger.warning("超链接快照失败", exc_info=True)
        return {"link_snapshot": {}, "page_char_objects": {}}


def _run_parse(
    pdf_path,
    workdir,
    lang_in,
    lang_out,
    layout,
    mineru_token,
    mineru_json,
    pages,
    mineru_language="en",
    mineru_cache_key=None,
    layout_coverage_threshold=0.005,
    mineru_use_ocr_text=False,
    recorder=None,
):
    from babeldoc.const import close_process_pool
    from babeldoc.format.pdf.document_il.midend.enclosed_marker_fixer import (
        EnclosedMarkerFixer,
    )
    from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator
    from babeldoc.format.pdf.document_il.midend.il_translator import (
        PageTranslateTracker,
    )
    from babeldoc.format.pdf.document_il.midend.inline_math_protector import (
        InlineMathProtector,
    )
    from babeldoc.format.pdf.document_il.midend.layout_parser import LayoutParser
    from babeldoc.format.pdf.document_il.midend.paragraph_finder import ParagraphFinder
    from babeldoc.format.pdf.document_il.midend.styles_and_formulas import (
        StylesAndFormulas,
    )
    from babeldoc.format.pdf.document_il.midend.toc_detector import TocDetector
    from babeldoc.format.pdf.new_parser.native_parse import (
        parse_prepared_pdf_with_new_parser_to_legacy_ir,
    )
    from babeldoc.tools.agent import debug_capture
    from babeldoc.tools.agent.sheet_translator import SheetProtocolTranslator

    workdir = Path(workdir)
    pdf_path = Path(pdf_path)
    # 诊断采集器（bdt --debug）：显式参数优先，缺省回落进程内上下文；
    # 深层 midend processor 经 config.debug_recorder 取（None = 关闭）。
    if recorder is None:
        from babeldoc import debug_recorder as _dr

        recorder = _dr.get_current()
    if recorder is not None:
        debug_capture.capture_input_pdf(recorder, pdf_path)
    if pages:
        pdf_path = workflow._trim_pages(
            pdf_path, workflow._parse_pages(pages), workdir / "trimmed.pdf"
        )

    config = workflow._base_config(pdf_path, workdir, lang_in, lang_out)
    config.debug_recorder = recorder
    from babeldoc.format.pdf.translation_config import TranslationConfig

    # --mineru-json 优先；否则按内容哈希解析 --mineru-cache-key。
    mineru_json = _resolve_mineru_json(mineru_json, mineru_cache_key)

    if layout == "paddle":
        from babeldoc.docvision.layout_selection import configure_paddle

        configure_paddle(config)
    elif mineru_json:
        from babeldoc.docvision.mineru_doclayout import MinerUDocLayoutModel

        os.environ["BABELDOC_MINERU_LAYOUT_JSON"] = str(mineru_json)
        config.doc_layout_model = MinerUDocLayoutModel(api_token=_MINERU_REPLAY_ARG)
    elif layout == "mineru":
        from babeldoc.docvision.mineru_doclayout import MinerUDocLayoutModel

        token = mineru_token or os.environ.get("MINERU_API_TOKEN")
        if not token:
            raise ValueError(
                "mineru 布局需要 --mineru-token / MINERU_API_TOKEN / --mineru-json"
            )
        config.doc_layout_model = MinerUDocLayoutModel(
            api_token=token, language=mineru_language
        )
    else:
        raise ValueError("layout 必须是 mineru 或 paddle")
    # provider IR 落到 <workdir>/agent/source/mineru/provider_ir.json
    config.provider_ir_dir = workflow.agent_dir(workdir)
    config.mineru_doclayout_enabled = layout == "mineru"
    config.mineru_use_ocr_text = bool(mineru_use_ocr_text)
    if layout == "mineru":
        config.mineru_skip_translate_effective_labels = (
            TranslationConfig.expand_mineru_skip_translate_layout_labels(
                TranslationConfig.get_mineru_default_skip_translate_layout_labels()
            )
        )
    config.layout_coverage_threshold = layout_coverage_threshold
    config.skip_scanned_detection = True

    doc_pdf, temp_pdf_path, mediabox_data = workflow._prepare_pdf(pdf_path, config)
    # 书签（outline）快照：解析阶段绑定源页，重建阶段据此写回 mono/dual。
    bookmark_result = _snapshot_bookmarks(temp_pdf_path, workdir)
    if recorder is not None:
        debug_capture.capture_pdf_prepared(
            recorder,
            temp_pdf_path,
            page_count=len(doc_pdf),
            trimmed=bool(pages),
        )
    docs = parse_prepared_pdf_with_new_parser_to_legacy_ir(
        temp_pdf_path, config=config, doc_pdf=doc_pdf
    )
    if recorder is not None:
        debug_capture.capture_page_frames(
            doc_pdf,
            recorder,
            original_pages=workflow._parse_pages(pages) if pages else None,
            mediabox_data=mediabox_data,
        )
        debug_capture.capture_native_chars(docs, recorder)
    try:
        docs = LayoutParser(config).process(docs, doc_pdf)
    except Exception as exc:
        # 覆盖率门禁等失败也要保留此前证据：layout 已赋到 docs.page，
        # coverage 报告在抛错前已落盘。
        if recorder is not None:
            debug_capture.capture_layout(docs, recorder, backend=layout, error=exc)
            debug_capture.capture_coverage(config, recorder)
            debug_capture.archive_provider_artifacts(
                recorder, workdir, backend=layout
            )
        raise
    if recorder is not None:
        debug_capture.capture_layout(docs, recorder, backend=layout)
        debug_capture.capture_coverage(config, recorder)
        debug_capture.archive_provider_artifacts(recorder, workdir, backend=layout)
    # 行内公式保护 + 原生字符↔MinerU span 对齐审计（需在 ParagraphFinder 之前：
    # 此时 page.pdf_character 仍是全量，且新 formula 区域会被 ParagraphFinder 采纳）。
    docs = InlineMathProtector(config).process(docs) or docs
    if recorder is not None:
        debug_capture.capture_inline_math(recorder, workdir)
    # 实验性高质量 OCR：在段落识别前安全回填字符文本，保留原生 bbox/样式。
    if config.mineru_use_ocr_text:
        from babeldoc.format.pdf.document_il.midend.provider_ocr import (
            ProviderOcrTextFusion,
        )

        docs = ProviderOcrTextFusion(config).process(docs) or docs
    if recorder is not None:
        debug_capture.capture_ocr_fusion(
            recorder, workdir, enabled=bool(config.mineru_use_ocr_text)
        )
    close_process_pool()
    marker_fixer = EnclosedMarkerFixer(config)
    docs = marker_fixer.process(docs)
    if recorder is not None:
        debug_capture.capture_enclosed_marker(marker_fixer, recorder)
    docs = ParagraphFinder(config).process(docs) or docs
    # 目录页条目化（需要段落结构；新段落要经过 StylesAndFormulas 的样式处理）。
    docs = TocDetector(config).process(docs) or docs
    if recorder is not None:
        debug_capture.capture_toc(recorder, workdir)
    docs = StylesAndFormulas(config).process(docs) or docs
    if recorder is not None:
        debug_capture.capture_styles_formulas(docs, recorder)

    _deterministic_ids(docs)
    if recorder is not None:
        debug_capture.capture_paragraphs(docs, recorder)

    # LaTeX bbox 源行几何（P3-0）：必须在译文回填之前采集——post_translate_paragraph
    # 会把 composition 换成纯文本 run，pdf_line 与源坐标随之丢失。md 路径是主协议，
    # 无条件采集（legacy extract 按 enable_latex_bbox_layout 门控（可关闭），
    # 依赖 reconstruct 的 page_char_objects 兜底；这里直接落精确几何，兜底仅在
    # 旧 workdir 复用时生效）。失败不阻断解析，reconstruct 仍有兜底路径。
    from babeldoc.format.pdf.document_il.backend.latex_bbox.source_geometry import (
        capture_source_line_geometry,
    )

    try:
        source_line_geometry = capture_source_line_geometry(docs)
    except Exception:  # noqa: BLE001 - 几何采集失败只影响 LaTeX 保真，有兜底
        logger.warning("源行几何采集失败", exc_info=True)
        source_line_geometry = {}
    if recorder is not None:
        debug_capture.capture_source_geometry(source_line_geometry, recorder)

    # 超链接快照：必须在 _deterministic_ids 之后（paragraph_ids 要拿确定性 id，
    # 与 reconstruct 阶段的段落对齐）、Typesetting 之前（字符 box 还是源坐标）。
    link_state = _snapshot_links(temp_pdf_path, workdir, docs)
    if recorder is not None:
        debug_capture.capture_links(
            link_state, bookmark_result, recorder, workdir
        )

    il_translator = ILTranslator(SheetProtocolTranslator(lang_in, lang_out, True), config)
    inputs = {}
    rows = []
    label_counts: dict[str, int] = {}
    skipped: dict[str, int] = {}
    skipped_rows: list[dict] = []
    selection_context = SelectionContext()
    for page in docs.page:
        page_font_map, page_xobj_font_map = workflow._page_font_maps(page)
        tracker = PageTranslateTracker()
        for paragraph, decision in select_page_paragraphs(page, selection_context):
            if not paragraph.debug_id:
                continue
            label = paragraph.layout_label or "text"
            if not decision.translate:
                skipped[label] = skipped.get(label, 0) + 1
                skipped_rows.append(
                    {
                        "id": paragraph.debug_id,
                        "page": page.page_number,
                        "layout_label": label,
                        "source": paragraph.unicode or "",
                        "reason": decision.reason or "protected",
                    }
                )
                continue
            workflow._bump_title_font_size(paragraph)
            text, translate_input = il_translator.pre_translate_paragraph(
                paragraph, tracker.new_paragraph(), page_font_map, page_xobj_font_map
            )
            if text is None:
                skipped[label] = skipped.get(label, 0) + 1
                continue
            inputs[paragraph.debug_id] = translate_input
            rows.append(
                {
                    "id": paragraph.debug_id,
                    "page": page.page_number,
                    "layout_label": label,
                    "source": text,
                }
            )
            label_counts[label] = label_counts.get(label, 0) + 1

    if recorder is not None:
        debug_capture.capture_selection(
            rows, skipped_rows, label_counts, skipped, recorder
        )

    return {
        "docs": docs,
        "inputs": inputs,
        "rows": rows,
        "label_counts": label_counts,
        "skipped_label_counts": skipped,
        "skipped_rows": skipped_rows,
        "link_state": link_state,
        "source_line_geometry": source_line_geometry,
        "temp_pdf_path": str(temp_pdf_path),
        "pdf_path": str(pdf_path),
        "lang_in": lang_in,
        "lang_out": lang_out,
        "mediabox_data": mediabox_data,
    }


def _label_token(label: str | None) -> str:
    """把 layout_label 规范成注释里安全的 token（空格/斜杠 → 下划线）。

    容器标签（如 native 布局的 "plain text"）含空格会让段落标记不可解析，
    这里复用 selection helper 的 ``normalize_label`` 统一折叠大小写/空格/斜杠，
    再把剩余不安全字符替换掉；解析侧仍兼容模型写回带空格的旧格式。
    """
    token = re.sub(r"[^A-Za-z0-9_-]+", "_", normalize_label(label))
    return token or "text"


# --------------------------------------------------------------------------- #
# Markdown 结构前缀（标题层级 / 列表 / 图注表注）
#
# 这些前缀是给翻译模型的**结构提示**：让模型感知「这是章节标题」「这是列表项」
# 「这是图注」，从而保持译文的段落层级与列表切分。回填时由
# ``_clean_markdown_body`` 剥掉，不进入译文 IR（composition 不变）。
# --------------------------------------------------------------------------- #
# 章节标题标签：MinerU 把论文标题与章节标题都映射成 ``title``，首个 ``title``
# 占 ``#``（最高级），其余用 ``##``；``paragraph_title`` 是小节，用 ``###``。
_TITLE_LEVEL1_LABELS = ("doc_title",)
_TITLE_LEVEL2_LABELS = ("title",)
_TITLE_LEVEL3_LABELS = ("paragraph_title",)
# 列表项渲染前缀
LIST_PREFIX = "- "
# 已经是列表行时（避免重复加前缀，也用于回填时判断前缀是不是我们加的）
_EXISTING_LIST_RE = re.compile(r"^[-*+]\s+")
# 段首这些字符是脚注/上标引用，不是列表标记（虽在 BULLET_POINT_PATTERN 里）
_FOOTNOTE_MARKER_CHARS = frozenset("†‡¶※·∗⁎")
_FOOTNOTE_MARKER_CHARS |= frozenset(
    ch
    for ch in "¹²³⁴⁵⁶⁷⁸⁹⁰₁₂₃₄₅₆₇₈₉₀ᵃᵇᶜᵈᵉᶠᵍʰⁱʲᵏˡᵐⁿᵒᵖʳˢᵗᵘᵛʷˣʸᶻ"
)


def _first_visible_char(markdown: str) -> str:
    """取正文首个可见字符，跳过前导样式/公式锚点（``[[S1]]`` / ``[[F3]]``）。"""
    rest = ANCHOR_RE.sub("", markdown.lstrip())
    return rest.lstrip()[:1]


def _is_list_item(markdown: str) -> bool:
    """段落是否应渲染成 Markdown 列表行（即我们会加 ``- `` 前缀）。

    判据：首个可见字符是项目符号。字符集合来自
    ``layout_helper.BULLET_POINT_PATTERN``（与排版阶段同一套口径，避免两处各维护
    一份字符表而漂移），但**排除脚注/上标类字符**（上标数字 ¹²³、†‡¶※·）——
    它们出现在段首时不是列表标记，而是脚注引用。

    已经以 ``- ``/``* ``/``+ `` 开头的行本来就有列表标记，不再加前缀。该函数的
    返回值同时用于回填阶段判断「那个 ``- `` 是不是我们加的」：只有我们加过前缀
    的段落，回填时才剥一层。
    """
    body = markdown.lstrip()
    if not body:
        return False
    if _EXISTING_LIST_RE.match(body):
        return False
    first = _first_visible_char(body)
    if not first:
        return False
    if first in _FOOTNOTE_MARKER_CHARS:
        return False
    # 圈号序数（①-⑳、ⓐ 等）在 CJK 字体里有字形，也是列表标记
    if 0x2460 <= ord(first) <= 0x24FF or 0x2776 <= ord(first) <= 0x2793:
        return True
    return bool(BULLET_POINT_PATTERN.match(first))


def _heading_prefix(label: str, h1_used: bool) -> str | None:
    """返回标题标签的 Markdown 前缀；非标题标签返回 None。

    ``h1_used`` 记录文档主标题（``#``）是否已被占用：MinerU 把论文标题与章节
    标题都标成 ``title``（``doc_title`` 在 MinerU 路径下不出现），首个命中者占
    ``#``，其余降为 ``##``；``paragraph_title`` 是小节，固定 ``###``。
    """
    if label in _TITLE_LEVEL1_LABELS or label in _TITLE_LEVEL2_LABELS:
        return "##" if h1_used else "#"
    if label in _TITLE_LEVEL3_LABELS:
        return "###"
    return None


def render_rows_markdown(rows, include_header: bool = True) -> str:
    """把行（id/layout_label/markdown）渲染成连续 Markdown。

    前缀规则（label → 形式）：

    ====================  ==============================
    label                  渲染
    ====================  ==============================
    ``doc_title``/首个 ``title``  ``# <正文>``
    其余 ``title``          ``## <正文>``
    ``paragraph_title``     ``### <正文>``
    ``figure_caption``      ``*<正文>*``
    ``table_caption``       ``**<正文>**``
    列表项（首字符是项目符号）  ``- <正文>``
    其余                    ``<正文>``
    ====================  ==============================

    ``toc_entry`` 按普通行渲染（条目本身是短行，无需前缀）。
    """
    lines: list[str] = []
    if include_header:
        lines.append(MD_HEADER.rstrip("\n"))
        lines.append("")
    h1_used = False
    for row in rows:
        label = row["layout_label"]
        body = row["markdown"]
        lines.append(f"<!-- id={row['id']} label={_label_token(label)} -->")
        normalized = normalize_label(label)
        prefix = _heading_prefix(normalized, h1_used)
        if prefix is not None:
            lines.append(f"{prefix} {body}")
            if prefix == "#":
                h1_used = True
        elif normalized == "figure_caption":
            lines.append(f"*{body}*")
        elif normalized == "table_caption":
            lines.append(f"**{body}**")
        elif _is_list_item(body):
            lines.append(f"{LIST_PREFIX}{body}")
        else:
            lines.append(body)
        lines.append("")
    return "\n".join(lines)


def _render_markdown(rows) -> str:
    return render_rows_markdown(
        [
            {
                "id": row["id"],
                "layout_label": row["layout_label"],
                "markdown": canonical_to_markdown(row["source"]),
            }
            for row in rows
        ]
    )


def extract_markdown(
    pdf_path,
    workdir,
    lang_in="en",
    lang_out="zh",
    layout="mineru",
    mineru_token=None,
    mineru_json=None,
    pages=None,
    mineru_language="en",
    mineru_cache_key=None,
    layout_coverage_threshold=0.005,
    mineru_use_ocr_text=False,
    recorder=None,
):
    """解析 PDF → 写 document.md / anchors.json / sheet.jsonl / state.pkl。"""
    workdir = Path(workdir)
    result = _run_parse(
        pdf_path,
        workdir,
        lang_in,
        lang_out,
        layout,
        mineru_token,
        mineru_json,
        pages,
        mineru_language=mineru_language,
        mineru_cache_key=mineru_cache_key,
        layout_coverage_threshold=layout_coverage_threshold,
        mineru_use_ocr_text=mineru_use_ocr_text,
        recorder=recorder,
    )
    agent = workflow.agent_dir(workdir)
    agent.mkdir(parents=True, exist_ok=True)

    rows = result["rows"]
    skipped_rows = result.get("skipped_rows", [])
    md = _render_markdown(rows)
    (agent / "document.md").write_text(md, encoding="utf-8")
    with (agent / "sheet.jsonl").open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    (agent / "anchors.json").write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "id": row["id"],
                        "page": row["page"],
                        "layout_label": row["layout_label"],
                        "canonical": row["source"],
                        "markdown": canonical_to_markdown(row["source"]),
                        "anchors": anchor_sequence(canonical_to_markdown(row["source"])),
                    }
                    for row in rows
                ],
                "skipped": skipped_rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    with workflow.state_path(workdir).open("wb") as f:
        pickle.dump(
            {
                "doc": result["docs"],
                "inputs": result["inputs"],
                "temp_pdf_path": result["temp_pdf_path"],
                "pdf_path": result["pdf_path"],
                "lang_in": lang_in,
                "lang_out": lang_out,
                "mediabox_data": result["mediabox_data"],
                "skipped_rows": skipped_rows,
                # 超链接映射状态：快照（链接 → 字符下标/段落 id）+ 字符对象列表
                # （与 char_indices 同序，pickle 后保持对象身份，重建阶段读新 box）。
                "link_snapshot": result.get("link_state", {}).get("link_snapshot", {}),
                "page_char_objects": result.get("link_state", {}).get(
                    "page_char_objects", {}
                ),
                # 源行几何（P3-0）：译文回填前采集，重建阶段直接复用
                # （reconstruct --latex-bbox 的首选来源）。
                "source_line_geometry": result.get("source_line_geometry", {}),
            },
            f,
        )
    return {
        "document_md": str(agent / "document.md"),
        "anchors_json": str(agent / "anchors.json"),
        "sheet": str(agent / "sheet.jsonl"),
        "paragraphs": len(rows),
        "chars": len(md),
        "label_counts": result["label_counts"],
        "skipped_label_counts": result["skipped_label_counts"],
        "skipped_rows": skipped_rows,
    }


# --------------------------------------------------------------------------- #
# 译文 Markdown → translated.jsonl（canonical）→ 写回 IR
# --------------------------------------------------------------------------- #
def strip_html_comments(text: str) -> str:
    """剔除 HTML 注释：模型会抄文件头注释，渲染后会变成可见乱码文本。"""
    if not text or "<!--" not in text:
        return text
    text = HTML_COMMENT_RE.sub(" ", text)
    return HTML_COMMENT_DANGLING_RE.sub(" ", text)


def _strip_caption_markers(body: str) -> str:
    """图注/表注的星号容错剥离与归一。

    期望形式：``figure_caption`` → ``*…*``；``table_caption`` → ``**…**``。
    模型常把两侧格式写错（只加单侧 `*`、或把图注写成 `**`），这里按期望形式
    归一：先剥两侧成对星号（任意层数），再剥单侧残留——两种 label 的最终正文
    都是无星号形式，因此不需要区分 label。
    """
    body = body.strip()
    if not body:
        return body
    # 成对剥离（不管写了多少层星号）
    if len(body) >= 2 and body.startswith("*") and body.endswith("*"):
        body = body.strip("*").strip()
    # 单侧残留（模型只加了一侧，或中文标点挤到星号之间）
    elif body.startswith("*"):
        body = body.lstrip("*").strip()
    elif body.endswith("*"):
        body = body.rstrip("*").strip()
    # 中间的孤立星号（如 `*x*` 被拆成 `*x *`）不影响正文，保留原文即可
    return body


def _clean_markdown_body(body: str, label: str, source_markdown: str | None = None) -> str:
    """剥掉渲染时加的 Markdown 结构前缀（标题/列表/图注表注），回到纯正文。

    前缀只是给翻译模型的结构提示，不能进入译文 IR（否则 `#`/`- ` 会被当成
    正文字符渲染）。这里按 label 期望形式剥离，并兼容模型自行添加/删减前缀。

    ``source_markdown`` 是源文渲染后的 Markdown（可选）：只有当渲染阶段给该
    段加过 ``- `` 前缀（即 ``_is_list_item(source_markdown)`` 为真）时，才把
    译文里对应的前缀剥掉；否则保留（可能是正文里真实的 ``- ``）。
    """
    body = strip_html_comments(body)
    body = body.strip()
    if not body:
        return body
    normalized = normalize_label(label)
    # 标题前缀：模型可能改写层级（`#`→`##`），一律剥到正文
    body = re.sub(r"^#{1,6}\s*", "", body)
    # 列表前缀：只剥一层，且只剥我们加过的那一层
    if normalized not in ("figure_caption", "table_caption") and _is_list_item(
        source_markdown or ""
    ):
        body = _EXISTING_LIST_RE.sub("", body, count=1)
    body = re.sub(r"\s*\n\s*", " ", body)  # 段内换行折成空格
    if normalized in ("figure_caption", "table_caption"):
        body = _strip_caption_markers(body)
    return body


def parse_translated_markdown(md_text: str) -> dict[str, tuple[str, str]]:
    """→ {id: (body, label)}。按 <!-- id=... --> 切块。"""
    out: dict[str, tuple[str, str]] = {}
    matches = list(ID_MARK_RE.finditer(md_text))
    for i, m in enumerate(matches):
        pid, label = m.group(1), m.group(2) or ""
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md_text)
        out[pid] = (md_text[start:end], label)
    return out


def missing_ids(workdir, md_text: str) -> list[str]:
    """返回译文中缺失的段落 id（供编排器重试）。"""
    with workflow.state_path(workdir).open("rb") as f:
        state = pickle.load(f)  # noqa: S301 - workdir 私有产物，非不可信输入
    parsed = parse_translated_markdown(md_text)
    return [pid for pid in state["inputs"] if pid not in parsed]


def render_retry_markdown(workdir, ids: list[str]) -> str:
    """只渲染缺失段落，作为补译提示词的输入文档。"""
    anchors = json.loads(
        (workflow.agent_dir(workdir) / "anchors.json").read_text(encoding="utf-8")
    )
    by_id = {row["id"]: row for row in anchors["rows"]}
    return render_rows_markdown([by_id[pid] for pid in ids if pid in by_id])


def apply_markdown(workdir, translated_md, *, debug_recorder=None):
    """校验译文 Markdown 并按锚点写回 IR。返回报告 dict。"""
    workdir = Path(workdir)
    agent = workflow.agent_dir(workdir)
    with workflow.state_path(workdir).open("rb") as f:
        state = pickle.load(f)  # noqa: S301 - workdir 私有产物，非不可信输入
    inputs = state["inputs"]
    anchors_meta = json.loads((agent / "anchors.json").read_text(encoding="utf-8"))
    labels = {r["id"]: r["layout_label"] for r in anchors_meta["rows"]}

    md_text = Path(translated_md).read_text(encoding="utf-8")
    parsed = parse_translated_markdown(md_text)

    extra = [pid for pid in parsed if pid not in inputs]

    violations: list[str] = []
    warnings: list[str] = []
    entries: list[dict] = []
    repaired: list[dict] = []
    fallback_ids: list[str] = []
    label_mismatches: list[dict] = []
    empty_ids: list[str] = []
    for pid, translate_input in inputs.items():
        if pid not in parsed:
            # 漏行回退原文（与旧 batch 协议一致，恒通过校验）
            fallback_ids.append(pid)
            entries.append({"id": pid, "target": translate_input.unicode})
            continue
        body, echoed_label = parsed[pid]
        source_markdown = canonical_to_markdown(translate_input.unicode)
        body = _clean_markdown_body(body, labels.get(pid, "text"), source_markdown)
        # 模型改写了段落标记里的 label（不影响回填，以源 label 为准）：记警告
        if echoed_label and normalize_label(echoed_label) != normalize_label(
            labels.get(pid, "text")
        ):
            label_mismatches.append(
                {
                    "id": pid,
                    "expected": normalize_label(labels.get(pid, "text")),
                    "got": normalize_label(echoed_label),
                }
            )
            warnings.append(
                f"label_mismatch: id {pid} expected "
                f"{normalize_label(labels.get(pid, 'text'))} got {normalize_label(echoed_label)}"
            )
        # 标记存在但正文为空：模型丢掉了该段译文 → 回退原文，不阻断重建
        if not body:
            empty_ids.append(pid)
            warnings.append(f"empty_translation: id {pid}")
            entries.append({"id": pid, "target": translate_input.unicode})
            continue
        src_md = source_markdown
        # 锚点多重集必须与源文一致；**顺序不强制**（模型按中文语序重排是合法翻译）
        src_seq = anchor_sequence(src_md)
        tgt_seq = anchor_sequence(body)
        has_empty = bool(
            re.search(r"\[\[S(\d+)\]\]\[\[/S\1\]\]", body)
        )
        multiset_match = Counter(tgt_seq) == Counter(src_seq)
        if multiset_match and src_seq != tgt_seq:
            # 不修复、不阻断，只记录真实发生率（跨 span 搬运需人工观察）
            warnings.append(f"anchor_reordered: id {pid}")
        if not multiset_match or has_empty:
            before_repair = body
            body, mode = repair_target(src_md, body)
            repaired.append({"id": pid, "mode": mode})
            if debug_recorder:
                debug_recorder.record_event("apply", "anchor_repair", {
                    "id": pid, "mode": mode, "source": src_md,
                    "before": before_repair, "after": body,
                    "source_anchors": src_seq, "target_anchors_before": tgt_seq,
                })
        # 修复后再校：多重集不一致才违规（顺序不再视为违规）
        if Counter(anchor_sequence(body)) != Counter(src_seq):
            violations.append(
                f"anchor_multiset_mismatch: id {pid} expected {len(src_seq)} anchors, "
                f"got {len(anchor_sequence(body))}"
            )
        target = markdown_to_canonical(body)
        # 空 span：模型省略了该片段（如英文冠词 The 无中文对应），不阻断重建
        for m in re.finditer(r"<style id='(\d+)'>(.*?)</style>", target, re.DOTALL):
            if not m.group(2).strip():
                warnings.append(f"empty_style_span: id {pid} style {m.group(1)}")
        entries.append({"id": pid, "target": target})

    if debug_recorder:
        from babeldoc.tools.agent import debug_capture

        debug_recorder.capture(
            "apply_validation", debug_capture.capture_apply_validation,
            debug_recorder, inputs, parsed, entries,
            extra_ids=extra, violations=violations, warnings=warnings,
            repaired=repaired, fallback_ids=fallback_ids, empty_ids=empty_ids,
            label_mismatches=label_mismatches, writeback_allowed=not (extra or violations),
        )
    if extra or violations:
        return {
            "ok": False,
            "missing_ids": fallback_ids,
            "extra_ids": extra,
            "violations": violations,
            "warnings": warnings,
            "parsed": len(parsed),
            "repaired": repaired,
            "label_mismatches": label_mismatches,
            "empty_ids": empty_ids,
        }

    sheet = agent / "translated.jsonl"
    with sheet.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    report = workflow.apply(
        workdir, str(sheet), **({"debug_recorder": debug_recorder} if debug_recorder else {})
    )
    report["markdown_sheet"] = str(sheet)
    report["repaired"] = repaired
    report["warnings"] = warnings
    report["fallback_ids"] = fallback_ids
    # 段落标记健壮性指标（不阻断）：label 被改写 / 正文为空的段落清单
    report["label_mismatches"] = label_mismatches
    report["empty_ids"] = empty_ids
    return report
