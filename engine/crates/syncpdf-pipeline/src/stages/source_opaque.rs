//! Visible control-code glyphs have no usable text identity. Preserve their
//! evidenced source drawing; never guess a Unicode replacement or silently erase.
//! A leading list bullet has text identity, but its look belongs to the source
//! font (a CJK face draws U+2022 as a small centered dot), so it keeps its
//! source drawing too when that drawing is proven.
use std::collections::BTreeMap;
use syncpdf_core::ir::{
    Atom, AtomKind, DisplayItem, Glyph, PageIR, Paragraph, SourceAtom, Translatable,
};
use syncpdf_core::{AtomId, Rect};

fn overlap(a: Rect, b: Rect) -> bool {
    a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1
}

fn visible(g: &Glyph) -> bool {
    !g.flags.invisible
        && !g.flags.outside_clip
        && (g.unicode.is_empty() || !g.unicode.iter().all(|c| c.is_whitespace()))
}

fn drawing(p: &Paragraph, index: u32, g: &Glyph, ir: &PageIR) -> Option<SourceAtom> {
    let ink = g.ink?;
    if ![ink.x0, ink.y0, ink.x1, ink.y1]
        .iter()
        .all(|x| x.is_finite())
        || ink.width() <= 0.0
        || ink.height() <= 0.0
    {
        return None;
    }
    let text: String = g.unicode.iter().collect();
    if !p
        .text_spans
        .iter()
        .any(|s| s.glyph_range == (index, index + 1) && s.text == text)
    {
        return None;
    }
    let line = p.lines.iter().find(|l| l.glyphs.contains(&g.id))?;
    if !line.baseline_y.is_finite() || (ink.center().y - line.baseline_y).abs() > g.size {
        return None;
    }
    if ir.glyphs().any(|other| {
        other.id != g.id && visible(other) && overlap(ink, other.ink.unwrap_or(other.bbox))
    }) {
        return None;
    }
    if p.atoms
        .iter()
        .filter_map(|a| a.source)
        .any(|a| overlap(ink, a.bbox))
    {
        return None;
    }
    if ir.items.iter().any(|item| match item {
        DisplayItem::Image { bbox } | DisplayItem::InlineImage { bbox } => overlap(ink, *bbox),
        DisplayItem::Path {
            bbox,
            is_fill,
            is_stroke,
            stroke,
        } if *is_fill || *is_stroke => {
            let pad = if *is_stroke {
                stroke.as_ref().map_or(0.5, |s| (s.width * 0.5).max(0.5))
            } else {
                0.0
            };
            overlap(
                ink,
                Rect::new(bbox.x0 - pad, bbox.y0 - pad, bbox.x1 + pad, bbox.y1 + pad),
            )
        }
        _ => false,
    }) {
        return None;
    }
    Some(SourceAtom {
        bbox: ink,
        baseline: line.baseline_y,
        advance: None,
    })
}

pub(crate) fn protect(paragraphs: &mut [Paragraph], ir: &PageIR) {
    let glyphs: BTreeMap<_, _> = ir.glyphs().map(|g| (g.id, g)).collect();
    let mut owners = BTreeMap::new();
    for p in paragraphs.iter() {
        for id in &p.glyphs {
            *owners.entry(*id).or_insert(0_usize) += 1;
        }
    }
    for p in paragraphs {
        if p.translatable != Translatable::Yes {
            continue;
        }
        let mut additions = Vec::new();
        let mut failed = false;
        let mut next = p.atoms.iter().map(|a| a.id.0).max().unwrap_or(0) + 1;
        for (i, id) in p.glyphs.iter().enumerate() {
            let Some(g) = glyphs.get(id) else { continue };
            if !visible(g)
                || !g
                    .unicode
                    .iter()
                    .any(|c| c.is_control() && !c.is_whitespace())
            {
                continue;
            }
            let index = i as u32;
            if let Some(a) = p
                .atoms
                .iter()
                .find(|a| a.glyph_range.0 <= index && index < a.glyph_range.1)
            {
                if a.source.is_some() {
                    continue;
                }
                failed = true;
                break;
            }
            let source = drawing(p, index, g, ir).filter(|_| owners.get(id) == Some(&1));
            let Some(source) = source else {
                failed = true;
                break;
            };
            additions.push(Atom {
                id: AtomId(next),
                kind: AtomKind::Symbol,
                glyph_range: (index, index + 1),
                text: g.unicode.iter().collect(),
                source: Some(source),
            });
            next += 1;
        }
        if failed {
            p.translatable = Translatable::No {
                reason: "unmapped_source_glyph".into(),
            };
        } else {
            p.atoms.extend(additions);
            p.atoms.sort_by_key(|a| a.glyph_range.0);
        }
    }
}

/// Keep a translatable paragraph's leading bullet as a source-drawn symbol.
/// Unproven drawings leave the bullet as ordinary text (current shaping).
pub(crate) fn keep_list_bullets(paragraphs: &mut [Paragraph], ir: &PageIR) {
    let glyphs: BTreeMap<_, _> = ir.glyphs().map(|g| (g.id, g)).collect();
    let mut owners = BTreeMap::new();
    for p in paragraphs.iter() {
        for id in &p.glyphs {
            *owners.entry(*id).or_insert(0_usize) += 1;
        }
    }
    for p in paragraphs {
        let Some(g) = p.glyphs.first().and_then(|id| glyphs.get(id)) else {
            continue;
        };
        if p.translatable != Translatable::Yes
            || !matches!(g.unicode.as_slice(), [c] if crate::stages::paragraph::is_list_bullet(*c))
            || !p.text.chars().skip(1).any(char::is_alphanumeric)
            || p.atoms.iter().any(|a| a.glyph_range.0 == 0)
            || owners.get(&g.id) != Some(&1)
        {
            continue;
        }
        let Some(mut source) = drawing(p, 0, g, ir) else {
            continue;
        };
        // The label column runs to the body's first glyph on the same line.
        let body = p.lines[0].glyphs.get(1).and_then(|id| glyphs.get(id));
        source.advance = body
            .map(|b| b.bbox.x0 - source.bbox.x0)
            .filter(|w| *w > source.bbox.width());
        p.atoms.push(Atom {
            id: AtomId(p.atoms.iter().map(|a| a.id.0).max().unwrap_or(0) + 1),
            kind: AtomKind::Symbol,
            glyph_range: (0, 1),
            text: g.unicode.iter().collect(),
            source: Some(source),
        });
        p.atoms.sort_by_key(|a| a.glyph_range.0);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use syncpdf_core::ir::*;
    use syncpdf_core::{Color, GlyphId, Matrix, ObjRef, OpKey, PageId, Rect, StyleId};

    fn fixture() -> (Vec<Paragraph>, PageIR) {
        let glyphs: Vec<_> = "x\u{2}m\u{3}"
            .chars()
            .enumerate()
            .map(|(i, c)| {
                let x = 10.0 + i as f32 * 6.0;
                Glyph {
                    id: GlyphId {
                        page: PageId(0),
                        op: OpKey::new(ObjRef::new(1, 0), 0),
                        ordinal: i as u16,
                    },
                    unicode: vec![c].into(),
                    code: c as u32,
                    font: 0,
                    size: 10.0,
                    matrix: Matrix::new(1.0, 0.0, 0.0, 1.0, x, 20.0),
                    bbox: Rect::new(x, 18.0, x + 5.0, 28.0),
                    ink: Some(Rect::new(x + 0.5, 18.5, x + 4.5, 27.5)),
                    advance: 6.0,
                    fill: Color::BLACK,
                    render_mode: 0,
                    source: GlyphSource {
                        element_index: 0,
                        string_operand_range: (i as u32, i as u32 + 1),
                        decoded_code_range: (i as u32, i as u32 + 1),
                    },
                    flags: GlyphFlags::default(),
                }
            })
            .collect();
        let bbox = Rect::new(10.0, 18.0, 34.0, 28.0);
        let p = Paragraph {
            id: "P01-001".parse().unwrap(),
            page: PageId(0),
            region: 0,
            kind: RegionKind::Text,
            bbox,
            lines: vec![Line {
                glyphs: glyphs.iter().map(|g| g.id).collect(),
                baseline_y: 20.0,
                bbox,
            }],
            glyphs: glyphs.iter().map(|g| g.id).collect(),
            text_spans: glyphs
                .iter()
                .enumerate()
                .map(|(i, g)| SourceTextSpan {
                    text: g.unicode.iter().collect(),
                    glyph_range: (i as u32, i as u32 + 1),
                })
                .collect(),
            style_runs: vec![StyleRun {
                id: StyleId(1),
                glyph_range: (0, 4),
                font: 0,
                size: 10.0,
                color: Color::BLACK,
                bold: false,
                italic: false,
                serif: true,
                mono: false,
                underline: false,
                rise: 0.0,
            }],
            atoms: vec![],
            decorations: vec![],
            text: "x\u{2}m\u{3}".into(),
            align: Align::Left,
            first_indent: 0.0,
            line_height: 15.0,
            is_rtl: false,
            translatable: Translatable::Yes,
        };
        let ir = PageIR {
            page: PageId(0),
            media_box: Rect::new(0.0, 0.0, 100.0, 100.0),
            crop_box: Rect::new(0.0, 0.0, 100.0, 100.0),
            rotation: 0,
            fonts: vec![],
            items: vec![DisplayItem::Text { glyphs }],
        };
        (vec![p], ir)
    }

    #[test]
    fn visible_control_glyphs_use_exact_source_keep_without_guessing_text() {
        let (mut ps, ir) = fixture();
        let original = ps[0].text.clone();
        protect(&mut ps, &ir);
        assert_eq!(ps[0].translatable, Translatable::Yes);
        assert_eq!(ps[0].text, original);
        assert_eq!(ps[0].atoms.len(), 2);
        for a in &ps[0].atoms {
            let g = ir.glyphs().nth(a.glyph_range.0 as usize).unwrap();
            assert_eq!(a.source.unwrap().bbox, g.ink.unwrap());
            assert_eq!(a.source.unwrap().baseline, 20.0);
            assert_eq!(a.text, g.unicode.iter().collect::<String>());
        }
        let unit = syncpdf_translate::build_unit(&ps[0], |id| {
            ir.glyphs()
                .find(|g| g.id == id)
                .map(|g| g.unicode.iter().collect())
        });
        assert!(
            unit.html.contains("{{KEEP_1}}") && unit.html.contains("{{KEEP_2}}"),
            "{}",
            unit.html
        );
        assert!(!unit.html.contains('\u{2}'));
        assert!(!unit.html.contains('\u{3}'));
    }

    #[test]
    fn missing_or_conflicting_control_ink_blocks_instead_of_erasing() {
        for case in ["missing ink", "neighbor ink", "path", "shared owner"] {
            let (mut ps, mut ir) = fixture();
            if let DisplayItem::Text { glyphs } = &mut ir.items[0] {
                match case {
                    "missing ink" => glyphs[1].ink = None,
                    "neighbor ink" => {
                        glyphs[2].ink = glyphs[1].ink;
                        glyphs[2].unicode.clear();
                    }
                    _ => {}
                }
            }
            if case == "path" {
                ir.items.push(DisplayItem::Path {
                    bbox: Rect::new(16.5, 23.0, 20.5, 23.0),
                    is_fill: false,
                    is_stroke: true,
                    stroke: None,
                });
            }
            if case == "shared owner" {
                ps.push(ps[0].clone());
            }
            protect(&mut ps, &ir);
            assert!(
                matches!(&ps[0].translatable,Translatable::No {reason} if reason=="unmapped_source_glyph"),
                "{case}"
            );
            assert!(ps[0].atoms.is_empty(), "must not publish partial ownership");
        }
    }

    #[test]
    fn ordinary_whitespace_and_existing_source_atoms_are_unchanged() {
        let (mut ps, mut ir) = fixture();
        if let DisplayItem::Text { glyphs } = &mut ir.items[0] {
            glyphs[1].unicode = vec!['\t'].into();
            glyphs[1].flags.is_space = true;
            let g = &glyphs[3];
            ps[0].atoms.push(Atom {
                id: syncpdf_core::AtomId(8),
                kind: AtomKind::Formula,
                glyph_range: (3, 4),
                text: "existing".into(),
                source: Some(SourceAtom {
                    bbox: g.ink.unwrap(),
                    baseline: 20.0,
                    advance: None,
                }),
            });
        }
        let before = ps.clone();
        protect(&mut ps, &ir);
        assert_eq!(ps, before);
    }

    fn text_fixture(text: &str) -> (Vec<Paragraph>, PageIR) {
        let (mut ps, mut ir) = fixture();
        let chars: Vec<char> = text.chars().collect();
        if let DisplayItem::Text { glyphs } = &mut ir.items[0] {
            for (g, c) in glyphs.iter_mut().zip(&chars) {
                g.unicode = vec![*c].into();
            }
        }
        for (span, c) in ps[0].text_spans.iter_mut().zip(&chars) {
            span.text = c.to_string();
        }
        ps[0].text = text.into();
        (ps, ir)
    }

    #[test]
    fn leading_list_bullets_keep_their_source_drawing() {
        for bullet in ['\u{2022}', '\u{25CF}', '\u{F0B7}', '\u{2013}'] {
            let (mut ps, ir) = text_fixture(&format!("{bullet}abc"));
            keep_list_bullets(&mut ps, &ir);
            assert_eq!(ps[0].translatable, Translatable::Yes);
            let [a] = ps[0].atoms.as_slice() else {
                panic!("{bullet:?}: {:?}", ps[0].atoms)
            };
            assert_eq!((a.kind, a.glyph_range), (AtomKind::Symbol, (0, 1)));
            assert_eq!(
                a.source.unwrap().bbox,
                ir.glyphs().next().unwrap().ink.unwrap()
            );
            // Label column: bullet ink left edge (10.5) to the body origin (16).
            assert_eq!(a.source.unwrap().advance, Some(5.5));
            let unit = syncpdf_translate::build_unit(&ps[0], |id| {
                ir.glyphs()
                    .find(|g| g.id == id)
                    .map(|g| g.unicode.iter().collect())
            });
            assert!(unit.html.contains("{{KEEP_1}}abc"), "{}", unit.html);
        }
    }

    #[test]
    fn non_leading_or_unproven_bullets_stay_ordinary_text() {
        for (case, text) in [
            ("letter first", "xabc"),
            ("bullet inside", "a\u{2022}bc"),
            ("bullet only", "\u{2022}  -"),
            ("neighbor ink", "\u{2022}abc"),
        ] {
            let (mut ps, mut ir) = text_fixture(text);
            if case == "neighbor ink" {
                if let DisplayItem::Text { glyphs } = &mut ir.items[0] {
                    glyphs[1].ink = glyphs[0].ink;
                }
            }
            keep_list_bullets(&mut ps, &ir);
            assert!(ps[0].atoms.is_empty(), "{case}");
            assert_eq!(ps[0].translatable, Translatable::Yes, "{case}");
        }
    }

    #[test]
    #[ignore = "requires saved source inventory (OPAQUE_INVENTORY)"]
    fn real_control_symbols_are_preserved() {
        let root = std::path::PathBuf::from(std::env::var("OPAQUE_INVENTORY").unwrap());
        let pages: Vec<PageIR> =
            serde_json::from_slice(&std::fs::read(root.join("source.json")).unwrap()).unwrap();
        let mut ps: Vec<Paragraph> =
            serde_json::from_slice(&std::fs::read(root.join("all-paragraphs.json")).unwrap())
                .unwrap();
        ps.retain(|p| p.page == PageId(19));
        let p = ps.iter().find(|p| p.id.to_string() == "P20-002").unwrap();
        let old_count = p.atoms.len();
        let old_text = p.text.clone();
        protect(&mut ps, &pages[19]);
        let p = ps.iter().find(|p| p.id.to_string() == "P20-002").unwrap();
        assert_eq!(p.translatable, Translatable::Yes);
        assert_eq!(p.text, old_text);
        assert_eq!(p.atoms.len(), old_count + 2);
        assert_eq!(
            p.atoms
                .iter()
                .filter(|a| a.text == "\u{2}" || a.text == "\u{3}")
                .count(),
            2
        );
    }
}
