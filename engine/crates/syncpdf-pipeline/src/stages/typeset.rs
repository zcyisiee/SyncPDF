//! typesetting 阶段：译文（`ParsedUnit`）→ 逐字形位置（`TypesetParagraph`）。
//!
//! 设计基准：02-技术路径与架构.md §8。`syncpdf-typeset` 通过 `Shaper` 抽象
//! 塑形，本模块把它接到 `syncpdf-font` 的真实字体上（[`StoreShaper`]）。

use syncpdf_core::ir::{Paragraph, TypesetParagraph};
use syncpdf_core::{AtomId, Color, Rect, StyleId};
use syncpdf_font::loader::Script;
use syncpdf_font::{FontId, FontProfile, FontStore, FontVariant, Role};
use syncpdf_translate::{ParsedUnit, Segment};
use syncpdf_typeset::shaper::UnderlineStyle;
use syncpdf_typeset::{
    FitOptions, FontMetrics, Inline, Lang, Obstacles, ParagraphSpec, ShapedGlyph, Shaper,
    StyleSpec, Typeset, TypesetResult,
};

use super::PipelineError;

/// Lowest document leading the descent may choose, and its step.
const MIN_LEADING: f32 = 1.2;
const LEADING_STEP: f32 = 0.1;

/// Explicit target typography; never mutates source IR or translation cache keys.
/// Leading is a dimensionless multiplier of the scaled paragraph font size.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Typography {
    font_scale: f32,
    line_height: Option<f32>,
}

impl Default for Typography {
    fn default() -> Self {
        Self {
            font_scale: 1.0,
            line_height: None,
        }
    }
}

impl Typography {
    pub fn new(font_scale: f32, line_height: Option<f32>) -> Result<Self, PipelineError> {
        for (name, value) in [
            ("font_scale", Some(font_scale)),
            ("line_height", line_height),
        ] {
            if value.is_some_and(|v| !v.is_finite() || v <= 0.0) {
                return Err(PipelineError::Protocol(format!(
                    "{name} 必须是有限正数倍数"
                )));
            }
        }
        Ok(Self {
            font_scale,
            line_height,
        })
    }

    /// The next document-wide leading to try when the requested one leaves
    /// paragraphs without room: one step lower, never below the readable
    /// floor. Source leading (no explicit multiplier) is never altered.
    pub fn lowered(self) -> Option<Self> {
        let current = self.line_height?;
        let next = ((current - LEADING_STEP) / LEADING_STEP).round() * LEADING_STEP;
        (current > MIN_LEADING + 1e-3).then_some(Self {
            line_height: Some(next.max(MIN_LEADING)),
            ..self
        })
    }

    /// 单块覆盖：给出的字段替换整篇值（取值已在入口按 [`Typography::new`] 校验）。
    pub fn overridden(self, font_scale: Option<f32>, line_height: Option<f32>) -> Self {
        Self {
            font_scale: font_scale.unwrap_or(self.font_scale),
            line_height: line_height.or(self.line_height),
        }
    }

    pub fn describe(self) -> String {
        self.line_height
            .map_or_else(|| "原文".into(), |v| format!("{v:.2}"))
    }

    fn apply(self, spec: &mut ParagraphSpec) {
        spec.font_size *= self.font_scale;
        for (_, style) in &mut spec.styles {
            if let Some(size) = &mut style.size {
                *size *= self.font_scale;
            }
        }
        if let Some(multiplier) = self.line_height {
            spec.line_height = multiplier;
        }
    }
}

/// 把 `syncpdf-font` 的字体存储 + 角色 profile 适配成 `Shaper`。
///
/// - `font: u32` 是 `FontId.0`（`TypesetParagraph` 里记的字体句柄）；
/// - 缺字回退：`font_for` 已按 profile 选好目标语言的主字体，
///   `shape` 时若主字体无该字符则逐个字符试 profile 回退链。
#[derive(Debug)]
pub struct StoreShaper<'a> {
    pub store: &'a FontStore,
    pub profile: &'a FontProfile,
    /// 段落角色（决定用哪条角色链）；默认 `Body`。
    pub role: Role,
}

impl<'a> StoreShaper<'a> {
    pub fn new(store: &'a FontStore, profile: &'a FontProfile) -> Self {
        Self {
            store,
            profile,
            role: Role::Body,
        }
    }

    pub fn with_role(mut self, role: Role) -> Self {
        self.role = role;
        self
    }

    fn is_italic_face(&self, font: FontId) -> bool {
        self.store.get(font).is_some_and(|f| f.italic)
    }

    /// 以 `primary` + 回退链塑形。样式要斜体而实际字形面不是斜体时，
    /// 该字形携带合成剪切量（不影响 advance 与断行）。
    fn shape_with_chain(
        &self,
        primary: FontId,
        chain: &[FontId],
        style: Option<&StyleSpec>,
        text: &str,
        size: f32,
        rtl: bool,
    ) -> Vec<ShapedGlyph> {
        if self.store.get(primary).is_none() {
            return Vec::new();
        }
        syncpdf_font::shape_runs_directional(self.store, text, size, primary, chain, rtl)
            .into_iter()
            .flat_map(|(fid, gs)| {
                let shear = match style {
                    Some(s) if s.italic && !self.is_italic_face(fid) => {
                        syncpdf_font::synthetic_italic_shear()
                    }
                    _ => 0.0,
                };
                gs.into_iter().map(move |g| ShapedGlyph {
                    gid: g.gid,
                    cluster: g.cluster,
                    cluster_end: g.cluster_end,
                    font: fid.0,
                    x_advance: g.x_advance,
                    x_offset: g.x_offset,
                    y_offset: g.y_offset,
                    shear_x: shear,
                })
            })
            .collect()
    }
}

impl Shaper for StoreShaper<'_> {
    fn shape(&self, font: u32, text: &str, size: f32, rtl: bool) -> Vec<ShapedGlyph> {
        self.shape_with_chain(
            FontId(font),
            &self.profile.fallbacks(),
            None,
            text,
            size,
            rtl,
        )
    }

    /// 按样式塑形。斜体样式落到非斜体面（CJK 无真斜体字面）时，先把有真
    /// 斜体面的脚本（拉丁）排到真面、主槽面退为首个回退，主槽覆盖不了的
    /// 字形仍由它承担并带合成剪切量返回。
    fn shape_styled(
        &self,
        font: u32,
        style: &StyleSpec,
        text: &str,
        size: f32,
        rtl: bool,
    ) -> Vec<ShapedGlyph> {
        let slot = FontId(font);
        // 等宽 run 的回退链以黑体打头（等宽包只有拉丁字形，CJK 由
        // Noto Sans CJK 承担），并绕开斜体真面重排——等宽链的主字体
        // 已是真斜体面。
        if style.mono {
            let chain = self.profile.mono_fallbacks();
            return self.shape_with_chain(slot, &chain, Some(style), text, size, rtl);
        }
        let mut chain = self.profile.fallbacks();
        let primary = if style.italic && !self.is_italic_face(slot) {
            // 斜体 run：有真斜体面的脚本（拉丁）排到真面，主槽面退为首个
            // 回退；serif 与否跟随角色选字结果（正文角色的主槽即衬线面，
            // 拉丁真斜体也应对应衬线族），源 run 的 serif 标志不再参与。
            let serif = self.role == Role::Body;
            match syncpdf_font::resolve_variant(
                self.store,
                serif,
                Script::Latin,
                FontVariant::of(style.bold, style.italic),
            ) {
                Some(face) if !face.synthetic_italic => {
                    chain.insert(0, slot);
                    face.font
                }
                _ => slot,
            }
        } else {
            slot
        };
        self.shape_with_chain(primary, &chain, Some(style), text, size, rtl)
    }

    fn glyph_bounds(&self, font: u32, gid: u16, size: f32) -> Option<Rect> {
        syncpdf_font::metrics::glyph_bounds(self.store.get(FontId(font))?, gid, size)
    }

    fn metrics(&self, font: u32) -> FontMetrics {
        let Some(loaded) = self.store.get(FontId(font)) else {
            // 退化值：与 `MonoShaper` 一致，避免除零与负高度。
            return FontMetrics {
                ascent: 0.8,
                descent: 0.2,
            };
        };
        // `syncpdf_font::metrics` 的 ascent/descent 已是 upem 归一值，
        // 直接取即可（ascent 向上为正、descent 向下为负）。
        let m = syncpdf_font::metrics(loaded);
        FontMetrics {
            ascent: m.ascent.max(0.0),
            // typeset 约定 descent 为正。
            descent: (-m.descent).max(0.0),
        }
    }

    fn font_for(&self, style: &StyleSpec) -> u32 {
        if let Some(font) = style.font {
            return font;
        }
        // 等宽 run 优先于区域角色（正文里的代码 run 仍用等宽面）。
        let role = if style.mono { Role::Mono } else { self.role };
        self.profile
            .pick_variant(role, FontVariant::of(style.bold, style.italic))
            .font
            .0
    }
}

/// `ParsedUnit` + 段落几何 → `Inline` 序列（原子按原字形 bbox 保留）。
pub fn inlines_from_parsed(parsed: &ParsedUnit, para: &Paragraph) -> Vec<Inline> {
    let mut out = Vec::new();
    for seg in &parsed.segments {
        push_segment(seg, StyleId(0), para, &mut out);
    }
    out
}

fn push_segment(seg: &Segment, style: StyleId, para: &Paragraph, out: &mut Vec<Inline>) {
    match seg {
        Segment::Text(text) => out.push(Inline::Text {
            text: text.clone(),
            style,
        }),
        Segment::Style { id, inner } => {
            for s in inner {
                push_segment(s, *id, para, out);
            }
        }
        Segment::Atom(id) => {
            if let Some(source) = para
                .atoms
                .iter()
                .find(|a| a.id == *id)
                .and_then(|a| a.source)
            {
                out.push(Inline::SourceAtom { id: *id, source });
                return;
            }
            let (w, h) = atom_size(para, *id);
            out.push(Inline::Atom {
                id: *id,
                width: w,
                height: h,
            });
        }
        Segment::Br => out.push(Inline::Br),
    }
}

/// 原子几何：段内该原子字形 bbox 并集；缺字形时给一个近似方框。
fn atom_size(para: &Paragraph, id: AtomId) -> (f32, f32) {
    let Some(atom) = para.atoms.iter().find(|a| a.id == id) else {
        return (6.0, 10.0);
    };
    let (s, e) = atom.glyph_range;
    let boxes: Vec<Rect> = para
        .lines
        .iter()
        .flat_map(|l| l.glyphs.iter())
        .skip(s as usize)
        .take((e.saturating_sub(s)) as usize)
        .filter_map(|gid| para_glyph_bbox(para, *gid))
        .collect();
    match boxes.first() {
        Some(first) => {
            let u = boxes[1..].iter().fold(*first, |acc, b| acc.union(b));
            (u.width().max(1.0), u.height().max(1.0))
        }
        // 段内没有逐字形几何时，按原子文本长度估个宽度。
        None => {
            let chars = atom.text.chars().count() as f32;
            let size = dominant_font_size(para);
            (chars * size * 0.6, size)
        }
    }
}

/// 段内某个 `GlyphId` 的字形框——`Paragraph` 只存 id，几何得从行里反查
/// （`Line` 只有 id 列表），因此这里退回按行 bbox 均分，保证原子有合理尺寸。
fn para_glyph_bbox(_para: &Paragraph, _gid: syncpdf_core::GlyphId) -> Option<Rect> {
    None
}

/// 按源字形数量加权的中位字号；样式节点数量不改变主字号。
pub fn dominant_font_size(para: &Paragraph) -> f32 {
    let mut sizes: Vec<(f32, u32)> = para
        .style_runs
        .iter()
        .filter(|r| {
            r.size.is_finite()
                && r.size > 0.0
                && (r.glyph_range.1 > r.glyph_range.0
                    || para
                        .style_runs
                        .iter()
                        .all(|r| r.glyph_range.0 == r.glyph_range.1))
        })
        .map(|r| {
            (
                r.size,
                r.glyph_range.1.saturating_sub(r.glyph_range.0).max(1),
            )
        })
        .collect();
    if sizes.is_empty() {
        return 12.0;
    }
    sizes.sort_by(|a, b| a.0.total_cmp(&b.0));
    let middle = sizes.iter().map(|(_, n)| u64::from(*n)).sum::<u64>() / 2;
    let mut seen = 0u64;
    for (size, n) in sizes {
        seen += u64::from(n);
        if seen > middle {
            return size;
        }
    }
    unreachable!("nonempty weighted size list")
}

/// 区域类别 → 字体角色（角色选字的唯一映射，见 `StoreShaper::font_for`）：
/// 文档/段落标题用无衬线标题面，其余一切区域（含 Caption/Abstract/List/Code）
/// 都按正文角色选字；`style.mono` 的等宽 run 在 `font_for` 内优先于本角色。
pub fn role_for_region(kind: syncpdf_core::ir::RegionKind) -> Role {
    use syncpdf_core::ir::RegionKind;
    match kind {
        RegionKind::Title => Role::DocTitle,
        RegionKind::ParagraphTitle => Role::ParagraphTitle,
        _ => Role::Body,
    }
}

/// 协议对齐 → IR 对齐。
pub fn align_of(align: syncpdf_protocol::BlockAlign) -> syncpdf_core::ir::Align {
    use syncpdf_core::ir::Align;
    use syncpdf_protocol::BlockAlign;
    match align {
        BlockAlign::Left => Align::Left,
        BlockAlign::Center => Align::Center,
        BlockAlign::Right => Align::Right,
        BlockAlign::Justify => Align::Justify,
    }
}

/// 单块排版的取值约束与整篇 [`Typography::new`] 相同。
pub fn check_block_style(style: &syncpdf_protocol::BlockStyle) -> Result<(), String> {
    Typography::new(style.font_scale.unwrap_or(1.0), style.line_height)
        .map(|_| ())
        .map_err(|e| e.to_string())
}

/// 字体族覆盖下的角色：衬线走正文槽（CJK 正文即宋体类），无衬线走标题槽
/// （黑体类）；未覆盖时按区域类别。
pub fn role_for(
    kind: syncpdf_core::ir::RegionKind,
    family: Option<syncpdf_protocol::FontFamily>,
) -> Role {
    use syncpdf_core::ir::RegionKind;
    use syncpdf_protocol::FontFamily;
    match family {
        None => role_for_region(kind),
        Some(FontFamily::Serif) => Role::Body,
        Some(FontFamily::Sans) if kind == RegionKind::Title => Role::DocTitle,
        Some(FontFamily::Sans) => Role::ParagraphTitle,
    }
}

/// 段落 → `ParagraphSpec`。
pub fn spec_for(para: &Paragraph) -> ParagraphSpec {
    let mut styles: Vec<(StyleId, StyleSpec)> = para
        .style_runs
        .iter()
        .map(|r| {
            (
                r.id,
                StyleSpec {
                    bold: r.bold,
                    italic: r.italic,
                    mono: r.mono,
                    script: false,
                    serif: r.serif,
                    size: Some(r.size),
                    color: Some(r.color),
                    font: None,
                    rise: r.rise,
                    // Geometry comes from the claimed source line, so a run is
                    // decorated only when that line was really attributed to it.
                    underline: para
                        .decorations
                        .iter()
                        .find(|d| {
                            r.underline
                                && d.glyph_range.0 < r.glyph_range.1
                                && r.glyph_range.0 < d.glyph_range.1
                        })
                        .map(|d| UnderlineStyle {
                            width: d.stroke.width,
                            offset: d.offset,
                            color: d.stroke.color,
                        }),
                },
            )
        })
        .collect();
    // Unstyled translated text is StyleId(0), even if source runs start at 1.
    if !styles.iter().any(|(id, _)| *id == StyleId(0)) {
        styles.push((StyleId(0), StyleSpec::default()));
    }
    let color: Color = para.style_runs.first().map(|r| r.color).unwrap_or_default();
    ParagraphSpec {
        bbox: para.bbox,
        font_size: dominant_font_size(para),
        first_baseline: None,
        line_height: para.line_height / dominant_font_size(para),
        // Target body text uses paragraph-wide justification; headings retain source alignment.
        align: if para.kind == syncpdf_core::ir::RegionKind::Text
            && para.align == syncpdf_core::ir::Align::Left
        {
            syncpdf_core::ir::Align::Justify
        } else {
            para.align
        },
        first_indent: para.first_indent,
        is_rtl: para.is_rtl,
        color,
        styles,
        lang: lang_of(para),
    }
}

/// 段落语言：含 CJK 字符 → `Zh`（译文是中文），否则 `En`。
fn lang_of(para: &Paragraph) -> Lang {
    if para.text.chars().any(is_cjk) {
        Lang::Zh
    } else {
        Lang::En
    }
}

fn is_cjk(c: char) -> bool {
    matches!(c as u32,
        0x3000..=0x303F | 0x2E80..=0x9FFF | 0xAC00..=0xD7FF | 0xF900..=0xFAFF
            | 0xFF00..=0xFF60)
}

/// 排一段：默认保持源字号与行距，不自动缩字。
pub fn typeset_paragraph(
    shaper: &dyn Shaper,
    para: &Paragraph,
    parsed: &ParsedUnit,
    obstacles: &Obstacles,
) -> TypesetResult {
    typeset_one(shaper, para, parsed, obstacles)
}

/// 排一段（`typeset_paragraph` 的实现体，名字对齐 brief 签名）。
pub fn typeset_one(
    shaper: &dyn Shaper,
    para: &Paragraph,
    parsed: &ParsedUnit,
    obstacles: &Obstacles,
) -> TypesetResult {
    typeset_with_frame(shaper, para, parsed, obstacles, None)
}

pub fn typeset_with_frame(
    shaper: &dyn Shaper,
    para: &Paragraph,
    parsed: &ParsedUnit,
    obstacles: &Obstacles,
    frame: Option<&super::frame::LayoutFrame>,
) -> TypesetResult {
    typeset_with_typography(
        shaper,
        para,
        parsed,
        obstacles,
        frame,
        Typography::default(),
    )
}

/// Apply a user-selected scale once; fit still cannot shrink unsuccessful paragraphs.
pub fn typeset_with_typography(
    shaper: &dyn Shaper,
    para: &Paragraph,
    parsed: &ParsedUnit,
    obstacles: &Obstacles,
    frame: Option<&super::frame::LayoutFrame>,
    typography: Typography,
) -> TypesetResult {
    let mut spec = spec_for(para);
    typography.apply(&mut spec);
    if let Some(frame) = frame {
        spec.bbox = frame.bbox;
        spec.first_baseline = Some(frame.first_baseline);
    }
    // Break and spacing policy follows translated text, not the source language.
    if parsed.text().chars().any(is_cjk) {
        spec.lang = Lang::Zh;
    }
    let inlines = inlines_from_parsed(parsed, para);
    let typeset = Typeset::new(shaper, FitOptions::default());
    let mut result = typeset.layout(para.id.clone(), &spec, &inlines, obstacles);
    if frame.is_some_and(|f| super::frame::collides(f, &result.paragraph.lines)) {
        result.paragraph.overflow = true;
        result
            .issues
            .push(syncpdf_typeset::TypesetIssue::Overflow { lines: 0 });
    }
    result
}

/// 加载内置字体包 + 建目标语言的默认 profile。
pub fn load_fonts(
    fonts_dir: &std::path::Path,
    target_lang: &str,
) -> Result<(FontStore, FontProfile), PipelineError> {
    let store =
        FontStore::load_builtin(fonts_dir).map_err(|e| PipelineError::Font(e.to_string()))?;
    let profile = syncpdf_font::default_profile(&store, target_lang);
    Ok((store, profile))
}

/// 便捷：把一段的排版结果收成 [`TypesetParagraph`]。
pub fn typeset_into(result: &TypesetResult, out: &mut Vec<TypesetParagraph>) {
    out.push(result.paragraph.clone());
}

#[cfg(test)]
mod tests {
    use super::*;
    use syncpdf_core::ir::{Align, Atom, AtomKind, Line, RegionKind, StyleRun, Translatable};
    use syncpdf_core::require_fixture;
    use syncpdf_core::{ObjRef, OpKey, PageId, ParagraphId};
    use syncpdf_translate::parse_unit_html;

    fn paragraph(id: &str, text: &str, bbox: Rect) -> Paragraph {
        Paragraph {
            id: id.parse().unwrap(),
            page: PageId(0),
            region: 0,
            kind: RegionKind::Text,
            bbox,
            lines: vec![Line {
                glyphs: vec![],
                baseline_y: bbox.y1 - 10.0,
                bbox,
            }],
            glyphs: vec![],
            text_spans: Vec::new(),
            decorations: Vec::new(),
            style_runs: vec![StyleRun {
                id: StyleId(1),
                glyph_range: (0, 0),
                font: 0,
                size: 10.0,
                color: Color::default(),
                bold: false,
                italic: false,
                serif: false,
                mono: false,

                underline: false,
                rise: 0.0,
            }],
            atoms: vec![],
            text: text.into(),
            align: Align::Left,
            first_indent: 0.0,
            line_height: 12.0,
            is_rtl: false,
            translatable: Translatable::Yes,
        }
    }

    fn fonts() -> Option<(FontStore, FontProfile)> {
        let dir = syncpdf_core::fixtures::fonts_dir()?;
        let store = FontStore::load_builtin(&dir).ok()?;
        let profile = syncpdf_font::default_profile(&store, "zh-CN");
        Some((store, profile))
    }

    #[test]
    fn typography_rejects_invalid_multipliers() {
        for value in [0.0, -1.0, f32::NAN, f32::INFINITY, f32::NEG_INFINITY] {
            assert!(Typography::new(value, None).is_err());
            assert!(Typography::new(1.0, Some(value)).is_err());
        }
    }

    #[test]
    fn typography_scales_all_runs_but_preserves_style_hierarchy_and_source() {
        let mut para = paragraph("P01-001", "body", Rect::new(0.0, 0.0, 200.0, 200.0));
        let mut small = para.style_runs[0].clone();
        small.id = StyleId(2);
        small.size = 7.0;
        small.bold = true;
        small.italic = true;
        small.color = Color {
            r: 1.0,
            g: 0.0,
            b: 0.0,
        };
        para.style_runs.push(small);
        let original = spec_for(&para);
        let mut spec = original.clone();
        Typography::new(0.9, Some(1.3)).unwrap().apply(&mut spec);
        assert_eq!(spec.font_size, 9.0);
        assert_eq!(spec.line_height, 1.3);
        assert_eq!(spec.styles[0].1.size, Some(9.0));
        assert!((spec.styles[1].1.size.unwrap() - 6.3).abs() < 0.0001);
        for ((_, before), (_, after)) in original.styles.iter().zip(&spec.styles) {
            assert_eq!(before.bold, after.bold);
            assert_eq!(before.italic, after.italic);
            assert_eq!(before.color, after.color);
            assert_eq!(before.font, after.font);
        }
        assert_eq!(spec_for(&para), original, "source IR must remain immutable");
        let mut unchanged = original.clone();
        Typography::default().apply(&mut unchanged);
        assert_eq!(unchanged, original);
        let mut scaled_source_ratio = original;
        Typography::new(0.9, None)
            .unwrap()
            .apply(&mut scaled_source_ratio);
        assert!(
            (scaled_source_ratio.font_size * scaled_source_ratio.line_height - 10.8).abs() < 0.0001
        );
    }

    #[test]
    fn relative_leading_follows_size_and_does_not_change_source_baseline() {
        let para = paragraph("P01-001", "body", Rect::new(0.0, 0.0, 200.0, 200.0));
        let parsed =
            parse_unit_html(r#"<p id="P01-001"><span data-style="1">甲乙丙<br>丁戊己</span></p>"#)
                .unwrap();
        let frame = super::super::frame::LayoutFrame {
            bbox: para.bbox,
            first_baseline: 180.0,
            obstacles: vec![],
        };
        for scale in [0.8, 0.9, 1.1] {
            let result = typeset_with_typography(
                &syncpdf_typeset::shaper::MonoShaper,
                &para,
                &parsed,
                &Obstacles::default(),
                Some(&frame),
                Typography::new(scale, Some(1.3)).unwrap(),
            );
            assert!(!result.paragraph.overflow);
            assert_eq!(result.scale, 1.0);
            let lines = &result.paragraph.lines;
            assert_eq!(lines.len(), 2);
            assert_eq!(lines[0].baseline_y, 180.0);
            let size = 10.0 * scale;
            assert!((lines[0].baseline_y - lines[1].baseline_y - size * 1.3).abs() < 0.0001);
            assert!((result.paragraph.line_height - size * 1.3).abs() < 0.0001);
            assert!(lines
                .iter()
                .flat_map(|l| &l.glyphs)
                .all(|g| (g.size - size).abs() < 0.0001));
        }
    }

    #[test]
    fn overflow_does_not_reduce_explicit_size_or_leading() {
        let para = paragraph("P01-001", "body", Rect::new(0.0, 0.0, 20.0, 10.0));
        let parsed =
            parse_unit_html(r#"<p id="P01-001"><span data-style="1">甲乙丙丁戊己庚辛</span></p>"#)
                .unwrap();
        let frame = super::super::frame::LayoutFrame {
            bbox: para.bbox,
            first_baseline: 8.0,
            obstacles: vec![],
        };
        let result = typeset_with_typography(
            &syncpdf_typeset::shaper::MonoShaper,
            &para,
            &parsed,
            &Obstacles::default(),
            Some(&frame),
            Typography::new(0.9, Some(1.3)).unwrap(),
        );
        assert!(result.paragraph.overflow);
        assert_eq!(result.scale, 1.0);
        assert!((result.paragraph.line_height - 11.7).abs() < 0.0001);
        assert!(result
            .paragraph
            .lines
            .iter()
            .flat_map(|l| &l.glyphs)
            .all(|g| g.size == 9.0));
    }

    #[test]
    fn store_shaper_metrics_are_positive_and_normalized() {
        let Some((store, profile)) = fonts() else {
            eprintln!("SKIP: 字体包缺失");
            return;
        };
        let shaper = StoreShaper::new(&store, &profile);
        let fid = shaper.font_for(&StyleSpec::default());
        let m = shaper.metrics(fid);
        assert!(m.ascent > 0.0 && m.ascent < 2.0, "{m:?}");
        assert!(m.descent > 0.0 && m.descent < 1.0, "{m:?}");
    }

    #[test]
    fn store_shaper_shapes_cjk_text_into_glyphs() {
        let Some((store, profile)) = fonts() else {
            eprintln!("SKIP: 字体包缺失");
            return;
        };
        let shaper = StoreShaper::new(&store, &profile);
        let fid = shaper.font_for(&StyleSpec::default());
        let gs = shaper.shape(fid, "这是一段测试译文。", 10.0, false);
        assert!(!gs.is_empty(), "中文应塑形出字形");
        assert!(gs.iter().all(|g| g.x_advance > 0.0), "{gs:?}");
        assert!(
            gs.iter().all(|g| g.gid != 0),
            "CJK 字形不应是 .notdef(0)：{gs:?}"
        );
    }

    #[test]
    fn store_shaper_font_for_follows_bold_and_italic() {
        let Some((store, profile)) = fonts() else {
            eprintln!("SKIP: 字体包缺失");
            return;
        };
        let shaper = StoreShaper::new(&store, &profile);
        let regular = shaper.font_for(&StyleSpec::default());
        let bold = shaper.font_for(&StyleSpec {
            bold: true,
            ..Default::default()
        });
        assert_eq!(
            regular,
            profile
                .pick_variant(Role::Body, FontVariant::Regular)
                .font
                .0
        );
        assert_eq!(
            bold,
            profile.pick_variant(Role::Body, FontVariant::Bold).font.0
        );
        // 两个句柄都必须能查到字体。
        assert!(store.get(FontId(regular)).is_some());
        assert!(store.get(FontId(bold)).is_some());
    }

    fn is_cjk_text(s: &str) -> bool {
        s.chars()
            .any(|c| matches!(c as u32, 0x2E80..=0x9FFF | 0xF900..=0xFAFF | 0xFF00..=0xFF60))
    }

    /// 排版层（Text 正文 serif，正例一）：italic run 中 CJK 片段用正体面 +
    /// 合成剪切，拉丁片段用真斜体面、无剪切。
    #[test]
    fn italic_cjk_gets_synthetic_shear_while_latin_uses_true_italic_face() {
        let Some((store, profile)) = fonts() else {
            eprintln!("SKIP: 字体包缺失");
            return;
        };
        let shaper = StoreShaper::new(&store, &profile);
        let mut para = paragraph("P01-001", "source", Rect::new(0.0, 0.0, 400.0, 100.0));
        para.kind = RegionKind::Text;
        para.style_runs[0].italic = true;
        let parsed = parse_unit_html(
            r#"<p id="P01-001"><span data-style="1">自回归pre-training微调</span></p>"#,
        )
        .unwrap();
        let result = typeset_one(&shaper, &para, &parsed, &Obstacles::default());
        assert!(!result.paragraph.overflow, "{:?}", result.issues);
        let shear = 10f32.to_radians().tan();
        let mut saw_cjk = false;
        let mut saw_latin = false;
        for g in result.paragraph.lines.iter().flat_map(|l| &l.glyphs) {
            if g.text.is_empty() {
                continue;
            }
            let font = store.get(FontId(g.font)).unwrap();
            if is_cjk_text(&g.text) {
                saw_cjk = true;
                assert!(
                    (g.shear_x - shear).abs() < 1e-4,
                    "CJK 字形应带合成剪切：{} shear={}",
                    g.text,
                    g.shear_x
                );
                assert_eq!(font.family, "Noto Serif CJK SC", "{}", g.text);
                assert!(!font.italic, "CJK 无真斜体面");
            } else {
                saw_latin = true;
                assert_eq!(g.shear_x, 0.0, "真斜体面不剪切：{} {}", g.text, font.family);
                assert!(font.italic, "拉丁应命中真斜体面：{}", font.family);
            }
        }
        assert!(saw_cjk && saw_latin, "混排两端都应有字形");
    }

    /// 排版层（正例二，不同形态）：无衬线标题区粗斜体 → CJK 用 bold 面 +
    /// 剪切、数字用真 BoldItalic 面。
    #[test]
    fn bold_italic_title_shears_cjk_and_keeps_bold_face() {
        let Some((store, profile)) = fonts() else {
            eprintln!("SKIP: 字体包缺失");
            return;
        };
        let shaper = StoreShaper::new(&store, &profile);
        let mut para = paragraph("P01-001", "source", Rect::new(0.0, 0.0, 400.0, 100.0));
        para.kind = RegionKind::Title;
        let shaper = shaper.with_role(role_for_region(para.kind));
        para.style_runs[0].bold = true;
        para.style_runs[0].italic = true;
        let parsed =
            parse_unit_html(r#"<p id="P01-001"><span data-style="1">2.1.1 多模态架构</span></p>"#)
                .unwrap();
        let result = typeset_one(&shaper, &para, &parsed, &Obstacles::default());
        assert!(!result.paragraph.overflow, "{:?}", result.issues);
        let shear = 10f32.to_radians().tan();
        let mut saw_cjk = false;
        let mut saw_digit = false;
        for g in result.paragraph.lines.iter().flat_map(|l| &l.glyphs) {
            if g.text.is_empty() {
                continue;
            }
            let font = store.get(FontId(g.font)).unwrap();
            if is_cjk_text(&g.text) {
                saw_cjk = true;
                assert!(
                    (g.shear_x - shear).abs() < 1e-4,
                    "标题 CJK 粗斜体应剪切：{}",
                    g.text
                );
                assert_eq!(font.family, "Noto Sans CJK SC", "{}", g.text);
                assert!(font.weight >= 600, "粗体保持：{}", font.weight);
            } else {
                saw_digit = true;
                assert_eq!(g.shear_x, 0.0);
                assert!(font.italic, "数字用真斜体面：{}", font.family);
                assert!(font.weight >= 600, "真 BoldItalic：{}", font.weight);
            }
        }
        assert!(saw_cjk && saw_digit);
    }

    /// 反例：正体 run（常规与粗体）任何字形都不携带剪切，也不误选斜体面。
    #[test]
    fn upright_runs_never_carry_shear() {
        let Some((store, profile)) = fonts() else {
            eprintln!("SKIP: 字体包缺失");
            return;
        };
        let shaper = StoreShaper::new(&store, &profile);
        for bold in [false, true] {
            let mut para = paragraph("P01-001", "source", Rect::new(0.0, 0.0, 400.0, 100.0));
            para.kind = RegionKind::Text;
            para.style_runs[0].bold = bold;
            let parsed = parse_unit_html(
                r#"<p id="P01-001"><span data-style="1">自回归pre-training微调</span></p>"#,
            )
            .unwrap();
            let result = typeset_one(&shaper, &para, &parsed, &Obstacles::default());
            assert!(!result.paragraph.overflow, "{:?}", result.issues);
            for g in result.paragraph.lines.iter().flat_map(|l| &l.glyphs) {
                assert_eq!(g.shear_x, 0.0, "正体不得剪切（bold={bold}）：{}", g.text);
                if !g.text.is_empty() && !is_cjk_text(&g.text) {
                    let font = store.get(FontId(g.font)).unwrap();
                    assert!(!font.italic, "正体不得误选斜体面：{}", font.family);
                }
            }
        }
    }

    /// 角色映射（唯一映射点）：Title→DocTitle、ParagraphTitle→ParagraphTitle、
    /// 其余一切区域（含 Text/Caption/List/Abstract/Code/Reference 等）→Body。
    #[test]
    fn role_for_region_maps_kinds_to_roles() {
        assert_eq!(
            role_for_region(RegionKind::Title),
            Role::DocTitle,
            "文档标题"
        );
        assert_eq!(
            role_for_region(RegionKind::ParagraphTitle),
            Role::ParagraphTitle,
            "段落标题"
        );
        for kind in [
            RegionKind::Text,
            RegionKind::List,
            RegionKind::Caption,
            RegionKind::Table,
            RegionKind::Figure,
            RegionKind::Formula,
            RegionKind::Header,
            RegionKind::Footer,
            RegionKind::FootNote,
            RegionKind::Reference,
            RegionKind::Code,
            RegionKind::Abstract,
            RegionKind::Other,
        ] {
            assert_eq!(role_for_region(kind), Role::Body, "{kind:?}");
        }
    }

    /// 角色选字：一切非标题区域（含 Caption/Abstract/List）→ 思源宋体；
    /// 标题角色 → 黑体。源 run 是衬线字体的标题仍用黑体（角色优先于源
    /// serif 标志）。反例：非标题区域的任何字形都不落黑体。
    #[test]
    fn role_selects_serif_body_and_heiti_headings() {
        let Some((store, profile)) = fonts() else {
            eprintln!("SKIP: 字体包缺失");
            return;
        };
        let parsed =
            parse_unit_html(r#"<p id="P01-001">正文<span data-style="1">强调</span></p>"#).unwrap();
        for kind in [
            RegionKind::Text,
            RegionKind::Abstract,
            RegionKind::Caption,
            RegionKind::List,
            RegionKind::Title,
            RegionKind::ParagraphTitle,
        ] {
            let mut para = paragraph("P01-001", "source", Rect::new(0., 0., 200., 100.));
            para.kind = kind;
            para.style_runs[0].bold = true;
            // 源衬线标志不得改变角色选字（标题仍黑体、正文仍按角色）。
            para.style_runs[0].serif = true;
            let shaper = StoreShaper::new(&store, &profile).with_role(role_for_region(kind));
            let result = typeset_one(&shaper, &para, &parsed, &Obstacles::default());
            assert!(!result.paragraph.overflow, "{kind:?}");
            let heading = matches!(kind, RegionKind::Title | RegionKind::ParagraphTitle);
            let family = if heading {
                "Noto Sans CJK SC"
            } else {
                "Noto Serif CJK SC"
            };
            for glyph in result.paragraph.lines.iter().flat_map(|l| &l.glyphs) {
                let font = store.get(FontId(glyph.font)).unwrap();
                assert_eq!(font.family, family, "{kind:?}: {}", glyph.text);
                // 源粗体只对正文角色改变字重；标题角色槽本身即粗面锚点。
                if !heading {
                    assert_eq!(font.weight >= 600, glyph.style == StyleId(1));
                } else {
                    assert!(font.weight >= 600, "标题面为粗面：{}", font.weight);
                }
                assert_eq!(glyph.size, 10.);
            }
            assert!(
                para.style_runs[0].serif,
                "target policy must not change source IR"
            );
        }
    }

    #[test]
    fn role_policy_preserves_run_properties_and_monospace_precedence() {
        let Some((store, profile)) = fonts() else {
            eprintln!("SKIP: 字体包缺失");
            return;
        };
        let shaper = StoreShaper::new(&store, &profile);
        let mut para = paragraph("P01-001", "source", Rect::new(0., 0., 200., 100.));
        let run = &mut para.style_runs[0];
        run.bold = true;
        run.italic = true;
        run.mono = false;
        run.size = 8.;
        run.color = Color {
            r: 0.5,
            g: 0.1,
            b: 0.2,
        };
        let body = spec_for(&para);
        para.kind = RegionKind::Caption;
        let other = spec_for(&para);
        // 角色策略不按区域 kind 写死 serif：两份 spec 的样式表完全一致。
        assert_eq!(body.styles, other.styles);
        assert_eq!(body.font_size, other.font_size);
        assert_eq!(body.line_height, other.line_height);
        // 正文角色（Caption → Body）粗斜体 → 思源宋体 bold 面。
        let caption = shaper.with_role(role_for_region(RegionKind::Caption));
        let font = store
            .get(FontId(caption.font_for(&body.styles[0].1)))
            .unwrap();
        assert_eq!(font.family, "Noto Serif CJK SC");
        assert!(font.weight >= 600);
        // mono 优先于区域角色：同一 spec 换成 mono → JetBrains Mono 粗斜体面。
        let mut mono_style = body.styles[0].1;
        mono_style.mono = true;
        let mfont = store.get(FontId(caption.font_for(&mono_style))).unwrap();
        assert_eq!(mfont.family, "JetBrains Mono");
        assert!(mfont.italic, "真斜体面");
        assert!(mfont.weight >= 600, "BoldItalic 面");
    }

    /// 等宽 run：拉丁全部落在 JetBrains Mono 且 advance 一致、正体无剪切；
    /// mono 优先于区域角色（正文里的代码 run 仍用等宽面）。
    #[test]
    fn monospace_run_uses_jetbrains_mono_with_uniform_advances() {
        let Some((store, profile)) = fonts() else {
            eprintln!("SKIP: 字体包缺失");
            return;
        };
        let shaper =
            StoreShaper::new(&store, &profile).with_role(role_for_region(RegionKind::Text));
        let style = StyleSpec {
            mono: true,
            ..Default::default()
        };
        let font = shaper.font_for(&style);
        assert_eq!(
            store.get(FontId(font)).unwrap().family,
            "JetBrains Mono",
            "mono 优先于正文角色"
        );
        let glyphs = shaper.shape_styled(font, &style, "int main() { return 0; }", 10.0, false);
        assert!(!glyphs.is_empty());
        let first = glyphs[0].x_advance;
        for g in &glyphs {
            assert_eq!(g.font, font, "拉丁字形应留在等宽面");
            assert!(g.x_advance > 0.0);
            assert!(
                (g.x_advance - first).abs() < 1e-6,
                "等宽 advance 应一致：{}",
                g.x_advance
            );
            assert_eq!(g.shear_x, 0.0, "正体无剪切");
        }
    }

    /// 等宽 run 的 CJK 回退黑体（Noto Sans CJK），正体无剪切。
    #[test]
    fn monospace_run_cjk_falls_back_to_sans_without_shear() {
        let Some((store, profile)) = fonts() else {
            eprintln!("SKIP: 字体包缺失");
            return;
        };
        let shaper =
            StoreShaper::new(&store, &profile).with_role(role_for_region(RegionKind::Text));
        let mut para = paragraph("P01-001", "source", Rect::new(0.0, 0.0, 400.0, 100.0));
        para.kind = RegionKind::Text;
        para.style_runs[0].mono = true;
        let parsed =
            parse_unit_html(r#"<p id="P01-001"><span data-style="1">代码 code 混排 42</span></p>"#)
                .unwrap();
        let result = typeset_one(&shaper, &para, &parsed, &Obstacles::default());
        assert!(!result.paragraph.overflow, "{:?}", result.issues);
        let mut saw_cjk = false;
        let mut saw_latin = false;
        for g in result.paragraph.lines.iter().flat_map(|l| &l.glyphs) {
            let font = store.get(FontId(g.font)).unwrap();
            if is_cjk_text(&g.text) {
                saw_cjk = true;
                assert_eq!(
                    font.family, "Noto Sans CJK SC",
                    "mono 的 CJK 回退是黑体：{}",
                    g.text
                );
                assert_eq!(g.shear_x, 0.0, "正体 CJK 无剪切");
            } else {
                saw_latin = true;
                assert_eq!(font.family, "JetBrains Mono", "{}", g.text);
            }
        }
        assert!(saw_cjk && saw_latin, "混排两端都应有字形");
    }

    /// 等宽斜体：JetBrains Mono 有真斜体面，不用合成剪切。
    #[test]
    fn monospace_italic_uses_true_jetbrains_face_without_shear() {
        let Some((store, profile)) = fonts() else {
            eprintln!("SKIP: 字体包缺失");
            return;
        };
        let shaper = StoreShaper::new(&store, &profile);
        for bold in [false, true] {
            let style = StyleSpec {
                mono: true,
                italic: true,
                bold,
                ..Default::default()
            };
            let font = shaper.font_for(&style);
            let f = store.get(FontId(font)).unwrap();
            assert_eq!(f.family, "JetBrains Mono");
            assert!(f.italic, "真斜体面（bold={bold}）");
            assert_eq!(f.weight >= 600, bold);
            let glyphs = shaper.shape_styled(font, &style, "return x;", 10.0, false);
            assert!(!glyphs.is_empty());
            for g in &glyphs {
                assert_eq!(g.shear_x, 0.0, "真斜体面不剪切（bold={bold}）");
                assert!(store.get(FontId(g.font)).unwrap().italic);
            }
        }
    }

    /// 反例：非等宽 run（各角色 × 各变体）永远不落等宽面。
    #[test]
    fn non_monospace_run_never_uses_monospace_face() {
        let Some((store, profile)) = fonts() else {
            eprintln!("SKIP: 字体包缺失");
            return;
        };
        for kind in [
            RegionKind::Text,
            RegionKind::Caption,
            RegionKind::Title,
            RegionKind::ParagraphTitle,
        ] {
            let shaper = StoreShaper::new(&store, &profile).with_role(role_for_region(kind));
            for (bold, italic) in [(false, false), (true, false), (false, true), (true, true)] {
                let style = StyleSpec {
                    bold,
                    italic,
                    ..Default::default()
                };
                let font = store.get(FontId(shaper.font_for(&style))).unwrap();
                assert_ne!(
                    font.family, "JetBrains Mono",
                    "{kind:?} bold={bold} italic={italic}"
                );
            }
        }
    }

    #[test]
    fn store_shaper_unknown_font_degrades_gracefully() {
        let Some((store, profile)) = fonts() else {
            eprintln!("SKIP: 字体包缺失");
            return;
        };
        let shaper = StoreShaper::new(&store, &profile);
        assert!(shaper.shape(u32::MAX - 1, "x", 10.0, false).is_empty());
        let m = shaper.metrics(u32::MAX - 1);
        assert_eq!(m.ascent, 0.8);
        assert_eq!(m.descent, 0.2);
    }

    #[test]
    fn inlines_from_parsed_expands_styles_and_atoms() {
        let parsed = parse_unit_html(
            r#"<p id="P01-001">A <span data-style="1">BC</span>{{KEEP_1}}<br>D</p>"#,
        )
        .unwrap();
        let para = paragraph("P01-001", "A BC D", Rect::new(0.0, 0.0, 200.0, 40.0));
        let inlines = inlines_from_parsed(&parsed, &para);
        // Text("A ") + Style(1) → Text("BC") + Atom + Br + Text("D")
        assert!(
            inlines
                .iter()
                .any(|i| matches!(i, Inline::Text { style, .. } if style.0 == 1)),
            "{inlines:?}"
        );
        assert!(
            inlines
                .iter()
                .any(|i| matches!(i, Inline::Atom { id, .. } if id.0 == 1)),
            "{inlines:?}"
        );
        assert!(
            inlines.iter().any(|i| matches!(i, Inline::Br)),
            "{inlines:?}"
        );
    }

    #[test]
    fn inlines_atom_uses_paragraph_atom_geometry() {
        let parsed = parse_unit_html(r#"<p id="P01-001">x{{KEEP_1}}y</p>"#).unwrap();
        let mut para = paragraph("P01-001", "x{{KEEP_1}}y", Rect::new(0.0, 0.0, 100.0, 20.0));
        para.atoms = vec![Atom {
            source: None,
            id: AtomId(1),
            glyph_range: (0, 3),
            kind: AtomKind::Formula,
            text: "x^2".into(),
        }];
        let inlines = inlines_from_parsed(&parsed, &para);
        let atom = inlines
            .iter()
            .find_map(|i| match i {
                Inline::Atom { width, height, .. } => Some((*width, *height)),
                _ => None,
            })
            .expect("应有原子");
        assert!(atom.0 > 0.0 && atom.1 > 0.0, "{atom:?}");
    }

    #[test]
    fn spec_for_uses_dominant_size_and_style_runs() {
        let mut para = paragraph("P01-001", "Hello", Rect::new(0.0, 0.0, 100.0, 20.0));
        para.style_runs = vec![
            StyleRun {
                id: StyleId(1),
                glyph_range: (0, 3),
                font: 0,
                size: 10.0,
                color: Color::default(),
                bold: false,
                italic: false,
                serif: false,
                mono: false,

                underline: false,
                rise: 0.0,
            },
            StyleRun {
                id: StyleId(2),
                glyph_range: (3, 5),
                font: 1,
                size: 14.0,
                color: Color::default(),
                bold: true,
                italic: false,
                serif: false,
                mono: false,

                underline: false,
                rise: 0.0,
            },
        ];
        let spec = spec_for(&para);
        assert_eq!(spec.styles.len(), 3); // two source runs plus unstyled target text
        assert_eq!(spec.styles[0].0, StyleId(1));
        assert!(spec.styles[1].1.bold);
        assert!((spec.font_size - 10.0).abs() < 0.01, "{}", spec.font_size);
        assert_eq!(spec.bbox, para.bbox);
        assert_eq!(spec.lang, Lang::En);
    }

    #[test]
    fn spec_for_detects_cjk_language() {
        let para = paragraph("P01-001", "这是一段中文", Rect::new(0.0, 0.0, 100.0, 20.0));
        assert_eq!(spec_for(&para).lang, Lang::Zh);
    }

    #[test]
    fn typeset_paragraph_places_cjk_text_inside_the_box() {
        let Some((store, profile)) = fonts() else {
            eprintln!("SKIP: 字体包缺失");
            return;
        };
        let shaper = StoreShaper::new(&store, &profile);
        let parsed = parse_unit_html(r#"<p id="P01-001">这是一段测试译文。</p>"#).unwrap();
        let bbox = Rect::new(0.0, 0.0, 200.0, 40.0);
        let para = paragraph("P01-001", "这是一段测试译文。", bbox);
        let result = typeset_one(&shaper, &para, &parsed, &Obstacles::default());
        let tp: &TypesetParagraph = &result.paragraph;
        assert!(!tp.lines.is_empty(), "应排出至少一行");
        assert!(
            tp.lines.iter().all(|l| !l.glyphs.is_empty()),
            "每行都应有字形：{tp:?}"
        );
        // 排版占用框在段落框附近（允许溢出，但不应跑飞）。
        assert!(
            tp.used_bbox.x0 >= bbox.x0 - 2.0 && tp.used_bbox.x1 <= bbox.x1 + 200.0,
            "{:?}",
            tp.used_bbox
        );
        assert!(
            result.scale > 0.0 && result.scale <= 1.0,
            "{}",
            result.scale
        );
    }

    #[test]
    fn typeset_paragraph_reports_overflow_issue_on_tiny_box() {
        let Some((store, profile)) = fonts() else {
            eprintln!("SKIP: 字体包缺失");
            return;
        };
        let shaper = StoreShaper::new(&store, &profile);
        let text = "这是一段很长的测试译文用于触发溢出分支。".repeat(4);
        let html = format!(r#"<p id="P01-001">{text}</p>"#);
        let parsed = parse_unit_html(&html).unwrap();
        // 框极小：必溢出。
        let bbox = Rect::new(0.0, 0.0, 60.0, 20.0);
        let para = paragraph("P01-001", &text, bbox);
        let result = typeset_one(&shaper, &para, &parsed, &Obstacles::default());
        assert!(result.paragraph.overflow, "{:?}", result.issues);
        assert_eq!(result.scale, 1.0, "不得用自动缩字号掩盖容纳失败");
        assert!(result
            .paragraph
            .lines
            .iter()
            .flat_map(|l| &l.glyphs)
            .all(|g| g.size == dominant_font_size(&para)));
    }

    #[test]
    fn load_fonts_reports_missing_dir() {
        let err = load_fonts(std::path::Path::new("/nonexistent/fonts"), "zh-CN").unwrap_err();
        assert!(matches!(err, PipelineError::Font(_)), "{err:?}");
    }

    #[test]
    fn load_fonts_works_with_vendor_fonts() {
        let Some(dir) = syncpdf_core::fixtures::fonts_dir() else {
            eprintln!("SKIP: 字体包缺失");
            return;
        };
        let (store, profile) = load_fonts(&dir, "zh-CN").unwrap();
        assert!(!store.is_empty());
        assert_eq!(profile.target_lang, "zh-CN");
    }

    #[test]
    fn typeset_uses_real_font_fixture_when_available() {
        // 顺带确认夹具定位可用（缺夹具时 skip）。
        let _ = require_fixture!("ci-test.pdf");
        let Some((store, profile)) = fonts() else {
            eprintln!("SKIP: 字体包缺失");
            return;
        };
        let shaper = StoreShaper::new(&store, &profile);
        let parsed = parse_unit_html(r#"<p id="P01-001">测试</p>"#).unwrap();
        let para = paragraph("P01-001", "测试", Rect::new(0.0, 0.0, 100.0, 30.0));
        let r = typeset_paragraph(&shaper, &para, &parsed, &Obstacles::default());
        assert_eq!(r.paragraph.id, ParagraphId::new(PageId(0), 1));
        let _ = ObjRef::new(1, 0);
        let _ = OpKey::new(ObjRef::new(1, 0), 0);
    }
    #[test]
    fn source_line_spacing_is_in_points_and_run_sizes_colors_survive() {
        let mut para = paragraph("P01-001", "source", Rect::new(0.0, 0.0, 200.0, 45.0));
        para.line_height = 12.5;
        para.style_runs[0].size = 9.963;
        para.style_runs[0].color = Color::rgb(0.2, 0.3, 0.4);
        let mut second = para.style_runs[0].clone();
        second.id = StyleId(2);
        second.size = 13.125;
        second.color = Color::rgb(0.7, 0.1, 0.2);
        para.style_runs.push(second);
        let parsed = parse_unit_html("<p id=\"P01-001\"><span data-style=\"1\">abc</span><br><span data-style=\"2\">def</span></p>").unwrap();
        let shaper = syncpdf_typeset::shaper::MonoShaper;
        let result = typeset_paragraph(&shaper, &para, &parsed, &Obstacles::default());
        assert!(!result.paragraph.overflow);
        assert_eq!(result.scale, 1.0);
        assert_eq!(result.paragraph.lines.len(), 2);
        let lines = &result.paragraph.lines;
        assert!((lines[0].baseline_y - lines[1].baseline_y - 12.5).abs() < 1e-4);
        for (line, run) in lines.iter().zip(&para.style_runs) {
            assert!(line
                .glyphs
                .iter()
                .all(|g| g.size == run.size && g.color == Some(run.color)));
        }
    }
    #[test]
    fn adapter_keeps_actual_fallback_font_cluster_range_and_ink() {
        let dir = syncpdf_core::fixtures::fonts_dir().expect("builtin fonts required");
        let (store, profile) = load_fonts(&dir, "en").unwrap();
        let shaper = StoreShaper::new(&store, &profile);
        let inter = store
            .find(&syncpdf_font::FontQuery {
                family: Some("Inter".into()),
                ..Default::default()
            })
            .unwrap();
        let glyphs = shaper.shape(inter.0, "A中B", 12.0, false);
        assert_eq!(glyphs.len(), 3);
        assert_eq!(
            glyphs
                .iter()
                .map(|g| (g.cluster, g.cluster_end))
                .collect::<Vec<_>>(),
            [(0, 1), (1, 4), (4, 5)]
        );
        assert_ne!(glyphs[1].font, inter.0);
        let ink = shaper
            .glyph_bounds(glyphs[1].font, glyphs[1].gid, 12.0)
            .unwrap();
        assert!(ink.width() > 0.0 && ink.height() > 0.0);
        let m = shaper.metrics(glyphs[1].font);
        assert!(ink.height() < (m.ascent + m.descent) * 12.0);
        assert_eq!(glyphs, shaper.shape(inter.0, "A中B", 12.0, false));
    }
    #[test]
    fn mathematical_unicode_uses_real_ink_without_character_normalization() {
        let (store, profile) = fonts().expect("builtin font fixture");
        let shaper = StoreShaper::new(&store, &profile);
        let para = paragraph("P01-001", "source", Rect::new(0.0, 0.0, 200.0, 40.0));
        let text = "∗ 𝜆 𝛼 𝑠 𝜖";
        let parsed = parse_unit_html(&format!("<p id=\"P01-001\">{text}</p>")).unwrap();
        let result = typeset_one(&shaper, &para, &parsed, &Obstacles::default());
        assert!(!result.paragraph.overflow);
        let glyphs: Vec<_> = result
            .paragraph
            .lines
            .iter()
            .flat_map(|l| &l.glyphs)
            .collect();
        assert!(glyphs.iter().all(|g| g.gid != 0));
        assert_eq!(
            glyphs.iter().map(|g| g.text.as_str()).collect::<String>(),
            text
        );
        for g in glyphs.iter().filter(|g| !g.text.trim().is_empty()) {
            let ink = shaper.glyph_bounds(g.font, g.gid, g.size).unwrap();
            assert!(ink.width() > 0.0 && ink.height() > 0.0);
        }
    }
    #[test]
    fn missing_glyph_never_publishes_a_notdef_replacement() {
        let (store, profile) = fonts().expect("builtin font fixture");
        let shaper = StoreShaper::new(&store, &profile);
        let para = paragraph("P01-001", "source", Rect::new(0.0, 0.0, 200.0, 40.0));
        let parsed = parse_unit_html("<p id=\"P01-001\">中\u{10ffff}文</p>").unwrap();
        let result = typeset_one(&shaper, &para, &parsed, &Obstacles::default());
        assert!(result.paragraph.overflow);
    }
}
