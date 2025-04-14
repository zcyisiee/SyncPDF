import base64
import json
import os
import sys

from babeldoc.format.office.document_il.filetypes import TRANSLATABLE_PARTS_PROCESSOR
from babeldoc.format.office.document_il.filetypes import TranslatablePartsProcessor
from babeldoc.format.office.document_il.opc.constants import CONTENT_TYPE as CT

# from docx.parts import Part
from babeldoc.format.office.document_il.opc.package import OpcPackage
from babeldoc.format.office.document_il.opc.package import Unmarshaller
from babeldoc.format.office.document_il.opc.part import Part
from babeldoc.format.office.document_il.opc.part import PartFactory
from babeldoc.format.office.document_il.opc.pkgreader import PackageReader

# from docx.opc.part import PartFactory
from babeldoc.format.office.document_il.types import ILData
from babeldoc.format.office.document_il.types import ILDataDocument
from babeldoc.format.office.document_il.types import ILDataPart
from babeldoc.format.office.document_il.types import ILDataRel
from babeldoc.format.office.document_il.types import ILDocxData
from babeldoc.format.office.document_il.types import ILPptxData
from babeldoc.format.office.document_il.types import ILXlsxData
from loguru import logger
from lxml import etree


class OfficeILCreator:
    translatable_parts_processor: TranslatablePartsProcessor

    @staticmethod
    def _get_xml_base64(element) -> str:
        """获取元素的原始XML字符串"""
        if isinstance(element, Part):
            return base64.b64encode(element.blob).decode("utf-8")
        elif hasattr(element, "element"):
            return (
                etree.tostring(element.element, encoding="unicode", pretty_print=True)
                if element
                else ""
            )
        return (
            etree.tostring(element, encoding="unicode", pretty_print=True)
            if element
            else ""
        )

    def __init__(self, docx_path: str):
        if not os.path.exists(docx_path):
            raise FileNotFoundError(f"Docx file not found: {docx_path}")

        try:
            pkg_reader = PackageReader.from_file(docx_path)
            package = OpcPackage()
            Unmarshaller.unmarshal(pkg_reader, package, PartFactory)

            document_part = package.main_document_part

            if document_part.content_type not in TRANSLATABLE_PARTS_PROCESSOR:
                tmpl = "File '%s' is not supported, content type is '%s'"
                raise ValueError(tmpl % (docx_path, document_part.content_type))

            self.translatable_parts_processor = TRANSLATABLE_PARTS_PROCESSOR[
                document_part.content_type
            ]
            self.package = package

            # 根据文件类型创建不同的IL数据结构
            il_data_types = {
                CT.WML_DOCUMENT_MAIN: ILDocxData,
                CT.SML_SHEET_MAIN: ILXlsxData,
                CT.PML_PRESENTATION_MAIN: ILPptxData,
            }

            # 基本参数，所有类型都需要
            base_params = {
                "content_type": document_part.content_type,
                "parts": [],
                "rels": [],
                "document": ILDataDocument(elements=[], rels=[]),
            }

            # 获取对应的IL数据类型，如果不存在则使用基本的ILData
            il_data_class = il_data_types.get(document_part.content_type, ILData)

            params = {**base_params}

            # 创建IL数据实例
            self.il_data = il_data_class(**params)

        except Exception as e:
            raise ValueError(f"Error initializing DocxILCreator: {str(e)}")

    def _process_parts(self):
        """处理文档中的所有部分"""
        try:
            for part in self.package.iter_parts():
                il_part = ILDataPart(
                    part_uri=str(part.partname),
                    content_type=part.content_type,
                    xml_content=self._get_xml_base64(part),
                )
                self.il_data.parts.append(il_part)
        except Exception as e:
            logger.exception(f"Warning: Error processing parts: {str(e)}")

    def _process_relationships(self):
        try:
            for rel in self.package.iter_rels():
                il_rel = ILDataRel(
                    rId=rel.rId,
                    target=rel.target_ref,
                    type=rel.reltype,
                    baseURI=rel._baseURI,
                    belongs_to=rel._belongs_to,
                    external=rel.is_external,
                )
                self.il_data.rels.append(il_rel)
        except Exception as e:
            logger.exception(e)

    def _process_main_part(self):
        """处理主部分"""
        return self.translatable_parts_processor.read(self.il_data)

    def process_document(self):
        """处理文档所有内容"""
        try:
            # 提取文档元数据
            self._process_parts()
            self._process_relationships()
            self._process_main_part()

            return self.il_data
        except Exception as e:
            raise RuntimeError(f"Error processing document: {str(e)}")

    def save_il(self, output_path: str):
        """保存中间语言文件"""
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(self.il_data.model_dump(), f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    # 使用当前目录下存在的测试文档
    input_path = "test.docx"
    if not os.path.exists(input_path):
        print(f"错误：输入文件 {input_path} 不存在")
        sys.exit(1)

    creator = OfficeILCreator(input_path)
    creator.process_document()
    creator.save_il("output.il.json")
