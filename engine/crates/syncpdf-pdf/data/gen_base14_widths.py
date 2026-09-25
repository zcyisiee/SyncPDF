"""生成 `src/base14_widths.rs`：标准 14 字体的推进宽度（1000 单位），按 Unicode 排序。

数据取自 MuPDF 内置的 URW Base-14 字体（与 Adobe Core14 AFM 度量兼容）。
用法：python crates/syncpdf-pdf/data/gen_base14_widths.py > crates/syncpdf-pdf/src/base14_widths.rs
"""

import pymupdf

FONTS = [
    "Times-Roman", "Times-Bold", "Times-Italic", "Times-BoldItalic",
    "Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique",
    "Courier", "Courier-Bold", "Courier-Oblique", "Courier-BoldOblique",
    "Symbol", "ZapfDingbats",
]

print("//! 由 `data/gen_base14_widths.py` 生成，勿手改。")
print("//! 标准 14 字体推进宽度（1000 单位），`(unicode, width)` 按 unicode 升序。")
print()
print("#[rustfmt::skip]")
print("pub(crate) static FONTS: [(&str, &[(u16, u16)]); 14] = [")
for name in FONTS:
    font = pymupdf.Font(fontname=name)
    pairs = sorted(
        (cp, round(font.glyph_advance(cp) * 1000))
        for cp in font.valid_codepoints()
        if 0 < cp <= 0xFFFF
    )
    body = ",".join(f"({cp},{w})" for cp, w in pairs)
    print(f'    ("{name}", &[{body}]),')
print("];")
