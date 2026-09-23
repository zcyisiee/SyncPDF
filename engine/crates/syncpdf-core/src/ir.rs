//! 跨阶段中间表示（IR）。设计基准：02-技术路径与架构.md §4。
//! 所有引用只用 id；全部可 serde。

use crate::{AtomId, Color, GlyphId, Matrix, PageId, ParagraphId, Rect, StyleId};
use serde::{Deserialize, Serialize};
use smallvec::SmallVec;

/// 字形在原始内容流中的溯源（hjfy GlyphSource 三元组）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GlyphSource {
    /// 该 text-show 操作在页 DisplayItem 序列中的序号。
    pub element_index: u32,
    /// 字形对应的字节范围，相对于该操作的字符串操作数（TJ 时为拼接后的串）。
    pub string_operand_range: (u32, u32),
    /// 解码后的 code 序号范围（多字节编码时一个字形可占多个 code）。
    pub decoded_code_range: (u32, u32),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct GlyphFlags {
    pub is_space: bool,
    pub is_type3: bool,
    /// 文本渲染模式 3/7（不可见）。
    pub invisible: bool,
    pub outside_clip: bool,
}

/// 页资源里的字体引用 + 解析后的最小描述。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FontRef {
    /// 页资源 /Font 字典键，如 `F1`。
    pub resource_name: String,
    /// 去子集前缀后的 BaseFont。
    pub base_font: String,
    pub is_serif: bool,
    pub is_fixed_pitch: bool,
    pub is_italic: bool,
    pub is_bold: bool,
}

/// 源字形。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Glyph {
    pub id: GlyphId,
    /// 解码文本；无 ToUnicode 时为空。
    pub unicode: SmallVec<[char; 2]>,
    pub code: u32,
    pub font: u32,
    /// 字号（已含文本矩阵与 CTM 的缩放）。
    pub size: f32,
    /// 最终文本矩阵（含 CTM）。
    pub matrix: Matrix,
    /// PDF 用户空间外接框。
    pub bbox: Rect,
    pub advance: f32,
    pub fill: Color,
    pub render_mode: u8,
    pub source: GlyphSource,
    pub flags: GlyphFlags,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum DisplayItem {
    Text {
        glyphs: Vec<Glyph>,
    },
    Image {
        bbox: Rect,
    },
    InlineImage {
        bbox: Rect,
    },
    Path {
        bbox: Rect,
        is_fill: bool,
        is_stroke: bool,
    },
    FormBegin {
        name: String,
        ctm: Matrix,
    },
    FormEnd,
}

/// 一页的解析产物。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PageIR {
    pub page: PageId,
    /// MediaBox（用户空间）。
    pub media_box: Rect,
    pub crop_box: Rect,
    pub rotation: i32,
    /// 页资源中出现的字体，下标即 `Glyph::font`。
    pub fonts: Vec<FontRef>,
    pub items: Vec<DisplayItem>,
}

impl PageIR {
    pub fn glyphs(&self) -> impl Iterator<Item = &Glyph> {
        self.items.iter().flat_map(|it| match it {
            DisplayItem::Text { glyphs } => glyphs.as_slice(),
            _ => &[],
        })
    }
}

/// 布局区域类别（PP-DocLayout 标签的归并）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RegionKind {
    Text,
    Title,
    ParagraphTitle,
    List,
    Caption,
    Table,
    Figure,
    Formula,
    Header,
    Footer,
    FootNote,
    Reference,
    Code,
    Abstract,
    Other,
}

impl RegionKind {
    /// 该类别正文是否参与翻译。
    pub fn translatable(self) -> bool {
        matches!(
            self,
            RegionKind::Text
                | RegionKind::Title
                | RegionKind::ParagraphTitle
                | RegionKind::List
                | RegionKind::Caption
                | RegionKind::Abstract
        )
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Region {
    pub page: PageId,
    pub index: u32,
    pub kind: RegionKind,
    /// PDF 用户空间。
    pub bbox: Rect,
    pub score: f32,
    /// 阅读顺序（越小越先）；无模型支持时由 XY-cut 填充。
    pub order: u32,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Line {
    pub glyphs: Vec<GlyphId>,
    pub baseline_y: f32,
    pub bbox: Rect,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct StyleRun {
    pub id: StyleId,
    /// 段内字形序号范围 `[start, end)`。
    pub glyph_range: (u32, u32),
    pub font: u32,
    pub size: f32,
    pub color: Color,
    pub bold: bool,
    pub italic: bool,
    #[serde(default)]
    pub serif: bool,
    #[serde(default)]
    pub mono: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AtomKind {
    Formula,
    Code,
    Url,
    Number,
    Symbol,
    Other,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Atom {
    pub id: AtomId,
    pub glyph_range: (u32, u32),
    pub kind: AtomKind,
    /// 原文，用于 ATOM_HINTS。
    pub text: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum Align {
    #[default]
    Left,
    Center,
    Right,
    Justify,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Translatable {
    Yes,
    No { reason: String },
}

/// 阅读序文本与真实源字形的映射，范围是段内字形索引的半开区间。
/// 零长度范围表示由几何证据生成的空格/换行，不对应可删除的源字节。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SourceTextSpan {
    pub text: String,
    pub glyph_range: (u32, u32),
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Paragraph {
    pub id: ParagraphId,
    pub page: PageId,
    pub region: u32,
    pub kind: RegionKind,
    pub bbox: Rect,
    pub lines: Vec<Line>,
    /// 段内字形（顺序即阅读顺序）。
    pub glyphs: Vec<GlyphId>,
    #[serde(default)]
    pub text_spans: Vec<SourceTextSpan>,
    pub style_runs: Vec<StyleRun>,
    pub atoms: Vec<Atom>,
    /// 逻辑源文本；旧数据可能含原子占位，优先使用 text_spans 映射。
    pub text: String,
    pub align: Align,
    pub first_indent: f32,
    /// Source baseline spacing in PDF points (not an em multiplier).
    pub line_height: f32,
    pub is_rtl: bool,
    pub translatable: Translatable,
}

/// 排版输出：一个放好位置的字形。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PlacedGlyph {
    /// 目标字体（字体 crate 的句柄 id）。
    pub font: u32,
    pub gid: u16,
    /// 对应的 Unicode 文本（ToUnicode 用）。
    pub text: String,
    pub x: f32,
    pub y: f32,
    pub size: f32,
    pub scale_x: f32,
    pub style: StyleId,
    /// Per-run color; absent in older IR means the paragraph color.
    #[serde(default)]
    pub color: Option<Color>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct LineBox {
    pub bbox: Rect,
    pub baseline_y: f32,
    pub glyphs: Vec<PlacedGlyph>,
    /// 行内原子按原字形几何原地保留（不重绘）。
    pub kept_atoms: Vec<AtomId>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TypesetParagraph {
    pub id: ParagraphId,
    pub lines: Vec<LineBox>,
    pub font_scale: f32,
    pub line_height: f32,
    pub color: Color,
    /// 排版实际占用框（可能大于段落框：加宽/溢出）。
    pub used_bbox: Rect,
    pub overflow: bool,
}

/// 段落最终状态（规约 #9：not_replaced 是落定不是失败）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ParagraphStatus {
    Pending,
    Translated,
    Typeset,
    NotReplaced,
    Fallback,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ir_serde_roundtrip() {
        let p = Paragraph {
            id: "P01-001".parse().unwrap(),
            page: PageId(0),
            region: 0,
            kind: RegionKind::Text,
            bbox: Rect::new(0.0, 0.0, 100.0, 20.0),
            lines: vec![],
            glyphs: vec![],
            text_spans: Vec::new(),
            style_runs: vec![],
            atoms: vec![Atom {
                id: AtomId(1),
                glyph_range: (0, 3),
                kind: AtomKind::Formula,
                text: "x^2".into(),
            }],
            text: "{{KEEP_1}} is".into(),
            align: Align::Justify,
            first_indent: 0.0,
            line_height: 12.0,
            is_rtl: false,
            translatable: Translatable::Yes,
        };
        let json = serde_json::to_string(&p).unwrap();
        assert!(json.contains("\"P01-001\""));
        assert_eq!(serde_json::from_str::<Paragraph>(&json).unwrap(), p);
        let mut legacy = serde_json::to_value(&p).unwrap();
        legacy.as_object_mut().unwrap().remove("text_spans");
        assert!(serde_json::from_value::<Paragraph>(legacy)
            .unwrap()
            .text_spans
            .is_empty());
    }

    #[test]
    fn region_kind_translatable() {
        assert!(RegionKind::Text.translatable());
        assert!(!RegionKind::Table.translatable());
        assert!(!RegionKind::Formula.translatable());
        assert!(!RegionKind::FootNote.translatable());
    }
}
