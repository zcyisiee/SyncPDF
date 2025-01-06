import pymupdf
from bitstring import Bits, BitStream

from yadt.il_try_1.document_il import il_try_1


class PDFCreater:
    def __init__(self, original_pdf_path: str, document: il_try_1.Document):
        self.original_pdf_path = original_pdf_path
        self.docs = document

    def render_graphic_state(
        self, draw_op: BitStream, graphic_state: il_try_1.GraphicState
    ):
        if graphic_state is None:
            return
        if graphic_state.stroking_color_space_name:
            draw_op.append(
                f"/{graphic_state.stroking_color_space_name}"
                f" CS \n".encode()
            )
        if graphic_state.non_stroking_color_space_name:
            draw_op.append(
                f"/{graphic_state.non_stroking_color_space_name}"
                f" cs \n".encode()
            )
        if graphic_state.ncolor is not None:
            draw_op.append(
                f"{' '.join((str(x) for x in graphic_state.ncolor))
                   } sc \n".encode()
            )
        if graphic_state.scolor is not None:
            draw_op.append(
                f"{' '.join((str(x) for x in graphic_state.ncolor))
                   } SC \n".encode()
            )

    def render_paragraph_to_char(
        self, paragraph: il_try_1.PdfParagraph
    ) -> [il_try_1.PdfCharacter]:
        if paragraph.pdf_character:
            return paragraph.pdf_character
        raise NotImplementedError

    def write(self, out_file: str):
        pdf = pymupdf.open(self.original_pdf_path)
        for page in self.docs.page:
            draw_op = BitStream()
            # q {ops_base}Q 1 0 0 1 {x0} {y0} cm {ops_new}
            draw_op.append(b"q ")
            draw_op.append(page.base_operations.value.encode())
            draw_op.append(b" Q ")
            draw_op.append(
                f"q Q 1 0 0 1 {page.cropbox.box.x} {
                    page.cropbox.box.y} cm \n".encode()
            )
            # draw_op.append(b'q ')
            chars = page.pdf_character
            for paragraph in page.pdf_paragraph:
                chars.extend(paragraph.pdf_character)

            for char in chars:
                if char.char_unicode == "\n":
                    continue
                char_size = char.size
                draw_op.append(b"q ")
                self.render_graphic_state(draw_op, char.graphic_state)
                draw_op.append(
                    f"BT /{char.pdf_font_id} {char_size:f} Tf 1 0 0 1 {
                        char.box.x:f} {char.box.y:f} Tm (".encode()
                )
                # pdf 32000 2008: 15
                # 7.3.4.2 Literal Strings
                if char.pdf_character_id in (
                    # ord("["),
                    # ord("]"),
                    ord("\\"),
                    ord("("),
                    ord(")"),
                ):
                    draw_op.append(b"\\")
                char_bit_length = 8
                if char.pdf_character_id >= 2**16:
                    char_bit_length = 32
                elif char.pdf_character_id >= 2**8:
                    char_bit_length = 16

                draw_op.append(
                    Bits(
                        uint=char.pdf_character_id, length=char_bit_length
                    ).tobytes()
                )

                draw_op.append(b") Tj ET Q \n")

            # draw_op.append(b'Q ')

            op_container = pdf.get_new_xref()
            # Since this is a draw instruction container,
            # no additional information is needed
            pdf.update_object(op_container, "<<>>")
            pdf.update_stream(op_container, draw_op.tobytes())
            pdf[page.page_number].set_contents(op_container)
        pdf.save(out_file, expand=True, pretty=True)
        pdf.save(out_file + ".compressed.pdf", garbage=3, deflate=True)
