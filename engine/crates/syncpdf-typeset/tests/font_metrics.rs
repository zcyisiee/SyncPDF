use syncpdf_core::{ir::Align, Color, Rect, StyleId};
use syncpdf_typeset::shaper::MonoShaper;
use syncpdf_typeset::{
    FitOptions, FontMetrics, Inline, Lang, Obstacles, ParagraphSpec, ShapedGlyph, Shaper,
    StyleSpec, Typeset,
};

struct SelectedFonts;

impl Shaper for SelectedFonts {
    fn shape(&self, font: u32, text: &str, size: f32, rtl: bool) -> Vec<ShapedGlyph> {
        assert!(matches!(font, 7 | 9));
        MonoShaper.shape(font, text, size, rtl)
    }

    fn metrics(&self, font: u32) -> FontMetrics {
        match font {
            7 => FontMetrics {
                ascent: 0.8,
                descent: 0.2,
            },
            9 => FontMetrics {
                ascent: 1.2,
                descent: 0.3,
            },
            _ => panic!("font {font} is unrelated to this paragraph"),
        }
    }

    fn font_for(&self, style: &StyleSpec) -> u32 {
        if style.bold {
            9
        } else {
            7
        }
    }
}

fn spec(height: f32) -> ParagraphSpec {
    ParagraphSpec {
        bbox: Rect::new(0.0, 0.0, 100.0, height),
        font_size: 10.0,
        line_height: 1.6,
        align: Align::Left,
        first_indent: 0.0,
        is_rtl: false,
        color: Color::BLACK,
        styles: vec![(
            StyleId(1),
            StyleSpec {
                bold: true,
                ..Default::default()
            },
        )],
        lang: Lang::En,
    }
}

fn fixed_options() -> FitOptions {
    FitOptions {
        min_scale: 1.0,
        line_height_steps: vec![1.0, 0.95],
        ..Default::default()
    }
}

#[test]
fn baseline_and_fit_use_selected_fonts_not_font_zero() {
    let shaper = SelectedFonts;
    let typeset = Typeset::new(&shaper, fixed_options());
    let text = [Inline::Text {
        text: "hello".into(),
        style: StyleId(0),
    }];
    let out = typeset.layout(
        "P01-001".parse().unwrap(),
        &spec(10.0),
        &text,
        &Obstacles::default(),
    );
    assert!(!out.paragraph.overflow);
    assert_eq!(out.paragraph.lines[0].baseline_y, 2.0);
    assert_eq!(out.paragraph.lines[0].bbox.y0, 0.0);
    assert!(out.paragraph.lines[0].glyphs.iter().all(|g| g.font == 7));

    let overflow = typeset.layout(
        "P01-001".parse().unwrap(),
        &spec(9.0),
        &text,
        &Obstacles::default(),
    );
    assert!(
        overflow.paragraph.overflow,
        "fit must use the same metrics as placement"
    );
    assert_eq!(overflow.scale, 1.0);
}

#[test]
fn mixed_styles_reserve_the_tallest_used_font() {
    let shaper = SelectedFonts;
    let typeset = Typeset::new(&shaper, fixed_options());
    let text = [
        Inline::Text {
            text: "A".into(),
            style: StyleId(0),
        },
        Inline::Text {
            text: "B".into(),
            style: StyleId(1),
        },
    ];
    let out = typeset.layout(
        "P01-001".parse().unwrap(),
        &spec(15.0),
        &text,
        &Obstacles::default(),
    );
    assert!(!out.paragraph.overflow);
    assert_eq!(out.paragraph.lines[0].baseline_y, 3.0);
    assert_eq!(out.paragraph.lines[0].bbox, Rect::new(0.0, 0.0, 10.0, 15.0));
}
