import base64
import re

from babeldoc.format.office.context import Context
from babeldoc.format.office.document_il.filetypes.base import TranslatablePartsProcessor
from babeldoc.format.office.document_il.opc.packuri import PackURI
from babeldoc.format.office.document_il.opc.part import Part
from babeldoc.format.office.document_il.types import ILData
from babeldoc.format.office.document_il.types import ILDataDElement
from babeldoc.format.office.document_il.types import ILDataDocument
from babeldoc.format.office.document_il.types import Slide
from babeldoc.format.office.document_il.utils import get_main_part
from babeldoc.format.office.document_il.utils import group_hashes
from babeldoc.format.office.document_il.utils import hash_dict
from loguru import logger
from lxml import etree


def text_handler(element: etree.Element, il_data: ILData, context: Context):
    """
    处理所有的文本元素，并进行翻译

    Args:
        element: 要处理的文本元素
        il_data: 要处理的IL数据
        context: 上下文
    """
    # 查找所有文本元素
    text_elements = element.findall(".//a:t", namespaces=element.nsmap)

    if not text_elements:
        # 尝试替代路径
        text_elements = element.xpath(
            "//a:t",
            namespaces={"a": "http://schemas.openxmlformats.org/drawingml/2006/main"},
        )
        if not text_elements:
            return

    # 按段落分组文本元素
    paragraphs = {}  # 段落ID -> 段落内的文本元素列表
    paragraph_order = []  # 保持段落顺序

    for text_elem in text_elements:
        if text_elem.attrib.get("translated", "false") == "true":
            continue

        # 查找所属段落
        parent_p = text_elem.xpath("./ancestor::a:p", namespaces=element.nsmap)
        if parent_p:
            p_id = id(parent_p[0])
            if p_id not in paragraphs:
                paragraphs[p_id] = {"elements": [], "paragraph": parent_p[0]}
                paragraph_order.append(p_id)
            paragraphs[p_id]["elements"].append(text_elem)
        else:
            # 如果找不到段落，则单独处理
            p_id = id(text_elem)
            paragraphs[p_id] = {"elements": [text_elem], "paragraph": None}
            paragraph_order.append(p_id)

    # 如果没有有效的文本元素，则返回
    if not paragraphs:
        return

    # 获取翻译器
    translator = context.translator
    if not translator:
        logger.warning("No translator found in context")
        return

    # 处理每个段落
    for p_id in paragraph_order:
        paragraph_data = paragraphs[p_id]
        text_elems = paragraph_data["elements"]

        # 收集段落中的所有文本和样式
        paragraph_text = []
        style_hash_list = []

        for text_elem in text_elems:
            # 获取文本样式信息
            style = {}
            # 查找父元素中的样式信息
            parent_rPr = text_elem.xpath(
                "./ancestor::a:r/a:rPr", namespaces=element.nsmap
            )
            if parent_rPr:
                for style_elem in parent_rPr[0]:
                    style[style_elem.tag.split("}")[-1]] = dict(style_elem.attrib)

            # 检查是否是超链接
            parent_r = text_elem.xpath("./ancestor::a:r", namespaces=element.nsmap)
            if parent_r:
                hyperlinks = parent_r[0].xpath(
                    "./a:hlinkClick | ./a:hlinkHover", namespaces=element.nsmap
                )
                if hyperlinks:
                    style["hyperlink"] = True
                    if "action" not in style:
                        style["action"] = {}
                    for hl in hyperlinks:
                        for attr, value in hl.attrib.items():
                            style["action"][attr.split("}")[-1]] = value

            # 创建样式哈希
            style_hash = hash_dict(style)
            style_hash_list.append(style_hash)

            # 获取文本内容
            if text_elem.text and text_elem.text.strip():
                paragraph_text.append(text_elem.text)
            else:
                paragraph_text.append("")

        if not paragraph_text:
            continue

        # 合并段落文本（如果是同一段落内的多个文本元素）
        if len(paragraph_text) > 1 and paragraph_data["paragraph"] is not None:
            # 检查是否应该合并（例如，如果它们在同一行内）
            # 在PPTX中，同一行内的文本通常在同一个段落中，但有不同的文本运行(text runs)
            combined_text = ["".join(paragraph_text)]
            combined_style = [style_hash_list[0]]  # 使用第一个元素的样式

            # 处理样式分组
            grouped_hashes = group_hashes(combined_style)

            # 翻译合并后的文本
            translated_combined = translator.translate(combined_text, grouped_hashes)

            # 将翻译后的文本应用到第一个元素，其余元素设为空
            if translated_combined and len(translated_combined) > 0:
                first_elem = text_elems[0]
                first_elem.attrib["translated"] = "true"
                first_elem.text = translated_combined[0]
                logger.info(
                    f"Translated paragraph: '{combined_text[0]}' -> '{translated_combined[0]}'"
                )

                # 将其余元素设为空
                for i in range(1, len(text_elems)):
                    text_elems[i].attrib["translated"] = "true"
                    text_elems[i].text = ""
        else:
            # 处理样式分组
            grouped_hashes = group_hashes(style_hash_list)

            # 翻译文本
            translated_text = translator.translate(paragraph_text, grouped_hashes)

            # 将翻译后的文本应用到原始元素
            for i, text_elem in enumerate(text_elems):
                try:
                    if (
                        i < len(translated_text)
                        and text_elem.text
                        and text_elem.text.strip()
                    ):
                        text_elem.attrib["translated"] = "true"
                        text_elem.text = translated_text[i]
                        logger.info(
                            f"Translated: '{paragraph_text[i]}' -> '{translated_text[i]}'"
                        )
                except Exception as e:
                    logger.error(f"Error applying translation: {str(e)}")


def table_handler(element: etree.Element, il_data: ILData, context: Context):
    """
    处理表格中的文本元素，并进行翻译

    Args:
        element: 表格元素
        il_data: 要处理的IL数据
        context: 上下文
    """
    # 查找表格中的所有txBody元素（表格单元格中的文本）
    cell_text_bodies = element.xpath(
        ".//a:txBody",
        namespaces={"a": "http://schemas.openxmlformats.org/drawingml/2006/main"},
    )

    if not cell_text_bodies:
        return

    # 处理每个单元格中的文本
    for tx_body in cell_text_bodies:
        # 直接使用text_handler处理单元格内的文本
        text_handler(tx_body, il_data, context)


def shape_handler(element: etree.Element, il_data: ILData, context: Context):
    """
    处理形状中的文本元素，并进行翻译

    Args:
        element: 形状元素
        il_data: 要处理的IL数据
        context: 上下文
    """
    # 查找形状中的所有文本体
    tx_body = element.find(".//a:txBody", namespaces=element.nsmap)
    if tx_body is not None:
        # 使用通用文本处理器处理文本
        text_handler(tx_body, il_data, context)


def chart_handler(element: etree.Element, il_data: ILData, context: Context):
    """
    处理图表中的文本元素，并进行翻译

    Args:
        element: 图表元素
        il_data: 要处理的IL数据
        context: 上下文
    """
    # 查找图表中的所有文本元素
    # 图表标题
    chart_title = element.xpath(
        ".//c:title//a:t",
        namespaces={
            "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
            "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
        },
    )

    # 图表轴标题
    axis_titles = element.xpath(
        ".//c:axisTitle//a:t",
        namespaces={
            "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
            "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
        },
    )

    # 图表数据标签
    data_labels = element.xpath(
        ".//c:dLbl//a:t",
        namespaces={
            "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
            "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
        },
    )

    # 图表图例
    legend_entries = element.xpath(
        ".//c:legend//a:t",
        namespaces={
            "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
            "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
        },
    )

    # 处理所有文本元素
    for text_container in [chart_title, axis_titles, data_labels, legend_entries]:
        if text_container:
            # 创建一个临时容器来使用text_handler
            temp_container = etree.Element("temp")
            for text_elem in text_container:
                temp_container.append(text_elem.getparent().getparent())

            text_handler(temp_container, il_data, context)


def picture_handler(element: etree.Element, il_data: ILData, context: Context):
    """
    处理图片中的文本元素，例如图片标题或描述

    Args:
        element: 图片元素
        il_data: 要处理的IL数据
        context: 上下文
    """
    # 查找图片中可能的文本元素（如标题或描述）
    tx_body = element.find(".//a:txBody", namespaces=element.nsmap)
    if tx_body is not None:
        # 使用通用文本处理器处理文本
        text_handler(tx_body, il_data, context)


def smartart_handler(element: etree.Element, il_data: ILData, context: Context):
    """
    处理SmartArt图表中的文本元素

    Args:
        element: SmartArt元素
        il_data: 要处理的IL数据
        context: 上下文
    """
    # 查找SmartArt中的所有文本元素
    text_elements = element.xpath(
        ".//dgm:t",
        namespaces={"dgm": "http://schemas.openxmlformats.org/drawingml/2006/diagram"},
    )

    if not text_elements:
        # 尝试替代路径，SmartArt也可能使用标准文本元素
        text_elements = element.xpath(
            ".//a:t",
            namespaces={"a": "http://schemas.openxmlformats.org/drawingml/2006/main"},
        )

        if not text_elements:
            return

    # 收集所有文本
    text = []
    style_hash_list = []

    for text_elem in text_elements:
        if text_elem.attrib.get("translated", "false") == "true":
            continue

        # 获取文本样式信息
        style = {}
        # 创建样式哈希
        style_hash = hash_dict(style)
        style_hash_list.append(style_hash)

        # 获取文本内容
        if text_elem.text and text_elem.text.strip():
            text.append(text_elem.text)
        else:
            text.append("")

    if not text:
        return

    # 处理样式分组
    grouped_hashes = group_hashes(style_hash_list)

    # 使用translator进行翻译
    translator = context.translator
    if not translator:
        logger.warning("No translator found in context")
        return

    translated_text = translator.translate(text, grouped_hashes)

    # 将翻译后的文本应用到原始元素
    for i, text_elem in enumerate(text_elements):
        try:
            if i < len(translated_text) and text_elem.text and text_elem.text.strip():
                text_elem.attrib["translated"] = "true"
                text_elem.text = translated_text[i]
                logger.info(
                    f"Translated SmartArt text: '{text[i]}' -> '{translated_text[i]}'"
                )
        except Exception as e:
            logger.error(f"Error applying SmartArt translation: {str(e)}")


def media_handler(element: etree.Element, il_data: ILData, context: Context):
    """
    处理音频和视频元素中的文本，例如标题或描述

    Args:
        element: 媒体元素
        il_data: 要处理的IL数据
        context: 上下文
    """
    # 处理媒体标题和描述
    # 查找媒体相关的文本元素
    text_elements = element.xpath(
        ".//p:nvPr/p:extLst//a:t",
        namespaces={
            "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
            "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
        },
    )

    # 媒体的标题可能在txBody中
    tx_body = element.find(".//a:txBody", namespaces=element.nsmap)
    if tx_body is not None:
        # 使用通用文本处理器处理文本
        text_handler(tx_body, il_data, context)

    # 处理其他文本元素
    if text_elements:
        # 创建一个临时容器
        temp_container = etree.Element("temp")
        for text_elem in text_elements:
            parent = text_elem.getparent()
            if parent is not None:
                temp_container.append(parent)

        text_handler(temp_container, il_data, context)


def comment_handler(element: etree.Element, il_data: ILData, context: Context):
    """
    处理注释中的文本

    Args:
        element: 注释元素
        il_data: 要处理的IL数据
        context: 上下文
    """
    # 查找注释文本
    text_elements = element.xpath(
        ".//p:text//a:t",
        namespaces={
            "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
            "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
        },
    )

    if not text_elements:
        return

    # 创建一个临时容器
    temp_container = etree.Element("temp")
    for text_elem in text_elements:
        parent = text_elem.getparent()
        if parent is not None:
            temp_container.append(parent)

    text_handler(temp_container, il_data, context)


def header_footer_handler(element: etree.Element, il_data: ILData, context: Context):
    """
    处理页眉和页脚中的文本

    Args:
        element: 页眉页脚元素
        il_data: 要处理的IL数据
        context: 上下文
    """
    # 查找页眉和页脚文本
    text_elements = element.xpath(
        ".//p:hdr//a:t | .//p:ftr//a:t",
        namespaces={
            "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
            "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
        },
    )

    # 查找幻灯片底部的日期、页脚和幻灯片编号的占位符文本
    ph_elements = element.xpath(
        ".//p:sp[.//p:ph[@type='dt' or @type='ftr' or @type='sldNum']]//a:t",
        namespaces={
            "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
            "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
        },
    )

    text_elements.extend(ph_elements)

    if not text_elements:
        return

    # 创建一个临时容器
    temp_container = etree.Element("temp")
    for text_elem in text_elements:
        parent = text_elem.getparent()
        if parent is not None:
            temp_container.append(parent)

    text_handler(temp_container, il_data, context)


def hyperlink_handler(element: etree.Element, il_data: ILData, context: Context):
    """
    专门处理文本运行中的超链接

    Args:
        element: 包含超链接的段落元素
        il_data: 要处理的IL数据
        context: 上下文
    """
    # 查找包含超链接的文本运行
    hyperlink_runs = element.xpath(
        ".//a:r[a:hlinkClick or a:hlinkHover]",
        namespaces={"a": "http://schemas.openxmlformats.org/drawingml/2006/main"},
    )

    if not hyperlink_runs:
        return

    # 处理每个超链接运行中的文本
    for run in hyperlink_runs:
        # 获取文本元素
        text_elements = run.xpath(
            ".//a:t",
            namespaces={"a": "http://schemas.openxmlformats.org/drawingml/2006/main"},
        )

        if not text_elements:
            continue

        # 创建临时容器
        temp_container = etree.Element("temp")
        for text_elem in text_elements:
            # 直接添加文本元素，而不是添加父元素
            temp_container.append(text_elem)

        # 使用文本处理器处理内容
        text_handler(temp_container, il_data, context)


def placeholder_handler(element: etree.Element, il_data: ILData, context: Context):
    """
    处理占位符中的文本

    Args:
        element: 占位符元素
        il_data: 要处理的IL数据
        context: 上下文
    """
    # 查找占位符中的文本
    # 占位符通常是形状元素，带有ph属性
    ph = element.find(
        ".//p:ph",
        namespaces={"p": "http://schemas.openxmlformats.org/presentationml/2006/main"},
    )

    if ph is not None:
        # 找到占位符中的文本
        tx_body = element.find(".//a:txBody", namespaces=element.nsmap)
        if tx_body is not None:
            text_handler(tx_body, il_data, context)


def background_text_handler(element: etree.Element, il_data: ILData, context: Context):
    """
    处理背景文本

    Args:
        element: 背景文本元素
        il_data: 要处理的IL数据
        context: 上下文
    """
    # 背景文本通常在母版布局的形状中
    # 或者在带有特定属性的形状中

    # 检查是否有背景文本标识（这通常是自定义的）
    is_background = False

    # 检查元素是否在背景层
    bg_elem = element.xpath(
        "./ancestor-or-self::p:bg",
        namespaces={"p": "http://schemas.openxmlformats.org/presentationml/2006/main"},
    )

    if bg_elem:
        is_background = True

    # 一些背景文本可能是形状上的水印或装饰
    # 这通常需要根据具体情况添加识别逻辑

    if is_background:
        # 查找文本内容
        tx_body = element.find(".//a:txBody", namespaces=element.nsmap)
        if tx_body is not None:
            text_handler(tx_body, il_data, context)


def equation_handler(element: etree.Element, il_data: ILData, context: Context):
    """
    处理数学公式

    Args:
        element: 公式元素
        il_data: 要处理的IL数据
        context: 上下文
    """
    # 查找OMML (Office Math Markup Language) 元素
    math_elements = element.xpath(
        ".//m:oMath | .//m:oMathPara",
        namespaces={"m": "http://schemas.openxmlformats.org/officeDocument/2006/math"},
    )

    if not math_elements:
        return

    # 提取公式中的文本元素
    for math_elem in math_elements:
        # 数学公式中的文本通常在m:t元素中
        text_elements = math_elem.xpath(
            ".//m:t",
            namespaces={
                "m": "http://schemas.openxmlformats.org/officeDocument/2006/math"
            },
        )

        if text_elements:
            # 创建一个临时容器
            temp_container = etree.Element("temp")
            for text_elem in text_elements:
                parent = text_elem.getparent()
                if parent is not None:
                    temp_container.append(parent)

            # 使用通用文本处理器
            text_handler(temp_container, il_data, context)


def wordart_handler(element: etree.Element, il_data: ILData, context: Context):
    """
    处理WordArt文本

    Args:
        element: WordArt元素
        il_data: 要处理的IL数据
        context: 上下文
    """
    # WordArt通常是特殊样式的文本
    # 在PPTX中，它们通常是带有特殊文本效果的形状

    # 查找WordArt特征
    # 这些通常在textEffects或类似节点中
    text_effects = element.xpath(
        ".//a:prstTxWarp | .//a:effectLst",
        namespaces={"a": "http://schemas.openxmlformats.org/drawingml/2006/main"},
    )

    if text_effects:
        # 查找文本内容
        tx_body = element.find(".//a:txBody", namespaces=element.nsmap)
        if tx_body is not None:
            text_handler(tx_body, il_data, context)


def animation_text_handler(element: etree.Element, il_data: ILData, context: Context):
    """
    处理带有动画效果的文本

    Args:
        element: 动画文本元素
        il_data: 要处理的IL数据
        context: 上下文
    """
    # 动画通常定义在p:timing元素下
    # 我们需要找到与动画相关联的文本

    # 处理元素本身可能包含的文本
    tx_body = element.find(".//a:txBody", namespaces=element.nsmap)
    if tx_body is not None:
        text_handler(tx_body, il_data, context)


def slide_notes_handler(element: etree.Element, il_data: ILData, context: Context):
    """
    处理幻灯片备注

    Args:
        element: 备注元素
        il_data: 要处理的IL数据
        context: 上下文
    """
    # 查找备注中的文本
    tx_body = element.find(".//a:txBody", namespaces=element.nsmap)
    if tx_body is not None:
        text_handler(tx_body, il_data, context)


class PptxPartsProcessor(TranslatablePartsProcessor):
    handlers = {
        "txBody": text_handler,
        "graphicFrame": table_handler,
        "sp": shape_handler,  # 形状
        "pic": picture_handler,  # 图片
        "chartSpace": chart_handler,  # 图表
        "dgm": smartart_handler,  # SmartArt
        "media": media_handler,  # 音频和视频
        "comment": comment_handler,  # 注释
        "hdrftr": header_footer_handler,  # 页眉和页脚
        "link": hyperlink_handler,  # 链接和动作
        "hyperlink": hyperlink_handler,  # 文本运行中的超链接
        "placeholder": placeholder_handler,  # 占位符
        "background": background_text_handler,  # 背景文本
        "equation": equation_handler,  # 数学公式
        "wordart": wordart_handler,  # WordArt
        "animation": animation_text_handler,  # 动画文本
        "notes": slide_notes_handler,  # 幻灯片备注
    }
    is_sync_process = True

    @staticmethod
    def read(il_data: ILData) -> ILData:
        """处理主部分"""
        logger.info("开始处理 PPTX 文件")

        try:
            # 获取presentation.xml主部分
            main_part = get_main_part(il_data.parts)
            if not main_part:
                logger.error("Main presentation part not found")
                return il_data

            main_part_xml = base64.b64decode(main_part.xml_content)
            root = etree.fromstring(main_part_xml)

            # 初始化数据结构
            slides = []
            slide_masters = []
            slide_layouts = []
            document = ILDataDocument(elements=[])

            # 记录命名空间
            document.namespaces = root.nsmap

            # 遍历所有parts寻找幻灯片、注释和备注
            for part in il_data.parts:
                # 检查是否是幻灯片部分
                if "/ppt/slides/slide" in part.part_uri and part.part_uri.endswith(
                    ".xml"
                ):
                    logger.info(f"处理幻灯片: {part.part_uri}")

                    try:
                        # 解析XML
                        slide_xml = base64.b64decode(part.xml_content)
                        slide_tree = etree.fromstring(slide_xml)

                        # 解析幻灯片编号
                        slide_num = re.search(r"slide(\d+)\.xml", part.part_uri)
                        if not slide_num:
                            continue
                        slide_num = slide_num.group(1)

                        # 查找所有文本体元素
                        text_bodies = slide_tree.xpath(
                            "//p:txBody",
                            namespaces={
                                "p": "http://schemas.openxmlformats.org/presentationml/2006/main"
                            },
                        )

                        for i, tx_body in enumerate(text_bodies):
                            element_id = f"slide_{slide_num}_textbody_{i}"

                            # 检查是否有文本内容
                            text_elements = tx_body.xpath(
                                ".//a:t",
                                namespaces={
                                    "a": "http://schemas.openxmlformats.org/drawingml/2006/main"
                                },
                            )

                            if not text_elements:
                                continue

                            il_element = ILDataDElement(
                                part_uri=part.part_uri,
                                element_type="txBody",
                                original_xml=etree.tostring(
                                    tx_body, encoding="unicode", method="xml"
                                ),
                                element_id=element_id,
                            )

                            document.elements.append(il_element)

                        # 查找所有表格元素
                        graphic_frames = slide_tree.xpath(
                            "//p:graphicFrame",
                            namespaces={
                                "p": "http://schemas.openxmlformats.org/presentationml/2006/main"
                            },
                        )

                        for i, graphic_frame in enumerate(graphic_frames):
                            # 检查是否包含表格
                            tables = graphic_frame.xpath(
                                ".//a:tbl",
                                namespaces={
                                    "a": "http://schemas.openxmlformats.org/drawingml/2006/main"
                                },
                            )

                            if not tables:
                                continue

                            element_id = f"slide_{slide_num}_table_{i}"

                            # 检查表格中是否有文本内容
                            text_elements = graphic_frame.xpath(
                                ".//a:t",
                                namespaces={
                                    "a": "http://schemas.openxmlformats.org/drawingml/2006/main"
                                },
                            )

                            if not text_elements:
                                continue

                            il_element = ILDataDElement(
                                part_uri=part.part_uri,
                                element_type="graphicFrame",
                                original_xml=etree.tostring(
                                    graphic_frame, encoding="unicode", method="xml"
                                ),
                                element_id=element_id,
                            )

                            document.elements.append(il_element)

                        # 查找所有形状元素
                        shapes = slide_tree.xpath(
                            "//p:sp",
                            namespaces={
                                "p": "http://schemas.openxmlformats.org/presentationml/2006/main"
                            },
                        )

                        for i, shape in enumerate(shapes):
                            # 跳过已处理的文本框（避免重复处理）
                            if shape.xpath(
                                ".//p:txBody",
                                namespaces={
                                    "p": "http://schemas.openxmlformats.org/presentationml/2006/main"
                                },
                            ):
                                # 仅处理那些还未作为txBody处理过的元素
                                continue

                            element_id = f"slide_{slide_num}_shape_{i}"

                            # 检查形状中是否有文本内容
                            text_elements = shape.xpath(
                                ".//a:t",
                                namespaces={
                                    "a": "http://schemas.openxmlformats.org/drawingml/2006/main"
                                },
                            )

                            if not text_elements:
                                continue

                            il_element = ILDataDElement(
                                part_uri=part.part_uri,
                                element_type="sp",
                                original_xml=etree.tostring(
                                    shape, encoding="unicode", method="xml"
                                ),
                                element_id=element_id,
                            )

                            document.elements.append(il_element)

                        # 查找所有图片元素
                        pictures = slide_tree.xpath(
                            "//p:pic",
                            namespaces={
                                "p": "http://schemas.openxmlformats.org/presentationml/2006/main"
                            },
                        )

                        for i, picture in enumerate(pictures):
                            element_id = f"slide_{slide_num}_picture_{i}"

                            # 检查图片中是否有文本内容（如标题或描述）
                            text_elements = picture.xpath(
                                ".//a:t",
                                namespaces={
                                    "a": "http://schemas.openxmlformats.org/drawingml/2006/main"
                                },
                            )

                            if not text_elements:
                                continue

                            il_element = ILDataDElement(
                                part_uri=part.part_uri,
                                element_type="pic",
                                original_xml=etree.tostring(
                                    picture, encoding="unicode", method="xml"
                                ),
                                element_id=element_id,
                            )

                            document.elements.append(il_element)

                        # 查找所有图表元素
                        chart_spaces = slide_tree.xpath(
                            "//c:chartSpace",
                            namespaces={
                                "c": "http://schemas.openxmlformats.org/drawingml/2006/chart"
                            },
                        )

                        if not chart_spaces:
                            # 尝试替代查找方法
                            chart_refs = slide_tree.xpath(
                                "//a:graphicData[@uri='http://schemas.openxmlformats.org/drawingml/2006/chart']",
                                namespaces={
                                    "a": "http://schemas.openxmlformats.org/drawingml/2006/main"
                                },
                            )

                            if chart_refs:
                                for chart_ref in chart_refs:
                                    # 查找chart关系引用
                                    chart_nodes = chart_ref.xpath(
                                        ".//c:chart",
                                        namespaces={
                                            "c": "http://schemas.openxmlformats.org/drawingml/2006/chart"
                                        },
                                    )

                                    # 这些引用可能需要在ppt/charts文件夹中查找
                                    # 这个处理会比较复杂，需要查找相关的图表文件
                                    # 暂时记录为图表引用
                                    for j, chart_node in enumerate(chart_nodes):
                                        element_id = f"slide_{slide_num}_chart_ref_{j}"

                                        il_element = ILDataDElement(
                                            part_uri=part.part_uri,
                                            element_type="chartRef",
                                            original_xml=etree.tostring(
                                                chart_ref,
                                                encoding="unicode",
                                                method="xml",
                                            ),
                                            element_id=element_id,
                                        )

                                        document.elements.append(il_element)

                        for i, chart_space in enumerate(chart_spaces):
                            element_id = f"slide_{slide_num}_chart_{i}"

                            # 检查图表中是否有文本内容
                            title_elements = chart_space.xpath(
                                ".//c:title//a:t",
                                namespaces={
                                    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
                                    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
                                },
                            )

                            axis_elements = chart_space.xpath(
                                ".//c:axisTitle//a:t",
                                namespaces={
                                    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
                                    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
                                },
                            )

                            if not (title_elements or axis_elements):
                                continue

                            il_element = ILDataDElement(
                                part_uri=part.part_uri,
                                element_type="chartSpace",
                                original_xml=etree.tostring(
                                    chart_space, encoding="unicode", method="xml"
                                ),
                                element_id=element_id,
                            )

                            document.elements.append(il_element)

                        # 查找所有SmartArt图表元素
                        diagrams = slide_tree.xpath(
                            "//a:graphicData[@uri='http://schemas.openxmlformats.org/drawingml/2006/diagram']",
                            namespaces={
                                "a": "http://schemas.openxmlformats.org/drawingml/2006/main"
                            },
                        )

                        for i, diagram in enumerate(diagrams):
                            element_id = f"slide_{slide_num}_diagram_{i}"

                            # 检查SmartArt中是否有文本内容
                            text_elements = diagram.xpath(
                                ".//dgm:t",
                                namespaces={
                                    "dgm": "http://schemas.openxmlformats.org/drawingml/2006/diagram"
                                },
                            )

                            if not text_elements:
                                # 尝试替代路径
                                text_elements = diagram.xpath(
                                    ".//a:t",
                                    namespaces={
                                        "a": "http://schemas.openxmlformats.org/drawingml/2006/main"
                                    },
                                )

                                if not text_elements:
                                    continue

                            il_element = ILDataDElement(
                                part_uri=part.part_uri,
                                element_type="dgm",
                                original_xml=etree.tostring(
                                    diagram, encoding="unicode", method="xml"
                                ),
                                element_id=element_id,
                            )

                            document.elements.append(il_element)

                        # 查找所有媒体元素 (音频和视频)
                        media_elements = slide_tree.xpath(
                            "//p:nvPr/p:videoFile | //p:nvPr/p:audioFile",
                            namespaces={
                                "p": "http://schemas.openxmlformats.org/presentationml/2006/main"
                            },
                        )

                        for i, media_elem in enumerate(media_elements):
                            element_id = f"slide_{slide_num}_media_{i}"

                            # 获取媒体所在的整个形状元素
                            parent_shape = media_elem.xpath(
                                "./ancestor::p:pic",
                                namespaces={
                                    "p": "http://schemas.openxmlformats.org/presentationml/2006/main"
                                },
                            )

                            if not parent_shape:
                                continue

                            # 检查是否有相关文本
                            text_elements = parent_shape[0].xpath(
                                ".//a:t",
                                namespaces={
                                    "a": "http://schemas.openxmlformats.org/drawingml/2006/main"
                                },
                            )

                            if not text_elements:
                                continue

                            il_element = ILDataDElement(
                                part_uri=part.part_uri,
                                element_type="media",
                                original_xml=etree.tostring(
                                    parent_shape[0], encoding="unicode", method="xml"
                                ),
                                element_id=element_id,
                            )

                            document.elements.append(il_element)

                        # 专门处理文本运行中的超链接
                        hyperlink_runs = slide_tree.xpath(
                            "//a:r[a:hlinkClick or a:hlinkHover]",
                            namespaces={
                                "a": "http://schemas.openxmlformats.org/drawingml/2006/main"
                            },
                        )

                        for i, run in enumerate(hyperlink_runs):
                            element_id = f"slide_{slide_num}_hyperlink_run_{i}"

                            # 检查是否有文本内容
                            text_elements = run.xpath(
                                ".//a:t",
                                namespaces={
                                    "a": "http://schemas.openxmlformats.org/drawingml/2006/main"
                                },
                            )

                            if (
                                not text_elements
                                or not text_elements[0].text
                                or not text_elements[0].text.strip()
                            ):
                                continue

                            # 直接存储run元素，而不是整个段落
                            il_element = ILDataDElement(
                                part_uri=part.part_uri,
                                element_type="hyperlink",
                                original_xml=etree.tostring(
                                    run, encoding="unicode", method="xml"
                                ),
                                element_id=element_id,
                            )

                            document.elements.append(il_element)

                        # 创建幻灯片对象
                        r_id = f"rId{slide_num}"  # 简化处理
                        slide = Slide(
                            slide_id=slide_num,
                            r_id=r_id,
                            element_id=f"slide_{slide_num}",
                        )

                        slides.append(slide)
                    except Exception as e:
                        logger.error(f"幻灯片 {part.part_uri} 处理错误: {str(e)}")

                # 处理幻灯片备注
                elif "/ppt/notesSlides/" in part.part_uri and part.part_uri.endswith(
                    ".xml"
                ):
                    logger.info(f"处理幻灯片备注: {part.part_uri}")

                    try:
                        # 解析XML
                        notes_xml = base64.b64decode(part.xml_content)
                        notes_tree = etree.fromstring(notes_xml)

                        # 解析备注编号
                        notes_num = re.search(r"notesSlide(\d+)\.xml", part.part_uri)
                        if not notes_num:
                            continue
                        notes_num = notes_num.group(1)

                        # 查找备注中的文本体
                        text_bodies = notes_tree.xpath(
                            "//p:txBody",
                            namespaces={
                                "p": "http://schemas.openxmlformats.org/presentationml/2006/main"
                            },
                        )

                        for i, tx_body in enumerate(text_bodies):
                            element_id = f"notes_{notes_num}_textbody_{i}"

                            # 检查是否有文本内容
                            text_elements = tx_body.xpath(
                                ".//a:t",
                                namespaces={
                                    "a": "http://schemas.openxmlformats.org/drawingml/2006/main"
                                },
                            )

                            if not text_elements:
                                continue

                            il_element = ILDataDElement(
                                part_uri=part.part_uri,
                                element_type="notes",
                                original_xml=etree.tostring(
                                    tx_body, encoding="unicode", method="xml"
                                ),
                                element_id=element_id,
                            )

                            document.elements.append(il_element)
                    except Exception as e:
                        logger.error(f"幻灯片备注 {part.part_uri} 处理错误: {str(e)}")

                # 处理注释
                elif "/ppt/comments/" in part.part_uri and part.part_uri.endswith(
                    ".xml"
                ):
                    logger.info(f"处理注释: {part.part_uri}")

                    try:
                        # 解析XML
                        comments_xml = base64.b64decode(part.xml_content)
                        comments_tree = etree.fromstring(comments_xml)

                        # 查找所有注释
                        comments = comments_tree.xpath(
                            "//p:cm",
                            namespaces={
                                "p": "http://schemas.openxmlformats.org/presentationml/2006/main"
                            },
                        )

                        for i, comment in enumerate(comments):
                            element_id = f"comment_{i}"

                            # 检查是否有文本内容
                            text_elements = comment.xpath(
                                ".//a:t",
                                namespaces={
                                    "a": "http://schemas.openxmlformats.org/drawingml/2006/main"
                                },
                            )

                            if not text_elements:
                                continue

                            il_element = ILDataDElement(
                                part_uri=part.part_uri,
                                element_type="comment",
                                original_xml=etree.tostring(
                                    comment, encoding="unicode", method="xml"
                                ),
                                element_id=element_id,
                            )

                            document.elements.append(il_element)
                    except Exception as e:
                        logger.error(f"注释 {part.part_uri} 处理错误: {str(e)}")

                # 处理页眉页脚
                elif (
                    "/ppt/slideLayouts/" in part.part_uri
                    or "/ppt/slideMasters/" in part.part_uri
                ):
                    logger.info(f"处理布局/母版: {part.part_uri}")

                    try:
                        # 解析XML
                        layout_xml = base64.b64decode(part.xml_content)
                        layout_tree = etree.fromstring(layout_xml)

                        # 解析布局编号
                        layout_num = re.search(
                            r"(slideLayout|slideMaster)(\d+)\.xml", part.part_uri
                        )
                        if not layout_num:
                            continue
                        layout_type = layout_num.group(1)
                        layout_num = layout_num.group(2)

                        # 查找页眉页脚占位符
                        hf_elements = layout_tree.xpath(
                            "//p:sp[.//p:ph[@type='hdr' or @type='ftr']]",
                            namespaces={
                                "p": "http://schemas.openxmlformats.org/presentationml/2006/main"
                            },
                        )

                        for i, hf_elem in enumerate(hf_elements):
                            element_id = f"{layout_type}_{layout_num}_hdrftr_{i}"

                            # 检查是否有文本内容
                            text_elements = hf_elem.xpath(
                                ".//a:t",
                                namespaces={
                                    "a": "http://schemas.openxmlformats.org/drawingml/2006/main"
                                },
                            )

                            if not text_elements:
                                continue

                            il_element = ILDataDElement(
                                part_uri=part.part_uri,
                                element_type="hdrftr",
                                original_xml=etree.tostring(
                                    hf_elem, encoding="unicode", method="xml"
                                ),
                                element_id=element_id,
                            )

                            document.elements.append(il_element)

                        # 查找背景文本
                        bg_elements = layout_tree.xpath(
                            "//p:bg//p:sp[.//a:t]",
                            namespaces={
                                "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
                                "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
                            },
                        )

                        for i, bg_elem in enumerate(bg_elements):
                            element_id = f"{layout_type}_{layout_num}_background_{i}"

                            il_element = ILDataDElement(
                                part_uri=part.part_uri,
                                element_type="background",
                                original_xml=etree.tostring(
                                    bg_elem, encoding="unicode", method="xml"
                                ),
                                element_id=element_id,
                            )

                            document.elements.append(il_element)
                    except Exception as e:
                        logger.error(f"布局/母版 {part.part_uri} 处理错误: {str(e)}")

            # 更新IL数据
            il_data.document = document
            il_data.slides = slides
            il_data.slide_masters = slide_masters
            il_data.slide_layouts = slide_layouts

            logger.info(f"找到 {len(document.elements)} 个需要翻译的元素")

        except Exception as e:
            logger.error(f"处理 PPTX 出错: {str(e)}")

        return il_data

    @staticmethod
    def write(il_data: ILData, parts: dict[PackURI, Part]) -> dict[PackURI, Part]:
        """重建PPTX文档"""
        logger.info("开始写入 PPTX 文件")

        try:
            # 按照part_uri分组元素
            elements_by_part = {}
            for il_element in il_data.document.elements:
                if il_element.part_uri not in elements_by_part:
                    elements_by_part[il_element.part_uri] = []
                elements_by_part[il_element.part_uri].append(il_element)

            # 处理每个包含元素的部分
            for part_uri, elements in elements_by_part.items():
                # 找到对应的部分
                part_key = None
                for key, part in parts.items():
                    if str(part.partname) == part_uri:
                        part_key = key
                        break

                if part_key:
                    # 获取并解析原始XML
                    xml_content = parts[part_key].blob
                    xml_tree = etree.fromstring(xml_content)

                    # 创建元素ID到原始XML的映射
                    element_map = {}
                    for element in elements:
                        element_id = element.element_id
                        if not element_id:
                            continue

                        # 获取处理后的XML或使用原始XML
                        xml_str = (
                            element.handled_xml
                            if element.handled_xml
                            else element.original_xml
                        )
                        element_tree = etree.fromstring(xml_str)
                        element_map[element_id] = element_tree

                    # 提取幻灯片编号
                    slide_num = None
                    if "slides/slide" in part_uri:
                        slide_num = re.search(r"slides/slide(\d+)\.xml", part_uri)
                        if slide_num:
                            slide_num = slide_num.group(1)

                    if slide_num:
                        # 处理所有已知元素类型
                        # 处理文本体元素
                        text_bodies = xml_tree.xpath(
                            "//p:txBody", namespaces=xml_tree.nsmap
                        )
                        for i, tx_body in enumerate(text_bodies):
                            element_id = f"slide_{slide_num}_textbody_{i}"
                            if element_id in element_map:
                                # 找到父元素
                                parent = tx_body.getparent()
                                if parent is not None:
                                    # 替换元素
                                    idx = parent.index(tx_body)
                                    parent.remove(tx_body)
                                    parent.insert(idx, element_map[element_id])

                        # 处理表格元素
                        graphic_frames = xml_tree.xpath(
                            "//p:graphicFrame", namespaces=xml_tree.nsmap
                        )
                        for i, graphic_frame in enumerate(graphic_frames):
                            element_id = f"slide_{slide_num}_table_{i}"
                            if element_id in element_map:
                                # 找到父元素
                                parent = graphic_frame.getparent()
                                if parent is not None:
                                    # 替换元素
                                    idx = parent.index(graphic_frame)
                                    parent.remove(graphic_frame)
                                    parent.insert(idx, element_map[element_id])

                        # 处理形状元素
                        shapes = xml_tree.xpath("//p:sp", namespaces=xml_tree.nsmap)
                        for i, shape in enumerate(shapes):
                            element_id = f"slide_{slide_num}_shape_{i}"
                            if element_id in element_map:
                                # 找到父元素
                                parent = shape.getparent()
                                if parent is not None:
                                    # 替换元素
                                    idx = parent.index(shape)
                                    parent.remove(shape)
                                    parent.insert(idx, element_map[element_id])

                            # 处理占位符
                            element_id = f"slide_{slide_num}_placeholder_{i}"
                            if element_id in element_map:
                                # 找到父元素
                                parent = shape.getparent()
                                if parent is not None:
                                    # 替换元素
                                    idx = parent.index(shape)
                                    parent.remove(shape)
                                    parent.insert(idx, element_map[element_id])

                            # 处理WordArt
                            element_id = f"slide_{slide_num}_wordart_{i}"
                            if element_id in element_map:
                                # 找到父元素
                                parent = shape.getparent()
                                if parent is not None:
                                    # 替换元素
                                    idx = parent.index(shape)
                                    parent.remove(shape)
                                    parent.insert(idx, element_map[element_id])

                        # 处理图片元素
                        pictures = xml_tree.xpath("//p:pic", namespaces=xml_tree.nsmap)
                        for i, picture in enumerate(pictures):
                            element_id = f"slide_{slide_num}_picture_{i}"
                            if element_id in element_map:
                                # 找到父元素
                                parent = picture.getparent()
                                if parent is not None:
                                    # 替换元素
                                    idx = parent.index(picture)
                                    parent.remove(picture)
                                    parent.insert(idx, element_map[element_id])

                            # 处理媒体元素
                            element_id = f"slide_{slide_num}_media_{i}"
                            if element_id in element_map:
                                # 找到父元素
                                parent = picture.getparent()
                                if parent is not None:
                                    # 替换元素
                                    idx = parent.index(picture)
                                    parent.remove(picture)
                                    parent.insert(idx, element_map[element_id])

                        # 处理图表元素
                        chart_spaces = xml_tree.xpath(
                            "//c:chartSpace",
                            namespaces={
                                "c": "http://schemas.openxmlformats.org/drawingml/2006/chart"
                            },
                        )

                        for i, chart_space in enumerate(chart_spaces):
                            element_id = f"slide_{slide_num}_chart_{i}"
                            if element_id in element_map:
                                # 找到父元素
                                parent = chart_space.getparent()
                                if parent is not None:
                                    # 替换元素
                                    idx = parent.index(chart_space)
                                    parent.remove(chart_space)
                                    parent.insert(idx, element_map[element_id])

                        # 处理图表引用
                        chart_refs = xml_tree.xpath(
                            "//a:graphicData[@uri='http://schemas.openxmlformats.org/drawingml/2006/chart']",
                            namespaces={
                                "a": "http://schemas.openxmlformats.org/drawingml/2006/main"
                            },
                        )

                        for i, chart_ref in enumerate(chart_refs):
                            element_id = f"slide_{slide_num}_chart_ref_{i}"
                            if element_id in element_map:
                                # 找到父元素
                                parent = chart_ref.getparent()
                                if parent is not None:
                                    # 替换元素
                                    idx = parent.index(chart_ref)
                                    parent.remove(chart_ref)
                                    parent.insert(idx, element_map[element_id])

                        # 处理SmartArt图表元素
                        diagrams = xml_tree.xpath(
                            "//a:graphicData[@uri='http://schemas.openxmlformats.org/drawingml/2006/diagram']",
                            namespaces={
                                "a": "http://schemas.openxmlformats.org/drawingml/2006/main"
                            },
                        )

                        for i, diagram in enumerate(diagrams):
                            element_id = f"slide_{slide_num}_diagram_{i}"
                            if element_id in element_map:
                                # 找到父元素
                                parent = diagram.getparent()
                                if parent is not None:
                                    # 替换元素
                                    idx = parent.index(diagram)
                                    parent.remove(diagram)
                                    parent.insert(idx, element_map[element_id])

                        # 处理超链接段落
                        hyperlink_runs = []
                        for i in range(100):  # 使用一个合理的上限来避免无限循环
                            element_id = f"slide_{slide_num}_hyperlink_run_{i}"
                            if element_id in element_map:
                                hyperlink_runs.append(
                                    (element_id, element_map[element_id])
                                )

                        if hyperlink_runs:
                            # 查找所有超链接运行元素
                            runs = xml_tree.xpath(
                                "//a:r[a:hlinkClick or a:hlinkHover]",
                                namespaces={
                                    "a": "http://schemas.openxmlformats.org/drawingml/2006/main"
                                },
                            )

                            run_count = 0
                            for run in runs:
                                # 获取文本元素
                                text_elements = run.xpath(
                                    ".//a:t",
                                    namespaces={
                                        "a": "http://schemas.openxmlformats.org/drawingml/2006/main"
                                    },
                                )

                                if (
                                    not text_elements
                                    or not text_elements[0].text
                                    or not text_elements[0].text.strip()
                                ):
                                    continue

                                element_id = (
                                    f"slide_{slide_num}_hyperlink_run_{run_count}"
                                )
                                run_count += 1

                                if element_id in element_map:
                                    logger.info(
                                        f"Processing hyperlink run: {element_id}"
                                    )
                                    # 从翻译后的XML中获取修改过的文本
                                    translated_text_elements = element_map[
                                        element_id
                                    ].xpath(
                                        ".//a:t",
                                        namespaces={
                                            "a": "http://schemas.openxmlformats.org/drawingml/2006/main"
                                        },
                                    )

                                    if translated_text_elements and len(
                                        text_elements
                                    ) == len(translated_text_elements):
                                        # 遍历并替换每个文本元素
                                        for i, text_elem in enumerate(text_elements):
                                            if i < len(translated_text_elements):
                                                # 确保我们有文本可以替换
                                                if translated_text_elements[i].text:
                                                    text_elem.text = (
                                                        translated_text_elements[i].text
                                                    )
                                                    text_elem.attrib["translated"] = (
                                                        "true"
                                                    )
                                                    logger.info(
                                                        f"Applied hyperlink translation: '{text_elem.text}'"
                                                    )
                                    else:
                                        # 如果元素数量不匹配或者无法替换文本，我们尝试一种简单方法
                                        if (
                                            len(translated_text_elements) > 0
                                            and translated_text_elements[0].text
                                        ):
                                            # 在只有一个文本元素的情况下简单地替换文本
                                            text_elements[
                                                0
                                            ].text = translated_text_elements[0].text
                                            text_elements[0].attrib["translated"] = (
                                                "true"
                                            )
                                            logger.info(
                                                f"Applied simple hyperlink translation: '{text_elements[0].text}'"
                                            )
                                        else:
                                            # 替换整个 run 元素
                                            parent = run.getparent()
                                            if parent is not None:
                                                idx = parent.index(run)
                                                parent.remove(run)
                                                parent.insert(
                                                    idx, element_map[element_id]
                                                )
                                                logger.info(
                                                    "Replaced entire hyperlink run"
                                                )

                        # 更新部分内容
                        parts[part_key]._blob = etree.tostring(
                            xml_tree, encoding="utf-8"
                        )
                        logger.info(f"更新部分: {part_uri}")
                    else:
                        logger.warning(f"未找到部分: {part_uri}")

        except Exception as e:
            logger.error(f"重建 PPTX 出错: {str(e)}")

        return parts
