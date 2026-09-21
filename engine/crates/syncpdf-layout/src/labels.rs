//! PP-DocLayoutV3 的 25 类标签表与 `RegionKind` 映射。
//!
//! 类别顺序（id 0..24）与 oar-ocr-core 0.9.2 的 `pp_doclayoutv3()` 预设一致，
//! 也与 hjfy 二进制内的字母序标签数组（`abstract..vision_footnote`）相印证。

use syncpdf_core::ir::RegionKind;

/// PP-DocLayoutV3 类别 id → 类别名。
pub const LABELS: [&str; 25] = [
    "abstract",          // 0
    "algorithm",         // 1
    "aside_text",        // 2
    "chart",             // 3
    "content",           // 4（目录）
    "display_formula",   // 5
    "doc_title",         // 6
    "figure_title",      // 7
    "footer",            // 8
    "footer_image",      // 9
    "footnote",          // 10
    "formula_number",    // 11
    "header",            // 12
    "header_image",      // 13
    "image",             // 14
    "inline_formula",    // 15
    "number",            // 16（页码）
    "paragraph_title",   // 17
    "reference",         // 18
    "reference_content", // 19
    "seal",              // 20
    "table",             // 21
    "text",              // 22
    "vertical_text",     // 23
    "vision_footnote",   // 24
];

/// 类别名 → `RegionKind`（`core::ir` 的 14 类归并）。
///
/// 归并原则：正文/标题类参与翻译；公式与代码不译；装饰类归 Other。
pub fn to_region_kind(label: &str) -> RegionKind {
    match label {
        "text" | "aside_text" | "vertical_text" => RegionKind::Text,
        "doc_title" => RegionKind::Title,
        "paragraph_title" | "content" => RegionKind::ParagraphTitle,
        "abstract" => RegionKind::Abstract,
        "footnote" | "vision_footnote" => RegionKind::FootNote,
        "reference" | "reference_content" => RegionKind::Reference,
        "algorithm" => RegionKind::Code,
        "table" => RegionKind::Table,
        "image" | "footer_image" | "header_image" | "seal" | "chart" => RegionKind::Figure,
        "figure_title" => RegionKind::Caption,
        "display_formula" | "inline_formula" | "formula_number" => RegionKind::Formula,
        "header" => RegionKind::Header,
        "footer" | "number" => RegionKind::Footer,
        _ => RegionKind::Other,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn labels_cover_all_ids() {
        for (id, name) in LABELS.iter().enumerate() {
            assert!(!name.is_empty(), "empty label at {id}");
        }
        assert_eq!(LABELS[22], "text");
        assert_eq!(LABELS[14], "image");
        assert_eq!(LABELS[21], "table");
        assert_eq!(LABELS[6], "doc_title");
        assert_eq!(LABELS[17], "paragraph_title");
    }

    #[test]
    fn mapping_is_total() {
        // 每个标签都必须映射到具体类别（不落 Other，除非确实归 Other）。
        assert_eq!(to_region_kind("text"), RegionKind::Text);
        assert_eq!(to_region_kind("doc_title"), RegionKind::Title);
        assert_eq!(
            to_region_kind("paragraph_title"),
            RegionKind::ParagraphTitle
        );
        assert_eq!(to_region_kind("abstract"), RegionKind::Abstract);
        assert_eq!(to_region_kind("table"), RegionKind::Table);
        assert_eq!(to_region_kind("image"), RegionKind::Figure);
        assert_eq!(to_region_kind("chart"), RegionKind::Figure);
        assert_eq!(to_region_kind("display_formula"), RegionKind::Formula);
        assert_eq!(to_region_kind("algorithm"), RegionKind::Code);
        assert_eq!(to_region_kind("header"), RegionKind::Header);
        assert_eq!(to_region_kind("footer"), RegionKind::Footer);
        assert_eq!(to_region_kind("number"), RegionKind::Footer);
        assert_eq!(to_region_kind("reference"), RegionKind::Reference);
        assert_eq!(to_region_kind("footnote"), RegionKind::FootNote);
        assert_eq!(to_region_kind("figure_title"), RegionKind::Caption);
        // 未知名落 Other
        assert_eq!(to_region_kind("nope"), RegionKind::Other);
        // 全表无 panic
        for name in LABELS {
            let _ = to_region_kind(name);
        }
    }
}
