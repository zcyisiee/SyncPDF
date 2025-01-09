import concurrent.futures

from tqdm import tqdm

from yadt.document_il import (
    Document,
    Page,
    PdfFormula,
    PdfParagraph,
    PdfParagraphComposition,
    PdfSameStyleCharacters,
    PdfSameStyleUnicodeCharacters,
)
from yadt.document_il.midend.paragraph_finder import Layout
from yadt.document_il.translator.translator import BaseTranslator
from yadt.document_il.utils.layout_helper import is_same_style
from yadt.translation_config import TranslationConfig


class RichTextPlaceholder:
    def __init__(
        self,
        id: int,
        composition: PdfSameStyleCharacters,
        left_placeholder: str,
        right_placeholder: str,
    ):
        self.id = id
        self.composition = composition
        self.left_placeholder = left_placeholder
        self.right_placeholder = right_placeholder


class FormulaPlaceholder:
    def __init__(self, id: int, formula: PdfFormula, placeholder: str):
        self.id = id
        self.formula = formula
        self.placeholder = placeholder


class PbarContext:
    def __init__(self, pbar: tqdm):
        self.pbar = pbar

    def __enter__(self):
        return self.pbar

    def __exit__(self, exc_type, exc_value, traceback):
        self.pbar.update(1)


class ILTranslator:
    def __init__(
        self, translate_engine: BaseTranslator, translation_config: TranslationConfig
    ):
        self.translate_engine = translate_engine
        self.translation_config = translation_config

    def translate(self, docs: Document):
        # count total paragraph
        total = sum(len(page.pdf_paragraph) for page in docs.page)
        with tqdm(total=total, desc="translate") as pbar:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=self.translation_config.qps * 2
            ) as executor:
                for page in docs.page:
                    self.process_page(page, executor, pbar)

    def process_page(
        self,
        page: Page,
        executor: concurrent.futures.ThreadPoolExecutor,
        pbar: tqdm | None = None,
    ):
        for paragraph in page.pdf_paragraph:
            # self.translate_paragraph(paragraph, pbar)

            executor.submit(self.translate_paragraph, paragraph, pbar)

    class TranslateInput:
        def __init__(
            self,
            unicode: str,
            placeholders: [RichTextPlaceholder | FormulaPlaceholder],
        ):
            self.unicode = unicode
            self.placeholders = placeholders

    def create_formula_placeholder(
        self, formula: PdfFormula, id: int, paragraph: PdfParagraph
    ):
        placeholder = self.translate_engine.get_formular_placeholder(id)
        if placeholder in paragraph.unicode:
            return self.create_formula_placeholder(formula, id + 1, paragraph)

        return FormulaPlaceholder(id, formula, placeholder)

    def create_rich_text_placeholder(
        self,
        composition: PdfSameStyleCharacters,
        id: int,
        paragraph: PdfParagraph,
    ):
        left_placeholder = self.translate_engine.get_rich_text_left_placeholder(id)
        right_placeholder = self.translate_engine.get_rich_text_right_placeholder(id)
        if (
            left_placeholder in paragraph.unicode
            or right_placeholder in paragraph.unicode
        ):
            return self.create_rich_text_placeholder(composition, id + 1, paragraph)

        return RichTextPlaceholder(id, composition, left_placeholder, right_placeholder)

    def get_translate_input(self, paragraph: PdfParagraph):
        if not paragraph.pdf_paragraph_composition:
            return
        if len(paragraph.pdf_paragraph_composition) == 1:
            composition = paragraph.pdf_paragraph_composition[0]
            if composition.pdf_line:
                return self.TranslateInput(paragraph.unicode, [])
            elif composition.pdf_formula:
                return None
            elif composition.pdf_same_style_characters:
                return self.TranslateInput(paragraph.unicode, [])
            elif composition.pdf_character:
                return self.TranslateInput(paragraph.unicode, [])
            else:
                raise Exception(
                    "Unexpected PdfParagraphComposition type "
                    "in PdfParagraph during translation. "
                    f"Composition: {composition}. "
                    f"Paragraph: {paragraph}. "
                )
        placeholder_id = 1
        placeholders = []
        chars = []
        for composition in paragraph.pdf_paragraph_composition:
            if composition.pdf_line:
                chars.extend(composition.pdf_line.pdf_character)
            elif composition.pdf_formula:
                formula_placeholder = self.create_formula_placeholder(
                    composition.pdf_formula, placeholder_id, paragraph
                )
                placeholders.append(formula_placeholder)
                placeholder_id = formula_placeholder.id + 1
                chars.extend(formula_placeholder.placeholder)
            elif composition.pdf_character:
                chars.append(composition.pdf_character)
            elif composition.pdf_same_style_characters:
                if is_same_style(
                    composition.pdf_same_style_characters.pdf_style,
                    paragraph.base_style,
                ):
                    chars.extend(composition.pdf_same_style_characters.pdf_character)
                    continue
                placeholder = self.create_rich_text_placeholder(
                    composition.pdf_same_style_characters,
                    placeholder_id,
                    paragraph,
                )
                placeholders.append(placeholder)
                placeholder_id = placeholder.id + 2
                chars.append(placeholder.left_placeholder)
                chars.extend(composition.pdf_same_style_characters.pdf_character)
                chars.append(placeholder.right_placeholder)
            else:
                raise Exception(
                    "Unexpected PdfParagraphComposition type "
                    "in PdfParagraph during translation. "
                    f"Composition: {composition}. "
                    f"Paragraph: {paragraph}. "
                )

        # 有些 PDF 文件没有明确包含空格字符，而是通过字符之间的间距来隐式表示。
        # 计算字符间距的中位数
        distances = []
        for i in range(len(chars) - 1):
            if isinstance(chars[i], str) or isinstance(chars[i + 1], str):
                continue
            distance = chars[i + 1].box.x - chars[i].box.x2
            if distance > 1:  # 只考虑正向距离
                distances.append(distance)
        distinct_distances = sorted(set(distances))
        if not distinct_distances:
            median_distance = 1
        elif len(distinct_distances) == 1:
            median_distance = distinct_distances[0]
        else:
            median_distance = distinct_distances[1]

        # 构建 unicode 字符串，根据间距插入空格
        unicode_chars = []
        for i in range(len(chars)):
            if isinstance(chars[i], str):
                unicode_chars.append(chars[i])
                continue
            unicode_chars.append(chars[i].char_unicode)
            if chars[i].char_unicode == " ":
                continue
            if i < len(chars) - 1 and not isinstance(chars[i + 1], str):
                distance = chars[i + 1].box.x - chars[i].box.x2
                if distance >= median_distance or Layout.is_newline(
                    chars[i], chars[i + 1]
                ):
                    unicode_chars.append(" ")

        # 更新 unicode（合并所有行的文本）
        # 这个位置的 unicode 主要用途是导出 xml 文件后人类阅读
        text = "".join(unicode_chars)
        return self.TranslateInput(text, placeholders)

    def parse_translate_output(
        self, input: TranslateInput, output: str
    ) -> [PdfParagraphComposition]:
        result = []
        current_pos = 0

        # 按顺序处理所有占位符
        while current_pos < len(output):
            # 检查是否有匹配的占位符
            placeholder_match = None

            for placeholder in input.placeholders:
                if isinstance(placeholder, FormulaPlaceholder):
                    # 检查公式占位符
                    pos = output.find(placeholder.placeholder, current_pos)
                    if pos == current_pos:
                        placeholder_match = placeholder
                        matched_text = placeholder.placeholder
                        break
                else:
                    # 检查富文本占位符
                    left_pos = output.find(placeholder.left_placeholder, current_pos)
                    if left_pos == current_pos:
                        right_pos = output.find(
                            placeholder.right_placeholder,
                            left_pos + len(placeholder.left_placeholder),
                        )
                        if right_pos != -1:
                            placeholder_match = placeholder
                            matched_text = output[
                                current_pos : right_pos
                                + len(placeholder.right_placeholder)
                            ]
                            break

            if placeholder_match:
                # 处理占位符
                if isinstance(placeholder_match, FormulaPlaceholder):
                    # 添加公式
                    comp = PdfParagraphComposition()
                    comp.pdf_formula = placeholder_match.formula
                    result.append(comp)
                    current_pos += len(matched_text)
                else:
                    # 添加富文本
                    text_start = current_pos + len(placeholder_match.left_placeholder)
                    text_end = output.find(
                        placeholder_match.right_placeholder, text_start
                    )
                    if text_end != -1:
                        text = output[text_start:text_end]
                        if isinstance(
                            placeholder_match.composition, PdfSameStyleCharacters
                        ) and text.replace(" ", "") == (
                            "".join(
                                [
                                    x.char_unicode
                                    for x in placeholder_match.composition.pdf_character
                                ]
                            ).replace(" ", "")
                        ):
                            comp = PdfParagraphComposition(
                                pdf_same_style_characters=placeholder_match.composition
                            )
                            result.append(comp)
                            current_pos = text_end + len(
                                placeholder_match.right_placeholder
                            )
                            continue
                        comp = PdfParagraphComposition()
                        comp.pdf_same_style_unicode_characters = (
                            PdfSameStyleUnicodeCharacters()
                        )
                        comp.pdf_same_style_unicode_characters.pdf_style = (
                            placeholder_match.composition.pdf_style
                        )
                        comp.pdf_same_style_unicode_characters.unicode = text
                        result.append(comp)
                        current_pos = text_end + len(
                            placeholder_match.right_placeholder
                        )
            else:
                # 处理普通文本直到下一个占位符
                next_pos = len(output)
                for placeholder in input.placeholders:
                    if isinstance(placeholder, FormulaPlaceholder):
                        pos = output.find(placeholder.placeholder, current_pos)
                        if pos != -1 and pos < next_pos:
                            next_pos = pos
                    else:
                        pos = output.find(placeholder.left_placeholder, current_pos)
                        if pos != -1 and pos < next_pos:
                            next_pos = pos

                text = output[current_pos:next_pos]
                if text:
                    comp = PdfParagraphComposition()
                    comp.pdf_same_style_unicode_characters = (
                        PdfSameStyleUnicodeCharacters()
                    )
                    comp.pdf_same_style_unicode_characters.unicode = text
                    result.append(comp)
                current_pos = next_pos

        return result

    def translate_paragraph(self, paragraph: PdfParagraph, pbar: tqdm | None = None):
        with PbarContext(pbar):
            if paragraph.vertical:
                return

            translate_input = self.get_translate_input(paragraph)
            if not translate_input:
                return

            text = translate_input.unicode
            translated_text = self.translate_engine.translate(text)
            if translated_text == text:
                return

            paragraph.unicode = translated_text
            paragraph.pdf_paragraph_composition = self.parse_translate_output(
                translate_input, translated_text
            )
