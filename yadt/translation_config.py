from yadt.const import (
    CACHE_FOLDER,
)
import os


class TranslationConfig:
    def __init__(
        self,
        input_file: str,
        translator,
        lang_in: str,
        lang_out: str,
        font: str | None = None,
        pages: str | None = None,
        output: str | None = None,
        debug: bool = False,
        working_dir: str | None = None,
        output_dir: str | None = None,
    ):
        self.input_file = input_file
        self.translator = translator
        self.font = font
        self.pages = pages
        self.output = output
        self.debug = debug
        self.lang_in = lang_in
        self.lang_out = lang_out

        if working_dir is None:
            working_dir = os.path.join(
                CACHE_FOLDER, 'working', os.path.basename(input_file).split(".")[0]
            )
        self.working_dir = working_dir

        os.makedirs(working_dir, exist_ok=True)

        if output_dir is None:
            output_dir = os.path.dirname(input_file)
        self.output_dir = output_dir

        os.makedirs(output_dir, exist_ok=True)

    def get_output_file_path(self, filename):
        return os.path.join(self.output_dir, filename)

    def get_working_file_path(self, filename):
        return os.path.join(self.working_dir, filename)
