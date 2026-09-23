//! Small textual numbers can be shaped from their exact source text. Formula,
//! citation and URL atoms still require source-paint/link placement support.
use lopdf::{Document, Object};
use syncpdf_core::ir::{AtomKind, Paragraph};
use syncpdf_core::{Rect, StyleId};
use syncpdf_translate::{ParsedUnit, Segment};

pub fn resolve(para: &Paragraph, parsed: &ParsedUnit, doc: &Document) -> Option<ParsedUnit> {
    if para.atoms.is_empty() {
        return Some(parsed.clone());
    }
    if para.atoms.iter().any(|a| {
        a.kind != AtomKind::Number
            || a.text.is_empty()
            || a.glyph_range.0 >= a.glyph_range.1
            || a.glyph_range.1 as usize > para.glyphs.len()
            || !para
                .style_runs
                .iter()
                .any(|s| s.glyph_range.0 <= a.glyph_range.0 && a.glyph_range.1 <= s.glyph_range.1)
    }) || !annotations_clear(doc, para.id.page, para.bbox)
    {
        return None;
    }
    let mut result = parsed.clone();
    let mut seen = std::collections::BTreeSet::new();
    fn expand(
        segments: &mut [Segment],
        style: Option<StyleId>,
        para: &Paragraph,
        seen: &mut std::collections::BTreeSet<syncpdf_core::AtomId>,
    ) -> Option<()> {
        for segment in segments {
            match segment {
                Segment::Style { id, inner } => expand(inner, Some(*id), para, seen)?,
                Segment::Atom(id) => {
                    if !seen.insert(*id) {
                        return None;
                    }
                    let a = para.atoms.iter().find(|a| a.id == *id)?;
                    let source_style = para
                        .style_runs
                        .iter()
                        .find(|s| {
                            s.glyph_range.0 <= a.glyph_range.0 && a.glyph_range.1 <= s.glyph_range.1
                        })?
                        .id;
                    let text = Segment::Text(a.text.clone());
                    *segment = match style {
                        Some(id) if id == source_style => text,
                        Some(_) => return None,
                        None => Segment::Style {
                            id: source_style,
                            inner: vec![text],
                        },
                    };
                }
                _ => {}
            }
        }
        Some(())
    }
    expand(&mut result.segments, None, para, &mut seen)?;
    (seen.len() == para.atoms.len()).then_some(result)
}

fn object<'a>(doc: &'a Document, value: &'a Object) -> Option<&'a Object> {
    match value {
        Object::Reference(id) => doc.get_object(*id).ok(),
        _ => Some(value),
    }
}

// Moving a number in an annotated paragraph needs rebuilt annotation geometry.
// Reject missing/unknown annotation geometry rather than moving a clickable label.
fn annotations_clear(doc: &Document, page: u32, bbox: Rect) -> bool {
    let pages = doc.get_pages();
    let Some(page) = pages.get(&page).and_then(|id| doc.get_dictionary(*id).ok()) else {
        return false;
    };
    let Ok(annots) = page.get(b"Annots") else {
        return true;
    };
    let Some(annots) = object(doc, annots).and_then(|a| a.as_array().ok()) else {
        return false;
    };
    annots.iter().all(|a| {
        let Some(coords) = object(doc, a)
            .and_then(|a| a.as_dict().ok())
            .and_then(|a| a.get(b"Rect").ok())
            .and_then(|r| object(doc, r))
            .and_then(|r| r.as_array().ok())
        else {
            return false;
        };
        let numbers: Option<Vec<f32>> = coords
            .iter()
            .map(|n| object(doc, n)?.as_float().ok())
            .collect();
        let Some(n) = numbers else { return false };
        if n.len() != 4 || n.iter().any(|x| !x.is_finite()) {
            return false;
        }
        let r = Rect::new(
            n[0].min(n[2]),
            n[1].min(n[3]),
            n[0].max(n[2]),
            n[1].max(n[3]),
        );
        r.x1 < bbox.x0 || bbox.x1 < r.x0 || r.y1 < bbox.y0 || bbox.y1 < r.y0
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use lopdf::dictionary;
    use syncpdf_core::ir::{Align, Atom, RegionKind, StyleRun, Translatable};
    use syncpdf_core::{AtomId, Color, GlyphId, PageId};

    fn case() -> (Paragraph, ParsedUnit, Document) {
        let p = Paragraph {
            id: "P01-001".parse().unwrap(),
            page: PageId(0),
            region: 0,
            kind: RegionKind::Text,
            bbox: Rect::new(10.0, 10.0, 200.0, 50.0),
            lines: vec![],
            glyphs: (0..3)
                .map(|ordinal| GlyphId {
                    page: PageId(0),
                    op: syncpdf_core::OpKey::new(syncpdf_core::ObjRef::new(1, 0), 0),
                    ordinal,
                })
                .collect(),
            text_spans: vec![],
            style_runs: vec![StyleRun {
                id: StyleId(1),
                glyph_range: (0, 3),
                font: 0,
                size: 11.9552,
                color: Color::default(),
                bold: false,
                italic: false,
                serif: true,
                mono: false,
            }],
            atoms: vec![Atom {
                id: AtomId(1),
                glyph_range: (0, 3),
                kind: AtomKind::Number,
                text: "86%".into(),
            }],
            text: "86%".into(),
            align: Align::Left,
            first_indent: 0.0,
            line_height: 13.0,
            is_rtl: false,
            translatable: Translatable::Yes,
        };
        let parsed = syncpdf_translate::parse_unit_html(
            "<p id=\"P01-001\">准确率<span data-style=\"1\">{{KEEP_1}}</span></p>",
        )
        .unwrap();
        let mut doc = Document::new();
        let pages = doc.new_object_id();
        let page = doc.add_object(dictionary! { "Type" => "Page", "Parent" => pages });
        doc.objects.insert(pages, Object::Dictionary(dictionary! { "Type" => "Pages", "Kids" => vec![Object::Reference(page)], "Count" => 1 }));
        let root = doc.add_object(dictionary! { "Type" => "Catalog", "Pages" => pages });
        doc.trailer.set("Root", root);
        (p, parsed, doc)
    }

    #[test]
    fn source_number_is_exact_and_keeps_its_style() {
        let (para, parsed, doc) = case();
        let expanded = resolve(&para, &parsed, &doc).unwrap();
        assert_eq!(expanded.text(), "准确率86%");
        assert_eq!(expanded.style_ids(), vec![StyleId(1)]);
        assert!(expanded.atom_ids().is_empty());
        assert_eq!(parsed.atom_ids(), vec![AtomId(1)]);
    }

    #[test]
    fn formula_citation_link_and_mixed_style_remain_protected() {
        let (mut p, parsed, mut doc) = case();
        for kind in [AtomKind::Formula, AtomKind::Other, AtomKind::Url] {
            p.atoms[0].kind = kind;
            assert!(resolve(&p, &parsed, &doc).is_none());
        }
        p.atoms[0].kind = AtomKind::Number;
        p.style_runs[0].glyph_range.1 = 2;
        assert!(resolve(&p, &parsed, &doc).is_none());
        p.style_runs[0].glyph_range.1 = 3;
        let id = doc.get_pages()[&1];
        doc.get_dictionary_mut(id).unwrap().set("Annots", vec![Object::Dictionary(dictionary! { "Subtype" => "Link", "Rect" => vec![10.into(),10.into(),50.into(),30.into()] })]);
        assert!(resolve(&p, &parsed, &doc).is_none());
    }
}
