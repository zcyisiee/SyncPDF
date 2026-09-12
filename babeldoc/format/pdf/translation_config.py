import enum
import logging
import shutil
import tempfile
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from babeldoc.const import CACHE_FOLDER
from babeldoc.format.pdf.split_manager import BaseSplitStrategy
from babeldoc.format.pdf.split_manager import PageCountStrategy
from babeldoc.glossary import Glossary
from babeldoc.glossary import GlossaryEntry
from babeldoc.progress_monitor import ProgressMonitor
from babeldoc.translator.translator import BaseTranslator

logger = logging.getLogger(__name__)


class WatermarkOutputMode(enum.Enum):
    Watermarked = "watermarked"
    NoWatermark = "no_watermark"
    Both = "both"


@dataclass(frozen=True, slots=True)
class TitleContextSnapshot:
    debug_id: str | None
    unicode: str | None
    layout_label: str | None = None


class SharedContextCrossSplitPart:
    def __init__(self):
        self.first_paragraph: TitleContextSnapshot | None = None
        self.recent_title_paragraph: TitleContextSnapshot | None = None
        self._lock = threading.Lock()
        self.user_glossaries: list[Glossary] = []
        self.auto_extracted_glossary: Glossary | None = None
        self.raw_extracted_terms: list[tuple[str, str]] = []
        self.auto_enabled_ocr_workaround = False
        # Statistics for valid characters/text across the whole file
        self.valid_char_count_total: int = 0
        self.total_valid_text_token_count: int = 0

    def snapshot_title_paragraph(self, paragraph) -> TitleContextSnapshot | None:
        if paragraph is None:
            return None
        return TitleContextSnapshot(
            debug_id=getattr(paragraph, "debug_id", None),
            unicode=getattr(paragraph, "unicode", None),
            layout_label=getattr(paragraph, "layout_label", None),
        )

    def initialize_glossaries(self, initial_glossaries: list[Glossary] | None):
        with self._lock:
            self.user_glossaries = (
                list(initial_glossaries) if initial_glossaries else []
            )
            self.auto_extracted_glossary = None
            self.raw_extracted_terms = []
            self.unique_name = self._generate_unique_auto_glossary_name()
            self.norm_terms = set()
            for g in self.user_glossaries:
                for entity in g.normalized_lookup:
                    self.norm_terms.add(entity)
            # reset statistics buffer when initializing
            self.valid_char_count_total = 0
            self.total_valid_text_token_count = 0

    def add_raw_extracted_term_pair(self, src: str, tgt: str):
        with self._lock:
            self.raw_extracted_terms.append((src, tgt))

    def _generate_unique_auto_glossary_name(self) -> str:
        base_name = "auto_extracted_glossary"
        current_name = base_name
        suffix = 0
        existing_names = {g.name for g in self.user_glossaries}
        if (
            self.auto_extracted_glossary
            and self.auto_extracted_glossary.name == current_name
        ):
            pass

        while current_name in existing_names:
            suffix += 1
            current_name = f"{base_name}#{suffix}"
        return current_name

    def contains_term(self, term: str) -> bool:
        with self._lock:
            try:
                return term in self.norm_terms
            except Exception:
                return False

    def finalize_auto_extracted_glossary(self):
        with self._lock:
            self.auto_extracted_glossary = None

            if not self.raw_extracted_terms:
                self.raw_extracted_terms = []
                return

            term_translations: dict[str, list[str]] = {}
            for src, tgt in self.raw_extracted_terms:
                term_translations.setdefault(src, []).append(tgt)

            final_entries: list[GlossaryEntry] = []
            for src, tgts in term_translations.items():
                if not tgts:
                    continue
                most_common_tgt = Counter(tgts).most_common(1)[0][0]
                final_entries.append(GlossaryEntry(src, most_common_tgt))

            if final_entries:
                self.auto_extracted_glossary = Glossary(
                    name=self.unique_name, entries=final_entries
                )

    def get_glossaries(self) -> list[Glossary]:
        with self._lock:
            all_glossaries = list(self.user_glossaries)
            if self.auto_extracted_glossary:
                all_glossaries.append(self.auto_extracted_glossary)
            return all_glossaries

    def get_glossaries_for_translation(
        self, auto_extract_enabled: bool
    ) -> list[Glossary]:
        with self._lock:
            if auto_extract_enabled and self.auto_extracted_glossary:
                return [self.auto_extracted_glossary]
            else:
                all_glossaries = list(self.user_glossaries)
                if self.auto_extracted_glossary:
                    all_glossaries.append(self.auto_extracted_glossary)
                return all_glossaries

    def add_valid_counts(self, char_count: int, token_count: int):
        """Accumulate valid character and token counts in a threadsafe way."""
        if char_count <= 0 and token_count <= 0:
            return
        with self._lock:
            if char_count > 0:
                self.valid_char_count_total += char_count
            if token_count > 0:
                self.total_valid_text_token_count += token_count


class TranslationConfig:
    # 别名展开刻意不包含 *_caption（标题型图注/表注/代码注默认要翻译）；
    # 如需跳过 caption，必须显式传入细粒度标签（如 "table_caption"）。
    MINERU_SKIP_TRANSLATE_ALIAS_MAP = {
        "reference": ("reference",),
        "table": ("table_text", "table_footnote"),
        "image": ("figure", "figure_text"),
        "code": ("code",),
        "author": ("author",),
        "header": ("header",),
        "footer": ("footer",),
        "page_number": ("page_number",),
        "page_footnote": ("page_footnote",),
        "aside_text": ("aside_text",),
    }
    MINERU_DEFAULT_SKIP_TRANSLATE_LAYOUT_LABELS = (
        "reference",
        "table",
        "image",
        "code",
        "header",
        "footer",
        "page_number",
        "page_footnote",
        "aside_text",
        "author",
        # 目录条目的页码/引导线段：位置由程序保持，不进翻译。
        "toc_entry_page",
    )

    @classmethod
    def get_mineru_default_skip_translate_layout_labels(cls) -> tuple[str, ...]:
        return cls.MINERU_DEFAULT_SKIP_TRANSLATE_LAYOUT_LABELS

    @classmethod
    def expand_mineru_skip_translate_layout_labels(cls, labels):
        out = set()
        for label in labels or ():
            out.update(
                cls.MINERU_SKIP_TRANSLATE_ALIAS_MAP.get(
                    str(label).strip().lower(), (str(label).strip().lower(),)
                )
            )
        return frozenset(x for x in out if x)

    def should_skip_translate_layout_label(self, layout_label):
        return bool(
            self.mineru_doclayout_enabled
            and str(layout_label or "").strip().lower()
            in self.mineru_skip_translate_effective_labels
        )

    @staticmethod
    def create_max_pages_per_part_split_strategy(max_pages_per_part: int):
        return PageCountStrategy(max_pages_per_part)

    # for backward compatibility,
    # new parameters should be added at the end of the function.
    def __init__(
        self,
        translator: BaseTranslator | None = None,
        input_file: str | Path = "",
        lang_in: str = "",
        lang_out: str = "",
        doc_layout_model=None,  # DocLayoutModel
        # for backward compatibility
        font: str | Path | None = None,
        pages: str | None = None,
        output_dir: str | Path | None = None,
        debug: bool = False,
        working_dir: str | Path | None = None,
        no_dual: bool = False,
        no_mono: bool = False,
        formular_font_pattern: str | None = None,
        formular_char_pattern: str | None = None,
        qps: int = 1,
        split_short_lines: bool = False,
        short_line_split_factor: float = 0.8,
        use_rich_pbar: bool = True,
        progress_monitor: ProgressMonitor | None = None,
        skip_clean: bool = False,
        dual_translate_first: bool = False,
        disable_rich_text_translate: bool = False,
        enhance_compatibility: bool = False,
        report_interval: float = 0.1,
        min_text_length: int = 5,
        use_side_by_side_dual: bool = True,  # Deprecated: 是否使用拼版式双语 PDF（并排显示原文和译文）向下兼容选项，已停用。
        use_alternating_pages_dual: bool = False,
        watermark_output_mode: WatermarkOutputMode = WatermarkOutputMode.Watermarked,
        # Add split-related parameters
        split_strategy: BaseSplitStrategy | None = None,
        table_model=None,
        show_char_box: bool = False,
        skip_scanned_detection: bool = False,
        ocr_workaround: bool = False,
        custom_system_prompt: str | None = None,
        add_formula_placehold_hint: bool = False,
        glossaries: list[Glossary] | None = None,
        pool_max_workers: int | None = None,
        auto_extract_glossary: bool = True,
        auto_enable_ocr_workaround: bool = False,
        primary_font_family: str | None = None,
        only_include_translated_page: bool | None = False,
        save_auto_extracted_glossary: bool = True,
        enable_graphic_element_process: bool = True,
        merge_alternating_line_numbers: bool = True,
        skip_translation: bool = False,
        skip_form_render: bool = False,
        skip_curve_render: bool = False,
        only_parse_generate_pdf: bool = False,
        remove_non_formula_lines: bool = False,
        non_formula_line_iou_threshold: float = 0.9,
        figure_table_protection_threshold: float = 0.9,
        skip_formula_offset_calculation: bool = False,
        term_extraction_translator: BaseTranslator | None = None,
        metadata_extra_data: str | None = None,
        term_pool_max_workers: int | None = None,
        disable_same_text_fallback: bool = False,
        mineru_doclayout_enabled: bool = False,
        mineru_skip_translate_layout_labels=None,
        layout_coverage_threshold: float = 0.005,
        # LaTeX bbox 排版（实验特性，默认关闭）：
        # 开启后在 generate 阶段对满足条件的段落用 XeLaTeX 在原 bbox 内
        # 重新排版并贴回；任何前置能力缺失/编译失败都会回退现有渲染路径。
        enable_latex_bbox_layout: bool = False,
        latex_xelatex_path: str | None = None,
        latex_cjk_font_path: str | None = None,
        latex_compile_timeout_seconds: float = 45.0,
        latex_max_compile_workers: int = 2,
        latex_fallback_policy: str = "fallback",
        latex_min_line_fill: float = 0.85,
    ):
        self.translator = translator
        self.term_extraction_translator = term_extraction_translator or translator
        initial_user_glossaries = list(glossaries) if glossaries else []

        self.input_file = input_file
        self.lang_in = lang_in
        self.lang_out = lang_out
        # just ignore font
        self.font = None

        self.pages = pages
        self.page_ranges = self.parse_pages(pages) if pages else None
        self.debug = debug
        self.watermark_output_mode = watermark_output_mode

        self.output_dir = output_dir
        self.working_dir = working_dir
        self.no_dual = no_dual
        self.no_mono = no_mono

        self.formular_font_pattern = formular_font_pattern
        self.formular_char_pattern = formular_char_pattern
        self.qps = qps
        # Set pool_max_workers with default value from qps
        self.pool_max_workers = (
            pool_max_workers if pool_max_workers is not None else qps
        )
        # Set term_pool_max_workers for automatic term extraction.
        # If not provided, default to pool_max_workers.
        self.term_pool_max_workers = (
            term_pool_max_workers
            if term_pool_max_workers is not None
            else self.pool_max_workers
        )
        self.split_short_lines = split_short_lines

        self.short_line_split_factor = short_line_split_factor
        self.use_rich_pbar = use_rich_pbar
        self.progress_monitor = progress_monitor
        self.doc_layout_model = doc_layout_model
        self.mineru_doclayout_enabled = mineru_doclayout_enabled
        # 布局覆盖率门禁阈值：未命中任何 layout 区域的原生字符占比上限。
        # 本地 ONNX 后端与字符聚类兜底（fallback_line）已移除，超阈值即明确失败。
        self.layout_coverage_threshold = layout_coverage_threshold
        # MinerU 是唯一布局后端；跳过翻译的标签集合固定，与后端选择无关。
        labels = (
            mineru_skip_translate_layout_labels
            if mineru_skip_translate_layout_labels is not None
            else self.MINERU_DEFAULT_SKIP_TRANSLATE_LAYOUT_LABELS
        )
        self.mineru_skip_translate_effective_labels = (
            self.expand_mineru_skip_translate_layout_labels(labels)
        )

        self.skip_clean = skip_clean or enhance_compatibility
        self.skip_scanned_detection = skip_scanned_detection

        self.dual_translate_first = dual_translate_first or enhance_compatibility
        self.disable_rich_text_translate = (
            disable_rich_text_translate or enhance_compatibility
        )

        self.report_interval = report_interval
        self.min_text_length = min_text_length
        self.use_alternating_pages_dual = use_alternating_pages_dual
        self.ocr_workaround = ocr_workaround
        self.merge_alternating_line_numbers = merge_alternating_line_numbers

        if self.ocr_workaround:
            self.skip_scanned_detection = True
            self.disable_rich_text_translate = True

        # for backward compatibility
        if use_side_by_side_dual is False and use_alternating_pages_dual is False:
            self.use_alternating_pages_dual = True

        if progress_monitor and progress_monitor.cancel_event is None:
            progress_monitor.cancel_event = threading.Event()

        if working_dir is None:
            if debug:
                working_dir = Path(CACHE_FOLDER) / "working" / Path(input_file).stem
                self._is_temp_dir = False
            else:
                working_dir = tempfile.mkdtemp()
                self._is_temp_dir = True
        else:
            working_dir = Path(working_dir) / Path(input_file).stem
            self._is_temp_dir = False

        self.working_dir = working_dir

        Path(working_dir).mkdir(parents=True, exist_ok=True)

        if output_dir is None:
            output_dir = Path.cwd()
        self.output_dir = output_dir

        Path(output_dir).mkdir(parents=True, exist_ok=True)

        if not doc_layout_model:
            # 本地 ONNX 后端已移除：必须显式提供布局模型（MinerU 或 RPC 网关）。
            raise ValueError(
                "未提供布局模型：本地 ONNX 后端（--layout native）已移除，"
                "请使用 MinerU 布局（--mineru-doclayout，需 --mineru-api-token / "
                "MINERU_API_TOKEN）或 RPC 网关（--rpc-doclayout*）。"
            )
        self.doc_layout_model = doc_layout_model

        self.shared_context_cross_split_part = SharedContextCrossSplitPart()
        self.shared_context_cross_split_part.initialize_glossaries(
            initial_user_glossaries
        )

        # Initialize split-related attributes
        self.split_strategy = split_strategy

        # Create a unique working directory for each part
        self._part_working_dirs: dict[int, Path] = {}
        self._part_output_dirs: dict[int, Path] = {}

        if table_model is not None:
            logger.warning(
                "TranslationConfig.table_model is deprecated and ignored; "
                "RapidOCR table-text detection has been retired."
            )
        self.table_model = None
        self.show_char_box = show_char_box
        self.custom_system_prompt = custom_system_prompt
        self.add_formula_placehold_hint = add_formula_placehold_hint
        self.auto_extract_glossary = auto_extract_glossary
        self.auto_enable_ocr_workaround = auto_enable_ocr_workaround
        self.skip_translation = skip_translation
        self.only_parse_generate_pdf = only_parse_generate_pdf

        if self.skip_translation or self.only_parse_generate_pdf:
            self.auto_extract_glossary = False

        if auto_enable_ocr_workaround:
            self.ocr_workaround = False
            self.skip_scanned_detection = False

        assert primary_font_family in [
            None,
            "serif",
            "sans-serif",
            "script",
        ]
        self.primary_font_family = primary_font_family

        if only_include_translated_page is None:
            only_include_translated_page = False

        self.only_include_translated_page = only_include_translated_page

        self.save_auto_extracted_glossary = save_auto_extracted_glossary

        self.enable_graphic_element_process = enable_graphic_element_process
        self.skip_form_render = skip_form_render
        self.skip_curve_render = skip_curve_render
        self.remove_non_formula_lines = remove_non_formula_lines
        self.non_formula_line_iou_threshold = non_formula_line_iou_threshold
        self.figure_table_protection_threshold = figure_table_protection_threshold
        self.skip_formula_offset_calculation = skip_formula_offset_calculation

        self.metadata_extra_data = metadata_extra_data

        self.term_extraction_token_usage: dict[str, int] = {
            "total_tokens": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cache_hit_prompt_tokens": 0,
        }
        self.disable_same_text_fallback = disable_same_text_fallback

        # agent 排版微调通道（由 babeldoc.tools.agent.layout_overrides 注入）：
        # 默认全空 = 不改变任何现有行为。
        # paragraph_layout_overrides: {debug_id: {scale_cap/font_scale/line_skip/
        #   force_break_after_text/force_break_after_offset/...}}
        self.paragraph_layout_overrides: dict = {}
        # page_font_scale: {1-based 页码: 字号乘数}
        self.page_font_scale: dict[int, float] = {}
        # 文档级行距覆盖（None = 用 Typesetting 内置默认值）
        self.line_skip_override: float | None = None
        # Typesetting 阶段记录的非致命警告（如强制换行锚点解析失败）
        self.layout_warnings: list[str] = []
        # agent 产物根目录（<workdir>/agent）：layout provider 把 provider IR 等
        # 结构化中间产物落盘到其下（如 source/mineru/provider_ir.json）；
        # None = 由 provider 自行决定（回退到 working_dir）。
        self.provider_ir_dir: Path | None = None

        # LaTeX bbox 排版配置（默认关闭；能力探测失败只告警并回退）。
        self.enable_latex_bbox_layout = enable_latex_bbox_layout
        self.latex_xelatex_path = latex_xelatex_path
        self.latex_cjk_font_path = latex_cjk_font_path
        self.latex_compile_timeout_seconds = float(latex_compile_timeout_seconds)
        self.latex_max_compile_workers = max(1, int(latex_max_compile_workers))
        if latex_fallback_policy != "fallback":
            logger.warning(
                "未知的 latex_fallback_policy=%s，按 fallback 处理",
                latex_fallback_policy,
            )
        self.latex_fallback_policy = "fallback"
        self.latex_min_line_fill = float(latex_min_line_fill)
        # LaTeX overlay 的源几何快照（capture_layout_sources 在 Typesetting
        # 之前写入）：paragraphs={debug_id: {page/box/font_size/...}}、
        # formula_source_boxes={page_number: {id(formula): box}}。
        # 公式对象按对象身份（id()）对应——Typesetting 原地重定位公式对象但
        # 保持身份，与 link_remap 的字符身份机制同一假设。
        self.latex_bbox_state: dict = {}
        # LaTeX bbox overlay 的统计（PDFCreater 写入）：
        # attempted/applied/fallback/failed + 原因/耗时/字号缩放明细。
        self.latex_bbox_stats: dict = {}

        if self.ocr_workaround:
            self.remove_non_formula_lines = False

    def parse_pages(self, pages_str: str | None) -> list[tuple[int, int]] | None:
        """解析页码字符串，返回页码范围列表

        Args:
            pages_str: 形如 "1-,2,-3,4" 的页码字符串

        Returns:
            包含 (start, end) 元组的列表，其中 -1 表示无限制
        """
        if not pages_str:
            return None

        ranges: list[tuple[int, int]] = []
        for part in pages_str.split(","):
            part = part.strip()
            if "-" in part:
                start, end = part.split("-")
                start_as_int = int(start) if start else 1
                end_as_int = int(end) if end else -1
                ranges.append((start_as_int, end_as_int))
            else:
                page = int(part)
                ranges.append((page, page))
        return ranges

    def should_translate_page(self, page_number: int) -> bool:
        """判断指定页码是否需要翻译
        Args:
            page_number: 页码
        Returns:
            是否需要翻译该页
        """
        if isinstance(self.page_ranges, list) and len(self.page_ranges) == 0:
            return False
        if not self.page_ranges:
            return True

        for start, end in self.page_ranges:
            if start <= page_number and (end == -1 or page_number <= end):
                return True
        return False

    def get_output_file_path(self, filename: str) -> Path:
        return Path(self.output_dir) / filename

    def get_working_file_path(self, filename: str) -> Path:
        return Path(self.working_dir) / filename

    def get_part_working_dir(self, part_index: int) -> Path:
        """Get working directory for a specific part"""
        if part_index not in self._part_working_dirs:
            if self.working_dir:
                part_dir = Path(self.working_dir) / f"part_{part_index}"
            else:
                part_dir = Path(tempfile.mkdtemp()) / f"part_{part_index}"
            part_dir.mkdir(parents=True, exist_ok=True)
            self._part_working_dirs[part_index] = part_dir
        return self._part_working_dirs[part_index]

    def get_part_output_dir(self, part_index: int) -> Path:
        """Get output directory for a specific part"""
        if part_index not in self._part_output_dirs:
            part_dir = Path(self.working_dir) / f"part_{part_index}_output"
            part_dir.mkdir(parents=True, exist_ok=True)
            self._part_output_dirs[part_index] = part_dir
        return self._part_output_dirs[part_index]

    def cleanup_part_output_dir(self, part_index: int):
        """Clean up output directory for a specific part"""
        if part_index in self._part_output_dirs:
            part_dir = self._part_output_dirs[part_index]
            if part_dir.exists():
                shutil.rmtree(part_dir)
            del self._part_output_dirs[part_index]

    def cleanup_part_working_dir(self, part_index: int):
        """Clean up working directory for a specific part"""
        if part_index in self._part_working_dirs:
            part_dir = self._part_working_dirs[part_index]
            if part_dir.exists():
                shutil.rmtree(part_dir, ignore_errors=True)
            del self._part_working_dirs[part_index]

    def cleanup_temp_files(self):
        """Clean up all temporary files including part working directories"""
        try:
            for part_index in list(self._part_working_dirs.keys()):
                self.cleanup_part_working_dir(part_index)
            if self._is_temp_dir:
                logger.info(f"cleanup temp files: {self.working_dir}")
                shutil.rmtree(self.working_dir, ignore_errors=True)
        except Exception:
            logger.exception("Error cleaning up temporary files")

    def raise_if_cancelled(self):
        if self.progress_monitor is not None:
            self.progress_monitor.raise_if_cancelled()

    def cancel_translation(self):
        if self.progress_monitor is not None:
            self.progress_monitor.cancel()

    def get_term_extraction_translator(self) -> BaseTranslator:
        """Return the translator to use for automatic term extraction."""
        return self.term_extraction_translator

    def record_term_extraction_usage(
        self,
        total_tokens: int,
        prompt_tokens: int,
        completion_tokens: int,
        cache_hit_prompt_tokens: int,
    ) -> None:
        """Accumulate token usage for automatic term extraction."""
        if total_tokens > 0:
            self.term_extraction_token_usage["total_tokens"] += total_tokens
        if prompt_tokens > 0:
            self.term_extraction_token_usage["prompt_tokens"] += prompt_tokens
        if completion_tokens > 0:
            self.term_extraction_token_usage["completion_tokens"] += completion_tokens
        if cache_hit_prompt_tokens > 0:
            self.term_extraction_token_usage["cache_hit_prompt_tokens"] += (
                cache_hit_prompt_tokens
            )


class TranslateResult:
    original_pdf_path: str
    total_seconds: float
    mono_pdf_path: Path | None
    dual_pdf_path: Path | None
    no_watermark_mono_pdf_path: Path | None
    no_watermark_dual_pdf_path: Path | None
    peak_memory_usage: int | None
    auto_extracted_glossary_path: Path | None
    total_valid_character_count: int | None
    total_valid_text_token_count: int | None

    def __init__(
        self,
        mono_pdf_path: Path | None,
        dual_pdf_path: Path | None,
        auto_extracted_glossary_path: Path | None = None,
    ):
        self.mono_pdf_path = mono_pdf_path
        self.dual_pdf_path = dual_pdf_path

        # For compatibility considerations, if only a non-watermarked PDF is generated,
        # the values of mono_pdf_path and no_watermark_mono_pdf_path are the same.
        self.no_watermark_mono_pdf_path = mono_pdf_path
        self.no_watermark_dual_pdf_path = dual_pdf_path

        self.auto_extracted_glossary_path = auto_extracted_glossary_path
        self.total_valid_character_count = None
        self.total_valid_text_token_count = None

    def __str__(self):
        """Return a human-readable string representation of the translation result."""
        result = []
        if hasattr(self, "original_pdf_path") and self.original_pdf_path:
            result.append(f"\tOriginal PDF: {self.original_pdf_path}")

        if hasattr(self, "total_seconds") and self.total_seconds:
            result.append(f"\tTotal time: {self.total_seconds:.2f} seconds")

        if self.mono_pdf_path:
            result.append(f"\tMonolingual PDF: {self.mono_pdf_path}")

        if self.dual_pdf_path:
            result.append(f"\tDual-language PDF: {self.dual_pdf_path}")

        if (
            hasattr(self, "no_watermark_mono_pdf_path")
            and self.no_watermark_mono_pdf_path
            and self.no_watermark_mono_pdf_path != self.mono_pdf_path
        ):
            result.append(
                f"\tNo-watermark Monolingual PDF: {self.no_watermark_mono_pdf_path}"
            )

        if (
            hasattr(self, "no_watermark_dual_pdf_path")
            and self.no_watermark_dual_pdf_path
            and self.no_watermark_dual_pdf_path != self.dual_pdf_path
        ):
            result.append(
                f"\tNo-watermark Dual-language PDF: {self.no_watermark_dual_pdf_path}"
            )

        if (
            hasattr(self, "auto_extracted_glossary_path")
            and self.auto_extracted_glossary_path
        ):
            result.append(
                f"\tAuto-extracted glossary: {self.auto_extracted_glossary_path}"
            )

        if hasattr(self, "peak_memory_usage") and self.peak_memory_usage:
            result.append(f"\tPeak memory usage: {self.peak_memory_usage} MB")

        if hasattr(self, "total_valid_character_count") and isinstance(
            self.total_valid_character_count, int
        ):
            result.append(
                f"\tTotal valid character count: {self.total_valid_character_count}"
            )

        if hasattr(self, "total_valid_text_token_count") and isinstance(
            self.total_valid_text_token_count, int
        ):
            result.append(
                f"\tTotal valid text token count (gpt-4o): {self.total_valid_text_token_count}"
            )

        if result:
            result.insert(0, "Translation results:")

        return "\n".join(result) if result else "No translation results available"
