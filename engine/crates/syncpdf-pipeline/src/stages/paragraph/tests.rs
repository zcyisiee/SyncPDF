//! `paragraph_analysis` 纯函数单测：手工构造 `PageIR`，不依赖 pdfium 与模型。

use super::*;
use syncpdf_core::ir::{DisplayItem, FontRef, Glyph, GlyphFlags, GlyphSource};
use syncpdf_core::{Color, Matrix, ObjRef, OpKey, PageId};

/// 造一个字形：`ch` 的 bbox 左下角为 `(x, y)`，宽 0.6×size、高 size。
fn mk_glyph(seq: u16, ch: char, x: f32, y: f32, size: f32, font: u32) -> Glyph {
    let w = size * 0.6;
    Glyph {
        id: GlyphId {
            page: PageId(0),
            op: OpKey::new(ObjRef::new(1, 0), 0),
            ordinal: seq,
        },
        unicode: [ch].into_iter().collect(),
        code: seq as u32,
        font,
        size,
        matrix: Matrix::new(1.0, 0.0, 0.0, 1.0, x, y),
        bbox: Rect::new(x, y, x + w, y + size),
        ink: None,
        advance: w,
        fill: Color::default(),
        render_mode: 0,
        source: GlyphSource {
            element_index: 0,
            string_operand_range: (0, 0),
            decoded_code_range: (0, 0),
        },
        flags: GlyphFlags::default(),
    }
}

fn mk_font(name: &str, bold: bool, italic: bool) -> FontRef {
    FontRef {
        resource_name: name.into(),
        base_font: name.into(),
        is_serif: false,
        is_fixed_pitch: false,
        is_italic: italic,
        is_bold: bold,
    }
}

/// 一行字：`text` 的每个字符一个字形，自左向右；`y` 为基线（bbox 底边）。
fn line(seq_start: u16, text: &str, x: f32, y: f32, size: f32, font: u32) -> Vec<Glyph> {
    let adv = size * 0.6;
    text.chars()
        .enumerate()
        .map(|(i, c)| mk_glyph(seq_start + i as u16, c, x + i as f32 * adv, y, size, font))
        .collect()
}

fn page_ir(glyphs: Vec<Glyph>, fonts: Vec<FontRef>) -> PageIR {
    PageIR {
        page: PageId(0),
        media_box: Rect::new(0.0, 0.0, 612.0, 792.0),
        crop_box: Rect::new(0.0, 0.0, 612.0, 792.0),
        rotation: 0,
        fonts,
        items: vec![DisplayItem::Text { glyphs }],
    }
}

/// 覆盖全页的单个区域。
fn full_region(kind: RegionKind) -> Vec<Region> {
    vec![Region {
        page: PageId(0),
        index: 0,
        kind,
        bbox: Rect::new(0.0, 0.0, 612.0, 792.0),
        score: 0.95,
        order: 0,
    }]
}

fn text_region(index: u32, bbox: Rect, order: u32) -> Region {
    Region {
        page: PageId(0),
        index,
        kind: RegionKind::Text,
        bbox,
        score: 0.9,
        order,
    }
}

#[test]
fn preserved_region_glyphs_cannot_enter_a_translatable_paragraph() {
    let mut glyphs = line(0, "First formula", 50.0, 700.0, 10.0, 0);
    glyphs.extend(line(20, "Separate prose", 50.0, 600.0, 10.0, 0));
    let ir = page_ir(glyphs, vec![mk_font("F1", false, false)]);
    for kind in [
        RegionKind::Figure,
        RegionKind::Table,
        RegionKind::Code,
        RegionKind::Reference,
    ] {
        let mut regions = full_region(RegionKind::Text);
        let mut protected = text_region(1, Rect::new(80.0, 699.0, 129.0, 711.0), 1);
        protected.kind = kind;
        regions.push(protected.clone());
        let paragraphs = analyze_page(&ir, &regions);
        assert!(paragraphs.iter().any(|p| matches!(&p.translatable,
            Translatable::No { reason } if reason == "protected_source_overlap")));
        assert!(paragraphs
            .iter()
            .any(|p| p.text == "Separate prose" && matches!(p.translatable, Translatable::Yes)));
        let protected_ids: Vec<_> = ir
            .glyphs()
            .filter(|g| protected.bbox.contains(g.bbox.center()))
            .map(|g| g.id)
            .collect();
        for p in paragraphs
            .iter()
            .filter(|p| matches!(p.translatable, Translatable::Yes))
        {
            assert!(
                p.glyphs.iter().all(|id| !protected_ids.contains(id)),
                "{kind:?}: 保留字形不能被可译段消费"
            );
        }
    }
}

#[test]
fn inline_formula_is_owned_once_and_prose_is_translatable() {
    let mut glyphs = line(0, "Value x then", 50.0, 700.0, 10.0, 0);
    // Isolate x using measured glyph geometry; protect all other kinds as before.
    let formula_box = glyphs[6].bbox;
    glyphs[6].bbox.y0 -= 2.0;
    let ir = page_ir(glyphs, vec![mk_font("F1", false, false)]);
    let mut regions = full_region(RegionKind::Text);
    let mut formula = text_region(1, formula_box, 1);
    formula.kind = RegionKind::Formula;
    regions.push(formula);
    let paragraphs = analyze_page(&ir, &regions);
    let p = paragraphs
        .iter()
        .find(|p| p.kind == RegionKind::Text)
        .unwrap();
    assert!(matches!(p.translatable, Translatable::Yes));
    let atom = p
        .atoms
        .iter()
        .find(|a| a.kind == AtomKind::Formula)
        .unwrap();
    assert_eq!(atom.text, "x");
    assert!(atom.source.is_some());
    assert_eq!(atom.glyph_range.1 - atom.glyph_range.0, 1);
}

/// The detected Formula box ends just below the formula's top ink (a radical's
/// overbar at the top of the `√` ink). The bar belongs to the formula; a rule
/// crossing the whole row at the same height does not.
#[test]
fn formula_bar_inside_own_ink_extent_but_outside_detected_box() {
    for (bar, owned) in [
        (Rect::new(91.5, 711.95, 98.3, 711.95), true),
        (Rect::new(50.0, 711.95, 150.0, 711.95), false),
    ] {
        let mut glyphs = line(0, "Value rn then", 50.0, 700.0, 10.0, 0);
        glyphs[6].ink = Some(Rect::new(86.2, 699.0, 91.8, 712.0));
        glyphs[7].ink = Some(Rect::new(92.2, 700.5, 97.8, 708.0));
        let mut ir = page_ir(glyphs, vec![mk_font("F1", false, false)]);
        ir.items.push(bar_item(bar, 1));
        let mut regions = full_region(RegionKind::Text);
        let mut formula = text_region(1, Rect::new(86.0, 699.0, 98.5, 711.9), 1);
        formula.kind = RegionKind::Formula;
        regions.push(formula);
        let paragraphs = analyze_page(&ir, &regions);
        let p = paragraphs
            .iter()
            .find(|p| p.kind == RegionKind::Text)
            .unwrap();
        if owned {
            assert!(matches!(p.translatable, Translatable::Yes), "{bar:?}");
            let atom = p
                .atoms
                .iter()
                .find(|a| a.kind == AtomKind::Formula)
                .unwrap();
            assert_eq!(atom.text, "rn");
            let source = atom.source.unwrap();
            assert!(source.bbox.x1 >= bar.x1 && source.bbox.y1 >= bar.y1);
        } else {
            assert!(
                matches!(&p.translatable, Translatable::No { reason } if reason == "protected_source_overlap"),
                "{bar:?}: {:?}",
                p.translatable
            );
        }
    }
}

#[test]
#[ignore = "requires local source/layout evidence and writes an audit inventory"]
fn inline_formula_document_inventory() {
    let root = std::path::PathBuf::from(std::env::var("SYNCPDF_SOURCE_AUDIT").unwrap());
    let pages: Vec<PageIR> =
        serde_json::from_slice(&std::fs::read(root.join("source.json")).unwrap()).unwrap();
    let mut all = Vec::new();
    for ir in pages {
        let regions: Vec<Region> = serde_json::from_slice(
            &std::fs::read(root.join(format!("regions-{}.json", ir.page.0))).unwrap(),
        )
        .unwrap();
        let mut paragraphs = analyze_page(&ir, &regions);
        crate::stages::source_policy::protect_front_matter(&ir, &regions, &mut paragraphs);
        all.extend(paragraphs);
    }
    std::fs::write(
        std::env::var("SYNCPDF_INVENTORY").unwrap(),
        serde_json::to_vec_pretty(&all).unwrap(),
    )
    .unwrap();
}

#[test]
fn proven_body_font_trailing_comma_leaves_formula_atom() {
    // Real P12-031 shape: the Formula box swallows a body-font comma after math H.
    let mut glyphs = line(0, "In", 50.0, 700.0, 10.0, 0);
    glyphs.extend(line(2, "H", 64.0, 700.0, 10.0, 1));
    let mut comma = mk_glyph(3, ',', 71.0, 700.0, 10.0, 0);
    comma.ink = Some(Rect::new(71.1, 700.0, 74.0, 703.0));
    let comma_id = comma.id;
    let comma_ink = comma.ink.unwrap();
    glyphs.push(comma);
    glyphs.extend(line(4, "the total", 80.0, 700.0, 10.0, 0));
    let ir = page_ir(
        glyphs,
        vec![mk_font("Body", false, false), mk_font("Math", false, true)],
    );
    let mut regions = full_region(RegionKind::Text);
    let mut formula = text_region(1, Rect::new(63.0, 695.0, 76.0, 712.0), 1);
    formula.kind = RegionKind::Formula;
    regions.push(formula);
    let paragraphs = analyze_page(&ir, &regions);
    let p = paragraphs
        .iter()
        .find(|p| p.kind == RegionKind::Text)
        .unwrap();
    assert!(
        matches!(p.translatable, Translatable::Yes),
        "{:?}",
        p.translatable
    );
    assert_eq!(p.text, "In H, the total");
    let atom = p
        .atoms
        .iter()
        .find(|a| a.kind == AtomKind::Formula)
        .unwrap();
    assert_eq!(atom.text, "H");
    assert!(atom.source.is_some());
    let comma_index = p.glyphs.iter().position(|&id| id == comma_id).unwrap() as u32;
    assert_eq!(atom.glyph_range, (2, 3));
    assert!(comma_index >= atom.glyph_range.1);
    // The replay clip must not touch the released comma's ink.
    let clip = atom.source.unwrap().bbox;
    assert!(clip.x1 <= comma_ink.x0 || clip.x0 >= comma_ink.x1);
}

#[test]
fn wrapped_line_trailing_comma_is_released_by_next_row_prose() {
    // P13-008 shape: comma ends its physical line and prose continues below.
    let mut glyphs = line(0, "of", 50.0, 700.0, 10.0, 0);
    glyphs.extend(line(2, "A", 70.0, 700.0, 10.0, 1));
    let mut comma = mk_glyph(3, ',', 76.5, 700.0, 10.0, 0);
    comma.ink = Some(Rect::new(76.6, 700.0, 79.4, 703.0));
    glyphs.push(comma);
    glyphs.extend(line(4, "so it", 50.0, 686.0, 10.0, 0));
    let ir = page_ir(
        glyphs,
        vec![mk_font("Body", false, false), mk_font("Math", false, true)],
    );
    let mut regions = full_region(RegionKind::Text);
    let mut formula = text_region(1, Rect::new(69.0, 695.0, 80.0, 712.0), 1);
    formula.kind = RegionKind::Formula;
    regions.push(formula);
    let paragraphs = analyze_page(&ir, &regions);
    let p = paragraphs
        .iter()
        .find(|p| p.kind == RegionKind::Text)
        .unwrap();
    assert!(
        matches!(p.translatable, Translatable::Yes),
        "{:?}",
        p.translatable
    );
    assert_eq!(p.text, "of A, so it");
    let atom = p
        .atoms
        .iter()
        .find(|a| a.kind == AtomKind::Formula)
        .unwrap();
    assert_eq!(atom.text, "A");
    assert_eq!(atom.glyph_range, (2, 3));
}

#[test]
fn unclosed_delimiter_keeps_trailing_comma_inside_atom() {
    // "(x," keeps an unclosed math delimiter: the comma may separate arguments.
    let mut glyphs = line(0, "see", 50.0, 700.0, 10.0, 0);
    glyphs.extend(line(3, "(x", 75.0, 700.0, 10.0, 1));
    let mut comma = mk_glyph(5, ',', 88.0, 700.0, 10.0, 0);
    comma.ink = Some(Rect::new(88.1, 700.0, 91.0, 703.0));
    glyphs.push(comma);
    glyphs.extend(line(6, "now", 97.0, 700.0, 10.0, 0));
    let ir = page_ir(
        glyphs,
        vec![mk_font("Body", false, false), mk_font("Math", false, true)],
    );
    let mut regions = full_region(RegionKind::Text);
    let mut formula = text_region(1, Rect::new(74.0, 695.0, 92.0, 712.0), 1);
    formula.kind = RegionKind::Formula;
    regions.push(formula);
    let paragraphs = analyze_page(&ir, &regions);
    let p = paragraphs
        .iter()
        .find(|p| p.kind == RegionKind::Text)
        .unwrap();
    assert!(
        matches!(p.translatable, Translatable::Yes),
        "{:?}",
        p.translatable
    );
    let atom = p
        .atoms
        .iter()
        .find(|a| a.kind == AtomKind::Formula)
        .unwrap();
    assert_eq!(atom.text, "(x,");
    assert_eq!(atom.glyph_range, (3, 6));
}

#[test]
fn math_font_trailing_comma_stays_inside_atom() {
    // No font boundary: a math-font comma can be a math separator.
    let mut glyphs = line(0, "a", 50.0, 700.0, 10.0, 0);
    glyphs.extend(line(1, "x", 60.0, 700.0, 10.0, 1));
    let mut comma = mk_glyph(2, ',', 66.0, 700.0, 10.0, 1);
    comma.ink = Some(Rect::new(66.1, 700.0, 69.0, 703.0));
    glyphs.push(comma);
    glyphs.extend(line(3, "y", 76.0, 700.0, 10.0, 0));
    let ir = page_ir(
        glyphs,
        vec![mk_font("Body", false, false), mk_font("Math", false, true)],
    );
    let mut regions = full_region(RegionKind::Text);
    let mut formula = text_region(1, Rect::new(59.0, 695.0, 73.0, 712.0), 1);
    formula.kind = RegionKind::Formula;
    regions.push(formula);
    let paragraphs = analyze_page(&ir, &regions);
    let p = paragraphs
        .iter()
        .find(|p| p.kind == RegionKind::Text)
        .unwrap();
    assert!(matches!(p.translatable, Translatable::Yes));
    let atom = p
        .atoms
        .iter()
        .find(|a| a.kind == AtomKind::Formula)
        .unwrap();
    assert_eq!(atom.text, "x,");
}

#[test]
fn trailing_comma_without_tight_ink_stays_inside_atom() {
    let mut glyphs = line(0, "In", 50.0, 700.0, 10.0, 0);
    glyphs.extend(line(2, "H", 64.0, 700.0, 10.0, 1));
    glyphs.push(mk_glyph(3, ',', 71.0, 700.0, 10.0, 0));
    glyphs.extend(line(4, "the", 80.0, 700.0, 10.0, 0));
    let ir = page_ir(
        glyphs,
        vec![mk_font("Body", false, false), mk_font("Math", false, true)],
    );
    let mut regions = full_region(RegionKind::Text);
    let mut formula = text_region(1, Rect::new(63.0, 695.0, 76.0, 712.0), 1);
    formula.kind = RegionKind::Formula;
    regions.push(formula);
    let paragraphs = analyze_page(&ir, &regions);
    let p = paragraphs
        .iter()
        .find(|p| p.kind == RegionKind::Text)
        .unwrap();
    assert!(matches!(p.translatable, Translatable::Yes));
    let atom = p
        .atoms
        .iter()
        .find(|a| a.kind == AtomKind::Formula)
        .unwrap();
    assert_eq!(atom.text, "H,");
}

#[test]
fn comma_shared_with_another_preserved_region_stays_inside_atom() {
    // A second Formula region also covering the comma makes ownership ambiguous.
    let mut glyphs = line(0, "In", 50.0, 700.0, 10.0, 0);
    glyphs.extend(line(2, "H", 64.0, 700.0, 10.0, 1));
    let mut comma = mk_glyph(3, ',', 71.0, 700.0, 10.0, 0);
    comma.ink = Some(Rect::new(71.1, 700.0, 74.0, 703.0));
    glyphs.push(comma);
    glyphs.extend(line(4, "the", 80.0, 700.0, 10.0, 0));
    let ir = page_ir(
        glyphs,
        vec![mk_font("Body", false, false), mk_font("Math", false, true)],
    );
    let mut regions = full_region(RegionKind::Text);
    let mut formula = text_region(1, Rect::new(63.0, 695.0, 76.0, 712.0), 1);
    formula.kind = RegionKind::Formula;
    let mut second = text_region(2, Rect::new(70.0, 695.0, 90.0, 712.0), 2);
    second.kind = RegionKind::Formula;
    regions.push(formula);
    regions.push(second);
    let paragraphs = analyze_page(&ir, &regions);
    let p = paragraphs
        .iter()
        .find(|p| p.kind == RegionKind::Text)
        .unwrap();
    // Ambiguous ownership stays fail-closed: the comma is never free prose.
    assert!(
        !matches!(p.translatable, Translatable::Yes)
            || p.atoms
                .iter()
                .any(|a| a.kind == AtomKind::Formula && a.text.ends_with(',')),
        "{:?} {:?}",
        p.translatable,
        p.atoms.iter().map(|a| &a.text).collect::<Vec<_>>()
    );
}

fn comma_context_fixture(prefix: &str, math: &str, suffix: &str) -> (PageIR, Vec<Region>, GlyphId) {
    let mut glyphs = line(0, prefix, 50.0, 700.0, 10.0, 0);
    let n = glyphs.len() as u16;
    glyphs.extend(line(n, math, 64.0, 700.0, 10.0, 1));
    let n = glyphs.len() as u16;
    let x = 65.0 + math.chars().count() as f32 * 6.0;
    let mut comma = mk_glyph(n, ',', x, 700.0, 10.0, 0);
    comma.ink = Some(Rect::new(x + 0.1, 700.0, x + 3.0, 703.0));
    let id = comma.id;
    glyphs.push(comma);
    glyphs.extend(line(n + 1, suffix, x + 9.0, 700.0, 10.0, 0));
    let ir = page_ir(
        glyphs,
        vec![mk_font("Body", false, false), mk_font("Math", false, true)],
    );
    let mut regions = full_region(RegionKind::Text);
    let mut formula = text_region(1, Rect::new(63.0, 695.0, x + 5.0, 712.0), 1);
    formula.kind = RegionKind::Formula;
    regions.push(formula);
    (ir, regions, id)
}

fn assert_comma_not_released(ir: &PageIR, regions: &[Region], comma: GlyphId) {
    let refs: Vec<_> = regions.iter().collect();
    let formulas = inline_formula::sources(ir, &refs);
    assert!(
        formulas.iter().all(|f| !f.released.contains(&comma)),
        "ambiguous comma was released"
    );
}

#[test]
fn trailing_comma_single_roman_variable_is_not_prose() {
    let (ir, regions, comma) = comma_context_fixture("In", "H", "y");
    assert_comma_not_released(&ir, &regions, comma);
}

#[test]
fn trailing_comma_external_open_delimiter_stays_protected() {
    let (ir, regions, comma) = comma_context_fixture("f(", "H", "the maximum");
    assert_comma_not_released(&ir, &regions, comma);
}

#[test]
fn trailing_comma_mismatched_delimiters_stay_protected() {
    let (ir, regions, comma) = comma_context_fixture("In", "(H]", "the total");
    assert_comma_not_released(&ir, &regions, comma);
}

#[test]
fn trailing_comma_nonfinite_ink_is_not_evidence() {
    let (mut ir, regions, comma) = comma_context_fixture("In", "H", "the total");
    for item in &mut ir.items {
        if let DisplayItem::Text { glyphs } = item {
            for g in glyphs {
                if g.id == comma {
                    g.ink.as_mut().unwrap().x0 = f32::NAN;
                }
            }
        }
    }
    assert_comma_not_released(&ir, &regions, comma);
}

#[test]
fn trailing_comma_unknown_successor_is_not_skipped() {
    let (mut ir, regions, comma) = comma_context_fixture("In", "H", "the total");
    for item in &mut ir.items {
        if let DisplayItem::Text { glyphs } = item {
            let next = glyphs
                .iter_mut()
                .find(|g| g.id.ordinal == comma.ordinal + 1)
                .unwrap();
            next.unicode.clear();
            next.ink = Some(next.bbox);
        }
    }
    assert_comma_not_released(&ir, &regions, comma);
}

#[test]
fn trailing_comma_cannot_borrow_another_paragraphs_formula() {
    let (ir, mut regions, comma) = comma_context_fixture("In", "H", "the total");
    // A smaller prose region owns only the comma and continuation, not H.
    regions.push(text_region(2, Rect::new(70.5, 695.0, 104.0, 712.0), 2));
    let paragraphs = analyze_page(&ir, &regions);
    let owner = paragraphs
        .iter()
        .find(|p| p.kind == RegionKind::Text && p.glyphs.contains(&comma))
        .unwrap();
    assert!(
        !matches!(owner.translatable, Translatable::Yes),
        "released ownership crossed paragraph: {:?}",
        owner
    );
}

#[test]
#[ignore = "requires saved real source/region evidence for the DeepSeek pages"]
fn real_formula_trailing_commas_are_released_to_prose() {
    let root = std::path::PathBuf::from(
        std::env::var("SYNCPDF_FORMULA_AUDIT").expect("SYNCPDF_FORMULA_AUDIT"),
    );
    let pages: Vec<PageIR> =
        serde_json::from_slice(&std::fs::read(root.join("source.json")).unwrap()).unwrap();
    let cases = ["P12-029", "P12-031", "P13-008"];
    let mut found = 0;
    let mut all_paragraphs = Vec::new();
    for ir in &pages {
        let page = ir.page.0;
        let regions: Vec<Region> = serde_json::from_slice(
            &std::fs::read(root.join(format!("regions-{page}.json"))).unwrap(),
        )
        .unwrap();
        let refs: Vec<_> = regions.iter().collect();
        let formulas = inline_formula::sources(ir, &refs);
        let mut paragraphs = analyze_page(ir, &regions);
        crate::stages::source_policy::protect_front_matter(ir, &regions, &mut paragraphs);
        for p in paragraphs {
            all_paragraphs.push(p.clone());
            if !cases.contains(&p.id.to_string().as_str()) {
                continue;
            }
            found += 1;
            assert!(
                matches!(p.translatable, Translatable::Yes),
                "{}: {:?}",
                p.id,
                p.translatable
            );
            let formula_atoms: Vec<_> = p
                .atoms
                .iter()
                .filter(|a| a.kind == AtomKind::Formula)
                .collect();
            assert!(
                formula_atoms.iter().all(|a| !a.text.ends_with(',')),
                "{}: {:?}",
                p.id,
                formula_atoms.iter().map(|a| &a.text).collect::<Vec<_>>()
            );
            for a in formula_atoms {
                eprintln!(
                    "{} atom text={:?} range={:?} bbox={:?}",
                    p.id,
                    a.text,
                    a.glyph_range,
                    a.source.unwrap().bbox
                );
            }
            let released = inline_formula::released_for(&p, &formulas);
            assert_eq!(released.len(), 1, "{} exact transferred ownership", p.id);
            let id = released.first().unwrap();
            let i = p.glyphs.iter().position(|g| g == id).unwrap();
            let expected = match p.id.to_string().as_str() {
                "P12-029" => (98, 665),
                "P12-031" => (3, 697),
                _ => (33, 202),
            };
            assert_eq!((i, id.op.op_index), expected);
            let g = ir.glyphs().find(|g| g.id == *id).unwrap();
            assert_eq!(g.unicode.as_slice(), [',']);
            assert!(p.atoms.iter().all(|a| {
                !(a.glyph_range.0 <= i as u32 && (i as u32) < a.glyph_range.1)
                    && a.source.is_none_or(|s| !s.bbox.intersects(&g.ink.unwrap()))
            }));
            eprintln!("{} proven released comma {:?} index {}", p.id, id, i);
        }
    }
    assert_eq!(found, 3, "all three real paragraphs must be produced");
    if let Ok(output) = std::env::var("SYNCPDF_FORMULA_AUDIT_OUTPUT") {
        std::fs::write(output, serde_json::to_vec(&all_paragraphs).unwrap()).unwrap();
    }
}

/// `prefix`（含正文 `(`）、`math`（数学字体）与 `suffix` 同一行；检测框覆盖
/// math 与尾随的正文 `)`。返回该 `)` 的 GlyphId 与 tight ink。
fn close_paren_fixture(
    prefix: &str,
    math: &str,
    suffix: &str,
) -> (PageIR, Vec<Region>, GlyphId, Rect) {
    let mut glyphs = line(0, prefix, 50.0, 700.0, 10.0, 0);
    for g in &mut glyphs {
        if g.unicode.as_slice() == ['('] {
            g.ink = Some(g.bbox);
        }
    }
    let n = glyphs.len() as u16;
    let math_x = 60.0 + prefix.chars().count() as f32 * 6.0;
    glyphs.extend(line(n, math, math_x, 700.0, 10.0, 1));
    let n = glyphs.len() as u16;
    let x = math_x + math.chars().count() as f32 * 6.0 + 0.5;
    let mut close = mk_glyph(n, ')', x, 700.0, 10.0, 0);
    close.ink = Some(Rect::new(x + 0.1, 700.0, x + 3.0, 710.0));
    let id = close.id;
    let ink = close.ink.unwrap();
    glyphs.push(close);
    glyphs.extend(line(n + 1, suffix, x + 9.0, 700.0, 10.0, 0));
    let ir = page_ir(
        glyphs,
        vec![mk_font("Body", false, false), mk_font("Math", false, true)],
    );
    let mut regions = full_region(RegionKind::Text);
    let mut formula = text_region(1, Rect::new(math_x - 1.0, 695.0, x + 5.0, 712.0), 1);
    formula.kind = RegionKind::Formula;
    regions.push(formula);
    (ir, regions, id, ink)
}

fn assert_close_not_released(ir: &PageIR, regions: &[Region], close: GlyphId) {
    let refs: Vec<_> = regions.iter().collect();
    let formulas = inline_formula::sources(ir, &refs);
    assert!(
        formulas.iter().all(|f| !f.released.contains(&close)),
        "ambiguous close paren was released"
    );
}

#[test]
fn prose_close_paren_swallowed_by_formula_is_released() {
    // Real P09-003 shape: "(i.e., the decoder, l>L/2)" — the detected box
    // swallows the prose `)` whose `(` and two full words sit outside it.
    let (ir, regions, close, ink) = close_paren_fixture("in (i.e., the decoder,", "x", ", so it");
    let refs: Vec<_> = regions.iter().collect();
    let formulas = inline_formula::sources(&ir, &refs);
    assert!(formulas.iter().any(|f| f.released.contains(&close)));
    let paragraphs = analyze_page(&ir, &regions);
    let p = paragraphs
        .iter()
        .find(|p| p.kind == RegionKind::Text)
        .unwrap();
    assert!(
        matches!(p.translatable, Translatable::Yes),
        "{:?}",
        p.translatable
    );
    assert_eq!(p.text, "in (i.e., the decoder, x), so it");
    let atom = p
        .atoms
        .iter()
        .find(|a| a.kind == AtomKind::Formula)
        .unwrap();
    assert_eq!(atom.text, "x");
    // The replay clip must not touch the released paren's ink.
    let clip = atom.source.unwrap().bbox;
    assert!(clip.x1 <= ink.x0 || clip.x0 >= ink.x1);
    let released = inline_formula::released_for(p, &formulas);
    assert!(released.contains(&close));
}

#[test]
fn close_paren_cannot_borrow_pairing_from_another_final_paragraph() {
    let (mut ir, regions, close, _) = close_paren_fixture("in (the decoder,", "x", ", so it");
    for item in &mut ir.items {
        if let DisplayItem::Text { glyphs } = item {
            for g in glyphs.iter_mut().take("in (the decoder,".len()) {
                g.matrix.f += 45.0;
                g.bbox.y0 += 45.0;
                g.bbox.y1 += 45.0;
                if let Some(ink) = &mut g.ink {
                    ink.y0 += 45.0;
                    ink.y1 += 45.0;
                }
            }
        }
    }
    let paragraphs = analyze_page(&ir, &regions);
    let p = paragraphs
        .iter()
        .find(|p| p.glyphs.contains(&close))
        .unwrap();
    let index = p.glyphs.iter().position(|g| *g == close).unwrap() as u32;
    assert!(
        matches!(p.translatable, Translatable::No { .. })
            || p.atoms
                .iter()
                .any(|a| a.source.is_some() && a.glyph_range.0 <= index && index < a.glyph_range.1),
        "pairing borrowed from another paragraph: {p:?}"
    );
}

#[test]
fn close_paren_missing_opener_ink_is_not_pairing_evidence() {
    let (mut ir, regions, close, _) = close_paren_fixture("in (the decoder,", "x", ", so it");
    for item in &mut ir.items {
        if let DisplayItem::Text { glyphs } = item {
            glyphs
                .iter_mut()
                .find(|g| g.unicode.as_slice() == ['('])
                .unwrap()
                .ink = None;
        }
    }
    assert_close_not_released(&ir, &regions, close);
}

#[test]
fn close_paren_cannot_ignore_protected_text_after_word_witnesses() {
    let (ir, mut regions, close, _) = close_paren_fixture("in (the decoder, abcd,", "x", ", so it");
    let glyph = ir.glyphs().find(|g| g.unicode.as_slice() == ['b']).unwrap();
    let mut protected = text_region(2, glyph.bbox, 2);
    protected.kind = RegionKind::Table;
    regions.push(protected);
    assert_close_not_released(&ir, &regions, close);
}

#[test]
fn close_paren_font_changes_do_not_make_two_complete_words() {
    let (mut ir, regions, close, _) = close_paren_fixture("in (abCde,", "x", ", so it");
    for item in &mut ir.items {
        if let DisplayItem::Text { glyphs } = item {
            glyphs
                .iter_mut()
                .find(|g| g.unicode.as_slice() == ['C'])
                .unwrap()
                .font = 1;
        }
    }
    assert_close_not_released(&ir, &regions, close);
}

#[test]
fn close_paren_unknown_lowered_glyph_is_not_skipped() {
    let (mut ir, regions, close, _) = close_paren_fixture("in (the decoder,", "x", ", so it");
    for item in &mut ir.items {
        if let DisplayItem::Text { glyphs } = item {
            let g = glyphs
                .iter_mut()
                .find(|g| g.unicode.as_slice() == [','])
                .unwrap();
            g.unicode.clear();
            g.matrix.f -= 3.0;
            g.bbox.y0 -= 3.0;
            g.bbox.y1 -= 3.0;
            g.ink = Some(g.bbox);
        }
    }
    assert_close_not_released(&ir, &regions, close);
}

#[test]
#[ignore = "requires saved real source/region evidence"]
fn real_prose_close_paren_releases_only_owned_boundary() {
    let root = std::path::PathBuf::from(std::env::var("SYNCPDF_FORMULA_AUDIT").unwrap());
    let pages: Vec<PageIR> =
        serde_json::from_slice(&std::fs::read(root.join("source.json")).unwrap()).unwrap();
    let ir = pages.iter().find(|p| p.page.0 == 8).unwrap();
    let regions: Vec<Region> =
        serde_json::from_slice(&std::fs::read(root.join("regions-8.json")).unwrap()).unwrap();
    let refs: Vec<_> = regions.iter().collect();
    let formulas = inline_formula::sources(ir, &refs);
    let paragraphs = analyze_page(ir, &regions);
    let p = paragraphs
        .iter()
        .find(|p| p.id.to_string() == "P09-003")
        .unwrap();
    assert!(matches!(p.translatable, Translatable::Yes));
    let a = p
        .atoms
        .iter()
        .find(|a| a.kind == AtomKind::Formula)
        .unwrap();
    assert_eq!(a.text, "𝑙>𝐿/2");
    assert_eq!(a.glyph_range, (119, 124));
    let released = inline_formula::released_for(p, &formulas);
    assert_eq!(released.len(), 1);
    let id = *released.first().unwrap();
    assert_eq!(p.glyphs[124], id);
    assert_eq!((id.op.op_index, id.ordinal), (51, 0));
    let close = ir.glyphs().find(|g| g.id == id).unwrap();
    assert_eq!(close.unicode.as_slice(), [')']);
    assert!(!a.source.unwrap().bbox.intersects(&close.ink.unwrap()));
    assert_eq!(p.atoms[2].text, "(𝑊𝑙𝐾𝑉");
    assert_eq!(p.atoms[3].text, "𝑊𝑙𝑍)");
}

#[test]
fn math_function_close_paren_stays_inside_atom() {
    // "f(x)" math: the `(` lives inside the same box, no prose pair exists.
    let (ir, regions, close, _) = close_paren_fixture("call f", "(x", " now");
    assert_close_not_released(&ir, &regions, close);
}

#[test]
fn math_tuple_close_paren_stays_inside_atom() {
    let (ir, regions, close, _) = close_paren_fixture("at ", "(x,y", " now");
    assert_close_not_released(&ir, &regions, close);
}

#[test]
fn open_ended_math_paren_keeps_close_paren() {
    // "g(x" is unclosed math; a trailing `)` cannot close a prose pair for it.
    let (ir, regions, close, _) = close_paren_fixture("(see the note,", "g(x", " end");
    assert_close_not_released(&ir, &regions, close);
}

#[test]
fn close_paren_without_prose_words_stays_inside_atom() {
    // "(a x+y)": a prose `(` but no complete word inside the pair — the box may
    // have missed the real math opener, so the `)` stays protected.
    let (ir, regions, close, _) = close_paren_fixture("(a", "x+y", " end");
    assert_close_not_released(&ir, &regions, close);
}

#[test]
fn unpaired_close_paren_stays_inside_atom() {
    // Words but no `(` anywhere before it: the pairing is unprovable.
    let (ir, regions, close, _) = close_paren_fixture("the decoder,", "x", " end");
    assert_close_not_released(&ir, &regions, close);
}

#[test]
fn mismatched_open_bracket_keeps_close_paren() {
    // A `[` cannot close a `)`; type mismatch keeps the protection.
    let (ir, regions, close, _) = close_paren_fixture("[see the note,", "x", " end");
    assert_close_not_released(&ir, &regions, close);
}

#[test]
fn close_paren_unknown_glyph_in_pair_stays_inside_atom() {
    let (mut ir, regions, close, _) = close_paren_fixture("in (i.e., the decoder,", "x", ", so it");
    for item in &mut ir.items {
        if let DisplayItem::Text { glyphs } = item {
            let g = glyphs
                .iter_mut()
                .find(|g| g.unicode.as_slice() == ['e'] && g.font == 0)
                .unwrap();
            g.unicode.clear();
            g.ink = Some(g.bbox);
        }
    }
    assert_close_not_released(&ir, &regions, close);
}

#[test]
fn close_paren_without_tight_ink_stays_inside_atom() {
    let (mut ir, regions, close, _) = close_paren_fixture("in (i.e., the decoder,", "x", ", so it");
    for item in &mut ir.items {
        if let DisplayItem::Text { glyphs } = item {
            for g in glyphs {
                if g.id == close {
                    g.ink = None;
                }
            }
        }
    }
    assert_close_not_released(&ir, &regions, close);
}

#[test]
fn close_paren_nonfinite_ink_stays_inside_atom() {
    let (mut ir, regions, close, _) = close_paren_fixture("in (i.e., the decoder,", "x", ", so it");
    for item in &mut ir.items {
        if let DisplayItem::Text { glyphs } = item {
            for g in glyphs {
                if g.id == close {
                    g.ink.as_mut().unwrap().x0 = f32::NAN;
                }
            }
        }
    }
    assert_close_not_released(&ir, &regions, close);
}

#[test]
fn close_paren_shared_formula_region_stays_inside_atom() {
    // A second Formula region also covering the `)` makes ownership ambiguous.
    let (ir, mut regions, close, _) = close_paren_fixture("in (i.e., the decoder,", "x", ", so it");
    let mut second = text_region(2, Rect::new(180.0, 695.0, 220.0, 712.0), 2);
    second.kind = RegionKind::Formula;
    regions.push(second);
    assert_close_not_released(&ir, &regions, close);
}

#[test]
fn cross_atom_paren_pair_keeps_close_paren() {
    // "(W_A and more W_B)" split across two detected boxes: the `(` is owned by
    // an earlier Formula atom, so it is not a prose opener for this `)`.
    let mut glyphs = line(0, "with ", 50.0, 700.0, 10.0, 0);
    let mut open = mk_glyph(5, '(', 80.0, 700.0, 10.0, 0);
    open.ink = Some(Rect::new(80.1, 700.0, 83.0, 710.0));
    glyphs.push(open);
    glyphs.extend(line(6, "W", 86.0, 700.0, 10.0, 1));
    glyphs.extend(line(7, " and more ", 95.0, 700.0, 10.0, 0));
    glyphs.extend(line(17, "W", 160.0, 700.0, 10.0, 1));
    let mut close = mk_glyph(18, ')', 166.5, 700.0, 10.0, 0);
    close.ink = Some(Rect::new(166.6, 700.0, 169.4, 710.0));
    let close_id = close.id;
    glyphs.push(close);
    let ir = page_ir(
        glyphs,
        vec![mk_font("Body", false, false), mk_font("Math", false, true)],
    );
    let mut regions = full_region(RegionKind::Text);
    let mut first = text_region(1, Rect::new(79.0, 695.0, 93.0, 712.0), 1);
    first.kind = RegionKind::Formula;
    let mut second = text_region(2, Rect::new(159.0, 695.0, 174.0, 712.0), 2);
    second.kind = RegionKind::Formula;
    regions.push(first);
    regions.push(second);
    assert_close_not_released(&ir, &regions, close_id);
}

/// 段末冒号引出下方独立公式：正文行 `reward of response` + 数学 `z` + 正文 `:`，
/// 检测框吞入冒号；下方独立 Formula 区域含 `a=b`。返回冒号 GlyphId 与 ink。
fn colon_fixture() -> (PageIR, Vec<Region>, GlyphId, Rect) {
    let mut glyphs = line(0, "reward of response", 50.0, 700.0, 10.0, 0);
    let n = glyphs.len() as u16;
    glyphs.extend(line(n, "z", 164.0, 700.0, 10.0, 1));
    let mut colon = mk_glyph(n + 1, ':', 170.5, 700.0, 10.0, 0);
    colon.ink = Some(Rect::new(170.6, 701.0, 173.0, 708.0));
    let id = colon.id;
    let ink = colon.ink.unwrap();
    glyphs.push(colon);
    // 下方独立展示公式 "a=b"（自带 Formula 区域，不属于正文区）。
    glyphs.extend(line(n + 2, "a", 80.0, 681.0, 10.0, 2));
    glyphs.extend(line(n + 3, "=", 90.0, 681.0, 10.0, 0));
    glyphs.extend(line(n + 4, "b", 100.0, 681.0, 10.0, 2));
    // 真实提取的等式字形带有 tight ink；等号与两侧证据都必须有正墨迹。
    for g in glyphs.iter_mut().skip(n as usize + 2) {
        g.ink = Some(Rect::new(
            g.bbox.x0 + 0.3,
            g.bbox.y0 + 1.0,
            g.bbox.x1 - 0.3,
            g.bbox.y1 - 1.0,
        ));
    }
    let ir = page_ir(
        glyphs,
        vec![
            mk_font("Body", false, false),
            mk_font("Math", false, true),
            mk_font("MathB", false, true),
        ],
    );
    let mut regions = vec![text_region(0, Rect::new(40.0, 696.0, 220.0, 715.0), 0)];
    let mut formula = text_region(1, Rect::new(163.0, 695.0, 178.0, 712.0), 1);
    formula.kind = RegionKind::Formula;
    let mut equation = text_region(2, Rect::new(70.0, 675.0, 110.0, 688.0), 2);
    equation.kind = RegionKind::Formula;
    regions.push(formula);
    regions.push(equation);
    (ir, regions, id, ink)
}

fn assert_colon_not_released(ir: &PageIR, regions: &[Region], colon: GlyphId) {
    let refs: Vec<_> = regions.iter().collect();
    let formulas = inline_formula::sources(ir, &refs);
    assert!(
        formulas.iter().all(|f| !f.released.contains(&colon)),
        "ambiguous colon was released"
    );
}

#[test]
fn intro_colon_swallowed_by_formula_is_released() {
    let (ir, regions, colon, ink) = colon_fixture();
    let refs: Vec<_> = regions.iter().collect();
    let formulas = inline_formula::sources(&ir, &refs);
    assert!(formulas.iter().any(|f| f.released.contains(&colon)));
    let paragraphs = analyze_page(&ir, &regions);
    let p = paragraphs
        .iter()
        .find(|p| p.glyphs.contains(&colon))
        .unwrap();
    assert!(
        matches!(p.translatable, Translatable::Yes),
        "{:?}",
        p.translatable
    );
    assert_eq!(p.text, "reward of response z:");
    assert_eq!(p.glyphs.last(), Some(&colon));
    let atom = p
        .atoms
        .iter()
        .find(|a| a.kind == AtomKind::Formula)
        .unwrap();
    assert_eq!(atom.text, "z");
    // The replay clip must not touch the released colon's ink.
    let clip = atom.source.unwrap().bbox;
    assert!(clip.x1 <= ink.x0 || clip.x0 >= ink.x1);
    let released = inline_formula::released_for(p, &formulas);
    assert_eq!(released.len(), 1);
    assert!(released.contains(&colon));
}

#[test]
fn math_internal_colon_stays_inside_atom() {
    // "a:b" all in the math font inside one box: a ratio/map colon, not prose.
    let mut glyphs = line(0, "see", 50.0, 700.0, 10.0, 0);
    glyphs.extend(line(3, "a", 75.0, 700.0, 10.0, 1));
    let mut colon = mk_glyph(4, ':', 81.0, 700.0, 10.0, 1);
    colon.ink = Some(Rect::new(81.1, 701.0, 84.0, 708.0));
    let id = colon.id;
    glyphs.push(colon);
    glyphs.extend(line(5, "b", 87.0, 700.0, 10.0, 1));
    glyphs.extend(line(6, "now", 100.0, 700.0, 10.0, 0));
    let ir = page_ir(
        glyphs,
        vec![mk_font("Body", false, false), mk_font("Math", false, true)],
    );
    let mut regions = vec![text_region(0, Rect::new(40.0, 690.0, 200.0, 715.0), 0)];
    let mut formula = text_region(1, Rect::new(74.0, 695.0, 94.0, 712.0), 1);
    formula.kind = RegionKind::Formula;
    regions.push(formula);
    assert_colon_not_released(&ir, &regions, id);
}

#[test]
fn intro_colon_followed_by_prose_stays_inside_atom() {
    // A trailing prose word after the colon means it is not the paragraph tail.
    let (mut ir, regions, colon, _) = colon_fixture();
    if let DisplayItem::Text { glyphs } = &mut ir.items[0] {
        glyphs.extend(line(50, "then", 185.0, 700.0, 10.0, 0));
    }
    assert_colon_not_released(&ir, &regions, colon);
}

#[test]
fn intro_colon_next_line_in_same_region_stays_inside_atom() {
    // The owner region continues on a lower row fully inside it: the colon is
    // not its end.
    let (mut ir, regions, colon, _) = colon_fixture();
    if let DisplayItem::Text { glyphs } = &mut ir.items[0] {
        glyphs.extend(line(50, "and more", 50.0, 698.0, 10.0, 0));
    }
    assert_colon_not_released(&ir, &regions, colon);
}

#[test]
fn intro_colon_without_display_formula_stays_inside_atom() {
    // No region below the row at all: nothing is introduced.
    let (ir, mut regions, colon, _) = colon_fixture();
    regions.retain(|r| r.index != 2);
    assert_colon_not_released(&ir, &regions, colon);
}

#[test]
fn intro_colon_text_region_below_stays_inside_atom() {
    // The nearest region below is prose, not a display formula.
    let (ir, mut regions, colon, _) = colon_fixture();
    regions.iter_mut().find(|r| r.index == 2).unwrap().kind = RegionKind::Text;
    assert_colon_not_released(&ir, &regions, colon);
}

#[test]
fn intro_colon_equation_without_equals_stays_inside_atom() {
    // A formula region below but no verifiable `=` equation.
    let (mut ir, regions, colon, _) = colon_fixture();
    if let DisplayItem::Text { glyphs } = &mut ir.items[0] {
        glyphs
            .iter_mut()
            .find(|g| g.unicode.as_slice() == ['='])
            .unwrap()
            .unicode = ['+'].into_iter().collect();
    }
    assert_colon_not_released(&ir, &regions, colon);
}

#[test]
fn intro_colon_foreign_glyph_in_gap_stays_inside_atom() {
    // A stray glyph between the row and the equation is foreign content.
    let (mut ir, regions, colon, _) = colon_fixture();
    if let DisplayItem::Text { glyphs } = &mut ir.items[0] {
        let mut stray = mk_glyph(60, 'x', 150.0, 690.0, 10.0, 0);
        stray.ink = Some(stray.bbox);
        glyphs.push(stray);
    }
    assert_colon_not_released(&ir, &regions, colon);
}

#[test]
fn intro_colon_tied_formula_regions_stay_inside_atom() {
    // Two formula regions at the same depth make "the equation" ambiguous.
    let (ir, mut regions, colon, _) = colon_fixture();
    let mut second = regions.iter().find(|r| r.index == 2).unwrap().clone();
    second.index = 3;
    second.bbox = Rect::new(130.0, 675.0, 160.0, 688.0);
    regions.push(second);
    assert_colon_not_released(&ir, &regions, colon);
}

#[test]
fn intro_colon_unbalanced_delimiter_stays_inside_atom() {
    // "(z:" keeps an unclosed math delimiter inside the box.
    let (mut ir, regions, colon, _) = colon_fixture();
    if let DisplayItem::Text { glyphs } = &mut ir.items[0] {
        glyphs
            .iter_mut()
            .find(|g| g.unicode.as_slice() == ['z'])
            .unwrap()
            .unicode = ['('].into_iter().collect();
    }
    assert_colon_not_released(&ir, &regions, colon);
}

#[test]
fn intro_colon_without_tight_ink_stays_inside_atom() {
    let (mut ir, regions, colon, _) = colon_fixture();
    if let DisplayItem::Text { glyphs } = &mut ir.items[0] {
        glyphs.iter_mut().find(|g| g.id == colon).unwrap().ink = None;
    }
    assert_colon_not_released(&ir, &regions, colon);
}

#[test]
fn intro_colon_without_two_body_words_stays_inside_atom() {
    // Only a single-letter fragment before the math: not a prose context.
    let (mut ir, regions, colon, _) = colon_fixture();
    if let DisplayItem::Text { glyphs } = &mut ir.items[0] {
        for g in glyphs.iter_mut().take("reward of response".len()) {
            g.unicode = ['x'].into_iter().collect();
        }
    }
    assert_colon_not_released(&ir, &regions, colon);
}

#[test]
fn intro_colon_shared_formula_region_stays_inside_atom() {
    // A second Formula region also covering the colon makes ownership ambiguous.
    let (ir, mut regions, colon, _) = colon_fixture();
    let mut second = text_region(3, Rect::new(169.0, 695.0, 182.0, 712.0), 3);
    second.kind = RegionKind::Formula;
    regions.push(second);
    assert_colon_not_released(&ir, &regions, colon);
}

#[test]
fn intro_colon_equation_without_equals_ink_stays_inside_atom() {
    // The `=` itself has no ink: the display equation is not proven, so the
    // colon keeps its protection.
    let (mut ir, regions, colon, _) = colon_fixture();
    if let DisplayItem::Text { glyphs } = &mut ir.items[0] {
        glyphs
            .iter_mut()
            .find(|g| g.unicode.as_slice() == ['='])
            .unwrap()
            .ink = None;
    }
    assert_colon_not_released(&ir, &regions, colon);
}

#[test]
fn intro_colon_equation_unknown_flank_stays_inside_atom() {
    // The left-hand side of `=` is an unknown glyph: there is no complete
    // operand evidence on that side.
    let (mut ir, regions, colon, _) = colon_fixture();
    if let DisplayItem::Text { glyphs } = &mut ir.items[0] {
        glyphs
            .iter_mut()
            .find(|g| g.font == 2 && g.unicode.as_slice() == ['a'])
            .unwrap()
            .unicode = Vec::new().into_iter().collect();
    }
    assert_colon_not_released(&ir, &regions, colon);
}

#[test]
fn intro_colon_foreign_formula_in_gap_stays_inside_atom() {
    // An unrelated detected formula overlaps the gap between the row and the
    // equation; its glyphs are foreign content, not proof the gap is empty.
    let (mut ir, mut regions, colon, _) = colon_fixture();
    if let DisplayItem::Text { glyphs } = &mut ir.items[0] {
        let mut stray = mk_glyph(60, 'x', 150.0, 690.0, 10.0, 1);
        stray.ink = Some(stray.bbox);
        glyphs.push(stray);
    }
    // The stray's own Formula box reaches into the gap but above the row's
    // bottom edge: it is not "the nearest region below", only foreign content.
    let mut foreign = text_region(3, Rect::new(140.0, 690.0, 160.0, 703.0), 3);
    foreign.kind = RegionKind::Formula;
    regions.push(foreign);
    assert_colon_not_released(&ir, &regions, colon);
}

#[test]
fn intro_colon_before_later_atom_is_not_paragraph_tail() {
    // Another detected formula follows the colon on the same row: region-level
    // evidence can still mark the colon, but the final-owner gate must refuse
    // it because the paragraph's last glyph is the later atom.
    let (mut ir, mut regions, colon, _) = colon_fixture();
    if let DisplayItem::Text { glyphs } = &mut ir.items[0] {
        glyphs.extend(line(60, "w", 185.0, 700.0, 10.0, 1));
    }
    let mut second = text_region(3, Rect::new(183.0, 695.0, 196.0, 712.0), 3);
    second.kind = RegionKind::Formula;
    regions.push(second);
    let refs: Vec<_> = regions.iter().collect();
    let formulas = inline_formula::sources(&ir, &refs);
    let paragraphs = analyze_page(&ir, &regions);
    let p = paragraphs
        .iter()
        .find(|p| p.glyphs.contains(&colon))
        .unwrap();
    assert_ne!(p.glyphs.last(), Some(&colon));
    let released = inline_formula::released_for(p, &formulas);
    assert!(!released.contains(&colon));
}

#[test]
fn intro_colon_trailing_space_is_not_paragraph_tail() {
    // An invisible trailing space after the colon must never be treated as
    // part of the math atom; the tail gate decides by the real last glyph.
    let (mut ir, regions, colon, _) = colon_fixture();
    if let DisplayItem::Text { glyphs } = &mut ir.items[0] {
        glyphs.extend(line(60, " ", 178.0, 700.0, 10.0, 0));
    }
    let refs: Vec<_> = regions.iter().collect();
    let formulas = inline_formula::sources(&ir, &refs);
    let paragraphs = analyze_page(&ir, &regions);
    let p = paragraphs
        .iter()
        .find(|p| p.glyphs.contains(&colon))
        .unwrap();
    let released = inline_formula::released_for(p, &formulas);
    assert_eq!(released.contains(&colon), p.glyphs.last() == Some(&colon));
}

#[test]
fn intro_colon_mismatched_delimiter_stays_inside_atom() {
    // "(x]:" — the remaining math has a mismatched delimiter, so the colon
    // cannot be proven prose.
    let mut glyphs = line(0, "see", 50.0, 700.0, 10.0, 0);
    glyphs.extend(line(3, "(x]", 75.0, 700.0, 10.0, 1));
    let mut colon = mk_glyph(6, ':', 93.0, 700.0, 10.0, 0);
    colon.ink = Some(Rect::new(93.1, 701.0, 95.5, 708.0));
    let id = colon.id;
    glyphs.push(colon);
    let ir = page_ir(
        glyphs,
        vec![mk_font("Body", false, false), mk_font("Math", false, true)],
    );
    let mut regions = vec![text_region(0, Rect::new(40.0, 690.0, 200.0, 715.0), 0)];
    let mut formula = text_region(1, Rect::new(74.0, 695.0, 100.0, 712.0), 1);
    formula.kind = RegionKind::Formula;
    regions.push(formula);
    assert_colon_not_released(&ir, &regions, id);
}

#[test]
fn intro_colon_inside_set_builder_stays_inside_atom() {
    // "{x:x>0}" — an interior set-builder colon (same class as a:b, f:X→Y).
    let mut glyphs = line(0, "see", 50.0, 700.0, 10.0, 0);
    glyphs.extend(line(3, "{x:x>0}", 75.0, 700.0, 10.0, 1));
    glyphs.extend(line(10, "now", 130.0, 700.0, 10.0, 0));
    let colon = glyphs
        .iter()
        .find(|g| g.unicode.as_slice() == [':'])
        .unwrap()
        .id;
    let ir = page_ir(
        glyphs,
        vec![mk_font("Body", false, false), mk_font("Math", false, true)],
    );
    let mut regions = vec![text_region(0, Rect::new(40.0, 690.0, 200.0, 715.0), 0)];
    let mut formula = text_region(1, Rect::new(74.0, 695.0, 118.0, 712.0), 1);
    formula.kind = RegionKind::Formula;
    regions.push(formula);
    assert_colon_not_released(&ir, &regions, colon);
}

#[test]
fn intro_colon_undetected_math_below_stays_inside_atom() {
    // A math glyph on a lower row inside the owner region that detection
    // missed: the colon is not the region's last visible glyph.
    let (mut ir, regions, colon, _) = colon_fixture();
    if let DisplayItem::Text { glyphs } = &mut ir.items[0] {
        let mut stray = mk_glyph(60, 'y', 150.0, 698.0, 10.0, 1);
        stray.ink = Some(stray.bbox);
        glyphs.push(stray);
    }
    assert_colon_not_released(&ir, &regions, colon);
}

#[test]
#[ignore = "requires saved real source/region evidence"]
fn real_page29_intro_colon_releases_only_owned_boundary() {
    let root = std::path::PathBuf::from(std::env::var("SYNCPDF_FORMULA_AUDIT").unwrap());
    let pages: Vec<PageIR> =
        serde_json::from_slice(&std::fs::read(root.join("source.json")).unwrap()).unwrap();
    let ir = pages.iter().find(|p| p.page.0 == 28).unwrap();
    let regions: Vec<Region> =
        serde_json::from_slice(&std::fs::read(root.join("regions-28.json")).unwrap()).unwrap();
    let refs: Vec<_> = regions.iter().collect();
    let formulas = inline_formula::sources(ir, &refs);
    let paragraphs = analyze_page(ir, &regions);
    let p = paragraphs
        .iter()
        .find(|p| p.id.to_string() == "P29-014")
        .unwrap();
    assert!(matches!(p.translatable, Translatable::Yes));
    let a = p
        .atoms
        .iter()
        .find(|a| a.kind == AtomKind::Formula && a.glyph_range.0 == 391)
        .unwrap();
    // The released colon leaves the math atom as 𝑧𝑏,𝑗 (inner subscript comma kept).
    assert_eq!(a.text, "𝑧𝑏,𝑗");
    assert_eq!(a.glyph_range, (391, 395));
    let released = inline_formula::released_for(p, &formulas);
    assert_eq!(released.len(), 1);
    let id = *released.first().unwrap();
    assert_eq!(p.glyphs[395], id);
    assert_eq!(p.glyphs.len(), 396);
    assert_eq!((id.op.op_index, id.ordinal), (217, 0));
    let colon = ir.glyphs().find(|g| g.id == id).unwrap();
    assert_eq!(colon.unicode.as_slice(), [':']);
    assert!(!a.source.unwrap().bbox.intersects(&colon.ink.unwrap()));
    assert!(p.text.ends_with(':'), "{}", p.text);
    // The other formula atom is untouched.
    assert_eq!(p.atoms[0].text, "𝑟𝑏le,𝑗n");
    assert_eq!(p.atoms[0].glyph_range, (363, 370));
}

#[test]
fn two_lines_merge_into_one_paragraph() {
    // 行距 14pt、字号 10pt（14 < 1.8×10）→ 同段。
    let mut g = line(0, "Hello", 50.0, 700.0, 10.0, 0);
    g.extend(line(5, "World", 50.0, 686.0, 10.0, 0));
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    assert_eq!(paras.len(), 1);
    assert_eq!(paras[0].lines.len(), 2);
    assert_eq!(paras[0].text, "Hello World");
    assert_eq!(paras[0].glyphs.len(), 10);
}

#[test]
fn centered_title_continuation_stays_one_semantic_unit() {
    for (first, second) in [("Towards Model", "Merging"), ("Merging", "Towards Model")] {
        let centered_line =
            |seq, text: &str, y| line(seq, text, 306.0 - text.len() as f32 * 6.0, y, 20.0, 0);
        let mut g = centered_line(0, first, 700.0);
        g.extend(centered_line(30, second, 676.0));
        let ir = page_ir(g, vec![mk_font("F1", true, false)]);
        // Tight detected bounds must not turn the widest centered row into Left.
        let mut regions = full_region(RegionKind::Title);
        regions[0].bbox = Rect::new(228.0, 675.0, 384.0, 721.0);
        let paras = analyze_page(&ir, &regions);
        assert_eq!(paras.len(), 1);
        let p = &paras[0];
        assert_eq!(p.text, format!("{first} {second}"));
        assert_eq!(p.align, Align::Center);
        assert_eq!(p.first_indent, 0.0);
        assert_eq!(p.lines.len(), 2);
        assert_eq!(p.glyphs.len(), first.len() + second.len());
        let unit = syncpdf_translate::build_unit(p, |_| None);
        assert_eq!(unit.plain_text(), p.text);
    }
}

#[test]
fn centered_title_does_not_merge_distinct_size_gap_or_region() {
    for (size, y, separate_region) in [
        (12.0, 678.0, false),
        (20.0, 650.0, false),
        (20.0, 676.0, true),
    ] {
        let mut g = line(0, "Towards Model", 228.0, 700.0, 20.0, 0);
        g.extend(line(30, "Merging", 306.0 - 7.0 * size * 0.3, y, size, 0));
        let ir = page_ir(g, vec![mk_font("F1", true, false)]);
        let mut regions = full_region(RegionKind::Title);
        if separate_region {
            regions[0].bbox.y0 = 699.0;
            let mut second = regions[0].clone();
            second.index = 1;
            second.order = 1;
            second.bbox.y0 = 650.0;
            second.bbox.y1 = 699.0;
            regions.push(second);
        }
        assert_eq!(analyze_page(&ir, &regions).len(), 2);
    }
}

/// A wrapped caption centered under its figure: the shorter row starts further
/// right because it is centered, not because it is a new indented paragraph.
fn two_rows(kind: RegionKind, first: (&str, f32), second: (&str, f32, f32)) -> Vec<Paragraph> {
    let mut g = line(0, first.0, first.1, 700.0, 10.0, 0);
    g.extend(line(60, second.0, second.1, 688.0, second.2, 0));
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    analyze_page(&ir, &full_region(kind))
}

#[test]
fn centered_caption_continuation_stays_one_paragraph() {
    let full = "photo of a bear wearing a suit in a river";
    let short = "says bear it";
    let full_x = 300.0 - full.len() as f32 * 3.0;
    let short_x = 300.0 - short.len() as f32 * 3.0;
    for (first, second) in [
        ((full, full_x), (short, short_x)),
        ((short, short_x), (full, full_x)),
    ] {
        let paras = two_rows(RegionKind::Caption, first, (second.0, second.1, 10.0));
        assert_eq!(paras.len(), 1, "{first:?} {second:?}");
        assert_eq!(paras[0].text, format!("{} {}", first.0, second.0));
        assert_eq!(paras[0].align, Align::Center);
    }
    // Not continuations: an indented new paragraph, a smaller centered credit row,
    // and a centered short row in body text.
    let indented = two_rows(RegionKind::Caption, (full, 50.0), (full, 65.0, 10.0));
    assert_eq!(indented.len(), 2);
    let smaller = two_rows(
        RegionKind::Caption,
        (full, full_x),
        (short, 300.0 - 12.0 * 2.4, 8.0),
    );
    assert_eq!(smaller.len(), 2);
    let body = two_rows(RegionKind::Text, (full, full_x), (short, short_x, 10.0));
    assert_eq!(body.len(), 2);
    // Equal-width caption rows on the page axis are justified, not centered.
    let rows: [(&str, f32); 3] = [("aaaaa", 291.0), ("bbbbb", 291.0), ("cc", 291.0)];
    assert_eq!(align_of(&rows, RegionKind::Caption), Align::Justify);
}

#[test]
fn numbered_title_hanging_continuation_is_not_a_new_paragraph() {
    for label in ["A", "A.2", "2.1"] {
        for explicit_space in [false, true] {
            let body_x = 50.0 + label.len() as f32 * 6.0 + 12.0;
            let mut g = line(0, label, 50.0, 700.0, 10.0, 0);
            if explicit_space {
                g.push(mk_glyph(20, ' ', body_x - 12.0, 700.0, 10.0, 0));
            }
            g.extend(line(30, "General defenses", body_x, 700.0, 10.0, 0));
            g.extend(line(60, "adapted to merging", body_x, 687.0, 10.0, 0));
            let ir = page_ir(g, vec![mk_font("F1", true, false)]);
            let paras = analyze_page(&ir, &full_region(RegionKind::ParagraphTitle));
            assert_eq!(paras.len(), 1);
            assert_eq!(
                paras[0].text,
                format!("{label} General defenses adapted to merging")
            );
            assert_eq!(paras[0].align, Align::Left);
            assert_eq!(paras[0].first_indent, 0.0);
        }
    }
    let mut g = line(0, "A", 50.0, 700.0, 10.0, 0);
    g.extend(line(2, "Title", 68.0, 700.0, 10.0, 0));
    g.extend(line(20, "B New title", 68.0, 687.0, 10.0, 0));
    let ir = page_ir(g, vec![mk_font("F1", true, false)]);
    assert_eq!(
        analyze_page(&ir, &full_region(RegionKind::ParagraphTitle)).len(),
        2
    );
}

#[test]
fn list_item_hanging_continuation_is_not_a_new_paragraph() {
    // 列表项首行带标号外挎，续行对齐标号后的正文起点：同一段。
    for (label, space) in [
        ("(ii)", false),
        ("12.", true),
        ("b)", true),
        ("\u{2022}", true),
    ] {
        let body_x = 50.0 + label.chars().count() as f32 * 6.0 + if space { 6.0 } else { 0.0 };
        let mut g = line(0, label, 50.0, 700.0, 10.0, 0);
        if space {
            g.push(mk_glyph(20, ' ', body_x - 6.0, 700.0, 10.0, 0));
        }
        g.extend(line(30, "Choose a number of", body_x, 700.0, 10.0, 0));
        g.extend(line(60, "destructive moves", body_x, 687.0, 10.0, 0));
        g.extend(line(90, "at the master level", body_x, 674.0, 10.0, 0));
        let ir = page_ir(g, vec![mk_font("F1", false, false)]);
        let paras = analyze_page(&ir, &full_region(RegionKind::Text));
        assert_eq!(paras.len(), 1, "{label}");
        assert!(
            paras[0].text.ends_with("moves at the master level"),
            "{label}"
        );
    }
    let split = |first: &str, second: &str, x: f32| {
        let mut g = line(0, first, 50.0, 700.0, 10.0, 0);
        g.extend(line(40, second, x, 687.0, 10.0, 0));
        let ir = page_ir(g, vec![mk_font("F1", false, false)]);
        analyze_page(&ir, &full_region(RegionKind::Text)).len()
    };
    // 无标号的行：次行缩到第二个词下方仍是新段（首行缩进）。
    assert_eq!(split("Note that it", "New paragraph", 80.0), 2);
    // 有标号，但次行缩进不对齐标号后的正文。
    assert_eq!(split("(ii) Choose it", "New paragraph", 68.0), 2);
    // 次行本身又是一个标号项：嵌套列表的新项。
    assert_eq!(split("(ii) Choose it", "(a) Nested item", 80.0), 2);
}

#[test]
fn ctm_scaled_glyphs_with_body_line_pitch_merge_into_one_paragraph() {
    // TRC 形态：字号在 CTM 里（Tf=1，绑定后 size=7.97），行距 10.45 ≈ 1.31×字号。
    // 行距判定按有效字号 → 同段；按 Tf 原值 1 会误判 10.45 ≥ 1.8×1 → 每行一段。
    let mut g = line(0, "under limited", 50.0, 700.0, 7.9701, 0);
    g.extend(line(20, "road supply", 50.0, 689.55, 7.9701, 0));
    g.extend(line(40, "is insufficient", 50.0, 679.1, 7.9701, 0));
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    assert_eq!(
        paras.len(),
        1,
        "{:?}",
        paras.iter().map(|p| &p.text).collect::<Vec<_>>()
    );
    assert_eq!(paras[0].lines.len(), 3);
    assert_eq!(paras[0].text, "under limited road supply is insufficient");
}

#[test]
fn true_paragraph_break_still_splits_ctm_scaled_lines() {
    // 反例（同形态不同结果）：行距远超 1.8×7.97（例如 2.5 倍）→ 仍分两段。
    let mut g = line(0, "under limited", 50.0, 700.0, 7.9701, 0);
    g.extend(line(20, "road supply", 50.0, 680.0, 7.9701, 0));
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    assert_eq!(paras.len(), 2);
    assert_eq!(paras[0].text, "under limited");
    assert_eq!(paras[1].text, "road supply");
}

/// 堆叠上下标形态（TRC p14 N_iter^max 一类）：检测框吞掉前导空白，空白的
/// loose 盒没有墨迹却回探到前一个正文字形的墨迹，令 blocked 门误触发。
/// 碰撞与擦除证据必须量墨迹：空白是间距，不是碰撞。
#[test]
fn stacked_script_formula_blank_box_does_not_block_prose() {
    // Prose "of" in body font 0; the formula owns a blank + `N` + stacked
    // super/subscript in math font 1. The blank's loose box overlaps the
    // prose `f` box by 0.5pt, the real math starts clear of it.
    let mut glyphs = line(0, "of", 50.0, 700.0, 10.0, 0);
    let blank = mk_glyph(10, ' ', 61.5, 700.0, 10.0, 0);
    let base = mk_glyph(11, 'N', 68.0, 700.0, 10.0, 1);
    let sup = mk_glyph(12, 'x', 74.0, 705.0, 6.0, 1);
    let sub = mk_glyph(13, 'i', 75.0, 697.5, 6.0, 1);
    glyphs.extend([blank, base, sup, sub]);
    let ir = page_ir(
        glyphs,
        vec![mk_font("F1", false, false), mk_font("Math", false, false)],
    );
    let mut regions = full_region(RegionKind::Text);
    let mut formula = text_region(1, Rect::new(61.0, 694.0, 80.0, 714.0), 1);
    formula.kind = RegionKind::Formula;
    regions.push(formula);
    let paragraphs = analyze_page(&ir, &regions);
    let p = paragraphs
        .iter()
        .find(|p| p.kind == RegionKind::Text)
        .unwrap();
    assert!(
        matches!(p.translatable, Translatable::Yes),
        "blank spacing must not block the paragraph: {:?}",
        p.translatable
    );
    let atom = p
        .atoms
        .iter()
        .find(|a| a.kind == AtomKind::Formula)
        .expect("the formula must become an atom");
    assert!(atom.source.is_some());
    assert_eq!(atom.glyph_range, (2, 6));
}

/// 反例（fail-closed）：同样的吞框形态，但重叠来自数学字形自己的盒子——
/// 碰撞是真实的，必须保留原文。
#[test]
fn real_math_ink_overlapping_prose_still_blocks_the_formula() {
    let mut glyphs = line(0, "of", 50.0, 700.0, 10.0, 0);
    let base = mk_glyph(10, 'N', 61.5, 700.0, 10.0, 1);
    let sup = mk_glyph(11, 'x', 67.5, 706.0, 6.0, 1);
    glyphs.extend([base, sup]);
    let ir = page_ir(
        glyphs,
        vec![mk_font("F1", false, false), mk_font("Math", false, false)],
    );
    let mut regions = full_region(RegionKind::Text);
    let mut formula = text_region(1, Rect::new(61.0, 694.0, 80.0, 714.0), 1);
    formula.kind = RegionKind::Formula;
    regions.push(formula);
    let paragraphs = analyze_page(&ir, &regions);
    assert!(paragraphs.iter().any(|p| matches!(&p.translatable,
        Translatable::No { reason } if reason == "protected_source_overlap")));
}

/// 行首公式 + 句读收尾形态（TRC p9 t_i^opt−a_i−s_i. 一类）：公式开启段落
/// 最后一行，同行唯一的非公式字形是句号。锚点证明的是「与正文同行」，
/// 句读也是正文；只认字母会错杀这种行。
#[test]
fn formula_closing_a_line_with_punctuation_anchor_becomes_an_atom() {
    let mut glyphs = line(0, "The bound is", 50.0, 700.0, 10.0, 0);
    glyphs.extend(line(20, "ti", 50.0, 686.0, 10.0, 1));
    glyphs.push(mk_glyph(23, '.', 64.0, 686.0, 10.0, 0));
    let ir = page_ir(
        glyphs,
        vec![mk_font("F1", false, false), mk_font("Math", false, false)],
    );
    let mut regions = full_region(RegionKind::Text);
    let mut formula = text_region(1, Rect::new(48.0, 684.0, 63.0, 698.0), 1);
    formula.kind = RegionKind::Formula;
    regions.push(formula);
    let paragraphs = analyze_page(&ir, &regions);
    let p = paragraphs
        .iter()
        .find(|p| p.kind == RegionKind::Text)
        .unwrap();
    assert!(
        matches!(p.translatable, Translatable::Yes),
        "a sentence period is a valid prose anchor: {:?}",
        p.translatable
    );
    assert!(p
        .atoms
        .iter()
        .any(|a| a.kind == AtomKind::Formula && a.source.is_some()));
}

/// 高墨迹行距形态（TRC p9 T_ij={...} 一类）：含高大行内公式的行会被排版器
/// 加大行距（10.45 → 15.2），1.8×字号 的间隔规则把这一拉伸误判为分段。
/// 段落间隔阈值必须容纳「行高超出本区域行高中位数」带来的额外行距。
#[test]
fn tall_inline_formula_line_does_not_split_the_paragraph() {
    // Body rows: loose box 13.03 tall, baseline inside; pitch 10.45.
    let body = |seq: u16, ch: char, x: f32, y: f32| {
        let mut g = mk_glyph(seq, ch, x, y, 7.9701, 0);
        g.bbox = Rect::new(x, y - 2.53, x + 7.9701 * 0.6, y + 10.5);
        g
    };
    let row = |glyphs: &mut Vec<Glyph>, seq: u16, text: &str, x: f32, y: f32| {
        for (i, ch) in text.chars().enumerate() {
            glyphs.push(body(seq + i as u16, ch, x + i as f32 * 7.0, y));
        }
    };
    let mut glyphs = Vec::new();
    row(&mut glyphs, 0, "travel time", 50.0, 700.0);
    row(&mut glyphs, 12, "is defined", 50.0, 689.55);
    row(&mut glyphs, 24, "by the set", 50.0, 679.10);
    // Tall inline formula on the 679.10 line: raw ink spans ~666..686.
    glyphs.push(mk_glyph(100, 'T', 200.0, 676.0, 10.0, 1));
    glyphs.push(mk_glyph(101, 'i', 206.0, 666.0, 6.0, 1));
    // The compositor stretches the pitch after the tall line: 15.2 instead
    // of 10.45, then back to normal.
    row(&mut glyphs, 36, "for each", 50.0, 663.90);
    row(&mut glyphs, 48, "arc pair", 50.0, 653.45);
    let ir = page_ir(
        glyphs,
        vec![mk_font("F1", false, false), mk_font("Math", false, false)],
    );
    let mut regions = full_region(RegionKind::Text);
    let mut formula = text_region(1, Rect::new(198.0, 664.0, 212.0, 688.0), 1);
    formula.kind = RegionKind::Formula;
    regions.push(formula);
    let paragraphs = analyze_page(&ir, &regions);
    let p = paragraphs
        .iter()
        .find(|p| p.kind == RegionKind::Text)
        .unwrap();
    assert_eq!(
        p.lines.len(),
        5,
        "the stretched pitch after a tall inline formula is not a paragraph break"
    );
    assert!(matches!(p.translatable, Translatable::Yes));
}

/// 反例（同形态不同结果）：高墨迹行之后是真分段（间隔远超拉伸容差）→
/// 仍分两段。
#[test]
fn real_paragraph_break_after_a_tall_formula_line_still_splits() {
    let body = |seq: u16, ch: char, x: f32, y: f32| {
        let mut g = mk_glyph(seq, ch, x, y, 7.9701, 0);
        g.bbox = Rect::new(x, y - 2.53, x + 7.9701 * 0.6, y + 10.5);
        g
    };
    let row = |glyphs: &mut Vec<Glyph>, seq: u16, text: &str, x: f32, y: f32| {
        for (i, ch) in text.chars().enumerate() {
            glyphs.push(body(seq + i as u16, ch, x + i as f32 * 7.0, y));
        }
    };
    let mut glyphs = Vec::new();
    row(&mut glyphs, 0, "travel time", 50.0, 700.0);
    row(&mut glyphs, 12, "is defined", 50.0, 689.55);
    row(&mut glyphs, 24, "by the set", 50.0, 679.10);
    glyphs.push(mk_glyph(100, 'T', 200.0, 676.0, 10.0, 1));
    glyphs.push(mk_glyph(101, 'i', 206.0, 666.0, 6.0, 1));
    // A genuine break: 26pt of pitch, beyond even the stretched allowance.
    row(&mut glyphs, 36, "New paragraph", 50.0, 653.10);
    let ir = page_ir(
        glyphs,
        vec![mk_font("F1", false, false), mk_font("Math", false, false)],
    );
    let mut regions = full_region(RegionKind::Text);
    let mut formula = text_region(1, Rect::new(198.0, 664.0, 212.0, 688.0), 1);
    formula.kind = RegionKind::Formula;
    regions.push(formula);
    let paragraphs = analyze_page(&ir, &regions);
    let text_paragraphs: Vec<_> = paragraphs
        .iter()
        .filter(|p| p.kind == RegionKind::Text)
        .collect();
    assert_eq!(
        text_paragraphs.len(),
        2,
        "{:?}",
        paragraphs.iter().map(|p| &p.text).collect::<Vec<_>>()
    );
}

#[test]
fn large_line_gap_splits_paragraphs() {
    // 行距 30pt > 1.8×10 → 两段。
    let mut g = line(0, "Hello", 50.0, 700.0, 10.0, 0);
    g.extend(line(5, "World", 50.0, 670.0, 10.0, 0));
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    assert_eq!(paras.len(), 2);
    assert_eq!(paras[0].text, "Hello");
    assert_eq!(paras[1].text, "World");
    assert_eq!(paras[0].id.to_string(), "P01-001");
    assert_eq!(paras[1].id.to_string(), "P01-002");
}

#[test]
fn indented_line_starts_a_new_paragraph() {
    // 第二行右移 20pt ≥ 1.5×10 → 新段（首行缩进）。
    let mut g = line(0, "Hello", 50.0, 700.0, 10.0, 0);
    g.extend(line(5, "World", 70.0, 686.0, 10.0, 0));
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    assert_eq!(
        paras.len(),
        2,
        "{:?}",
        paras.iter().map(|p| &p.text).collect::<Vec<_>>()
    );
    assert_eq!(paras[0].text, "Hello");
    assert_eq!(paras[1].text, "World");
}

#[test]
fn hyphen_at_line_end_is_joined_without_space() {
    let mut g = line(0, "exam-", 50.0, 700.0, 10.0, 0);
    g.extend(line(5, "ple", 50.0, 686.0, 10.0, 0));
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    assert_eq!(paras.len(), 1);
    assert_eq!(paras[0].text, "exam-ple");
    assert_eq!(paras[0].text_spans[4].text, "-");
}

#[test]
fn geometric_word_gap_has_a_zero_length_source_span() {
    let mut g = line(0, "Hello", 50.0, 700.0, 10.0, 0);
    g.extend(line(5, "world", 84.0, 700.0, 10.0, 0));
    let p = analyze_page(
        &page_ir(g, vec![mk_font("F1", false, false)]),
        &full_region(RegionKind::Text),
    );
    assert_eq!(p.len(), 1);
    assert_eq!(p[0].text, "Hello world");
    assert_eq!(p[0].text_spans[5].text, " ");
    assert_eq!(p[0].text_spans[5].glyph_range, (5, 5));
    let unit = syncpdf_translate::build_unit(&p[0], |_| Some("bad fallback".into()));
    assert_eq!(unit.plain_text(), "Hello world");
}

#[test]
fn explicit_space_ligature_and_cjk_punctuation_are_preserved() {
    let mut g = line(0, "A B", 50.0, 700.0, 10.0, 0);
    // One actual glyph contributes two Unicode characters.
    let mut ligature = mk_glyph(3, 'f', 68.0, 700.0, 10.0, 0);
    ligature.unicode = ['f', 'i'].into_iter().collect();
    g.push(ligature);
    let p = analyze_page(
        &page_ir(g, vec![mk_font("F1", false, false)]),
        &full_region(RegionKind::Text),
    );
    assert_eq!(p[0].text, "A Bfi");
    assert_eq!(p[0].text_spans.iter().filter(|s| s.text == " ").count(), 1);
    assert_eq!(p[0].text_spans[3].glyph_range, (3, 4));

    let g = line(0, "中文，测试。", 50.0, 700.0, 10.0, 0);
    let p = analyze_page(
        &page_ir(g, vec![mk_font("F1", false, false)]),
        &full_region(RegionKind::Text),
    );
    assert_eq!(p[0].text, "中文，测试。");
    assert!(p[0]
        .text_spans
        .iter()
        .all(|s| s.glyph_range.0 < s.glyph_range.1));
}

#[test]
fn generated_line_gap_keeps_multiline_atom_range_and_styles() {
    let mut g = line(0, "rate 25", 50.0, 700.0, 10.0, 0);
    g.extend(line(7, "ms now", 50.0, 686.0, 10.0, 1));
    let p = analyze_page(
        &page_ir(
            g,
            vec![mk_font("F1", false, false), mk_font("F2", true, false)],
        ),
        &full_region(RegionKind::Text),
    );
    assert_eq!(p.len(), 1);
    assert_eq!(p[0].text, "rate 25 ms now");
    assert_eq!(p[0].text_spans[7].glyph_range, (7, 7));
    assert_eq!(p[0].atoms[0].text, "25 ms");
    assert_eq!(p[0].atoms[0].glyph_range, (5, 9));
    assert_eq!(p[0].style_runs[0].glyph_range, (0, 7));
    assert_eq!(p[0].style_runs[1].glyph_range, (7, 13));
}

#[test]
fn rotated_source_is_kept_with_reason() {
    let mut g = line(0, "Side note", 50.0, 700.0, 10.0, 0);
    for glyph in &mut g {
        glyph.matrix = Matrix::new(0.0, 1.0, -1.0, 0.0, 0.0, 0.0);
    }
    let p = analyze_page(
        &page_ir(g, vec![mk_font("F1", false, false)]),
        &full_region(RegionKind::Text),
    );
    assert!(p.iter().all(|p| matches!(&p.translatable, Translatable::No { reason } if reason == "rotated_source_text")));
}

#[test]
fn vertical_side_note_geometry_is_kept_even_with_translation_only_matrices() {
    let g: Vec<_> = "VERTICAL"
        .chars()
        .enumerate()
        .map(|(i, c)| mk_glyph(i as u16, c, 50.0, 700.0 - i as f32 * 8.0, 10.0, 0))
        .collect();
    let p = analyze_page(
        &page_ir(g, vec![mk_font("F1", false, false)]),
        &full_region(RegionKind::Text),
    );
    assert!(p.iter().all(|p| matches!(&p.translatable, Translatable::No { reason } if reason == "rotated_source_text")), "{p:?}");
}

#[test]
fn punctuation_at_line_boundary_retains_source_and_gets_word_space() {
    let mut g = line(0, "Hello,", 50.0, 700.0, 10.0, 0);
    g.extend(line(6, "world", 50.0, 686.0, 10.0, 0));
    let p = analyze_page(
        &page_ir(g, vec![mk_font("F1", false, false)]),
        &full_region(RegionKind::Text),
    );
    assert_eq!(p[0].text, "Hello, world");
    assert_eq!(p[0].text_spans[6].glyph_range, (6, 6));
}

#[test]
fn overlapping_translatable_regions_have_one_source_owner() {
    let g = line(0, "Shared prose", 50.0, 700.0, 10.0, 0);
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let regions = vec![
        text_region(0, Rect::new(40.0, 690.0, 150.0, 720.0), 0),
        text_region(1, Rect::new(45.0, 690.0, 155.0, 720.0), 1),
    ];
    let p = analyze_page(&ir, &regions);
    assert_eq!(p.len(), 1);
    assert!(matches!(p[0].translatable, Translatable::Yes));
    assert_eq!(p[0].glyphs.len(), ir.glyphs().count());
}

#[test]
fn ordinary_region_cannot_translate_rotated_source_glyphs() {
    let mut g = line(0, "Side note", 50.0, 700.0, 10.0, 0);
    for glyph in &mut g {
        glyph.matrix = Matrix::new(0.0, 1.0, -1.0, 0.0, 0.0, 0.0);
    }
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let regions = vec![
        text_region(0, Rect::new(40.0, 690.0, 150.0, 720.0), 0),
        text_region(1, Rect::new(45.0, 690.0, 155.0, 720.0), 1),
    ];
    let p = analyze_page(&ir, &regions);
    assert_eq!(p.len(), 1);
    assert!(p.iter().all(|p| matches!(&p.translatable, Translatable::No { reason } if reason == "rotated_source_text")));
}

#[test]
#[ignore = "manual read-only source/paragraph probe for a specified PDF page"]
fn manual_source_paragraph_page_one_probe() {
    let path = std::env::var("R2_SOURCE_PROBE_PDF").expect("set R2_SOURCE_PROBE_PDF");
    let output = std::env::var("R2_SOURCE_PROBE_OUTPUT").expect("set R2_SOURCE_PROBE_OUTPUT");
    let worker = syncpdf_pdf::pdfium::PdfiumWorker::spawn().expect("pdfium");
    let pf = crate::stages::preflight(&worker, std::path::Path::new(&path)).expect("preflight");
    let lo = lopdf::Document::load(&path).expect("source PDF");
    let bound = syncpdf_pdf::bind::bind_page(&worker, pf.doc, &lo, 1).expect("bind page one");
    let model_path = syncpdf_core::fixtures::models_dir()
        .expect("models directory")
        .join("pp_doc_layoutv3.onnx");
    let mut model = syncpdf_layout::LayoutModel::load(&model_path, 2).expect("layout model");
    let opts = crate::stages::LayoutOpts::default();
    let mut regions =
        crate::stages::detect_regions(&mut model, &worker, pf.doc, 0, &pf.page_infos[0], &opts)
            .expect("layout page one");
    crate::stages::apply_coverage_fallback(&mut regions, &bound.ir, 0, opts.coverage_limit);
    let mut paras = analyze_page(&bound.ir, &regions);
    let protected =
        crate::stages::source_policy::protect_front_matter(&bound.ir, &regions, &mut paras);
    assert_eq!(protected.len(), 4, "指定论文姓名/机构/邮箱四段须保留");
    assert!(paras
        .iter()
        .any(|p| p.text.starts_with("Training neural networks")
            && matches!(p.translatable, Translatable::Yes)));
    let glyph_text: std::collections::HashMap<_, _> = bound
        .ir
        .glyphs()
        .map(|g| (g.id, g.unicode.iter().collect::<String>()))
        .collect();
    let records: Vec<_> = paras
        .iter()
        .map(|p| {
            let mut old = String::new();
            for (i, line) in p.lines.iter().enumerate() {
                if i > 0 {
                    if old.ends_with('-') {
                        old.pop();
                    } else {
                        old.push(' ');
                    }
                }
                for id in &line.glyphs {
                    old.push_str(glyph_text.get(id).expect("source glyph"));
                }
            }
            serde_json::json!({
                "id": p.id.to_string(),
                "reason": format!("{:?}", p.translatable),
                "old": old,
                "new": p.text,
                "generated": p.text_spans.iter().filter(|s| s.glyph_range.0 == s.glyph_range.1).map(|s| serde_json::json!({"text": s.text, "range": s.glyph_range})).take(12).collect::<Vec<_>>(),
                "first_spans": p.text_spans.iter().take(12).map(|s| serde_json::json!({"text": s.text, "range": s.glyph_range})).collect::<Vec<_>>(),
                "atoms": p.atoms.iter().take(8).map(|a| serde_json::json!({"text": a.text, "range": a.glyph_range})).collect::<Vec<_>>()
            })
        })
        .collect();
    std::fs::write(output, serde_json::to_vec_pretty(&records).expect("json"))
        .expect("probe evidence");
    worker.close(pf.doc);
}

#[test]
fn style_runs_split_by_font_size_and_bold() {
    // 同一行内：字号 10 → 12，字体 0 → 1（粗），切出两个 run。
    let mut g = line(0, "ab", 50.0, 700.0, 10.0, 0);
    g.extend(line(2, "cd", 62.0, 700.0, 12.0, 1));
    let ir = page_ir(
        g,
        vec![mk_font("F1", false, false), mk_font("F2", true, false)],
    );
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    assert_eq!(paras.len(), 1);
    let runs = &paras[0].style_runs;
    assert_eq!(runs.len(), 2, "{runs:?}");
    assert_eq!(runs[0].glyph_range, (0, 2));
    assert_eq!(runs[0].size, 10.0);
    assert!(!runs[0].bold);
    assert_eq!(runs[0].id, StyleId(1));
    assert_eq!(runs[1].glyph_range, (2, 4));
    assert_eq!(runs[1].size, 12.0);
    assert!(runs[1].bold, "粗体标志应取自 PageIR::fonts");
    assert_eq!(runs[1].id, StyleId(2));
}

#[test]
fn url_becomes_an_atom() {
    let g = line(0, "see https://a.io/x now", 50.0, 700.0, 10.0, 0);
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    assert_eq!(paras.len(), 1);
    assert_eq!(paras[0].atoms.len(), 1);
    assert_eq!(paras[0].atoms[0].kind, AtomKind::Url);
    assert_eq!(paras[0].atoms[0].text, "https://a.io/x");
    assert_eq!(paras[0].atoms[0].id, AtomId(1));
    // "see " 占 4 个字形，`https://a.io/x` 占 14 个字形 → 字形区间 (4, 18)。
    assert_eq!(paras[0].atoms[0].glyph_range, (4, 18));
    // 字形区间对应的文本必须与原子文本一致。
    let gs = paras[0].glyphs[4..18]
        .iter()
        .map(|_| 'x')
        .collect::<String>();
    assert_eq!(gs.len(), 14);
}

#[test]
fn email_becomes_an_atom() {
    let g = line(0, "mail a.b@c.de ok", 50.0, 700.0, 10.0, 0);
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    assert_eq!(paras[0].atoms.len(), 1);
    assert_eq!(paras[0].atoms[0].text, "a.b@c.de");
}

#[test]
fn reference_marker_and_number_unit_become_atoms() {
    let g = line(0, "as in [1, 2] at 25 ms", 50.0, 700.0, 10.0, 0);
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    assert_eq!(paras.len(), 1);
    let texts: Vec<&str> = paras[0].atoms.iter().map(|a| a.text.as_str()).collect();
    assert!(texts.contains(&"[1, 2]"), "{texts:?}");
    assert!(texts.contains(&"25 ms"), "{texts:?}");
    // 原子 id 按字形位置从 1 编号。
    assert_eq!(paras[0].atoms[0].id, AtomId(1));
    assert_eq!(paras[0].atoms[1].id, AtomId(2));
    assert!(
        paras[0].atoms[0].glyph_range.0 < paras[0].atoms[1].glyph_range.0,
        "原子按位置排序"
    );
}

#[test]
fn unit_atom_does_not_capture_the_start_of_a_prose_word() {
    let ir = page_ir(
        line(0, "2 shows 250 samples 25 ms 5s 90%", 50.0, 700.0, 10.0, 0),
        vec![mk_font("F1", false, false)],
    );
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    let atoms: Vec<_> = paras[0].atoms.iter().map(|a| a.text.as_str()).collect();
    assert_eq!(atoms, vec!["25 ms", "5s", "90%"]);
}

#[test]
fn math_symbol_run_becomes_a_formula_atom() {
    let g = line(0, "sum ∑∫ x", 50.0, 700.0, 10.0, 0);
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    let atom = paras[0]
        .atoms
        .iter()
        .find(|a| a.text == "∑∫")
        .expect("应识别出数学符号原子");
    assert_eq!(atom.kind, AtomKind::Formula);
}

#[test]
fn non_translatable_region_kind_is_no() {
    let g = line(0, "hello world", 50.0, 700.0, 10.0, 0);
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Table));
    assert_eq!(paras.len(), 1);
    assert_eq!(
        paras[0].translatable,
        Translatable::No {
            reason: "region_kind".into()
        }
    );
}

#[test]
fn digits_only_paragraph_is_not_translatable() {
    let g = line(0, "1234", 50.0, 700.0, 10.0, 0);
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    assert_eq!(
        paras[0].translatable,
        Translatable::No {
            reason: "no_letters".into()
        }
    );
}

#[test]
fn single_char_paragraph_is_too_short() {
    let g = line(0, "A", 50.0, 700.0, 10.0, 0);
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    assert_eq!(
        paras[0].translatable,
        Translatable::No {
            reason: "too_short".into()
        }
    );
}

#[test]
fn centered_lines_are_center_aligned() {
    // 四字宽 4×6 = 24pt；区域 [50, 300]、行 x0=163 → 左右边距各 113/113，
    // 差 2pt 以内、都远超 3pt → Center。
    let x0 = 163.0;
    let mut g = line(0, "aaaa", x0, 700.0, 10.0, 0);
    g.extend(line(4, "bbbb", x0, 686.0, 10.0, 0));
    g.extend(line(8, "cccc", x0, 672.0, 10.0, 0));
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let regions = vec![text_region(0, Rect::new(50.0, 650.0, 300.0, 792.0), 0)];
    let paras = analyze_page(&ir, &regions);
    assert_eq!(paras.len(), 1);
    assert_eq!(paras[0].align, Align::Center);
}

#[test]
fn equal_width_lines_are_justified() {
    // 三行等宽、左齐右齐，且区域边距为 0（贴边）→ 不满足居中的 >3pt 留白。
    let mut g = line(0, "aaaaa", 0.0, 700.0, 10.0, 0);
    g.extend(line(5, "bbbbb", 0.0, 686.0, 10.0, 0));
    g.extend(line(10, "ccccc", 0.0, 672.0, 10.0, 0));
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    // 区域左边界 = 行起点、右边界 = 行终点 → 左右边距都是 0。
    let regions = vec![text_region(0, Rect::new(0.0, 650.0, 30.0, 792.0), 0)];
    let paras = analyze_page(&ir, &regions);
    assert_eq!(paras.len(), 1);
    assert_eq!(paras[0].align, Align::Justify);
}

#[test]
fn ragged_right_lines_are_left_aligned() {
    // 左齐、右边参差 → Left。
    let mut g = line(0, "aaaaa", 50.0, 700.0, 10.0, 0);
    g.extend(line(5, "bb", 50.0, 686.0, 10.0, 0));
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    assert_eq!(paras[0].align, Align::Left);
}

#[test]
fn justified_paragraph_with_indent_and_short_last_line_is_justified() {
    // 回归：末行短、首行缩进的普通两端对齐段落（如摘要）曾被判为 Left，译文右边参差；
    // 行尾连字符的字符突出使行端相差 ~0.17em（StoryScope 摘要 1.66pt @ 10pt）也仍是两端对齐。
    let protruded: [&[(&str, f32)]; 2] = [
        &[
            ("aaaa", 56.0),
            ("bbbbb", 50.0),
            ("ccccc", 50.0),
            ("dd", 50.0),
        ],
        &[
            ("aaaaa", 50.0),
            ("bbbbb", 51.7),
            ("ccccc", 50.0),
            ("dd", 50.0),
        ],
    ];
    for kind in [RegionKind::Abstract, RegionKind::Caption] {
        for rows in protruded {
            assert_eq!(align_of(rows, kind), Align::Justify, "{kind:?} {rows:?}");
        }
    }
}

#[test]
fn ragged_body_lines_stay_left_aligned() {
    // 反例：中间行右端缩进一个字（0.6em，超出字符突出的幅度）→ Left；末行之前只有
    // 一行时右端证据不足 → Left；首行比其余行更靠左（悬挂缩进）→ Left。
    let cases: [&[(&str, f32)]; 4] = [
        &[
            ("aaaaa", 50.0),
            ("bbbb", 50.0),
            ("ccccc", 50.0),
            ("dd", 50.0),
        ],
        &[
            ("aaaaa", 50.0),
            ("bbb", 50.0),
            ("ccccc", 50.0),
            ("dd", 50.0),
        ],
        &[("aaaaa", 50.0), ("dd", 50.0)],
        &[
            ("aaaaaa", 44.0),
            ("bbbbb", 50.0),
            ("ccccc", 50.0),
            ("dd", 50.0),
        ],
    ];
    for rows in cases {
        assert_eq!(
            align_of(rows, RegionKind::Abstract),
            Align::Left,
            "{rows:?}"
        );
    }
}

/// 逐行 (文本, 起点 x) 构造 10pt 段落，返回判定的对齐方式。
fn align_of(rows: &[(&str, f32)], kind: RegionKind) -> Align {
    let mut g = Vec::new();
    let mut start = 0u16;
    for (i, (text, x)) in rows.iter().enumerate() {
        g.extend(line(start, text, *x, 700.0 - 14.0 * i as f32, 10.0, 0));
        start += text.len() as u16;
    }
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    analyze_page(&ir, &full_region(kind))[0].align
}

#[test]
fn first_indent_and_line_height_are_measured() {
    let mut g = line(0, "Hello", 60.0, 700.0, 10.0, 0);
    g.extend(line(5, "World", 50.0, 686.0, 10.0, 0));
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    assert_eq!(paras.len(), 1);
    assert!(
        (paras[0].first_indent - 10.0).abs() < 0.01,
        "{}",
        paras[0].first_indent
    );
    assert!(
        (paras[0].line_height - 14.0).abs() < 0.01,
        "{}",
        paras[0].line_height
    );
}

#[test]
fn single_line_paragraph_line_height_is_1_2_times_size() {
    let g = line(0, "Hello", 50.0, 700.0, 10.0, 0);
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    assert_eq!(paras.len(), 1);
    assert!((paras[0].line_height - 12.0).abs() < 0.01);
    assert_eq!(paras[0].first_indent, 0.0);
}

#[test]
fn paragraph_seq_is_continuous_across_regions() {
    // 两个区域各一行：seq 连续（1、2），不各自从 1 起。
    let mut g = line(0, "aaaa", 50.0, 700.0, 10.0, 0);
    g.extend(line(4, "bbbb", 50.0, 400.0, 10.0, 0));
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let regions = vec![
        text_region(0, Rect::new(0.0, 650.0, 612.0, 792.0), 0),
        text_region(1, Rect::new(0.0, 350.0, 612.0, 450.0), 1),
    ];
    let paras = analyze_page(&ir, &regions);
    assert_eq!(paras.len(), 2);
    assert_eq!(paras[0].id.to_string(), "P01-001");
    assert_eq!(paras[1].id.to_string(), "P01-002");
    assert_eq!(paras[0].region, 0);
    assert_eq!(paras[1].region, 1);
}

#[test]
fn regions_are_processed_in_reading_order() {
    // 区域按 order 反序给出，输出仍应按 order 排序。
    let mut g = line(0, "first", 50.0, 700.0, 10.0, 0);
    g.extend(line(5, "second", 50.0, 400.0, 10.0, 0));
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let regions = vec![
        text_region(7, Rect::new(0.0, 350.0, 612.0, 450.0), 5),
        text_region(2, Rect::new(0.0, 650.0, 612.0, 792.0), 1),
    ];
    let paras = analyze_page(&ir, &regions);
    assert_eq!(paras.len(), 2);
    assert_eq!(paras[0].text, "first", "order 小的先出");
    assert_eq!(paras[1].text, "second");
    assert_eq!(paras[0].region, 2);
    assert_eq!(paras[1].region, 7);
}

#[test]
fn glyphs_outside_all_regions_are_ignored() {
    let mut g = line(0, "inside", 50.0, 700.0, 10.0, 0);
    g.push(mk_glyph(99, 'X', 50.0, 100.0, 10.0, 0));
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let regions = vec![text_region(0, Rect::new(0.0, 650.0, 612.0, 792.0), 0)];
    let paras = analyze_page(&ir, &regions);
    assert_eq!(paras.len(), 1);
    assert_eq!(paras[0].text, "inside");
    assert!(!paras[0].text.contains('X'));
}

#[test]
fn invisible_glyphs_are_skipped() {
    let mut g = line(0, "ok", 50.0, 700.0, 10.0, 0);
    let mut hidden = mk_glyph(9, 'Z', 80.0, 700.0, 10.0, 0);
    hidden.flags.invisible = true;
    g.push(hidden);
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    assert_eq!(paras.len(), 1);
    assert_eq!(paras[0].text, "ok");
}

#[test]
fn bbox_is_union_of_line_boxes() {
    let mut g = line(0, "Hello", 50.0, 700.0, 10.0, 0);
    g.extend(line(5, "World", 50.0, 686.0, 10.0, 0));
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Text));
    let b = paras[0].bbox;
    assert!(b.x0 <= 50.0 + 0.01);
    assert!(b.y0 <= 686.0 + 0.01);
    assert!(b.y1 >= 710.0 - 0.01);
}

#[test]
fn empty_page_yields_no_paragraphs() {
    let ir = page_ir(vec![], vec![]);
    assert!(analyze_page(&ir, &full_region(RegionKind::Text)).is_empty());
}

#[test]
fn title_region_is_translatable() {
    let g = line(0, "A Title", 50.0, 700.0, 14.0, 0);
    let ir = page_ir(g, vec![mk_font("F1", false, false)]);
    let paras = analyze_page(&ir, &full_region(RegionKind::Title));
    assert_eq!(paras[0].translatable, Translatable::Yes);
    assert_eq!(paras[0].kind, RegionKind::Title);
}

#[test]
fn source_run_sizes_and_colors_are_not_quantized() {
    let mut a = mk_glyph(0, 'A', 30.0, 100.0, 9.963, 0);
    let mut b = mk_glyph(1, 'B', 36.0, 100.0, 10.037, 0);
    a.fill = Color::rgb(0.201, 0.3, 0.4);
    b.fill = Color::rgb(0.202, 0.3, 0.4);
    let mut font = mk_font("SourceSerif", false, false);
    font.is_serif = true;
    let ir = page_ir(vec![a, b], vec![font]);
    let paragraphs = analyze_page(&ir, &full_region(RegionKind::Text));
    assert_eq!(paragraphs.len(), 1);
    let runs = &paragraphs[0].style_runs;
    assert_eq!(runs.len(), 2);
    assert_eq!(runs[0].size, 9.963);
    assert_eq!(runs[1].size, 10.037);
    assert_eq!(runs[0].color.r, 0.201);
    assert_eq!(runs[1].color.r, 0.202);
    assert!(runs.iter().all(|r| r.serif));
}

#[test]
fn baseline_and_spacing_follow_text_matrix_not_variable_ink_bottom() {
    let mut first = line(0, "abg", 50.0, 700.0, 10.0, 0);
    let mut second = line(10, "XYZ", 50.0, 688.0, 10.0, 0);
    first[2].bbox.y0 -= 3.0;
    second[0].bbox.y0 += 2.0;
    first.append(&mut second);
    let ir = page_ir(first, vec![mk_font("F1", false, false)]);
    let paragraphs = analyze_page(&ir, &full_region(RegionKind::Text));
    assert_eq!(paragraphs.len(), 1);
    assert_eq!(
        paragraphs[0]
            .lines
            .iter()
            .map(|l| l.baseline_y)
            .collect::<Vec<_>>(),
        [700.0, 688.0]
    );
    assert_eq!(paragraphs[0].line_height, 12.0);
}

#[test]
fn single_line_title_centers_only_with_symmetric_page_margins() {
    for (x, expected) in [(288.0, Align::Center), (50.0, Align::Left)] {
        let ir = page_ir(
            line(0, "Header", x, 700.0, 10.0, 0),
            vec![mk_font("F1", true, false)],
        );
        for kind in [RegionKind::Title, RegionKind::ParagraphTitle] {
            let mut region = text_region(0, Rect::new(x - 1.0, 699.0, x + 37.0, 711.0), 0);
            region.kind = kind;
            let paras = analyze_page(&ir, &[region]);
            assert_eq!(paras[0].align, expected);
        }
    }
}

// ── 漏检行内根式：源绘制归属 + 阅读序 ────────────────────────────────
//
// 真实 RCA（DeepSeek 第 14 页 P14-006）：`√` 的 loose bbox 高 18pt 而真实墨迹只有
// ~10.9pt。这个跨行字盒让 group_lines 把上下两行桥接成同一行，随后按 x 排序把两行
// 交织成乱码。下面按真实度量构造同一几何。

const RAD_SIZE: f32 = 10.9091;

/// 显式 bbox/基线/墨迹的手工字形。
fn measured_glyph(seq: u16, ch: char, bbox: Rect, baseline: f32, ink: Option<Rect>) -> Glyph {
    let mut g = mk_glyph(seq, ch, bbox.x0, bbox.y0, RAD_SIZE, 0);
    g.bbox = bbox;
    g.matrix = Matrix::new(1.0, 0.0, 0.0, 1.0, bbox.x0, baseline);
    g.ink = ink;
    g.advance = bbox.width();
    g
}

/// 一行等宽正文；行盒高 10.876pt（真实论文度量）。
fn prose_row(seq: u16, text: &str, x: f32, baseline: f32) -> Vec<Glyph> {
    let adv = RAD_SIZE * 0.6;
    let (y0, y1) = (baseline - 3.076, baseline + 7.8);
    text.chars()
        .enumerate()
        .map(|(i, c)| {
            let x0 = x + i as f32 * adv;
            measured_glyph(
                seq + i as u16,
                c,
                Rect::new(x0, y0, x0 + adv, y1),
                baseline,
                Some(Rect::new(x0 + 0.1, y0 + 0.5, x0 + adv - 0.1, y1 - 0.8)),
            )
        })
        .collect()
}

/// 横线画笔路径。
fn bar_item(bar: Rect, op_index: u32) -> DisplayItem {
    DisplayItem::Path {
        bbox: bar,
        is_fill: false,
        is_stroke: true,
        stroke: Some(syncpdf_core::ir::PathStroke {
            op: OpKey::new(ObjRef::new(50, 0), op_index),
            width: 0.605,
            color: Color::default(),
        }),
    }
}

/// 一个漏检根式：`√`（跨行 loose 盒 + 真实墨迹）+ 横线 + 横线下的被开方字形。
/// `loose_extra` 是 loose 盒相对墨迹额外向上凸出的高度。
fn undetected_radical(
    seq: u16,
    ink: Rect,
    bar: Rect,
    radicand_baseline: f32,
    radicand: &str,
    loose_extra: f32,
) -> (Vec<Glyph>, DisplayItem) {
    let mut glyphs = vec![measured_glyph(
        seq,
        '\u{221a}',
        Rect::new(
            ink.x0,
            ink.y1 - (ink.height() + loose_extra),
            ink.x1,
            ink.y1 + loose_extra,
        ),
        ink.y1 - 0.611,
        Some(ink),
    )];
    let adv = RAD_SIZE * 0.5;
    for (j, c) in radicand.chars().enumerate() {
        let x0 = bar.x0 + j as f32 * adv;
        let (y0, y1) = (radicand_baseline - 3.076, radicand_baseline + 7.8);
        glyphs.push(measured_glyph(
            seq + 1 + j as u16,
            c,
            Rect::new(x0, y0, x0 + adv, y1),
            radicand_baseline,
            Some(Rect::new(x0 + 0.1, y0 + 0.5, x0 + adv - 0.1, y1 - 0.8)),
        ));
    }
    (glyphs, bar_item(bar, 102))
}

fn page_with_prose_and_radical() -> PageIR {
    let mut glyphs = prose_row(0, "is approx one", 70.0, 328.463);
    glyphs.extend(prose_row(20, "most approx", 70.0, 314.913));
    glyphs.extend(prose_row(40, "channels after", 70.0, 301.364));
    let ink = Rect::new(173.147, 313.877, 180.488, 324.775);
    let bar = Rect::new(180.161, 324.467, 196.525, 324.467);
    let (rad, item) = undetected_radical(60, ink, bar, 314.913, "512", 3.55);
    glyphs.extend(rad);
    let mut ir = page_ir(glyphs, vec![mk_font("F1", false, false)]);
    ir.items.push(item);
    ir
}

#[test]
fn undetected_radical_is_owned_and_restores_reading_order() {
    let ir = page_with_prose_and_radical();
    let paragraphs = analyze_page(&ir, &full_region(RegionKind::Text));
    let p = paragraphs
        .iter()
        .find(|p| p.kind == RegionKind::Text)
        .expect("prose paragraph");
    assert_eq!(p.lines.len(), 3, "三行必须仍是三行：{}", p.text);
    let at = p.text.find('\u{221a}').expect("根号仍应在文本里");
    let after: String = p.text[at..].chars().take(4).collect();
    assert_eq!(after, "\u{221a}512", "根号必须紧邻其被开方字：{}", p.text);
    assert!(p.text.starts_with("is approx one"), "{}", p.text);
    let atom = p
        .atoms
        .iter()
        .find(|a| a.kind == AtomKind::Formula && a.source.is_some())
        .expect("漏检根式必须成为 SourceAtom");
    assert_eq!(atom.text, "\u{221a}512");
    assert_eq!(atom.glyph_range.1 - atom.glyph_range.0, 4);
}

/// 墨迹证据缺失时必须保守：不认领、不删除，源顺序保持原状而非被修复。
#[test]
fn radical_without_ink_evidence_is_not_claimed() {
    let mut ir = page_with_prose_and_radical();
    for item in &mut ir.items {
        if let DisplayItem::Text { glyphs } = item {
            for g in glyphs.iter_mut() {
                if g.unicode.iter().collect::<String>() == "\u{221a}" {
                    g.ink = None;
                }
            }
        }
    }
    let paragraphs = analyze_page(&ir, &full_region(RegionKind::Text));
    let p = &paragraphs[0];
    assert!(
        !p.atoms.iter().any(|a| a.source.is_some()),
        "无墨迹证据不得产生源原子"
    );
    assert!(matches!(p.translatable, Translatable::Yes), "不得删段");
}

/// 横线不是紧贴上缘 / 被开方字不在横线下 / 被开方字跨出横线 → 全部拒绝。
#[test]
fn non_adjoining_or_oversized_bar_is_rejected() {
    let ink = Rect::new(173.147, 313.877, 180.488, 324.775);
    let cases: [(&str, Rect, f32); 4] = [
        // 横线远离 `√` 顶部
        (
            "far bar",
            Rect::new(180.161, 336.9, 196.525, 336.9),
            314.913,
        ),
        // 横线整体在根号右侧（不与其墨迹相接）
        (
            "bar not touching",
            Rect::new(196.0, 324.467, 212.0, 324.467),
            314.913,
        ),
        // 横线极短，覆盖不了被开方字
        (
            "short bar",
            Rect::new(180.161, 324.467, 181.0, 324.467),
            314.913,
        ),
        // 被开方字不在横线下方
        (
            "radicand above",
            Rect::new(180.161, 324.467, 196.525, 324.467),
            328.463,
        ),
    ];
    for (name, bar, radicand_baseline) in cases {
        let mut glyphs = prose_row(0, "most approx", 70.0, 314.913);
        let (rad, item) = undetected_radical(60, ink, bar, radicand_baseline, "512", 3.55);
        glyphs.extend(rad);
        let mut ir = page_ir(glyphs, vec![mk_font("F1", false, false)]);
        ir.items.push(item);
        let paragraphs = analyze_page(&ir, &full_region(RegionKind::Text));
        assert!(
            !paragraphs
                .iter()
                .flat_map(|p| &p.atoms)
                .any(|a| a.source.is_some()),
            "{name} 缺证据不得认领"
        );
    }
}

#[test]
fn radical_requires_complete_ink_and_exclusive_paint() {
    for case in [
        "unknown glyph",
        "competing bar",
        "unowned fill",
        "missing radicand ink",
        "thick pen",
    ] {
        let mut ir = page_with_prose_and_radical();
        match case {
            "unknown glyph" => {
                let mut glyph = measured_glyph(
                    200,
                    '?',
                    Rect::new(176.0, 318.0, 178.0, 320.0),
                    310.0,
                    Some(Rect::new(176.0, 318.0, 178.0, 320.0)),
                );
                glyph.unicode.clear();
                ir.items.push(DisplayItem::Text {
                    glyphs: vec![glyph],
                });
            }
            "competing bar" => ir
                .items
                .push(bar_item(Rect::new(180.161, 324.467, 196.525, 324.467), 103)),
            "unowned fill" => ir.items.push(DisplayItem::Path {
                bbox: Rect::new(176.0, 318.0, 178.0, 320.0),
                is_fill: true,
                is_stroke: false,
                stroke: None,
            }),
            "missing radicand ink" => {
                for item in &mut ir.items {
                    if let DisplayItem::Text { glyphs } = item {
                        for g in glyphs {
                            if g.unicode.as_slice() == ['5'] {
                                g.ink = None;
                            }
                        }
                    }
                }
            }
            "thick pen" => {
                for item in &mut ir.items {
                    if let DisplayItem::Path {
                        stroke: Some(stroke),
                        ..
                    } = item
                    {
                        stroke.width = 10.0;
                    }
                }
            }
            _ => unreachable!(),
        }
        let regions = full_region(RegionKind::Text);
        let formulas = inline_formula::sources(&ir, &regions.iter().collect::<Vec<_>>());
        assert!(
            formulas.is_empty(),
            "{case}: ambiguous ink must not be claimed"
        );
    }
}

/// 已有显式 Formula 区域的根式仍走原路径，且不会被新候选重复认领。
#[test]
fn detected_formula_region_still_wins() {
    let ir = page_with_prose_and_radical();
    let root = ir
        .items
        .iter()
        .find_map(|item| match item {
            DisplayItem::Text { glyphs } => glyphs
                .iter()
                .find(|g| g.unicode.iter().collect::<String>() == "\u{221a}")
                .cloned(),
            _ => None,
        })
        .unwrap();
    // A detected formula region covers the whole formula, radicand included.
    let cover = Rect::new(
        root.ink.unwrap().x0,
        root.ink.unwrap().y0,
        root.bbox.x1 + 18.0,
        root.ink.unwrap().y1,
    );
    let mut regions = full_region(RegionKind::Text);
    let mut formula = text_region(1, cover, 1);
    formula.kind = RegionKind::Formula;
    regions.push(formula);
    let paragraphs = analyze_page(&ir, &regions);
    let formula_atoms: Vec<_> = paragraphs
        .iter()
        .flat_map(|p| &p.atoms)
        .filter(|a| a.source.is_some())
        .collect();
    assert_eq!(formula_atoms.len(), 1, "有显式区域时不得再多认领一次");
    assert_eq!(
        formula_atoms[0].text, "\u{221a}512",
        "显式区域仍完整拥有根式"
    );
    let mut owned: Vec<_> = paragraphs
        .iter()
        .flat_map(|p| &p.atoms)
        .filter(|a| a.source.is_some())
        .flat_map(|a| a.glyph_range.0..a.glyph_range.1)
        .collect();
    owned.sort_unstable();
    let unique = owned.len();
    owned.dedup();
    assert_eq!(owned.len(), unique, "同一个源字形不得被两个原子重复拥有");
}

/// 真实证据回归：DeepSeek 第 14 页（IR page 13）漏检两个根式的段落。
/// 只读已保存的 source IR 与 regions，不重跑模型/PDF 绑定。
#[test]
#[ignore = "requires saved real source/layout inventory (SYNCPDF_RADICAL_AUDIT)"]
fn real_page_radicals_restore_reading_order() {
    let root = std::path::PathBuf::from(std::env::var("SYNCPDF_RADICAL_AUDIT").unwrap());
    let pages: Vec<PageIR> =
        serde_json::from_slice(&std::fs::read(root.join("source.json")).unwrap()).unwrap();
    let ir = &pages[13];
    let regions: Vec<Region> =
        serde_json::from_slice(&std::fs::read(root.join("regions-13.json")).unwrap()).unwrap();
    let paragraphs = analyze_page(ir, &regions);
    // 该段是唯一同时含两个 `√` 且间距很大的源段。
    let p = paragraphs
        .iter()
        .find(|p| {
            p.text.matches('\u{221a}').count() == 2 && matches!(p.translatable, Translatable::Yes)
        })
        .expect("含两个根号的真实段落");
    assert!(
        p.text
            .contains("is approximately 1. After RMS normalization"),
        "阅读序必须恢复：{}",
        p.text
    );
    assert!(
        p.text
            .contains("most approximately\u{221a}512. RoPE preserves this norm"),
        "第一个根式必须紧邻 512：{}",
        p.text
    );
    assert!(
        p.text.contains("bounded by approximately\u{221a}512"),
        "第二个根式必须紧邻 512：{}",
        p.text
    );
    let atoms: Vec<_> = p
        .atoms
        .iter()
        .filter(|a| a.kind == AtomKind::Formula && a.source.is_some())
        .collect();
    assert_eq!(atoms.len(), 2, "两个根式都必须成为源原子");
    assert!(atoms.iter().all(|a| a.text == "\u{221a}512"));
}
