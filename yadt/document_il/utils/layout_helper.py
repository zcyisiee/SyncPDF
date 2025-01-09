from yadt.document_il.il_version_1 import PdfCharacter

def get_char_unicode_string(char: [PdfCharacter]) -> str:
    return "".join(char.char_unicode for char in char)