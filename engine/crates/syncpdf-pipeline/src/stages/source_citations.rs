//! Preserve source citation-link identities through KEEP, rather than guessing
//! which repeated author/year literal a translated citation belongs to.
//! Only explicit local `cite.*` destinations are classified here; other links
//! keep their existing literal/block-level relocation gates.
use lopdf::{Document, Object};
use std::collections::BTreeMap;
use syncpdf_core::ir::{Atom, AtomKind, Glyph, PageIR, Paragraph, Translatable};
use syncpdf_core::{AtomId, Rect};

fn object<'a>(doc: &'a Document, value: &'a Object) -> Option<&'a Object> {
    match value {
        Object::Reference(id) => doc.get_object(*id).ok(),
        other => Some(other),
    }
}
fn visible(g: &Glyph) -> bool {
    !g.flags.invisible
        && !g.flags.outside_clip
        && (g.unicode.is_empty() || !g.unicode.iter().all(|c| c.is_whitespace()))
}
fn citation_rect(doc: &Document, value: &Object) -> Option<Rect> {
    let d = object(doc, value)?.as_dict().ok()?;
    if d.get(b"Subtype").ok()?.as_name().ok()? != b"Link" {
        return None;
    }
    let dest = if let Ok(dest) = d.get(b"Dest") {
        object(doc, dest)?
    } else {
        let action = object(doc, d.get(b"A").ok()?)?.as_dict().ok()?;
        if action.get(b"S").ok()?.as_name().ok()? != b"GoTo" {
            return None;
        }
        object(doc, action.get(b"D").ok()?)?
    };
    let name = dest.as_str().ok().or_else(|| dest.as_name().ok())?;
    if !name.starts_with(b"cite.") || name.len() <= 5 {
        return None;
    }
    let coords = object(doc, d.get(b"Rect").ok()?)?.as_array().ok()?;
    let c: Vec<_> = coords
        .iter()
        .map(|v| v.as_float().ok())
        .collect::<Option<_>>()?;
    if c.len() != 4 || !c.iter().all(|v| v.is_finite()) || c[0] >= c[2] || c[1] >= c[3] {
        return None;
    }
    Some(Rect::new(c[0], c[1], c[2], c[3]))
}

fn source_text(p: &Paragraph, start: u32, end: u32) -> Option<String> {
    let mut next = start;
    let mut text = String::new();
    for s in &p.text_spans {
        let (a, b) = s.glyph_range;
        if a == b && start < a && a < end {
            if a != next || !s.text.chars().all(char::is_whitespace) {
                return None;
            }
            text.push_str(&s.text);
        } else if a < b && a < end && start < b {
            if a != next || b > end {
                return None;
            }
            text.push_str(&s.text);
            next = b;
        }
    }
    (next == end && !text.trim().is_empty()).then_some(text)
}

pub(crate) fn protect(paragraphs: &mut [Paragraph], ir: &PageIR, doc: &Document) {
    let Some(page) = doc.get_pages().get(&ir.page.number()).copied() else {
        return;
    };
    let Some(annots) = doc
        .get_dictionary(page)
        .ok()
        .and_then(|p| p.get(b"Annots").ok())
        .and_then(|a| object(doc, a))
        .and_then(|a| a.as_array().ok())
    else {
        return;
    };
    let links: Vec<_> = annots
        .iter()
        .filter_map(|a| citation_rect(doc, a))
        .collect();
    let glyphs: BTreeMap<_, _> = ir.glyphs().map(|g| (g.id, g)).collect();
    for p in paragraphs {
        if p.translatable != Translatable::Yes {
            continue;
        }
        let mut next = p.atoms.iter().map(|a| a.id.0).max().unwrap_or(0) + 1;
        for r in &links {
            let owned: Vec<_> = p
                .glyphs
                .iter()
                .enumerate()
                .filter(|(_, id)| glyphs.get(id).is_some_and(|g| r.contains(g.bbox.center())))
                .map(|(i, _)| i as u32)
                .collect();
            let (Some(&start), Some(&last)) = (owned.first(), owned.last()) else {
                continue;
            };
            let end = last + 1;
            // Shared annotations, missing evidence and non-contiguous ownership
            // must keep the old fail-closed relocation path.
            if ir
                .glyphs()
                .any(|g| visible(g) && r.contains(g.bbox.center()) && !p.glyphs.contains(&g.id))
                || p.glyphs[start as usize..end as usize].iter().any(|id| {
                    glyphs.get(id).is_none_or(|g| {
                        visible(g) && (g.unicode.is_empty() || !r.contains(g.bbox.center()))
                    })
                })
                || p.atoms
                    .iter()
                    .any(|a| a.glyph_range.0 < end && start < a.glyph_range.1)
            {
                continue;
            }
            // A textual KEEP expands with one source style. Do not silently
            // flatten differently styled parts of a citation into that first run.
            let styles: Vec<_> = p
                .style_runs
                .iter()
                .filter(|s| s.glyph_range.0 < end && start < s.glyph_range.1)
                .collect();
            if styles.is_empty()
                || styles.windows(2).any(|w| {
                    let (a, b) = (w[0], w[1]);
                    a.font != b.font
                        || a.size != b.size
                        || a.color != b.color
                        || a.bold != b.bold
                        || a.italic != b.italic
                        || a.serif != b.serif
                        || a.mono != b.mono
                        || a.underline != b.underline
                })
            {
                continue;
            }
            let Some(text) = source_text(p, start, end) else {
                continue;
            };
            p.atoms.push(Atom {
                id: AtomId(next),
                kind: AtomKind::Citation,
                glyph_range: (start, end),
                text,
                source: None,
            });
            next += 1;
        }
        p.atoms.sort_by_key(|a| a.glyph_range.0);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use lopdf::dictionary;
    use syncpdf_core::ir::*;
    use syncpdf_core::{Color, GlyphId, Matrix, ObjRef, OpKey, PageId, StyleId};

    fn fixture(destination: &str) -> (Paragraph, PageIR, Document) {
        let glyphs: Vec<_> = "2026 2026"
            .chars()
            .enumerate()
            .map(|(i, c)| Glyph {
                id: GlyphId {
                    page: PageId(0),
                    op: OpKey::new(ObjRef::new(1, 0), 0),
                    ordinal: i as u16,
                },
                unicode: vec![c].into(),
                code: c as u32,
                font: 0,
                size: 10.,
                matrix: Matrix::IDENTITY,
                bbox: Rect::new(i as f32 * 5., 10., i as f32 * 5. + 5., 20.),
                ink: None,
                advance: 5.,
                fill: Color::BLACK,
                render_mode: 0,
                source: GlyphSource {
                    element_index: 0,
                    string_operand_range: (i as u32, i as u32 + 1),
                    decoded_code_range: (i as u32, i as u32 + 1),
                },
                flags: GlyphFlags::default(),
            })
            .collect();
        let p = Paragraph {
            id: "P01-001".parse().unwrap(),
            page: PageId(0),
            region: 0,
            kind: RegionKind::Text,
            bbox: Rect::new(0., 10., 45., 20.),
            lines: vec![],
            glyphs: glyphs.iter().map(|g| g.id).collect(),
            text_spans: glyphs
                .iter()
                .enumerate()
                .map(|(i, g)| SourceTextSpan {
                    text: g.unicode.iter().collect(),
                    glyph_range: (i as u32, i as u32 + 1),
                })
                .collect(),
            decorations: vec![],
            style_runs: vec![StyleRun {
                id: StyleId(1),
                underline: false,
                glyph_range: (0, 9),
                font: 0,
                size: 10.,
                color: Color::BLACK,
                bold: false,
                italic: false,
                serif: true,
                mono: false,
            }],
            atoms: vec![],
            text: "2026 2026".into(),
            align: Align::Left,
            first_indent: 0.,
            line_height: 15.,
            is_rtl: false,
            translatable: Translatable::Yes,
        };
        let ir = PageIR {
            page: PageId(0),
            media_box: Rect::new(0., 0., 100., 100.),
            crop_box: Rect::new(0., 0., 100., 100.),
            rotation: 0,
            fonts: vec![],
            items: vec![DisplayItem::Text { glyphs }],
        };
        let mut doc = Document::new();
        let pages = doc.new_object_id();
        let a=doc.add_object(dictionary! { "Type"=>"Annot","Subtype"=>"Link","Rect"=>vec![0.into(),10.into(),20.into(),20.into()],"Dest"=>Object::string_literal(format!("{destination}a")) });
        let b=doc.add_object(dictionary! { "Type"=>"Annot","Subtype"=>"Link","Rect"=>vec![25.into(),10.into(),45.into(),20.into()],"Dest"=>Object::string_literal(format!("{destination}b")) });
        let page = doc.add_object(
            dictionary! { "Type"=>"Page","Parent"=>pages,"Annots"=>vec![a.into(),b.into()] },
        );
        doc.objects.insert(
            pages,
            Object::Dictionary(
                dictionary! { "Type"=>"Pages","Kids"=>vec![page.into()],"Count"=>1 },
            ),
        );
        let root = doc.add_object(dictionary! { "Type"=>"Catalog","Pages"=>pages });
        doc.trailer.set("Root", root);
        (p, ir, doc)
    }
    #[test]
    fn repeated_years_keep_distinct_source_destinations_after_reordering() {
        let (mut p, ir, doc) = fixture("cite.");
        let ambiguous =
            syncpdf_translate::parse_unit_html("<p id=\"P01-001\">甲2026乙2026</p>").unwrap();
        // 两个同文不同目标的链接在改序译文中无法区分：不猜，全部移除并保留译文。
        let dropped = crate::stages::link_text::prepare(&p, &ir, &doc, &ambiguous).unwrap();
        assert_eq!(dropped.dropped.len(), 2);
        protect(std::slice::from_mut(&mut p), &ir, &doc);
        assert_eq!(p.atoms.len(), 2);
        assert_eq!(p.atoms[0].text, "2026");
        assert_eq!(p.atoms[1].text, "2026");
        let parsed =
            syncpdf_translate::parse_unit_html("<p id=\"P01-001\">乙{{KEEP_2}}甲{{KEEP_1}}</p>")
                .unwrap();
        assert!(crate::stages::link_text::prepare(&p, &ir, &doc, &parsed).is_some());
    }
    #[test]
    fn changed_citation_literal_and_mixed_source_styles_fail_closed() {
        let (mut p, ir, doc) = fixture("cite.");
        let mut second = p.style_runs[0].clone();
        second.id = StyleId(2);
        second.glyph_range = (2, 9);
        second.italic = true;
        p.style_runs[0].glyph_range = (0, 2);
        p.style_runs.push(second);
        protect(std::slice::from_mut(&mut p), &ir, &doc);
        assert_eq!(p.atoms.len(), 1);
        assert_eq!(p.atoms[0].glyph_range, (5, 9));
        let (mut p, ir, doc) = fixture("cite.");
        protect(std::slice::from_mut(&mut p), &ir, &doc);
        p.atoms[0].text = "2025".into();
        let parsed =
            syncpdf_translate::parse_unit_html("<p id=\"P01-001\">{{KEEP_1}}和{{KEEP_2}}</p>")
                .unwrap();
        assert!(crate::stages::link_text::prepare(&p, &ir, &doc, &parsed).is_none());
    }

    #[test]
    fn ordinary_links_and_shared_annotations_are_not_frozen() {
        let (mut p, ir, doc) = fixture("section.");
        protect(std::slice::from_mut(&mut p), &ir, &doc);
        assert!(p.atoms.is_empty());
        let (mut p, mut ir, doc) = fixture("cite.");
        let DisplayItem::Text { glyphs } = &mut ir.items[0] else {
            unreachable!()
        };
        // Unknown visible glyphs cannot be treated as citation whitespace.
        glyphs[0].unicode.clear();
        let mut foreign = glyphs[0].clone();
        foreign.id.ordinal = 99;
        glyphs.push(foreign);
        protect(std::slice::from_mut(&mut p), &ir, &doc);
        assert_eq!(p.atoms.len(), 1);
        assert_eq!(p.atoms[0].glyph_range, (5, 9));
    }
}
