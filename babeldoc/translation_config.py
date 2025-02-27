import tempfile
import threading
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import DirectoryPath
from pydantic import Field
from pydantic import FilePath
from pydantic import PositiveFloat
from pydantic import PositiveInt
from pydantic import field_validator

from babeldoc.const import CACHE_FOLDER
from babeldoc.document_il.translator.translator import BaseTranslator
from babeldoc.docvision.doclayout import DocLayoutModel
from babeldoc.progress_monitor import ProgressMonitor


class ConfigModel(BaseModel):
    @staticmethod
    def _page_range_pattern() -> str:
        option_start_page_number = r"(\d+)?"
        option_range = r"(-\d+)?"
        # repeat matching starting page number and optional page range
        repeat = option_start_page_number + rf"(,{option_range})*"
        return repeat

    model_config: ClassVar[ConfigDict] = ConfigDict(arbitrary_types_allowed=True)

    input_file: FilePath
    lang_in: str
    lang_out: str
    output_dir: DirectoryPath = Path().cwd()
    working_dir: DirectoryPath = Path(tempfile.mkdtemp(prefix="babeldoc")).absolute()

    # `page_range` supported input formats:
    #   1. Specifying individual pages to translate:
    #       e.g. `--page-range 1,3,5,7,9`; translates the specified pages [1, 3, 5, 7, 9]
    #   2. Specifying a range of pages to translate:
    #       e.g. `--page-range 1-3,5,7-9`; translates the specified pages [1, 2, 3, 5, 7, 8, 9]
    page_range: str | None = Field(default=None, pattern=_page_range_pattern())
    doc_layout_model: DocLayoutModel = DocLayoutModel.load_onnx()
    font: FilePath | None = None

    no_dual: bool = False
    no_mono: bool = False
    formular_font_pattern: str | None = None
    formular_char_pattern: str | None = None

    # 是否使用交替页式双语 PDF（交替显示原文和译文）
    use_alternating_pages_dual: bool = False

    # FIXME(awwaawwa): 强制拆分短行为新段落，可能会出现糟糕的排版或其他bug
    force_split_short_lines: bool = False
    # 切分阈值系数。实际阈值为当前页所有行长度中位数*此系数
    short_line_split_factor: PositiveFloat = 0.8
    qps: PositiveInt = 1

    # Progress report interval in seconds
    report_interval: PositiveFloat = 0.1
    # Minimum text length to translate
    min_text_length: PositiveInt = 5

    use_rich_process_bar: bool = True
    progress_monitor: ProgressMonitor | None = None

    enhance_compatibility: bool = False
    # Enabling `enhance_compatibility` is equivalent to enabling `skip_clean`,
    # `dual_translate_first`, and `disable_rich_text_translate`.
    skip_clean: bool = False
    dual_translate_first: bool = False
    disable_rich_text_translate: bool = False

    debug: bool = False

    @field_validator("page_range", mode="before", check_fields=True)
    @classmethod
    def page_number_completion(cls, page_range: str | None) -> list[int] | None:
        """Complete the page range notation in `page_range`.
        e.g. `page_number_completion('2-4') -> [2, 3, 4]
        """
        if page_range is None:
            return None

        completed_page_range: set[int] = set()

        for sub_range in page_range.split(","):
            match sub_range.strip().split("-"):
                case start, end:
                    completed_page_range.update(range(int(start), int(end) + 1))
                case start, _:
                    completed_page_range.add(int(start))
                case _, end:
                    completed_page_range.add(int(end))
                case _:
                    continue

        return sorted(completed_page_range)

    @field_validator("enhance_compatibility", mode="after", check_fields=True)
    @classmethod
    def enable_enhance_compatibility(cls, enhance_compatibility: bool):
        if enhance_compatibility is True:
            cls.skip_clean = True
            cls.dual_translate_first = True
            cls.disable_rich_text_translate = True


class TranslationConfig:
    def __init__(
        self,
        translator: BaseTranslator,
        input_file: str | Path,
        lang_in: str,
        lang_out: str,
        doc_layout_model: DocLayoutModel,
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
        use_side_by_side_dual: bool = True,  # Deprecated: 是否使用拼版式双语 PDF（并排显示原文和译文） 向下兼容选项，已停用。
        use_alternating_pages_dual: bool = False,
    ):
        self.translator = translator

        self.input_file = input_file
        self.lang_in = lang_in
        self.lang_out = lang_out
        self.font = font

        self.pages = pages
        self.page_ranges = self._parse_pages(pages) if pages else None
        self.debug = debug

        self.output_dir = output_dir
        self.working_dir = working_dir
        self.no_dual = no_dual
        self.no_mono = no_mono

        self.formular_font_pattern = formular_font_pattern
        self.formular_char_pattern = formular_char_pattern
        self.qps = qps
        self.split_short_lines = split_short_lines

        self.short_line_split_factor = short_line_split_factor
        self.use_rich_pbar = use_rich_pbar
        self.progress_monitor = progress_monitor
        self.doc_layout_model = doc_layout_model

        self.skip_clean = skip_clean or enhance_compatibility

        self.dual_translate_first = dual_translate_first or enhance_compatibility
        self.disable_rich_text_translate = (
            disable_rich_text_translate or enhance_compatibility
        )

        self.report_interval = report_interval
        self.min_text_length = min_text_length
        self.use_alternating_pages_dual = use_alternating_pages_dual

        # for backward compatibility
        if use_side_by_side_dual is False and use_alternating_pages_dual is False:
            self.use_alternating_pages_dual = True

        if progress_monitor and progress_monitor.cancel_event is None:
            progress_monitor.cancel_event = threading.Event()

        if working_dir is None:
            if debug:
                working_dir = Path(CACHE_FOLDER) / "working" / Path(input_file).stem
            else:
                working_dir = tempfile.mkdtemp()
        self.working_dir = working_dir
        self._is_temp_dir = not debug and str(working_dir).startswith(
            tempfile.gettempdir()
        )

        Path(working_dir).mkdir(parents=True, exist_ok=True)

        if output_dir is None:
            output_dir = Path.cwd()
        self.output_dir = output_dir

        Path(output_dir).mkdir(parents=True, exist_ok=True)

        if not doc_layout_model:
            doc_layout_model = DocLayoutModel.load_available()
        self.doc_layout_model = doc_layout_model

        # FIXME: due to compatibility issues, `self.config` is not used for the time being
        self.config = ConfigModel(
            input_file=Path(self.input_file),
            lang_in=self.lang_in,
            lang_out=self.lang_out,
            # TODO(thR CIrcU5): add `font` arg
            page_range=self.pages,
            debug=self.debug,
            # TODO(thR CIrcU5): add `output_dir` and `working_dir` args
            no_dual=self.no_dual,
            no_mono=self.no_mono,
            formular_font_pattern=self.formular_font_pattern,
            formular_char_pattern=self.formular_char_pattern,
            qps=self.qps,
            force_split_short_lines=self.split_short_lines,
            short_line_split_factor=self.short_line_split_factor,
            use_rich_process_bar=self.use_rich_pbar,
            progress_monitor=self.progress_monitor,
            doc_layout_model=self.doc_layout_model,
            skip_clean=self.skip_clean,
            dual_translate_first=self.dual_translate_first,
            disable_rich_text_translate=self.disable_rich_text_translate,
            report_interval=self.report_interval,
            min_text_length=self.min_text_length,
            use_alternating_pages_dual=self.use_alternating_pages_dual,
        )

    def _parse_pages(self, pages_str: str | None) -> list[tuple[int, int]] | None:
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
        if not self.page_ranges:
            return True

        for start, end in self.page_ranges:
            if start <= page_number and (end == -1 or page_number <= end):
                return True
        return False

    # FIXME: due to compatibility issues, `_create_folders()` is not used for the time being
    def _create_folders(self):
        self.working_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def get_output_file_path(self, filename: str) -> Path:
        return Path(self.output_dir) / filename

    def get_working_file_path(self, filename: str) -> Path:
        return Path(self.working_dir) / filename

    def raise_if_cancelled(self):
        if self.progress_monitor is not None:
            self.progress_monitor.raise_if_cancelled()

    def cancel_translation(self):
        if self.progress_monitor is not None:
            self.progress_monitor.cancel()


class TranslateResult:
    original_pdf_path: str
    total_seconds: float
    mono_pdf_path: str | None
    dual_pdf_path: str | None

    def __init__(self, mono_pdf_path: str | None, dual_pdf_path: str | None):
        self.mono_pdf_path = mono_pdf_path
        self.dual_pdf_path = dual_pdf_path
