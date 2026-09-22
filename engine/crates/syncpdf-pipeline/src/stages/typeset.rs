//! typesetting 阶段：译文（`ParsedUnit`）→ 逐字形位置（`TypesetParagraph`）。
//!
//! 设计基准：02-技术路径与架构.md §8。`syncpdf-typeset` 通过 `Shaper` 抽象
//! 塑形，本模块把它接到 `syncpdf-font` 的真实字体上（[`StoreShaper`]）。

use syncpdf_core::ir::{Paragraph, TypesetParagraph};
use syncpdf_core::{AtomId, Color, Rect, StyleId};
use syncpdf_font::{FontId, FontProfile, FontStore, Role};
use syncpdf_translate::{ParsedUnit, Segment};
use syncpdf_typeset::{
    FitOptions, FontMetrics, Inline, Lang, Obstacles, ParagraphSpec, ShapedGlyph, Shaper,
    StyleSpec, Typeset, TypesetResult,
};

use super::PipelineError;

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
}

impl Shaper for StoreShaper<'_> {
    fn shape(&self, font: u32, text: &str, size: f32, rtl: bool) -> Vec<ShapedGlyph> {
        // `shape_runs` 内部按 unicode-script 判方向；RTL 段落此处不必额外处理
        // （阿拉伯文段落的 `is_rtl` 目前恒为 false，见 paragraph_analysis）。
        let _ = rtl;
        let primary = FontId(font);
        let Some(loaded) = self.store.get(primary) else {
            tracing::warn!(font, "塑形时字体句柄不存在，返回空结果");
            return Vec::new();
        };
        let fallbacks = self.profile.fallbacks();
        syncpdf_font::shape_runs(self.store, text, size, loaded.id, &fallbacks)
            .into_iter()
            .flat_map(|(_fid, gs)| gs)
            .map(|g| ShapedGlyph {
                gid: g.gid,
                cluster: g.cluster,
                x_advance: g.x_advance,
                x_offset: g.x_offset,
                y_offset: g.y_offset,
            })
            .collect()
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
        let role = if style.mono { Role::Mono } else { self.role };
        self.profile.pick(role, style.bold, style.italic).0
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

/// 段的主字号：样式 run 里出现最多的字号；无 run 时 12pt。
pub fn dominant_font_size(para: &Paragraph) -> f32 {
    let sizes: Vec<f32> = para
        .style_runs
        .iter()
        .map(|r| r.size)
        .filter(|s| *s > 0.0)
        .collect();
    if sizes.is_empty() {
        return 12.0;
    }
    // 取中位数，避免标题里偶发的超大字号拉偏。
    let mut sorted = sizes;
    sorted.sort_by(f32::total_cmp);
    sorted[sorted.len() / 2]
}

/// 段落 → `ParagraphSpec`。
pub fn spec_for(para: &Paragraph) -> ParagraphSpec {
    let styles: Vec<(StyleId, StyleSpec)> = para
        .style_runs
        .iter()
        .map(|r| {
            (
                r.id,
                StyleSpec {
                    bold: r.bold,
                    italic: r.italic,
                    mono: false,
                    script: false,
                },
            )
        })
        .collect();
    let color: Color = para.style_runs.first().map(|r| r.color).unwrap_or_default();
    ParagraphSpec {
        bbox: para.bbox,
        font_size: dominant_font_size(para),
        line_height: para.line_height.max(1.0),
        align: para.align,
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

/// 排一段：默认 fit 阶梯。
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
    let spec = spec_for(para);
    let inlines = inlines_from_parsed(parsed, para);
    let typeset = Typeset::new(shaper, FitOptions::default());
    typeset.layout(para.id.clone(), &spec, &inlines, obstacles)
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
            style_runs: vec![StyleRun {
                id: StyleId(1),
                glyph_range: (0, 0),
                font: 0,
                size: 10.0,
                color: Color::default(),
                bold: false,
                italic: false,
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
        assert_eq!(regular, profile.pick(Role::Body, false, false).0);
        assert_eq!(bold, profile.pick(Role::Body, true, false).0);
        // 两个句柄都必须能查到字体。
        assert!(store.get(FontId(regular)).is_some());
        assert!(store.get(FontId(bold)).is_some());
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
            },
            StyleRun {
                id: StyleId(2),
                glyph_range: (3, 5),
                font: 1,
                size: 14.0,
                color: Color::default(),
                bold: true,
                italic: false,
            },
        ];
        let spec = spec_for(&para);
        assert_eq!(spec.styles.len(), 2);
        assert_eq!(spec.styles[0].0, StyleId(1));
        assert!(spec.styles[1].1.bold);
        assert!((spec.font_size - 14.0).abs() < 0.01, "{}", spec.font_size);
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
}
