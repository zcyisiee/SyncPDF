//! 标准 14 字体的内置度量。
//!
//! PDF 允许不内嵌的标准 14 字体（及其 Acrobat 备用名，如 `Arial,Bold`、
//! `TimesNewRomanPS-BoldMT`）省略 `/Widths`，字宽由阅读器内置的 AFM 决定——
//! arXiv 侧边水印就是这样的 Times-Roman。其它缺 `/Widths` 的字体仍按未知处理。
use crate::base14_widths::FONTS;
use crate::bind::strip_subset_prefix;

/// 内置度量表：`(unicode, width1000)`，按 unicode 升序。
#[derive(Debug, Clone, Copy)]
pub struct Base14(&'static [(u16, u16)]);

impl Base14 {
    /// 按 `/BaseFont` 查找；不是标准 14 字体（或其备用名）返回 `None`。
    pub fn find(base_font: &str) -> Option<Self> {
        let name = strip_subset_prefix(base_font);
        let (family, style) = match name.find([',', '-']) {
            Some(i) => (&name[..i], &name[i + 1..]),
            None => (name.as_str(), ""),
        };
        let family = family.strip_suffix("MT").unwrap_or(family);
        let family = family.strip_suffix("PS").unwrap_or(family);
        let style = style.strip_suffix("MT").unwrap_or(style);
        let (family, slant) = match family {
            "Times" | "TimesNewRoman" => ("Times", "Italic"),
            "Helvetica" | "Arial" => ("Helvetica", "Oblique"),
            "Courier" | "CourierNew" => ("Courier", "Oblique"),
            "Symbol" | "ZapfDingbats" if style.is_empty() => return Self::named(family),
            _ => return None,
        };
        let style = match style {
            "" | "Roman" | "Regular" => String::new(),
            "Bold" => "Bold".into(),
            "Italic" | "Oblique" => slant.into(),
            "BoldItalic" | "BoldOblique" => format!("Bold{slant}"),
            _ => return None,
        };
        match (family, style.as_str()) {
            ("Times", "") => Self::named("Times-Roman"),
            (family, "") => Self::named(family),
            (family, style) => Self::named(&format!("{family}-{style}")),
        }
    }

    fn named(name: &str) -> Option<Self> {
        FONTS
            .iter()
            .find(|(n, _)| *n == name)
            .map(|(_, table)| Self(table))
    }

    /// 某字形（按 Unicode）的宽度（1000 单位）；表里没有返回 `None`。
    pub fn width_1000(&self, ch: char) -> Option<f32> {
        let cp = u16::try_from(u32::from(ch)).ok()?;
        self.0
            .binary_search_by_key(&cp, |(c, _)| *c)
            .ok()
            .map(|i| f32::from(self.0[i].1))
    }
}

#[cfg(test)]
mod tests {
    use super::Base14;

    fn width(font: &str, ch: char) -> Option<f32> {
        Base14::find(font).and_then(|b| b.width_1000(ch))
    }

    #[test]
    fn standard_names_and_acrobat_alternates_share_the_afm_metrics() {
        // AFM：Times-Roman W 944 / Times-Bold W 1000 / Helvetica-BoldOblique a 556 / Courier 600。
        assert_eq!(width("Times-Roman", 'W'), Some(944.0));
        assert_eq!(width("ABCDEF+TimesNewRomanPSMT", 'W'), Some(944.0));
        assert_eq!(width("TimesNewRoman,Bold", 'W'), Some(1000.0));
        assert_eq!(width("TimesNewRomanPS-BoldMT", 'W'), Some(1000.0));
        assert_eq!(width("Arial,BoldItalic", 'a'), Some(556.0));
        assert_eq!(width("Helvetica-BoldOblique", 'a'), Some(556.0));
        assert_eq!(width("CourierNew,Italic", 'x'), Some(600.0));
        assert_eq!(width("Symbol", 'α'), Some(631.0));
    }

    #[test]
    fn other_fonts_and_missing_glyphs_stay_unknown() {
        for font in [
            "ArialNarrow",
            "Arial-Black",
            "Times-Light",
            "NimbusRomNo9L-Regu",
            "Symbol,Bold",
            "Foo",
        ] {
            assert!(Base14::find(font).is_none(), "{font}");
        }
        assert_eq!(width("Times-Roman", '中'), None);
    }
}
