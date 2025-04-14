import base64
import json
import os
from pathlib import PosixPath

from babeldoc.format.office.document_il.filetypes import TRANSLATABLE_PARTS_PROCESSOR
from babeldoc.format.office.document_il.opc.constants import CONTENT_TYPE as CT
from babeldoc.format.office.document_il.opc.package import OpcPackage
from babeldoc.format.office.document_il.opc.packuri import PackURI
from babeldoc.format.office.document_il.opc.part import Part
from babeldoc.format.office.document_il.opc.pkgwriter import PackageWriter
from babeldoc.format.office.document_il.opc.rel import Relationships
from babeldoc.format.office.document_il.types import ILData
from babeldoc.format.office.document_il.types import ILDocxData
from babeldoc.format.office.document_il.types import ILPptxData
from babeldoc.format.office.document_il.types import ILXlsxData
from babeldoc.format.office.document_il.utils import get_main_part
from loguru import logger


class OfficeBuilder:
    """从中间语言JSON文件重建Office文档"""

    def __init__(self, il_path: str):
        """
        初始化构建器

        Args:
            il_path: IL JSON文件路径
        """
        # 加载中间语言文件
        try:
            with open(il_path, encoding="utf-8") as f:
                il_data_dict = json.load(f)

                # 根据content_type创建对应的IL数据结构
                content_type = il_data_dict.get("content_type", "")

                # 使用字典映射内容类型到对应的IL数据类
                il_data_types = {
                    CT.WML_DOCUMENT_MAIN: ILDocxData,  # Word文档
                    CT.SML_SHEET_MAIN: ILXlsxData,  # Excel工作簿
                    CT.PML_PRESENTATION_MAIN: ILPptxData,  # PowerPoint演示文稿
                }

                # 获取对应的IL数据类型，如果不存在则使用基本的ILData
                il_data_class = il_data_types.get(content_type, ILData)

                # 创建IL数据实例
                self.il_data = il_data_class(**il_data_dict)

        except Exception as e:
            logger.exception(e)
            raise ValueError(f"无法加载中间语言文件: {str(e)}")

        # 创建新的文档
        self.parts: dict[str, Part] = {}
        self.rels = Relationships("/")
        self.package = OpcPackage()

        # 用于跟踪已处理的关系
        self.processed_rels = set()

    def _restore_parts(self):
        """还原文档部件"""
        try:
            for part_data in self.il_data.parts:
                if not part_data.part_uri or not part_data.xml_content:
                    continue

                # 解码Base64内容
                binary_data = base64.b64decode(part_data.xml_content)

                # 使用docx包的Part类创建新的部分
                part_uri = PackURI(part_data.part_uri)
                part = Part(part_uri, part_data.content_type, binary_data, self.package)

                # 将部分添加到文档包中
                self.parts[part_uri] = part

        except Exception as e:
            logger.exception(e)

    def _restore_relationships(self):
        try:
            main_part = get_main_part(self.parts)

            for rel_data in self.il_data.rels:
                if rel_data.external:
                    target_part = rel_data.target
                else:
                    target_part = self.parts[
                        os.path.normpath(
                            str(PosixPath(rel_data.baseURI) / rel_data.target)
                        )
                    ]

                if rel_data.baseURI == "/":
                    self.rels.add_relationship(
                        rel_data.type, target_part, rel_data.rId, rel_data.external
                    )
                else:
                    self.parts[rel_data.belongs_to].rels.add_relationship(
                        rel_data.type, target_part, rel_data.rId, rel_data.external
                    )

        except Exception as e:
            logger.exception(e)

    def _restore_translatable_parts(self):
        processor = TRANSLATABLE_PARTS_PROCESSOR[self.il_data.content_type]
        processor.write(self.il_data, self.parts)

    def build_document(self):
        """构建文档"""
        self._restore_parts()
        self._restore_relationships()
        self._restore_translatable_parts()

    def save(self, output_path: str):
        """保存文档到指定路径"""
        try:
            PackageWriter.write(output_path, self.rels, self.parts.values())
        except Exception as e:
            logger.exception(e)
            raise RuntimeError(f"保存文档失败: {str(e)}")


def restore_from_il(il_path: str, output_path: str):
    """
    从中间语言文件恢复Office文档

    Args:
        il_path: 中间语言JSON文件路径
        output_path: 输出文档文件路径
    """
    try:
        builder = OfficeBuilder(il_path)
        doc = builder.build_document()
        builder.save(output_path)
        # return doc
    except Exception as e:
        logger.exception(e)
        raise RuntimeError(f"从中间语言恢复文档失败: {str(e)}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("用法: python il_docx_builder.py <输入IL文件> <输出文档文件>")
        sys.exit(1)

    input_path = sys.argv[1]
    output_path = sys.argv[2]

    if not os.path.exists(input_path):
        print(f"错误: 输入文件 {input_path} 不存在")
        sys.exit(1)

    try:
        restore_from_il(input_path, output_path)
        print(f"成功将中间语言文件 {input_path} 转换为文档文件 {output_path}")
    except Exception as e:
        print(f"转换失败: {e}")
        sys.exit(1)
