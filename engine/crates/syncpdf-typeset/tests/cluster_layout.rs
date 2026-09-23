use syncpdf_core::ir::Align;
use syncpdf_core::{Color, ParagraphId, Rect, StyleId};
use syncpdf_typeset::shaper::{FontMetrics, ShapedGlyph, Shaper, StyleSpec};
use syncpdf_typeset::{FitOptions, Inline, Lang, Obstacles, ParagraphSpec, Typeset, TypesetIssue};

struct Controlled;
impl Shaper for Controlled {
    fn shape(&self, font: u32, text: &str, size: f32, rtl: bool) -> Vec<ShapedGlyph> {
        let mut groups: Vec<_> = text
            .char_indices()
            .map(|(a, c)| (a, a + c.len_utf8(), c))
            .collect();
        if rtl {
            groups.reverse();
        }
        let mut result = Vec::new();
        for (a, b, c) in groups {
            if c == '∅' {
                continue;
            }
            let width = if c == ' ' { 0.25 } else { 0.5 } * size;
            let font = if c == 'Ω' { 9 } else { font };
            result.push(ShapedGlyph {
                gid: c as u16,
                cluster: a as u32,
                cluster_end: b as u32,
                font,
                x_advance: width,
                x_offset: if c == 'Ω' { 1.0 } else { 0.0 },
                y_offset: if c == 'Ω' { 2.0 } else { 0.0 },
            });
        }
        if text == "fi" {
            result = vec![ShapedGlyph {
                gid: 501,
                cluster: 0,
                cluster_end: 2,
                font,
                x_advance: size,
                x_offset: 0.0,
                y_offset: 0.0,
            }];
        } else if text == "e\u{301}" {
            result = vec![
                ShapedGlyph {
                    gid: 601,
                    cluster: 0,
                    cluster_end: 3,
                    font,
                    x_advance: size * 0.5,
                    x_offset: 0.0,
                    y_offset: 0.0,
                },
                ShapedGlyph {
                    gid: 602,
                    cluster: 0,
                    cluster_end: 3,
                    font,
                    x_advance: 0.0,
                    x_offset: -2.0,
                    y_offset: 3.0,
                },
            ];
        }
        result
    }
    fn glyph_bounds(&self, font: u32, gid: u16, size: f32) -> Option<Rect> {
        if gid == ' ' as u16 {
            return Some(Rect::new(0.0, 0.0, 0.0, 0.0));
        }
        if font == 9 {
            return Some(Rect::new(0.0, -0.2 * size, 0.4 * size, 0.7 * size));
        }
        Some(Rect::new(0.0, -0.1 * size, 0.4 * size, 0.5 * size))
    }
    fn metrics(&self, font: u32) -> FontMetrics {
        if font == 9 {
            FontMetrics {
                ascent: 2.0,
                descent: 1.0,
            }
        } else {
            FontMetrics {
                ascent: 1.5,
                descent: 0.5,
            }
        }
    }
    fn font_for(&self, _: &StyleSpec) -> u32 {
        3
    }
}

static SHAPER: Controlled = Controlled;
fn spec(bbox: Rect) -> ParagraphSpec {
    ParagraphSpec {
        bbox,
        font_size: 10.0,
        first_baseline: None,
        line_height: 1.2,
        align: Align::Left,
        first_indent: 0.0,
        is_rtl: false,
        color: Color::BLACK,
        styles: vec![(StyleId(1), StyleSpec::default())],
        lang: Lang::En,
    }
}
fn text(s: &str, style: u32) -> Inline {
    Inline::Text {
        text: s.to_owned(),
        style: StyleId(style),
    }
}
fn run(spec: &ParagraphSpec, inlines: &[Inline]) -> syncpdf_typeset::TypesetResult {
    Typeset::new(&SHAPER, FitOptions::default()).layout(
        ParagraphId { page: 1, seq: 1 },
        spec,
        inlines,
        &Obstacles::default(),
    )
}
fn overflow(result: &syncpdf_typeset::TypesetResult) -> bool {
    result.paragraph.overflow
        && result
            .issues
            .iter()
            .any(|issue| matches!(issue, TypesetIssue::Overflow { .. }))
}

#[test]
fn ligature_and_multiglyph_cluster_are_indivisible() {
    let a = run(&spec(Rect::new(0.0, 0.0, 8.0, 20.0)), &[text("fi", 1)]);
    assert!(overflow(&a));
    assert_eq!(a.paragraph.lines[0].glyphs[0].text, "fi");
    let b = run(
        &spec(Rect::new(0.0, 0.0, 10.0, 20.0)),
        &[text("e\u{301}", 1)],
    );
    assert!(!overflow(&b));
    let glyphs = &b.paragraph.lines[0].glyphs;
    assert_eq!(glyphs.len(), 2);
    assert_eq!(glyphs[0].text, "e\u{301}");
    assert!(glyphs[1].text.is_empty());
    assert_eq!(glyphs[1].gid, 602);
}

#[test]
fn style_boundary_inside_word_does_not_break() {
    let r = run(
        &spec(Rect::new(0.0, 0.0, 15.0, 30.0)),
        &[text("ab", 1), text("cd", 2)],
    );
    assert!(overflow(&r));
    assert_eq!(r.paragraph.lines.len(), 1);
}

#[test]
fn real_ink_fits_despite_large_global_metrics_and_baseline_is_fixed() {
    let mut s = spec(Rect::new(0.0, 0.0, 20.0, 10.0));
    let a = run(&s, &[text("Ω", 1)]);
    assert!(!overflow(&a), "{:?}", a.issues);
    let g = &a.paragraph.lines[0].glyphs[0];
    assert_eq!(g.font, 9);
    assert_eq!(g.x, 1.0);
    assert!((g.y - 3.0).abs() < 0.01);
    assert!((a.paragraph.used_bbox.y1 - 10.0).abs() < 0.01);
    s.first_baseline = Some(3.25);
    let b = run(&s, &[text("Ω", 1)]);
    assert!((b.paragraph.lines[0].baseline_y - 3.25).abs() < 0.0001);
    assert!(overflow(&b));
}

#[test]
fn run_size_color_offset_and_actual_font_survive() {
    let mut s = spec(Rect::new(0.0, 0.0, 50.0, 30.0));
    let red = Color {
        r: 1.0,
        g: 0.0,
        b: 0.0,
    };
    s.styles.push((
        StyleId(2),
        StyleSpec {
            size: Some(16.0),
            color: Some(red),
            ..StyleSpec::default()
        },
    ));
    let r = run(&s, &[text("a", 1), text("Ω", 2)]);
    assert!(!overflow(&r), "{:?}", r.issues);
    let glyphs = &r.paragraph.lines[0].glyphs;
    assert_eq!(glyphs[0].size, 10.0);
    assert_eq!(glyphs[1].size, 16.0);
    assert_eq!(glyphs[1].font, 9);
    assert_eq!(glyphs[1].color, Some(red));
    assert_eq!(glyphs[1].x, 6.0);
    assert!(glyphs[1].y > r.paragraph.lines[0].baseline_y);
}

#[test]
fn advance_too_wide_never_reports_success() {
    let r = run(&spec(Rect::new(0.0, 0.0, 8.0, 40.0)), &[text("ab", 1)]);
    assert!(overflow(&r));
}

#[test]
fn consecutive_breaks_keep_empty_line() {
    let r = run(
        &spec(Rect::new(0.0, 0.0, 50.0, 50.0)),
        &[text("a", 1), Inline::Br, Inline::Br, text("b", 1)],
    );
    assert_eq!(r.paragraph.lines.len(), 3);
    assert!(r.paragraph.lines[1].glyphs.is_empty());
}

#[test]
fn no_break_space_and_missing_shape_cannot_succeed() {
    let no_break = run(
        &spec(Rect::new(0.0, 0.0, 15.0, 30.0)),
        &[text("aa\u{00A0}bb", 1)],
    );
    assert!(overflow(&no_break));
    assert_eq!(no_break.paragraph.lines.len(), 1);
    let missing = run(&spec(Rect::new(0.0, 0.0, 50.0, 30.0)), &[text("a∅b", 1)]);
    assert!(overflow(&missing));
}

#[test]
fn rtl_shaper_receives_logical_word_once() {
    struct Capture(std::sync::Mutex<Vec<(String, bool)>>);
    impl Shaper for Capture {
        fn shape(&self, font: u32, text: &str, size: f32, rtl: bool) -> Vec<ShapedGlyph> {
            self.0.lock().unwrap().push((text.to_owned(), rtl));
            Controlled.shape(font, text, size, rtl)
        }
        fn glyph_bounds(&self, font: u32, gid: u16, size: f32) -> Option<Rect> {
            Controlled.glyph_bounds(font, gid, size)
        }
        fn metrics(&self, font: u32) -> FontMetrics {
            Controlled.metrics(font)
        }
        fn font_for(&self, style: &StyleSpec) -> u32 {
            Controlled.font_for(style)
        }
    }
    let shaper = Capture(std::sync::Mutex::new(Vec::new()));
    let mut s = spec(Rect::new(0.0, 0.0, 100.0, 20.0));
    s.is_rtl = true;
    s.lang = Lang::Ar;
    let word = "مرحبا";
    let r = Typeset::new(&shaper, FitOptions::default()).layout(
        ParagraphId { page: 1, seq: 1 },
        &s,
        &[text(word, 1)],
        &Obstacles::default(),
    );
    assert!(!overflow(&r));
    assert_eq!(&*shaper.0.lock().unwrap(), &[(word.to_owned(), true)]);
    assert_eq!(r.paragraph.lines[0].glyphs[0].text, "ا");
}
