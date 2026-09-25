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
        } else if gid == b'R' as u16 {
            0.1
        } else if gid == b'g' as u16 || gid == b's' as u16 {
            0.4
        } else {
            0.0
        };
        let right = if gid == b'R' as u16 {
            100.03125
        } else if gid == b'W' as u16 {
            100.0
        } else if gid == b'g' as u16 {
            size * 0.5
        } else if gid == b's' as u16 {
            size * 0.5 + 0.2
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
fn target_underline_is_real_ink_for_bounds_and_collision() {
    let spec = ParagraphSpec {
        bbox: Rect::new(0.0, 0.0, 100.0, 50.0),
        first_baseline: Some(40.0),
        font_size: 10.0,
        line_height: 1.5,
        align: Align::Left,
        first_indent: 0.0,
        is_rtl: false,
        color: Color::BLACK,
        styles: vec![(
            StyleId(0),
            StyleSpec {
                underline: Some(syncpdf_typeset::shaper::UnderlineStyle {
                    width: 0.4,
                    offset: 3.0,
                    color: Color::BLACK,
                }),
                ..StyleSpec::default()
            },
        )],
        lang: Lang::En,
    };
    let engine = Typeset::new(&InkShaper, FitOptions::default());
    let result = engine.layout(
        "P01-001".parse().unwrap(),
        &spec,
        &[text("a")],
        &Obstacles::default(),
    );
    assert!(!result.paragraph.overflow);
    let line = &result.paragraph.lines[0];
    assert_eq!(line.underlines.len(), 1);
    assert!(
        line.bbox.y0 <= line.underlines[0].bbox.y0,
        "underline omitted from line bounds"
    );
    let mut narrow = spec.clone();
    narrow.bbox.y0 = 37.0;
    let result = engine.layout(
        "P01-001".parse().unwrap(),
        &narrow,
        &[text("a")],
        &Obstacles::default(),
    );
    assert!(
        result.paragraph.overflow,
        "text fits, but its underline crosses the frame"
    );
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
fn right_side_bearing_uses_available_left_space_without_shrinking() {
    let result = layout(&[text("R")]);
    assert!(!result.paragraph.overflow);
    let line = &result.paragraph.lines[0];
    assert_eq!(line.bbox.x1, 100.0);
    assert!(line.bbox.x0 >= 0.0);
    assert_eq!(line.glyphs[0].x, -0.03125);
    assert_eq!(line.glyphs[0].size, 10.0);
    assert!(layout(&[text("W")]).paragraph.overflow);
}

#[test]
fn side_bearing_nudge_moves_source_atoms_with_text_without_resizing() {
    let source = syncpdf_core::ir::SourceAtom {
        bbox: Rect::new(10.0, 20.0, 100.0, 23.0),
        baseline: 20.0,
        advance: None,
    };
    let result = layout(&[
        text("g"),
        Inline::SourceAtom {
            id: AtomId(1),
            source,
        },
        text("s"),
    ]);
    assert!(!result.paragraph.overflow);
    assert_eq!(result.paragraph.lines.len(), 1);
    let line = &result.paragraph.lines[0];
    assert!(line.bbox.x0 >= 0.0 && line.bbox.x1 <= 100.0);
    let atom = &line.placed_atoms[0];
    assert_eq!(atom.source, source.bbox);
    assert_eq!(atom.bbox.width(), source.bbox.width());
    assert_eq!(atom.bbox.height(), source.bbox.height());
    assert_eq!(line.kept_atoms, vec![AtomId(1)]);
    assert!((line.glyphs[0].x + 0.2).abs() < 0.001);
    assert!((atom.bbox.x0 - 4.8).abs() < 0.001);
    assert!((line.glyphs[1].x - 94.8).abs() < 0.001);
    assert_eq!(line.baseline_y, 40.0);
    assert!(line
        .glyphs
        .iter()
        .all(|g| g.size == 10.0 && g.scale_x == 1.0));
    // A leading source atom consumes the left margin, so no safe nudge exists.
    let unfit = layout(&[
        Inline::SourceAtom {
            id: AtomId(1),
            source,
        },
        text("gs"),
    ]);
    assert!(unfit.paragraph.overflow);
}

#[test]
fn source_atom_advance_keeps_its_label_gap_before_following_text() {
    let origin = |advance| {
        let source = syncpdf_core::ir::SourceAtom {
            bbox: Rect::new(10.0, 20.0, 14.0, 24.0),
            baseline: 20.0,
            advance,
        };
        let result = layout(&[
            Inline::SourceAtom {
                id: AtomId(1),
                source,
            },
            text("s"),
        ]);
        let line = &result.paragraph.lines[0];
        (
            line.glyphs[0].x - line.placed_atoms[0].bbox.x0,
            line.placed_atoms[0].bbox.x0,
        )
    };
    let (ink, ink_x) = origin(None);
    let (label, label_x) = origin(Some(9.0));
    assert!((ink - 4.0).abs() < 0.001, "{ink}");
    assert!((label - 9.0).abs() < 0.001, "{label}");
    assert_eq!(ink_x, label_x);
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

#[test]
fn source_formula_retains_geometry_and_gets_only_necessary_leading() {
    let source = syncpdf_core::ir::SourceAtom {
        bbox: Rect::new(5.0, 17.0, 15.0, 30.0),
        baseline: 20.0,
        advance: None,
    };
    let result = layout(&[
        Inline::SourceAtom {
            id: AtomId(1),
            source,
        },
        Inline::Br,
        text("T"),
        Inline::Br,
        text("a"),
    ]);
    assert!(!result.paragraph.overflow);
    let lines = &result.paragraph.lines;
    let formula = &lines[0].placed_atoms[0];
    assert_eq!(formula.source, source.bbox);
    assert_eq!(formula.bbox.width(), 10.0);
    assert_eq!(formula.bbox.height(), 13.0);
    assert_eq!(lines[0].baseline_y, 40.0);
    assert!(lines[0].baseline_y - lines[1].baseline_y > 10.0);
    assert!((lines[1].baseline_y - lines[2].baseline_y - 10.0).abs() < 0.001);
    assert!(lines[1].bbox.y1 <= formula.bbox.y0);
    assert!(lines.iter().flat_map(|l| &l.glyphs).all(|g| g.size == 10.0));
}

struct SerifOverhang;
impl Shaper for SerifOverhang {
    fn shape(&self, font: u32, text: &str, size: f32, rtl: bool) -> Vec<ShapedGlyph> {
        MonoShaper.shape(font, text, size, rtl)
    }
    fn glyph_bounds(&self, _: u32, _: u16, size: f32) -> Option<Rect> {
        Some(Rect::new(0., 0., size * 0.5 + 0.35, size * 0.8))
    }
    fn metrics(&self, font: u32) -> FontMetrics {
        MonoShaper.metrics(font)
    }
    fn font_for(&self, _: &StyleSpec) -> u32 {
        0
    }
}

#[test]
fn justified_serif_ink_uses_less_added_glue_without_shrinking_glyphs() {
    let spec = ParagraphSpec {
        bbox: Rect::new(0., 0., 28., 100.),
        first_baseline: Some(90.),
        font_size: 10.,
        line_height: 1.5,
        align: Align::Justify,
        first_indent: 0.,
        is_rtl: false,
        color: Color::BLACK,
        styles: vec![],
        lang: Lang::En,
    };
    let out = Typeset::new(&SerifOverhang, FitOptions::default()).layout(
        "P01-001".parse().unwrap(),
        &spec,
        &[text("aa bb cc dd ee ff")],
        &Obstacles::default(),
    );
    assert!(!out.paragraph.overflow);
    assert_eq!(out.paragraph.lines.len(), 3);
    for line in &out.paragraph.lines {
        assert!(line.bbox.x0 >= 0. && line.bbox.x1 <= 28.);
        assert!(line.glyphs.iter().all(|g| g.size == 10. && g.scale_x == 1.));
        for pair in line.glyphs.windows(2) {
            let natural = MonoShaper.shape(0, &pair[0].text, 10., false)[0].x_advance;
            assert!(
                pair[1].x - pair[0].x >= natural - 0.001,
                "natural spacing cannot be compressed"
            );
        }
    }
    assert_eq!(
        out.paragraph.lines[0].baseline_y - out.paragraph.lines[1].baseline_y,
        15.
    );
    let mut narrow = spec;
    narrow.bbox.x1 = 9.9;
    let rejected = Typeset::new(&SerifOverhang, FitOptions::default()).layout(
        "P01-001".parse().unwrap(),
        &narrow,
        &[text("aa")],
        &Obstacles::default(),
    );
    assert!(rejected.paragraph.overflow, "unfit natural ink still fails");
}

#[test]
fn shrunk_justified_line_spends_more_of_its_shrink_when_ink_overhangs() {
    let spec = ParagraphSpec {
        bbox: Rect::new(0., 0., 64.5, 100.),
        first_baseline: Some(90.),
        font_size: 10.,
        line_height: 1.5,
        align: Align::Justify,
        first_indent: 0.,
        is_rtl: false,
        color: Color::BLACK,
        styles: vec![],
        lang: Lang::En,
    };
    let run = |spec: &ParagraphSpec, t: &str| {
        Typeset::new(&SerifOverhang, FitOptions::default()).layout(
            "P01-001".parse().unwrap(),
            spec,
            &[text(t)],
            &Obstacles::default(),
        )
    };
    // "aaaa bbbb cccc" is 65pt of advances: the breaker shrinks its spaces by 0.5pt,
    // and the last glyph's serif then reaches past the measure.
    let out = run(&spec, "aaaa bbbb cccc dddd");
    assert!(!out.paragraph.overflow);
    let first = &out.paragraph.lines[0];
    assert_eq!(
        first
            .glyphs
            .iter()
            .map(|g| g.text.as_str())
            .collect::<String>(),
        "aaaa bbbb cccc"
    );
    assert!(
        first.bbox.x0 >= 0. && first.bbox.x1 <= 64.5,
        "{:?}",
        first.bbox
    );
    assert!(out
        .paragraph
        .lines
        .iter()
        .flat_map(|l| &l.glyphs)
        .all(|g| g.size == 10. && g.scale_x == 1.));
    // A line without any glue has no shrink to spend: it still fails.
    let mut solid = spec;
    solid.bbox.x1 = 40.;
    assert!(run(&solid, "aaaaaaaa").paragraph.overflow);
}
