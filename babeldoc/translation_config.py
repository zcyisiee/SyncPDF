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

from babeldoc.document_il.translator.translator import BaseTranslator
from babeldoc.docvision.doclayout import DocLayoutModel
from babeldoc.docvision.doclayout import OnnxModel
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
    doc_layout_model: OnnxModel = DocLayoutModel.load_onnx()
    font: FilePath | None = None

    no_dual: bool = False
    no_mono: bool = False
    formular_font_pattern: str | None = None
    formular_char_pattern: str | None = None

    # 是否使用拼版式双语 PDF (并排显示原文和译文) 向下兼容选项, 已停用
    use_side_by_side_dual: bool = True
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
    def __init__(self, translator: type[BaseTranslator], config: ConfigModel):
        self.translator = translator
        self.config = config

        if self.config.progress_monitor is not None:
            if self.config.progress_monitor.cancel_event is None:
                self.config.progress_monitor.cancel_event = threading.Event()

    def _create_folders(self):
        self.config.working_dir.mkdir(parents=True, exist_ok=True)
        self.config.output_dir.mkdir(parents=True, exist_ok=True)

    def get_output_file_path(self, filename: str) -> Path:
        return Path(self.config.output_dir) / filename

    def get_working_file_path(self, filename: str) -> Path:
        return Path(self.config.working_dir) / filename

    def raise_if_cancelled(self):
        if self.config.progress_monitor is not None:
            self.config.progress_monitor.raise_if_cancelled()

    def cancel_translation(self):
        if self.config.progress_monitor is not None:
            self.config.progress_monitor.cancel()


class TranslateResult:
    original_pdf_path: str
    total_seconds: float
    mono_pdf_path: str | None
    dual_pdf_path: str | None

    def __init__(self, mono_pdf_path: str | None, dual_pdf_path: str | None):
        self.mono_pdf_path = mono_pdf_path
        self.dual_pdf_path = dual_pdf_path
