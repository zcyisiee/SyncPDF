import base64

from loguru import logger
from lxml import etree
from pydantic import BaseModel

from babeldoc.format.office.context import Context
from babeldoc.format.office.document_il.filetypes.base import TranslatablePartsProcessor
from babeldoc.format.office.document_il.opc.packuri import PackURI
from babeldoc.format.office.document_il.opc.part import Part
from babeldoc.format.office.document_il.types import ILDataDElement
from babeldoc.format.office.document_il.types import ILDocxData
from babeldoc.format.office.document_il.utils import get_main_part
from babeldoc.format.office.document_il.utils import group_hashes
from babeldoc.format.office.document_il.utils import hash_dict


class PHandlerRun(BaseModel):
    style_hash: str
    text: str
    style: dict


def p_handler(element: etree.Element, il_data: ILDocxData, context: Context):
    """
    读取所有的run，并进行翻译

    Args:
        element: 要处理的段落元素
        il_data: 要处理的IL数据
        context: 上下文
    """
    # 查找所有的run元素
    run_elements = element.findall(".//w:r", namespaces=element.nsmap)
    text = []
    style_hash_list = []

    # 处理每个run元素
    for run in run_elements:
        if run.attrib.get("translated", "false") == "true":
            continue
        # 获取run的样式信息
        style = {}
        rPr = run.find(".//w:rPr", namespaces=element.nsmap)
        if rPr is not None:
            # 提取样式属性，如字体、大小、颜色等
            for style_elem in rPr:
                style[style_elem.tag.split("}")[-1]] = dict(style_elem.attrib)

        # Create a hash string representation of the style dictionary
        # Convert nested dictionaries to tuples for hashing
        style_hash = hash_dict(style)
        style_hash_list.append(style_hash)

        # 获取文本内容
        text_elements = run.findall(".//w:t", namespaces=element.nsmap)
        text += [t.text or "" for t in text_elements]

    if not text:
        return

    grouped_hashes = group_hashes(style_hash_list)

    translator = context.translator

    translated_text = translator.translate(text, grouped_hashes)

    offset = 0
    for i, run in enumerate(run_elements):
        try:
            run.attrib["translated"] = "true"
            text_elements = run.findall(".//w:t", namespaces=element.nsmap)
            if not text_elements:
                # 可能存在没有t元素的run，这种情况需要跳过
                offset += 1
                continue
            text_elements[0].text = translated_text[i - offset]

        except Exception as e:
            raise e


class DocxPartsProcessor(TranslatablePartsProcessor):
    handlers = {
        "p": p_handler,
    }

    @staticmethod
    def read(il_data: ILDocxData):
        """处理主部分"""
        try:
            main_part = get_main_part(il_data.parts)
            main_part_xml = base64.b64decode(main_part.xml_content)
            root = etree.fromstring(main_part_xml)

            body_element = root.find(".//w:body", namespaces=root.nsmap)

            il_data.document.namespaces = root.nsmap

            if body_element is not None:
                for element in body_element:
                    if isinstance(element, etree._Comment):
                        continue

                    element_tag = etree.QName(element).localname

                    il_element = ILDataDElement(
                        part_uri=main_part.part_uri,
                        element_type=element_tag,
                        original_xml=etree.tostring(
                            element, encoding="unicode", method="xml"
                        ),
                    )

                    il_data.document.elements.append(il_element)
        except Exception as e:
            raise ValueError(f"Error processing main part: {str(e)}")

    @staticmethod
    def write(il_data: ILDocxData, parts: dict[PackURI, Part]):
        try:
            # 获取主文档部分
            main_part = get_main_part(il_data.parts)

            # 获取主文档XML内容并解析
            main_part_xml = base64.b64decode(main_part.xml_content)
            root = etree.fromstring(main_part_xml)

            # 查找body元素
            body_element = root.find(
                ".//w:body", namespaces=il_data.document.namespaces
            )

            if body_element is not None:
                # 清除body中的所有现有元素
                for child in list(body_element):
                    body_element.remove(child)

                # 从IL数据中恢复元素
                for il_element in il_data.document.elements:
                    restore_xml = (
                        il_element.handled_xml
                        if il_element.handled_xml
                        else il_element.original_xml
                    )
                    element = etree.fromstring(restore_xml)
                    body_element.append(element)

            # 更新主文档部分的XML内容
            package_main_part = get_main_part(parts)
            package_main_part._blob = etree.tostring(
                root, encoding="unicode", method="xml"
            )

        except Exception as e:
            logger.exception(f"重建主文档部分失败: {str(e)}")
            raise RuntimeError(f"重建主文档部分失败: {str(e)}")
