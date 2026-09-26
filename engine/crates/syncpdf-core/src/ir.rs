//! 跨阶段中间表示（IR）。设计基准：02-技术路径与架构.md §4。
//! 所有引用只用 id；全部可 serde。

use crate::{AtomId, Color, GlyphId, Matrix, OpKey, PageId, ParagraphId, Rect, StyleId};
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
    /// 字形原点（`e`/`f`）+ 页空间基线方向的单位旋转（含 Tm、CTM 与 Form 矩阵）；
    /// 不含字号缩放，视觉字号见 [`Glyph::size`]。
    pub matrix: Matrix,
    /// PDF 用户空间外接框。
    pub bbox: Rect,
    /// pdfium tight char box：真实墨迹，不含字体上下沿。仅当 pdfium 给出非退化
    /// 几何时才有值；无证据为 `None`，此时调用方必须沿用 [`Glyph::bbox`] 的
    /// 保守语义，不得当作「无碰撞」。
    ///
    /// 旧 IR 缺少此字段时读为 None 并保守使用 loose 盒；生产运行每次重新
    /// bind_page 提取证据，不复用持久化的 source_analysis 作为绑定结果。
    #[serde(default)]
    pub ink: Option<Rect>,
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
        /// Source paint binding for a plain stroked line (`S`/`s`). Absent when
        /// the paint is composite (`B`, fill+stroke), clipped, or its style is
        /// unknown, so such a path can never be treated as text decoration.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        stroke: Option<PathStroke>,
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
    /// This run was underlined in the source and must be redrawn underlined.
    #[serde(default)]
    pub underline: bool,
    /// 上下标：本 run 基线相对所在行主基线的偏移，以本 run 字号为单位
    /// （正为上标、负为下标，0 为同基线）。
    #[serde(default)]
    pub rise: f32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AtomKind {
    /// An exact source span owned by an explicit local citation link.
    Citation,
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
    /// Exact source drawing, captured before deleting translated text.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source: Option<SourceAtom>,
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct SourceAtom {
    pub bbox: Rect,
    pub baseline: f32,
    /// In-line advance when wider than the ink (a list label keeps the source
    /// gap to its body); `None` means the ink width.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub advance: Option<f32>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PlacedAtom {
    pub id: AtomId,
    pub source: Rect,
    pub bbox: Rect,
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

/// Identifies one plain stroked source line by its paint operator plus the
/// stroke style it inherits, so a decoration can be erased and re-drawn with
/// the original width and color instead of a guessed default.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct PathStroke {
    pub op: OpKey,
    /// `/w` line width in PDF user space.
    pub width: f32,
    /// Stroke color resolved at paint time.
    pub color: Color,
}

/// A source underline claimed by exactly one paragraph. `glyph_range` is a
/// half-open range over the paragraph's reading-order glyphs (same indexing as
/// [`StyleRun::glyph_range`]), so redrawing can follow the translated glyphs
/// that correspond to the underlined source words.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SourceDecoration {
    pub stroke: PathStroke,
    pub bbox: Rect,
    pub glyph_range: (u32, u32),
    /// Distance below the owning line's baseline, measured from the source glyph
    /// ink and the claimed rule. Recorded here so redraw needs no re-derivation.
    pub offset: f32,
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
    /// Source underlines owned by this paragraph. Empty in older IR.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub decorations: Vec<SourceDecoration>,
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
    /// 合成斜体的水平剪切量（tan 值，正值 = 向右倾斜），施加在写回文本矩阵
    /// 的 c 分量；正体与真斜体面为 0。
    #[serde(default, skip_serializing_if = "is_zero")]
    pub shear_x: f32,
    pub style: StyleId,
    /// Per-run color; absent in older IR means the paragraph color.
    #[serde(default)]
    pub color: Option<Color>,
}

fn is_zero(v: &f32) -> bool {
    *v == 0.0
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct LineBox {
    pub bbox: Rect,
    pub baseline_y: f32,
    pub glyphs: Vec<PlacedGlyph>,
    /// Atom identities retained by this line.
    pub kept_atoms: Vec<AtomId>,
    #[serde(default)]
    pub placed_atoms: Vec<PlacedAtom>,
    /// Source underlines redrawn under this line's translated glyphs.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub underlines: Vec<Underline>,
}

/// One redrawn underline segment, positioned from the translated ink.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct Underline {
    pub bbox: Rect,
    pub color: Color,
    /// Original source line width, never a guessed default.
    pub width: f32,
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
            decorations: Vec::new(),
            atoms: vec![Atom {
                source: None,
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
