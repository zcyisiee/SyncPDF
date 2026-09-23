//! Exact numbers, citations and HTTP(S) URLs use normal shaping. Link identity
//! and relocation are verified separately in link_text; formulas remain protected.
#[cfg(test)]
use lopdf::{Document, Object};
use syncpdf_core::ir::{Atom, AtomKind, Paragraph};
#[cfg(test)]
use syncpdf_core::Rect;
use syncpdf_core::StyleId;
use syncpdf_translate::{ParsedUnit, Segment};

#[cfg(test)]
pub fn resolve(para: &Paragraph, parsed: &ParsedUnit, doc: &Document) -> Option<ParsedUnit> {
    if !para.atoms.is_empty() && !annotations_clear(doc, para.id.page, para.bbox) {
        return None;
    }
    resolve_text(para, parsed)
}

/// Annotation relocation is proved separately by link_text before publication.
pub(super) fn resolve_text(para: &Paragraph, parsed: &ParsedUnit) -> Option<ParsedUnit> {
    if para.atoms.is_empty() {
        return Some(parsed.clone());
    }
    if para.atoms.iter().any(|a| {
        !supported_text_atom(para, a)
            || a.text.is_empty()
            || a.glyph_range.0 >= a.glyph_range.1
            || a.glyph_range.1 as usize > para.glyphs.len()
            || first_source_style(para, a).is_none()
    }) {
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
                Segment::Style { id, inner } => {
                    if !para.style_runs.iter().any(|run| run.id == *id) {
                        return None;
                    }
                    expand(inner, Some(*id), para, seen)?;
                }
                Segment::Atom(id) => {
                    if !seen.insert(*id) {
                        return None;
                    }
                    let a = para.atoms.iter().find(|a| a.id == *id)?;
                    let text = Segment::Text(a.text.clone());
                    *segment = match style {
                        Some(_) => text,
                        None => Segment::Style {
                            id: first_source_style(para, a)?,
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

/// Source coverage must be complete, but one atom may cross adjacent font/style
/// runs (e.g. the digits and percent sign in 90%). Unstyled KEEP uses its first run.
fn first_source_style(para: &Paragraph, atom: &Atom) -> Option<StyleId> {
    let (start, end) = atom.glyph_range;
    let mut runs: Vec<_> = para
        .style_runs
        .iter()
        .filter(|r| r.glyph_range.0 < end && start < r.glyph_range.1)
        .collect();
    runs.sort_by_key(|r| r.glyph_range.0);
    let first = runs.first()?.id;
    let mut next = start;
    for run in runs {
        if run.glyph_range.0.max(start) != next {
            return None;
        }
        next = run.glyph_range.1.min(end);
    }
    (next == end).then_some(first)
}

fn supported_text_atom(para: &Paragraph, atom: &Atom) -> bool {
    match atom.kind {
        AtomKind::Number => true,
        AtomKind::Url => {
            (atom.text.starts_with("https://") || atom.text.starts_with("http://"))
                && exact_source_text(para, atom)
        }
        // Other also contains emails/unknown tokens: never approve the entire kind.
        AtomKind::Other => {
            static CITATION: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
            let syntax = CITATION.get_or_init(|| {
                regex::Regex::new(r"^\[[0-9]+(?:, *[0-9]+)*\]$").expect("citation regex")
            });
            syntax.is_match(&atom.text) && exact_source_text(para, atom)
        }
        _ => false,
    }
}

/// Require contiguous real glyph coverage and exact reading-order text. Generated
/// separators *inside* the range are included, but separators at its edges are not.
/// Missing/overlapping/partial spans must not let an atom silently delete extra text.
fn exact_source_text(para: &Paragraph, atom: &Atom) -> bool {
    let (start, end) = atom.glyph_range;
    let mut next = start;
    let mut text = String::new();
    for span in &para.text_spans {
        let (a, b) = span.glyph_range;
        if a == b {
            if start < a && a < end {
                if a != next || !span.text.chars().all(char::is_whitespace) {
                    return false;
                }
                text.push_str(&span.text);
            }
        } else if a < end && start < b {
            if a != next || b <= a || b > end {
                return false;
            }
            text.push_str(&span.text);
            next = b;
        }
    }
    next == end && text == atom.text
}

#[cfg(test)]
fn object<'a>(doc: &'a Document, value: &'a Object) -> Option<&'a Object> {
    match value {
        Object::Reference(id) => doc.get_object(*id).ok(),
        _ => Some(value),
    }
}

// Moving text in an annotated paragraph needs rebuilt annotation geometry.
// Reject missing/unknown annotation geometry rather than moving a clickable label.
#[cfg(test)]
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
mod real_paper;

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
    fn number_follows_target_style_even_when_visual_attributes_differ() {
        let (mut p, _, doc) = case();
        let mut target_style = p.style_runs[0].clone();
        target_style.id = StyleId(2);
        target_style.glyph_range = (3, 4);
        target_style.size = 9.0;
        target_style.bold = true;
        target_style.color = Color {
            r: 1.0,
            g: 0.0,
            b: 0.0,
        };
        p.style_runs.push(target_style);
        let parsed = syncpdf_translate::parse_unit_html(
            r#"<p id="P01-001">准确率<span data-style="2">{{KEEP_1}}</span></p>"#,
        )
        .unwrap();
        let out = resolve(&p, &parsed, &doc).expect("model-selected known style is allowed");
        assert_eq!(out.text(), "准确率86%");
        assert_eq!(out.style_ids(), vec![StyleId(2)]);
        let result = super::super::typeset::typeset_one(
            &syncpdf_typeset::shaper::MonoShaper,
            &p,
            &out,
            &syncpdf_typeset::Obstacles::default(),
        );
        let digits: Vec<_> = result
            .paragraph
            .lines
            .iter()
            .flat_map(|l| &l.glyphs)
            .filter(|g| g.text.chars().any(|c| c.is_ascii_digit() || c == '%'))
            .collect();
        assert_eq!(digits.len(), 3);
        assert!(digits.iter().all(|g| g.style == StyleId(2)
            && g.size == 9.0
            && g.color.is_some_and(|c| c.r == 1.0)));
    }

    #[test]
    fn text_atom_can_span_multiple_source_styles() {
        for citation in [false, true] {
            let (mut p, parsed, doc) = if citation {
                citation_case("[2]")
            } else {
                case()
            };
            p.style_runs[0].glyph_range = (0, 2);
            let mut tail = p.style_runs[0].clone();
            tail.id = StyleId(2);
            tail.font = 1;
            tail.glyph_range = (2, 3);
            p.style_runs.push(tail);
            let out = resolve(&p, &parsed, &doc).expect("source font boundary is not a failure");
            assert_eq!(out.text(), format!("准确率{}", p.atoms[0].text));
            assert_eq!(out.style_ids(), vec![StyleId(1)]);
            let unstyled =
                syncpdf_translate::parse_unit_html(r#"<p id="P01-001">{{KEEP_1}}</p>"#).unwrap();
            let out = resolve(&p, &unstyled, &doc).unwrap();
            assert_eq!(out.text(), p.atoms[0].text);
            assert_eq!(
                out.style_ids(),
                vec![StyleId(1)],
                "unstyled KEEP inherits its first source glyph style"
            );
        }
    }

    #[test]
    fn moved_atoms_still_require_known_styles_and_valid_source_ranges() {
        let (mut p, _, doc) = case();
        let unknown = syncpdf_translate::parse_unit_html(
            r#"<p id="P01-001"><span data-style="99">{{KEEP_1}}</span></p>"#,
        )
        .unwrap();
        assert!(resolve(&p, &unknown, &doc).is_none());
        let plain =
            syncpdf_translate::parse_unit_html(r#"<p id="P01-001">{{KEEP_1}}</p>"#).unwrap();
        p.atoms[0].glyph_range = (0, 4);
        assert!(resolve(&p, &plain, &doc).is_none());
    }

    fn citation_case(text: &str) -> (Paragraph, ParsedUnit, Document) {
        let (mut p, parsed, doc) = case();
        p.glyphs = (0..text.chars().count() as u16)
            .map(|ordinal| GlyphId {
                ordinal,
                ..p.glyphs[0]
            })
            .collect();
        let end = p.glyphs.len() as u32;
        p.atoms[0].kind = AtomKind::Other;
        p.atoms[0].text = text.into();
        p.atoms[0].glyph_range = (0, end);
        p.style_runs[0].glyph_range = (0, end);
        p.text_spans = text
            .chars()
            .enumerate()
            .map(|(i, c)| syncpdf_core::ir::SourceTextSpan {
                text: c.to_string(),
                glyph_range: (i as u32, i as u32 + 1),
            })
            .collect();
        (p, parsed, doc)
    }

    #[test]
    fn http_url_is_exact_and_never_reconstructed_from_partial_source() {
        let url = "https://example.org/path?q=1&lang=en";
        let (mut p, parsed, _) = citation_case(url);
        p.atoms[0].kind = AtomKind::Url;
        let expanded = resolve_text(&p, &parsed).unwrap();
        assert_eq!(expanded.text(), format!("准确率{url}"));
        assert!(expanded.atom_ids().is_empty());
        p.text_spans[0].text = "x".into();
        assert!(resolve_text(&p, &parsed).is_none());
    }

    #[test]
    fn unlinked_citation_restores_exact_source_text_and_style() {
        for text in ["[2]", "[17, 8, 29, 53, 42]"] {
            let (p, parsed, doc) = citation_case(text);
            let expanded = resolve(&p, &parsed, &doc).unwrap();
            assert_eq!(expanded.text(), format!("准确率{text}"));
            assert!(expanded.atom_ids().is_empty());
            assert_eq!(expanded.style_ids(), vec![StyleId(1)]);
            assert_eq!(
                syncpdf_translate::parse_unit_html(&expanded.to_html()).unwrap(),
                expanded
            );
            assert_eq!(parsed.atom_ids(), vec![AtomId(1)]);
        }
    }

    #[test]
    fn citation_requires_complete_exact_source_span_and_supported_syntax() {
        for text in ["mail@example.org", "[a+b]", "[1;2]", "[1-3]", "[]", "[１]"] {
            let (p, parsed, doc) = citation_case(text);
            assert!(resolve(&p, &parsed, &doc).is_none(), "{text}");
        }
        let (mut p, parsed, doc) = citation_case("[2]");
        p.text_spans[1].text = "3".into();
        assert!(resolve(&p, &parsed, &doc).is_none());
        p.text_spans[1].text = "2".into();
        p.text_spans[1].glyph_range = (1, 3);
        assert!(resolve(&p, &parsed, &doc).is_none());
        p.text_spans.clear();
        assert!(resolve(&p, &parsed, &doc).is_none());
    }

    #[test]
    fn citation_identity_and_annotation_guards_are_not_relaxed() {
        let (p, parsed, mut doc) = citation_case("[2]");
        for body in ["{{KEEP_1}}{{KEEP_1}}", "{{KEEP_2}}", "没有引用"] {
            let invalid =
                syncpdf_translate::parse_unit_html(&format!("<p id=\"P01-001\">{body}</p>"))
                    .unwrap();
            assert!(resolve(&p, &invalid, &doc).is_none());
        }
        let id = doc.get_pages()[&1];
        for annotation in [
            dictionary! { "Subtype" => "Link", "Rect" => vec![10.into(),10.into(),50.into(),30.into()], "A" => dictionary! { "S" => "URI", "URI" => Object::string_literal("https://example.org") } },
            dictionary! { "Subtype" => "Link" },
        ] {
            doc.get_dictionary_mut(id)
                .unwrap()
                .set("Annots", vec![Object::Dictionary(annotation)]);
            let before = doc.objects.clone();
            assert!(resolve(&p, &parsed, &doc).is_none());
            assert_eq!(
                before, doc.objects,
                "must not change source link/destination"
            );
        }
        doc.get_dictionary_mut(id).unwrap().set(
            "Annots",
            vec![Object::Dictionary(dictionary! {
                "Subtype" => "Link", "Rect" => vec![300.into(),300.into(),350.into(),330.into()]
            })],
        );
        assert!(resolve(&p, &parsed, &doc).is_some());
    }

    #[test]
    fn formula_other_link_and_incomplete_style_coverage_remain_protected() {
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
