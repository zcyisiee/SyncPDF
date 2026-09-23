//! A line's union bbox is a broad phase, not proof that its glyphs touch.
use syncpdf_core::ir::Align;
use syncpdf_core::{AtomId, Color, Rect, StyleId};
use syncpdf_typeset::shaper::MonoShaper;
use syncpdf_typeset::{
    FitOptions, FontMetrics, Inline, Lang, Obstacles, ParagraphSpec, ShapedGlyph, Shaper,
    StyleSpec, Typeset,
};

struct InkShaper;
impl Shaper for InkShaper {
    fn shape(&self, font: u32, text: &str, size: f32, rtl: bool) -> Vec<ShapedGlyph> {
        MonoShaper.shape(font, text, size, rtl)
    }
    fn glyph_bounds(&self, _font: u32, gid: u16, size: f32) -> Option<Rect> {
        let (bottom, top) = match char::from_u32(u32::from(gid)).unwrap() {
            'd' => (-0.4, 0.5),
            'T' => (0.0, 0.8),
            'X' => (0.0, 2.2),
            _ => (0.0, 0.4),
        };
        let left = if gid == b'j' as u16 || gid == b'W' as u16 {
            -0.046875
        } else {
            0.0
        };
        let right = if gid == b'W' as u16 {
            100.0
        } else {
            size * 0.4
        };
        Some(Rect::new(left, bottom * size, right, top * size))
    }
    fn metrics(&self, _: u32) -> FontMetrics {
        FontMetrics {
            ascent: 0.8,
            descent: 0.4,
        }
    }
    fn font_for(&self, _: &StyleSpec) -> u32 {
        0
    }
}

fn layout(inlines: &[Inline]) -> syncpdf_typeset::TypesetResult {
    let spec = ParagraphSpec {
        bbox: Rect::new(0.0, 0.0, 100.0, 50.0),
        first_baseline: Some(40.0),
        font_size: 10.0,
        line_height: 1.0,
        align: Align::Left,
        first_indent: 0.0,
        is_rtl: false,
        color: Color::BLACK,
        styles: vec![],
        lang: Lang::En,
    };
    Typeset::new(&InkShaper, FitOptions::default()).layout(
        "P01-001".parse().unwrap(),
        &spec,
        inlines,
        &Obstacles::default(),
    )
}
fn text(t: &str) -> Inline {
    Inline::Text {
        text: t.into(),
        style: StyleId(0),
    }
}

#[test]
fn negative_side_bearing_repositions_ink_without_shrinking_or_clipping() {
    let result = layout(&[text("j")]);
    assert!(!result.paragraph.overflow);
    assert_eq!(result.paragraph.lines[0].bbox.x0, 0.0);
    assert_eq!(result.paragraph.lines[0].glyphs[0].x, 0.046875);
    assert_eq!(result.paragraph.lines[0].glyphs[0].size, 10.0);
    assert!(layout(&[text("W")]).paragraph.overflow);
}

#[test]
fn disjoint_ink_does_not_overflow_when_line_boxes_overlap() {
    let result = layout(&[text("ad"), Inline::Br, text("Ta")]);
    assert!(!result.paragraph.overflow);
    assert_eq!(result.scale, 1.0);
    let lines = &result.paragraph.lines;
    assert_eq!(lines.len(), 2);
    assert!(lines[0].bbox.y0 < lines[1].bbox.y1 - 1.0);
    assert_eq!(lines[0].baseline_y - lines[1].baseline_y, 10.0);
    assert!(lines.iter().flat_map(|l| &l.glyphs).all(|g| g.size == 10.0));
}

#[test]
fn true_ink_collision_is_still_an_overflow() {
    assert!(
        layout(&[text("ad"), Inline::Br, text("aT")])
            .paragraph
            .overflow
    );
}

#[test]
fn tall_ink_is_checked_against_nonadjacent_lines_too() {
    assert!(
        layout(&[text("a"), Inline::Br, text("   a"), Inline::Br, text("X")])
            .paragraph
            .overflow
    );
}

#[test]
fn atom_rectangles_still_participate_in_collision_check() {
    let result = layout(&[
        text("d"),
        Inline::Br,
        Inline::Atom {
            id: AtomId(1),
            width: 4.0,
            height: 7.0,
        },
    ]);
    assert!(result.paragraph.overflow);
}
