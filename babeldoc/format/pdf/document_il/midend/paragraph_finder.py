import logging
import random
import re

import hdbscan
import numpy as np

from babeldoc.babeldoc_exception.BabelDOCException import ExtractTextError
from babeldoc.format.pdf.document_il import Box
from babeldoc.format.pdf.document_il import Document
from babeldoc.format.pdf.document_il import Page
from babeldoc.format.pdf.document_il import PdfCharacter
from babeldoc.format.pdf.document_il import PdfLine
from babeldoc.format.pdf.document_il import PdfParagraph
from babeldoc.format.pdf.document_il import PdfParagraphComposition
from babeldoc.format.pdf.document_il import PdfRectangle
from babeldoc.format.pdf.document_il.utils.layout_helper import (
    HEIGHT_NOT_USFUL_CHAR_IN_CHAR,
)
from babeldoc.format.pdf.document_il.utils.layout_helper import Layout
from babeldoc.format.pdf.document_il.utils.layout_helper import add_space_dummy_chars
from babeldoc.format.pdf.document_il.utils.layout_helper import build_layout_index
from babeldoc.format.pdf.document_il.utils.layout_helper import calculate_iou_for_boxes
from babeldoc.format.pdf.document_il.utils.layout_helper import get_char_unicode_string
from babeldoc.format.pdf.document_il.utils.layout_helper import get_character_layout
from babeldoc.format.pdf.document_il.utils.layout_helper import is_bullet_point
from babeldoc.format.pdf.document_il.utils.layout_helper import (
    is_character_in_formula_layout,
)
from babeldoc.format.pdf.document_il.utils.layout_helper import is_text_layout
from babeldoc.format.pdf.document_il.utils.paragraph_helper import is_cid_paragraph
from babeldoc.format.pdf.document_il.utils.style_helper import INDIGO
from babeldoc.format.pdf.document_il.utils.style_helper import WHITE
from babeldoc.format.pdf.translation_config import TranslationConfig

logger = logging.getLogger(__name__)

# Base58 alphabet (Bitcoin style, without numbers 0, O, I, l)
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def generate_base58_id(length: int = 5) -> str:
    """Generate a random base58 ID of specified length."""
    return "".join(random.choice(BASE58_ALPHABET) for _ in range(length))


class ParagraphFinder:
    stage_name = "Parse Paragraphs"

    # 定义项目符号的正则表达式模式

    def __init__(self, translation_config: TranslationConfig):
        self.translation_config = translation_config

    def _preprocess_formula_layouts(self, page: Page):
        """
        Identifies 'formula' layouts that do not significantly overlap with any text layouts
        and re-labels them as 'isolate_formula'.
        """
        # Use a simplified Layout object for is_text_layout check
        text_layouts = [
            layout
            for layout in page.page_layout
            if is_text_layout(Layout(layout.id, layout.class_name))
        ]
        formula_layouts = [
            layout for layout in page.page_layout if layout.class_name == "formula"
        ]

        if not text_layouts or not formula_layouts:
            return

        for formula_layout in formula_layouts:
            is_isolated = True
            for text_layout in text_layouts:
                iou = calculate_iou_for_boxes(formula_layout.box, text_layout.box)
                if iou >= 0.5:
                    is_isolated = False
                    break

            if is_isolated:
                formula_layout.class_name = "isolate_formula"

    def add_text_fill_background(self, page: Page):
        layout_map = {layout.id: layout for layout in page.page_layout}
        for paragraph in page.pdf_paragraph:
            layout_id = paragraph.layout_id
            if layout_id is None:
                continue
            layout = layout_map[layout_id]
            if paragraph.box is None:
                continue
            x1, y1, x2, y2 = (
                paragraph.box.x,
                paragraph.box.y,
                paragraph.box.x2,
                paragraph.box.y2,
            )
            layout_box = layout.box
            if layout_box.x < x1:
                x1 = layout_box.x
            if layout_box.y < y1:
                y1 = layout_box.y
            if layout_box.x2 > x2:
                x2 = layout_box.x2
            if layout_box.y2 > y2:
                y2 = layout_box.y2
            assert x2 > x1 and y2 > y1
            page.pdf_rectangle.append(
                PdfRectangle(
                    box=Box(x1, y1, x2, y2),
                    fill_background=True,
                    graphic_state=WHITE,
                    debug_info=False,
                    xobj_id=paragraph.xobj_id,
                )
            )

    def update_paragraph_data(self, paragraph: PdfParagraph, update_unicode=False):
        if not paragraph.pdf_paragraph_composition:
            return

        chars = []
        for composition in paragraph.pdf_paragraph_composition:
            if composition.pdf_line:
                chars.extend(composition.pdf_line.pdf_character)
            elif composition.pdf_formula:
                chars.extend(composition.pdf_formula.pdf_character)
            elif composition.pdf_character:
                chars.append(composition.pdf_character)
            elif composition.pdf_same_style_unicode_characters:
                continue
            else:
                logger.error(
                    "Unexpected composition type"
                    " in PdfParagraphComposition. "
                    "This type only appears in the IL "
                    "after the translation is completed.",
                )
                continue

        if update_unicode and chars:
            paragraph.unicode = get_char_unicode_string(chars)
        if not chars:
            return
        # 更新边界框
        min_x = min(char.visual_bbox.box.x for char in chars)
        min_y = min(char.visual_bbox.box.y for char in chars)
        max_x = max(char.visual_bbox.box.x2 for char in chars)
        max_y = max(char.visual_bbox.box.y2 for char in chars)
        paragraph.box = Box(min_x, min_y, max_x, max_y)
        paragraph.vertical = chars[0].vertical
        paragraph.xobj_id = chars[0].xobj_id

        paragraph.first_line_indent = False
        if (
            paragraph.pdf_paragraph_composition
            and paragraph.pdf_paragraph_composition[0].pdf_line
            and paragraph.pdf_paragraph_composition[0]
            .pdf_line.pdf_character[0]
            .visual_bbox.box.x
            - paragraph.box.x
            > 1
        ):
            paragraph.first_line_indent = True

    def update_line_data(self, line: PdfLine):
        min_x = min(char.visual_bbox.box.x for char in line.pdf_character)
        min_y = min(char.visual_bbox.box.y for char in line.pdf_character)
        max_x = max(char.visual_bbox.box.x2 for char in line.pdf_character)
        max_y = max(char.visual_bbox.box.y2 for char in line.pdf_character)
        line.box = Box(min_x, min_y, max_x, max_y)

    def add_debug_info(self, page: Page):
        if not self.translation_config.debug:
            return
        for paragraph in page.pdf_paragraph:
            for composition in paragraph.pdf_paragraph_composition:
                if composition.pdf_line:
                    line = composition.pdf_line
                    page.pdf_rectangle.append(
                        PdfRectangle(
                            box=line.box,
                            fill_background=False,
                            graphic_state=INDIGO,
                            debug_info=True,
                            line_width=0.2,
                        )
                    )

    def process(self, document):
        with self.translation_config.progress_monitor.stage_start(
            self.stage_name,
            len(document.page),
        ) as pbar:
            if not document.page:
                return
            for page in document.page:
                self.translation_config.raise_if_cancelled()
                self.process_page(page)
                pbar.advance()

            total_paragraph_count = 0
            for page in document.page:
                total_paragraph_count += len(page.pdf_paragraph)
            if total_paragraph_count == 0:
                raise ExtractTextError("The document contains no paragraphs.")

            if self.check_cid_paragraph(document):
                raise ExtractTextError("The document contains too many CID paragraphs.")

    def check_cid_paragraph(self, doc: Document):
        cid_para_count = 0
        para_total = 0
        for page in doc.page:
            para_total += len(page.pdf_paragraph)
            for para in page.pdf_paragraph:
                if is_cid_paragraph(para):
                    cid_para_count += 1
        return cid_para_count / para_total > 0.8

    def bbox_overlap(self, bbox1: Box, bbox2: Box) -> bool:
        return (
            bbox1.x < bbox2.x2
            and bbox1.x2 > bbox2.x
            and bbox1.y < bbox2.y2
            and bbox1.y2 > bbox2.y
        )

    def process_page(self, page: Page):
        # build layout index for fast query
        build_layout_index(page)

        # 预处理公式布局的标签
        self._preprocess_formula_layouts(page)

        # 第一步：根据 layout 创建 paragraphs
        # 在这一步中，page.pdf_character 中的字符会被移除
        paragraphs = self._group_characters_into_paragraphs(page)
        page.pdf_paragraph = paragraphs

        # 第二步：将段落内的字符拆分为行
        for paragraph in paragraphs:
            self._split_paragraph_into_lines(paragraph)

        # 第三步：处理段落中的空格
        for paragraph in paragraphs:
            add_space_dummy_chars(paragraph)
            self.process_paragraph_spacing(paragraph)
            self.update_paragraph_data(paragraph)

        # 第四步：计算所有行宽度的中位数
        median_width = self.calculate_median_line_width(paragraphs)

        # 第五步：处理独立段落
        self.process_independent_paragraphs(paragraphs, median_width)

        for paragraph in paragraphs:
            self.update_paragraph_data(paragraph, update_unicode=True)

        if self.translation_config.ocr_workaround:
            self.add_text_fill_background(page)
            # since this is ocr file,
            # image characters are not needed
            page.pdf_character = []

        self.fix_overlapping_paragraphs(page)

        # 第六步：对每一行的字符进行排序
        self._sort_characters_in_lines(page)

        self.add_debug_info(page)
        # clean up to save memory
        del page.layout_index
        del page.layout_map

    def is_isolated_formula(self, char: PdfCharacter):
        return char.char_unicode in (
            "(cid:122)",
            "(cid:123)",
            "(cid:124)",
            "(cid:125)",
        )

    def _group_characters_into_paragraphs(self, page: Page) -> list[PdfParagraph]:
        paragraphs: list[PdfParagraph] = []
        if page.pdf_paragraph:
            paragraphs.extend(page.pdf_paragraph)
            page.pdf_paragraph = []

        char_areas = [
            (char.box.x2 - char.box.x) * (char.box.y2 - char.box.y)
            for char in page.pdf_character
        ]
        median_char_area = 0.0
        if char_areas:
            char_areas.sort()
            mid = len(char_areas) // 2
            median_char_area = (
                char_areas[mid]
                if len(char_areas) % 2 == 1
                else (char_areas[mid - 1] + char_areas[mid]) / 2
            )

        current_paragraph: PdfParagraph | None = None
        current_layout: Layout | None = None
        skip_chars = []

        for char in page.pdf_character:
            char_layout = get_character_layout(char, page)
            # Check if character is in any formula layout and set formula_layout_id
            char.formula_layout_id = is_character_in_formula_layout(char, page)

            if not is_text_layout(char_layout) or self.is_isolated_formula(char):
                skip_chars.append(char)
                continue

            char_box = char.visual_bbox.box
            char_pdf_box = char.box
            if calculate_iou_for_boxes(char_box, char_pdf_box) < 0.2:
                char_box = char_pdf_box
            char_area = (char_box.x2 - char_box.x) * (char_box.y2 - char_box.y)
            is_small_char = char_area < median_char_area * 0.05

            is_new_paragraph = False
            if current_paragraph is None:
                is_new_paragraph = True
            elif (
                not (
                    is_small_char
                    and current_paragraph.pdf_paragraph_composition
                    and char_layout.id == current_layout.id
                )
                and char.char_unicode not in HEIGHT_NOT_USFUL_CHAR_IN_CHAR
            ):
                if (
                    char_layout.id != current_layout.id
                    or (  # not same xobject
                        current_paragraph.pdf_paragraph_composition
                        and current_paragraph.pdf_paragraph_composition[
                            -1
                        ].pdf_character.xobj_id
                        != char.xobj_id
                    )
                    or (
                        is_bullet_point(char)
                        and not current_paragraph.pdf_paragraph_composition
                    )
                ):
                    is_new_paragraph = True

            if is_new_paragraph:
                current_layout = char_layout
                current_paragraph = PdfParagraph(
                    pdf_paragraph_composition=[],
                    layout_id=current_layout.id,
                    debug_id=generate_base58_id(),
                    layout_label=current_layout.name,
                )
                paragraphs.append(current_paragraph)

            current_paragraph.pdf_paragraph_composition.append(
                PdfParagraphComposition(pdf_character=char)
            )

        page.pdf_character = skip_chars
        for para in paragraphs:
            self.update_paragraph_data(para)
        return paragraphs

    def _merge_overlapping_clusters(
        self, lines: dict[int, list[PdfCharacter]]
    ) -> dict[int, list[PdfCharacter]]:
        """
        Merge clusters that have significant y-axis overlap.
        If y_intersection / min_height > 0.5, merge the two clusters.
        """
        if len(lines) <= 1:
            return lines

        # Calculate y-axis ranges for each cluster
        cluster_ranges = {}
        for label, chars in lines.items():
            y_values = [char.visual_bbox.box.y for char in chars] + [
                char.visual_bbox.box.y2 for char in chars
            ]
            cluster_ranges[label] = (min(y_values), max(y_values))

        # Keep merging until no more merges are possible
        changed = True
        while changed:
            changed = False
            labels_to_check = list(lines.keys())

            for i in range(len(labels_to_check)):
                if not changed:  # Only continue if no merge happened in this iteration
                    for j in range(i + 1, len(labels_to_check)):
                        label1, label2 = labels_to_check[i], labels_to_check[j]

                        # Skip if either label has been merged away
                        if label1 not in lines or label2 not in lines:
                            continue

                        y1_min, y1_max = cluster_ranges[label1]
                        y2_min, y2_max = cluster_ranges[label2]

                        # Calculate intersection
                        intersection_start = max(y1_min, y2_min)
                        intersection_end = min(y1_max, y2_max)

                        if (
                            intersection_end > intersection_start
                        ):  # There is intersection
                            intersection_height = intersection_end - intersection_start
                            height1 = y1_max - y1_min
                            height2 = y2_max - y2_min
                            min_height = min(height1, height2)

                            # Check if intersection ratio exceeds threshold
                            if (
                                min_height > 0
                                and intersection_height / min_height > 0.5
                            ):
                                # Merge label2 into label1
                                lines[label1].extend(lines[label2])
                                del lines[label2]

                                # Update cluster range for the merged cluster
                                cluster_ranges[label1] = (
                                    min(y1_min, y2_min),
                                    max(y1_max, y2_max),
                                )
                                del cluster_ranges[label2]

                                changed = True
                                break

        return lines

    def _split_paragraph_into_lines(self, paragraph: PdfParagraph):
        if not paragraph.pdf_paragraph_composition:
            return

        # 1. Extract all characters from the paragraph
        all_chars = []
        other_compositions = []
        for comp in paragraph.pdf_paragraph_composition:
            if comp.pdf_character:
                all_chars.append(comp.pdf_character)
            else:
                other_compositions.append(comp)

        if not all_chars:
            # No characters to process, leave paragraph as is.
            return

        # Calculate character height average and paragraph height
        char_heights = []
        for char in all_chars:
            visual_box = char.visual_bbox.box
            pdf_box = char.box

            # Use visual box if IoU with bbox is >= 0.5, otherwise use bbox
            if calculate_iou_for_boxes(visual_box, pdf_box) >= 0.5:
                char_height = visual_box.y2 - visual_box.y
            else:
                char_height = pdf_box.y2 - pdf_box.y

            char_heights.append(char_height)

        # Calculate average of character heights
        if char_heights:
            char_height_average = sum(char_heights) / len(char_heights)
        else:
            char_height_average = 0

        # Calculate paragraph height
        if all_chars:
            y_coords = []
            y2_coords = []
            for char in all_chars:
                visual_box = char.visual_bbox.box
                pdf_box = char.box

                # Use visual box if IoU with bbox is >= 0.3, otherwise use bbox
                if calculate_iou_for_boxes(visual_box, pdf_box) >= 0.3:
                    y_coords.append(visual_box.y)
                    y2_coords.append(visual_box.y2)
                else:
                    y_coords.append(pdf_box.y)
                    y2_coords.append(pdf_box.y2)

            para_y_min = min(y_coords)
            para_y_max = max(y2_coords)
            paragraph_height = para_y_max - para_y_min
        else:
            paragraph_height = 0

        # If all characters' y coordinates are within character height average from y coordinate average, treat as single line
        if all_chars:
            # Calculate y coordinate average
            y_coords_for_avg = []
            for char in all_chars:
                visual_box = char.visual_bbox.box
                pdf_box = char.box

                # Use visual box if IoU with bbox is >= 0.3, otherwise use bbox
                if calculate_iou_for_boxes(visual_box, pdf_box) >= 0.3:
                    y_coords_for_avg.append(visual_box.y)
                else:
                    y_coords_for_avg.append(pdf_box.y)

            y_coordinate_average = sum(y_coords_for_avg) / len(y_coords_for_avg)

            # Check if all characters' y coordinates are within char_height_average from y_coordinate_average
            all_chars_within_threshold = True
            for char in all_chars:
                visual_box = char.visual_bbox.box
                pdf_box = char.box

                # Use visual box if IoU with bbox is >= 0.3, otherwise use bbox
                if calculate_iou_for_boxes(visual_box, pdf_box) >= 0.3:
                    char_y = visual_box.y
                else:
                    char_y = pdf_box.y

                if abs(char_y - y_coordinate_average) > char_height_average:
                    all_chars_within_threshold = False
                    break

            if all_chars_within_threshold:
                # Sort characters by x-coordinate and create a single line
                all_chars.sort(key=lambda c: c.visual_bbox.box.x)
                single_line_composition = self.create_line(all_chars)
                paragraph.pdf_paragraph_composition = [
                    single_line_composition
                ] + other_compositions
                self.update_paragraph_data(paragraph)
                return

        # 2. Cluster characters into lines using HDBSCAN on their y-centers
        y_centers = np.array([char.box.y for char in all_chars]).reshape(-1, 1)

        clusterer = hdbscan.HDBSCAN(min_cluster_size=5, gen_min_span_tree=True)
        clusterer.fit(y_centers)
        labels = clusterer.labels_

        lines: dict[int, list[PdfCharacter]] = {}
        outlier_indices: list[int] = []
        for i, label in enumerate(labels):
            if label == -1:
                outlier_indices.append(i)
            else:
                lines.setdefault(label, []).append(all_chars[i])

        # Post-process clusters: merge clusters with significant y-axis overlap
        lines = self._merge_overlapping_clusters(lines)

        # 3. Handle outliers by assigning them to the nearest line
        if lines and outlier_indices:
            line_y_centers = {
                label: np.mean(
                    [
                        (c.visual_bbox.box.y + c.visual_bbox.box.y2) / 2
                        for c in line_chars
                    ]
                )
                for label, line_chars in lines.items()
            }
            for i in outlier_indices:
                outlier_char_y = y_centers[i][0]
                closest_label = min(
                    line_y_centers.keys(),
                    key=lambda label: abs(line_y_centers[label] - outlier_char_y),
                )
                lines[closest_label].append(all_chars[i])
        # If no initial clusters, treat each outlier as its own line
        elif not lines and outlier_indices:
            for i, idx in enumerate(outlier_indices):
                lines[i] = [all_chars[idx]]

        # 4. Rebuild the paragraph's composition list
        new_line_compositions = []

        # Sort characters within each line by x-coordinate (left-to-right)
        for line_chars in lines.values():
            line_chars.sort(key=lambda c: c.visual_bbox.box.x)
            if line_chars:
                new_line_compositions.append(self.create_line(line_chars))

        # Combine with other non-character compositions and sort all vertically
        all_new_compositions = new_line_compositions + other_compositions

        def get_composition_y_sort_key(comp):
            # In PDF coords, smaller Y is higher on the page.
            if comp.pdf_line and comp.pdf_line.box:
                return -comp.pdf_line.box.y
            # Add other composition types here if they have a box
            return float("inf")

        all_new_compositions.sort(key=get_composition_y_sort_key)

        paragraph.pdf_paragraph_composition = all_new_compositions
        self.update_paragraph_data(paragraph)

    def process_paragraph_spacing(self, paragraph: PdfParagraph):
        if not paragraph.pdf_paragraph_composition:
            return

        # 处理行级别的空格
        processed_lines = []
        for composition in paragraph.pdf_paragraph_composition:
            if not composition.pdf_line:
                processed_lines.append(composition)
                continue

            line = composition.pdf_line
            if not "".join(
                x.char_unicode for x in line.pdf_character
            ).strip():  # 跳过完全空白的行
                continue

            # 处理行内字符的尾随空格
            processed_chars = []
            for char in line.pdf_character:
                if not char.char_unicode.isspace():
                    processed_chars = processed_chars + [char]
                elif processed_chars:  # 只有在有非空格字符后才考虑保留空格
                    processed_chars.append(char)

            # 移除尾随空格
            while processed_chars and processed_chars[-1].char_unicode.isspace():
                processed_chars.pop()

            if processed_chars:  # 如果行内还有字符
                line = self.create_line(processed_chars)
                processed_lines.append(line)

        paragraph.pdf_paragraph_composition = processed_lines
        self.update_paragraph_data(paragraph)

    def create_line(self, chars: list[PdfCharacter]) -> PdfParagraphComposition:
        assert chars

        line = PdfLine(pdf_character=chars)
        self.update_line_data(line)
        return PdfParagraphComposition(pdf_line=line)

    def calculate_median_line_width(self, paragraphs: list[PdfParagraph]) -> float:
        # 收集所有行的宽度
        line_widths = []
        for paragraph in paragraphs:
            for composition in paragraph.pdf_paragraph_composition:
                if composition.pdf_line:
                    line = composition.pdf_line
                    line_widths.append(line.box.x2 - line.box.x)

        if not line_widths:
            return 0.0

        # 计算中位数
        line_widths.sort()
        mid = len(line_widths) // 2
        if len(line_widths) % 2 == 0:
            return (line_widths[mid - 1] + line_widths[mid]) / 2
        return line_widths[mid]

    def process_independent_paragraphs(
        self,
        paragraphs: list[PdfParagraph],
        median_width: float,
    ):
        i = 0
        while i < len(paragraphs):
            paragraph = paragraphs[i]
            if len(paragraph.pdf_paragraph_composition) <= 1:  # 跳过只有一行的段落
                i += 1
                continue

            j = 1
            while j < len(paragraph.pdf_paragraph_composition):
                prev_composition = paragraph.pdf_paragraph_composition[j - 1]
                if not prev_composition.pdf_line:
                    j += 1
                    continue

                prev_line = prev_composition.pdf_line
                prev_width = prev_line.box.x2 - prev_line.box.x
                prev_text = "".join([c.char_unicode for c in prev_line.pdf_character])

                # 检查是否包含连续的点（至少 20 个）
                # 如果有至少连续 20 个点，则代表这是目录条目
                if re.search(r"\.{20,}", prev_text):
                    # 创建新的段落
                    new_paragraph = PdfParagraph(
                        box=Box(0, 0, 0, 0),  # 临时边界框
                        pdf_paragraph_composition=(
                            paragraph.pdf_paragraph_composition[j:]
                        ),
                        unicode="",
                        debug_id=generate_base58_id(),
                        layout_label=paragraph.layout_label,
                        layout_id=paragraph.layout_id,
                    )
                    # 更新原段落
                    paragraph.pdf_paragraph_composition = (
                        paragraph.pdf_paragraph_composition[:j]
                    )

                    # 更新两个段落的数据
                    self.update_paragraph_data(paragraph)
                    self.update_paragraph_data(new_paragraph)

                    # 在原段落后插入新段落
                    paragraphs.insert(i + 1, new_paragraph)
                    break

                # 如果前一行宽度小于中位数的一半，将当前行及后续行分割成新段落
                if (
                    self.translation_config.split_short_lines
                    and prev_width
                    < median_width * self.translation_config.short_line_split_factor
                ):
                    # 创建新的段落
                    new_paragraph = PdfParagraph(
                        box=Box(0, 0, 0, 0),  # 临时边界框
                        pdf_paragraph_composition=(
                            paragraph.pdf_paragraph_composition[j:]
                        ),
                        unicode="",
                        debug_id=generate_base58_id(),
                        layout_label=paragraph.layout_label,
                        layout_id=paragraph.layout_id,
                    )
                    # 更新原段落
                    paragraph.pdf_paragraph_composition = (
                        paragraph.pdf_paragraph_composition[:j]
                    )

                    # 更新两个段落的数据
                    self.update_paragraph_data(paragraph)
                    self.update_paragraph_data(new_paragraph)

                    # 在原段落后插入新段落
                    paragraphs.insert(i + 1, new_paragraph)
                    break
                j += 1
            i += 1

    @staticmethod
    def is_bbox_contain_in_vertical(bbox1: Box, bbox2: Box) -> bool:
        """Check if one bounding box is completely contained within the other."""
        # Check if bbox1 is contained in bbox2
        bbox1_in_bbox2 = bbox1.y >= bbox2.y and bbox1.y2 <= bbox2.y2
        # Check if bbox2 is contained in bbox1
        bbox2_in_bbox1 = bbox2.y >= bbox1.y and bbox2.y2 <= bbox1.y2
        return bbox1_in_bbox2 or bbox2_in_bbox1

    def fix_overlapping_paragraphs(self, page: Page):
        """
        Adjusts the bounding boxes of paragraphs on a page to resolve vertical overlaps.

        Iteratively checks pairs of paragraphs and adjusts their vertical boundaries
        (y and y2) if they overlap, aiming to place the boundary at the midpoint
        of the vertical overlap.
        """
        paragraphs = page.pdf_paragraph
        if not paragraphs or len(paragraphs) < 2:
            return

        max_iterations = len(paragraphs) * len(paragraphs)  # Safety break
        iterations = 0

        while iterations < max_iterations:
            iterations += 1
            overlap_found_in_pass = False

            for i in range(len(paragraphs)):
                for j in range(i + 1, len(paragraphs)):
                    para1 = paragraphs[i]
                    para2 = paragraphs[j]

                    if para1.box is None or para2.box is None:
                        continue

                    if para1.xobj_id != para2.xobj_id:
                        continue

                    # Check for overlap using the existing method
                    if self.bbox_overlap(para1.box, para2.box):
                        if self.is_bbox_contain_in_vertical(para1.box, para2.box):
                            continue
                        # Calculate vertical overlap details
                        overlap_y_start = max(para1.box.y, para2.box.y)
                        overlap_y_end = min(para1.box.y2, para2.box.y2)
                        overlap_height = overlap_y_end - overlap_y_start

                        # Calculate horizontal overlap details
                        overlap_x_start = max(para1.box.x, para2.box.x)
                        overlap_x_end = min(para1.box.x2, para2.box.x2)
                        overlap_width = overlap_x_end - overlap_x_start

                        # Ensure there's a real 2D overlap, focusing on vertical adjustment
                        if overlap_height > 1e-6 and overlap_width > 1e-6:
                            overlap_found_in_pass = True

                            # Determine which paragraph is visually higher
                            if para1.box.y2 > para2.box.y and para1.box.y < para2.box.y:
                                lower_para = para1
                                higher_para = para2
                            # Handle cases where y values are identical (or very close)
                            # Prefer the one with smaller y2 as the higher one, or break tie arbitrarily
                            elif para1.box.y2 < para2.box.y2:
                                lower_para = para1
                                higher_para = para2
                            else:
                                lower_para = para2
                                higher_para = para1

                            # Calculate the midpoint of the vertical overlap
                            mid_y = overlap_y_start + overlap_height / 2

                            # Adjust boxes, ensuring they remain valid (y2 > y)
                            if mid_y > higher_para.box.y and mid_y < lower_para.box.y2:
                                higher_para.box.y = mid_y + 1
                                lower_para.box.y2 = mid_y - 1
                            else:
                                # This might happen if one box is fully contained vertically
                                # within another, or due to floating point issues.
                                # Log a warning and skip adjustment for this pair in this iteration.
                                # A more complex strategy might be needed for full containment.
                                logger.warning(
                                    "Could not resolve overlap between paragraphs"
                                    f" {higher_para.debug_id} and {lower_para.debug_id}"
                                    " using simple midpoint strategy."
                                    f" Midpoint: {mid_y},"
                                    f" Higher Box: {higher_para.box},"
                                    f" Lower Box: {lower_para.box}"
                                )

            # If no overlaps were found and adjusted in this pass, we're done.
            if not overlap_found_in_pass:
                break

        if iterations == max_iterations:
            logger.warning(
                f"Maximum iterations ({max_iterations}) reached in"
                f" fix_overlapping_paragraphs for page {page.page_number}."
                " Some overlaps might remain."
            )

    def _sort_characters_in_lines(self, page: Page):
        """Sort characters in each line from left to right, top to bottom."""
        for paragraph in page.pdf_paragraph:
            for composition in paragraph.pdf_paragraph_composition:
                if composition.pdf_line:
                    line = composition.pdf_line
                    line.pdf_character.sort(key=self._get_char_sort_key)

    def _get_char_sort_key(self, char: PdfCharacter):
        """Get sort key for character positioning (top to bottom, left to right)."""
        visual_box = char.visual_bbox.box
        pdf_box = char.box

        # Use visual box if IoU with bbox is >= 0.1, otherwise use bbox
        if calculate_iou_for_boxes(visual_box, pdf_box) >= 0.1:
            box = visual_box
        else:
            box = pdf_box

        # Sort by y coordinate first (top to bottom), then x coordinate (left to right)
        # Note: In PDF coordinate system, y increases upward, so we negate y for top-to-bottom sorting
        return (box.x, -box.y)
