from babeldoc.format.office.document_il.filetypes.base import TranslatablePartsProcessor
from babeldoc.format.office.document_il.filetypes.docx import DocxPartsProcessor
from babeldoc.format.office.document_il.filetypes.pptx import PptxPartsProcessor
from babeldoc.format.office.document_il.filetypes.xlsx import XlsxPartsProcessor
from babeldoc.format.office.document_il.opc.constants import CONTENT_TYPE as CT

TRANSLATABLE_PARTS_PROCESSOR = {
    CT.WML_DOCUMENT_MAIN: DocxPartsProcessor,
    CT.SML_SHEET_MAIN: XlsxPartsProcessor,
    CT.PML_PRESENTATION_MAIN: PptxPartsProcessor,
}
