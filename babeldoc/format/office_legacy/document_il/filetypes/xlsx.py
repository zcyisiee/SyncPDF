import base64
import time
import uuid
from collections import defaultdict

from loguru import logger
from lxml import etree

from babeldoc.format.office.context import Context
from babeldoc.format.office.document_il.filetypes.base import TranslatablePartsProcessor
from babeldoc.format.office.document_il.opc.packuri import PackURI
from babeldoc.format.office.document_il.opc.part import Part
from babeldoc.format.office.document_il.types import DefinedName
from babeldoc.format.office.document_il.types import ILDataDElement
from babeldoc.format.office.document_il.types import ILDataPart
from babeldoc.format.office.document_il.types import ILXlsxData
from babeldoc.format.office.document_il.types import SharedString
from babeldoc.format.office.document_il.types import SheetName
from babeldoc.format.office.document_il.utils import QueueProcessor
from babeldoc.format.office.document_il.utils import group_hashes
from babeldoc.format.office.document_il.utils import hash_dict

BATCH_SIZE = 20


def generate_unique_id(xml_content=None):
    """Generate a unique identifier string for XML elements

    Args:
        xml_content: Optional XML content to use for generating a deterministic ID

    Returns:
        A unique identifier prefixed with 'babel_'
    """
    if xml_content:
        # Generate a hash based on the XML content
        import hashlib

        hash_obj = hashlib.sha256(str(xml_content).encode("utf-8"))
        return f"babel_{hash_obj.hexdigest()[:12]}"
    else:
        # Fallback to UUID if no XML content provided
        return f"babel_{uuid.uuid4().hex[:12]}"


def process_shared_strings(
    element: etree.Element, il_data: ILXlsxData, context: Context
):
    """
    Process shared strings for translation by batching them for context preservation

    Args:
        element: The shared strings element (either sst root or individual si element)
        il_data: The IL data
        context: The context
    """
    # Check if already translated
    if element.attrib.get("translated", "false") == "true":
        return

    # Extract text from t elements (may be multiple t elements for rich text)
    text_elements = element.findall(".//t", namespaces=element.nsmap)
    text = "".join([t.text or "" for t in text_elements])

    if not text.strip():
        il_data.shared_string_index_map.pop(element.attrib.get("babel_id"), None)
        return

    # Extract style information
    style = {}
    rPr_elements = element.findall(".//rPr", namespaces=element.nsmap)
    for rPr in rPr_elements:
        for style_elem in rPr:
            style[style_elem.tag.split("}")[-1]] = dict(style_elem.attrib)

    style_hash = hash_dict(style)

    # Get element ID
    element_id = element.attrib.get("babel_id")
    if not element_id:
        logger.warning("No babel_id found for shared string.")
        element_id = generate_unique_id(
            etree.tostring(element, encoding="unicode", method="xml")
        )
    # Create a single SharedString object for this element
    shared_string = SharedString(
        text=text, style_hash=style_hash, style=style, element_id=element_id
    )

    # Initialize batch context if not exists
    if not hasattr(context, "shared_string_batch"):
        context.shared_string_batch = QueueProcessor(
            threshold=BATCH_SIZE, process_interval=0.3
        )
        context.shared_string_batch.process_items = (
            lambda x: _process_shared_string_batch(context, il_data, x)
        )

    context.shared_string_batch.add_item(shared_string)


def _process_shared_string_batch(
    context: Context, il_data: ILXlsxData, batch: list[SharedString]
):
    """
    Process a batch of shared strings for translation

    Args:
        context: The context containing the batch
        il_data: The IL data
    """

    # Translate texts in batch
    translator = context.translator
    translated_texts = translator.translate(
        [shared_string.text for shared_string in batch],
        group_hashes([shared_string.style_hash for shared_string in batch]),
    )

    if len(translated_texts) != len(batch):
        logger.warning(
            f"Failed to translate {len(translated_texts)} shared strings from {len(batch)}"
        )
        logger.warning(f"Batch: {[shared_string.text for shared_string in batch]}")
        logger.warning(f"Translated texts: {translated_texts}")

    il_data.shared_strings.extend(batch)

    # Update each element with its translation
    for i, (shared_string, translated_text) in enumerate(
        zip(batch, translated_texts, strict=False)
    ):
        # Update the shared string object
        shared_string.translated_text = translated_text

        # Also update the dictionary for backward compatibility and easy lookup
        if not hasattr(il_data, "translated_shared_strings"):
            il_data.translated_shared_strings = {}

        # print(f"Translated shared string: {shared_string.text} -> {shared_string.translated_text}")
        # print(len(il_data.shared_strings), len(il_data.shared_string_index_map.items()))
        il_data.translated_shared_strings[shared_string.text] = (
            shared_string.translated_text
        )
        original_element = il_data.document.elements[
            il_data.shared_string_index_map[shared_string.element_id]
        ]

        if not shared_string.style:
            # If no style, use simple text replacement
            original_element.handled_xml = original_element.original_xml.replace(
                shared_string.text, shared_string.translated_text
            )
        else:
            # If there's style, parse the XML and rebuild with the translated text
            root = etree.fromstring(original_element.original_xml)

            # Remove all child elements while keeping the root
            for child in list(root):
                root.remove(child)

            # Create a run element with the style if available
            r_element = etree.SubElement(root, "r")
            rPr_element = etree.SubElement(r_element, "rPr")

            # Add style properties to rPr
            for key, value in shared_string.style.items():
                if isinstance(value, dict):
                    style_elem = etree.SubElement(rPr_element, key)
                    for attr_key, attr_val in value.items():
                        style_elem.attrib[attr_key] = attr_val

            # Add text element with translated text
            t_element = etree.SubElement(r_element, "t")
            t_element.text = shared_string.translated_text

            # Convert back to XML string
            original_element.handled_xml = etree.tostring(
                root, encoding="unicode", method="xml"
            )

    if len(il_data.shared_strings) == len(il_data.shared_string_index_map.items()):
        logger.info("Translate shared string finished")


def process_sheet_names(element: etree.Element, il_data: ILXlsxData, context: Context):
    """
    Process sheet names for translation

    Args:
        element: The workbook element
        il_data: The IL data
        context: The context
    """
    sheet_names = []

    # Find all sheet elements
    sheet_elements = element.findall(".//sheet", namespaces=element.nsmap)

    # Create a mapping of element IDs to elements
    sheet_elements_by_id = {}

    for sheet in sheet_elements:
        # Skip already translated sheet names
        if sheet.attrib.get("translated", "false") == "true":
            continue

        name = sheet.attrib.get("name", "")
        sheet_id = sheet.attrib.get("sheetId", "")
        r_id = sheet.attrib.get(
            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id",
            "",
        )

        if not name:
            continue

        # Get or create element ID
        element_id = sheet.attrib.get("babel_id")
        if not element_id:
            logger.warning("No babel_id found for sheet name.")
            element_id = generate_unique_id(
                etree.tostring(sheet, encoding="unicode", method="xml")
            )
            sheet.attrib["babel_id"] = element_id

        # Store element in mapping
        sheet_elements_by_id[element_id] = sheet

        sheet_names.append(
            SheetName(name=name, sheet_id=sheet_id, r_id=r_id, element_id=element_id)
        )

    if not sheet_names:
        return

    # Prepare names for translation
    names = [sn.name for sn in sheet_names]

    # Translate names
    translator = context.translator
    translated_names = translator.translate(names, None)

    # Update the sheet elements with translations
    for i, sheet_name in enumerate(sheet_names):
        sheet_name.translated_name = translated_names[i]

        # Find the corresponding sheet element by ID
        sheet = sheet_elements_by_id[sheet_name.element_id]

        # Update the name attribute
        sheet.attrib["name"] = sheet_name.translated_name

        # Mark as translated
        sheet.attrib["translated"] = "true"

    # Add the sheet names to the IL data's structured list
    il_data.sheet_names.extend(sheet_names)

    # Also update the dictionary for backward compatibility and easy lookup
    if not hasattr(il_data, "translated_sheet_names"):
        il_data.translated_sheet_names = {}

    for sn in sheet_names:
        il_data.translated_sheet_names[sn.name] = sn.translated_name


def process_defined_names(
    element: etree.Element, il_data: ILXlsxData, context: Context
):
    """
    Process defined names for translation

    Args:
        element: The workbook element
        il_data: The IL data
        context: The context
    """
    while len(il_data.shared_strings) != len(il_data.shared_string_index_map.items()):
        time.sleep(0.1)

    defined_names = []

    # Find the definedNames element
    defined_names_element = element.find(".//definedNames", namespaces=element.nsmap)

    if defined_names_element is None:
        return

    # Find all definedName elements
    defined_name_elements = defined_names_element.findall(
        ".//definedName", namespaces=element.nsmap
    )

    # Create a mapping of element IDs to elements
    defined_name_elements_by_id = {}

    for dn in defined_name_elements:
        # Skip already translated defined names
        if dn.attrib.get("translated", "false") == "true":
            continue

        name = dn.attrib.get("name", "")
        local_sheet_id = dn.attrib.get("localSheetId")
        formula = dn.text or ""

        if not name:
            continue

        # Get or create element ID
        element_id = dn.attrib.get("babel_id")
        if not element_id:
            logger.warning("No babel_id found for defined name.")
            element_id = generate_unique_id(
                etree.tostring(dn, encoding="unicode", method="xml")
            )
            dn.attrib["babel_id"] = element_id

        # Store element in mapping
        defined_name_elements_by_id[element_id] = dn

        defined_names.append(
            DefinedName(
                name=name,
                local_sheet_id=local_sheet_id,
                formula=formula,
                element_id=element_id,
            )
        )

    if not defined_names:
        return

    # Prepare names for translation
    names = [dn.name for dn in defined_names]

    # Translate names
    translator = context.translator
    translated_names = translator.translate(names, None)

    # Update the definedName elements with translations
    for i, defined_name in enumerate(defined_names):
        defined_name.translated_name = translated_names[i]

        # Find the corresponding definedName element by ID
        dn = defined_name_elements_by_id[defined_name.element_id]

        # Update the name attribute
        dn.attrib["name"] = defined_name.translated_name

        # Mark as translated
        dn.attrib["translated"] = "true"

    # Add the defined names to the IL data's structured list
    il_data.defined_names.extend(defined_names)

    # Also update the dictionary for backward compatibility and easy lookup
    if not hasattr(il_data, "translated_defined_names"):
        il_data.translated_defined_names = {}

    for dn in defined_names:
        il_data.translated_defined_names[dn.name] = dn.translated_name


def _process_element_text(
    element: etree.Element, il_data: ILXlsxData, element_type: str
) -> bool:
    """
    Process text content of an element (formula or value) to replace shared strings and defined names

    Args:
        element: The element to process (formula or value)
        il_data: The IL data containing translations
        element_type: String indicating element type ("formula" or "value") for logging

    Returns:
        bool: True if changes were made to the element, False otherwise
    """
    if element.attrib.get("translated", "false") == "true":
        return False

    element_text = element.text or ""

    # Skip empty content or numbers
    if not element_text or element_text.isdigit():
        return False

    updated_text = element_text

    # Replace occurrences of shared strings
    if element_text in il_data.translated_shared_strings:
        element.text = il_data.translated_shared_strings[element_text]
    # Replace occurrences of defined names
    for defined_name in il_data.defined_names:
        if defined_name.translated_name:
            # Make sure we're replacing whole words only
            original = defined_name.name
            translated = defined_name.translated_name
            updated_text = updated_text.replace(original, translated)

    # Update the element if changes were made
    if updated_text != element_text:
        element.text = updated_text
        element.attrib["translated"] = "true"
        return True

    return False


def process_cell_contents(
    element: etree.Element, il_data: ILXlsxData, context: Context
):
    """
    Update formula strings and values in cells to use consistent translations

    Args:
        element: The worksheet element
        il_data: The IL data
        context: The context (not used for worksheets but kept for signature consistency)
    """
    while len(il_data.shared_strings) != len(il_data.shared_string_index_map.items()):
        time.sleep(0.1)

    # Check if we have translations to apply
    if not (il_data.shared_strings or il_data.defined_names):
        return

    # Find all formula and value elements
    formula_elements = element.findall(".//f", namespaces=element.nsmap)
    value_elements = element.findall(".//v", namespaces=element.nsmap)

    # Process formula elements
    for formula in formula_elements:
        _process_element_text(formula, il_data, "formula")

    # Process value elements
    for value in value_elements:
        _process_element_text(value, il_data, "value")


def _read_shared_strings(root: etree.Element, il_data: ILXlsxData, part: ILDataPart):
    """
    Read shared strings from the element
    """
    il_data.shared_string_root = ILDataDElement(
        part_uri=part.part_uri,
        element_type="sst",
        original_xml=etree.tostring(root, encoding="unicode", method="xml"),
    )

    # Extract each si element individually
    for si_index, si in enumerate(root.findall(".//si", namespaces=root.nsmap)):
        element_id = generate_unique_id(
            etree.tostring(si, encoding="unicode", method="xml")
        )
        si.attrib["babel_id"] = element_id

        # Create separate ILDataDElement for each si element
        il_element_si = ILDataDElement(
            part_uri=part.part_uri,
            element_type="si",
            element_id=element_id,
            element_index=si_index,
            original_xml=etree.tostring(si, encoding="unicode", method="xml"),
        )
        current_length = len(il_data.document.elements)
        il_data.document.elements.append(il_element_si)
        il_data.shared_string_index_map[element_id] = current_length


def _read_workbook(root: etree.Element, il_data: ILXlsxData, part: ILDataPart):
    """
    Read workbook from the element
    """
    root_tag = etree.QName(root).localname
    for sheet in root.findall(".//sheet", namespaces=root.nsmap):
        element_id = generate_unique_id(
            etree.tostring(sheet, encoding="unicode", method="xml")
        )
        sheet.attrib["babel_id"] = element_id

    # Add IDs to defined names
    defined_names_elem = root.find(".//definedNames", namespaces=root.nsmap)
    if defined_names_elem is not None:
        for defined_name in defined_names_elem.findall(
            ".//definedName", namespaces=root.nsmap
        ):
            element_id = generate_unique_id(
                etree.tostring(defined_name, encoding="unicode", method="xml")
            )
            defined_name.attrib["babel_id"] = element_id

    # Create ILDataDElement for workbook
    return ILDataDElement(
        part_uri=part.part_uri,
        element_type=root_tag,
        original_xml=etree.tostring(root, encoding="unicode", method="xml"),
    )


def _read_worksheet(root: etree.Element, il_data: ILXlsxData, part: ILDataPart):
    root_tag = etree.QName(root).localname
    for formula in root.findall(".//f", namespaces=root.nsmap):
        if formula.text and formula.text.strip():
            element_id = generate_unique_id()
            formula.attrib["babel_id"] = element_id

    return ILDataDElement(
        part_uri=part.part_uri,
        element_type=root_tag,
        original_xml=etree.tostring(root, encoding="unicode", method="xml"),
    )


class XlsxPartsProcessor(TranslatablePartsProcessor):
    handlers = {
        # "workbook": lambda element, il_data, context: (
        #     # process_sheet_names(element, il_data, context),
        #     # process_defined_names(element, il_data, context)
        # ),
        "worksheet": process_cell_contents,
        "si": process_shared_strings,
    }

    @staticmethod
    def read(il_data: ILXlsxData):
        """Process Excel parts for translation"""
        sheets = []
        try:
            # Initialize structured model lists if they don't exist
            if not hasattr(il_data, "shared_strings"):
                il_data.shared_strings = []
            if not hasattr(il_data, "sheet_names"):
                il_data.sheet_names = []
            if not hasattr(il_data, "defined_names"):
                il_data.defined_names = []

            # 只处理需要翻译的部分：workbook、sharedStrings和worksheet
            needed_part_types = {
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",  # 工作簿
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml",  # 共享字符串
                "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",  # 工作表
            }

            sheets = []
            for part in il_data.parts:
                # 只处理需要的部分类型
                if part.content_type not in needed_part_types:
                    continue

                # 解析XML内容
                xml_content = base64.b64decode(part.xml_content)
                root = etree.fromstring(xml_content)

                # 获取根元素的本地名称
                root_tag = etree.QName(root).localname

                if root_tag == "sst":
                    _read_shared_strings(root, il_data, part)

                elif root_tag == "workbook":
                    il_element = _read_workbook(root, il_data, part)
                    il_data.document.elements.append(il_element)

                elif root_tag == "worksheet":
                    il_element = _read_worksheet(root, il_data, part)
                    sheets.append(il_element)

            # sheets should be the last elements
            il_data.document.elements.extend(sheets)

        except Exception as e:
            logger.exception(f"处理Excel部分时出错: {str(e)}")
            raise ValueError(f"处理Excel部分时出错: {str(e)}")

    @staticmethod
    def write(il_data: ILXlsxData, parts: dict[PackURI, Part]):
        """Write back the translated Excel parts"""
        try:
            # Group elements by part_uri and element_type
            elements_by_part = defaultdict(list)
            for il_element in il_data.document.elements:
                elements_by_part[(il_element.part_uri, il_element.element_type)].append(
                    il_element
                )

            translated_shared_map = {}
            for translated_shared_string in il_data.shared_strings:
                translated_shared_map[translated_shared_string.element_id] = (
                    translated_shared_string.translated_text
                )

            # Process each part
            for part_uri, part in parts.items():
                # Handle shared strings table (sst) specially
                il_si_elements = elements_by_part.get((part_uri, "si"), [])

                if il_si_elements:
                    # Get the root element
                    root_element = il_data.shared_string_root
                    root = etree.fromstring(
                        root_element.handled_xml
                        if root_element.handled_xml
                        else root_element.original_xml
                    )
                    for child in list(root):
                        root.remove(child)

                    # Sort si elements by their original index to maintain order
                    il_si_elements.sort(key=lambda e: getattr(e, "element_index", 0))

                    # Replace each si element in the root with its translated version
                    for si in il_si_elements:
                        root.append(
                            etree.fromstring(
                                si.handled_xml if si.handled_xml else si.original_xml
                            )
                        )

                    # Update the part's content with the merged XML
                    part._blob = etree.tostring(root, encoding="unicode", method="xml")

                else:
                    # Handle regular elements (workbook, worksheet)
                    regular_elements = elements_by_part.get(
                        (part_uri, "workbook"), []
                    ) + elements_by_part.get((part_uri, "worksheet"), [])

                    if regular_elements:
                        # Just use the first matching element (there should only be one per part_uri/element_type)
                        il_element = regular_elements[0]

                        # Use the handled XML if available, otherwise use original XML
                        xml_content = (
                            il_element.handled_xml
                            if il_element.handled_xml
                            else il_element.original_xml
                        )

                        # Update the part's blob with the XML content
                        part._blob = xml_content

        except Exception as e:
            logger.exception(f"Error writing Excel parts: {str(e)}")
            raise RuntimeError(f"Error writing Excel parts: {str(e)}")
