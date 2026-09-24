//! Preserve link identity while relocating clickable geometry to translated glyphs.
//! KEEP anchors are exact. Legacy unmarked references require unambiguous matches;
//! repeated unmarked labels may map by occurrence only when destinations agree.
//!
//! Click geometry is ink-based: a whitespace glyph carries no ink, so it must not
//! veto the rest of its label (CFF fonts report no bounds for a space, unlike
//! TrueType's empty box). Any other glyph without ink evidence still fails closed.
//!
//! A single Link that precisely covers every visible non-blank source glyph of the
//! paragraph (`Target::whole`) is a block-level anchor, such as a table-of-contents
//! title. It follows the paragraph's translated identity instead of a literal label
//! match. Partial coverage, a rival link, or a glyph outside the paragraph keeps the
//! literal-anchored path and its fail-closed fallback.
use lopdf::{Document, Object, ObjectId};
use std::collections::BTreeMap;
use syncpdf_core::ir::{Glyph, PageIR, Paragraph, TypesetParagraph};
use syncpdf_core::{AtomId, Rect, StyleId};
use syncpdf_translate::{ParsedUnit, Segment};
use syncpdf_typeset::Shaper;

#[derive(Debug, Clone)]
pub(crate) struct Target {
    pub para: Paragraph,
    pub parsed: ParsedUnit,
    pub html: String,
    links: Vec<(ObjectId, Vec<StyleId>)>,
    atom_links: Vec<(ObjectId, AtomId, Rect)>,
    /// Block-level anchors: clickable over the whole translated paragraph.
    whole: Vec<ObjectId>,
}
fn object<'a>(doc: &'a Document, o: &'a Object) -> Option<&'a Object> {
    match o {
        Object::Reference(id) => doc.get_object(*id).ok(),
        _ => Some(o),
    }
}
fn rect(doc: &Document, o: &Object) -> Option<Rect> {
    let a = object(doc, o)?.as_array().ok()?;
    let v: Vec<_> = a.iter().map(|v| v.as_float().ok()).collect::<Option<_>>()?;
    if v.len() != 4 || v.iter().any(|x| !x.is_finite()) {
        return None;
    }
    Some(Rect::new(
        v[0].min(v[2]),
        v[1].min(v[3]),
        v[0].max(v[2]),
        v[1].max(v[3]),
    ))
}
/// Whether a glyph paints visible ink that a link may cover. A glyph with no unicode
/// still counts: CID fonts may leave it unmapped while it paints ink, and counting it
/// keeps this branch stricter than the literal path rather than looser.
fn visible_nonblank(g: &Glyph) -> bool {
    let blank = !g.unicode.is_empty() && g.unicode.iter().all(|c| c.is_whitespace());
    !g.flags.invisible && !g.flags.outside_clip && !blank
}

fn expanded(
    segs: &[Segment],
    p: &Paragraph,
    text: &mut String,
    atoms: &mut BTreeMap<AtomId, (usize, usize)>,
) -> Option<()> {
    for s in segs {
        match s {
            Segment::Text(t) => text.push_str(t),
            Segment::Style { inner, .. } => expanded(inner, p, text, atoms)?,
            Segment::Br => {}
            Segment::Atom(id) => {
                let a = p.atoms.iter().find(|a| a.id == *id)?;
                if a.source.is_some() {
                    // Source drawings occupy no bytes in ParsedUnit::text().
                    continue;
                }
                let start = text.len();
                text.push_str(&a.text);
                atoms.insert(*id, (start, text.len()));
            }
        }
    }
    Some(())
}
fn matches(text: &str, label: &str) -> Vec<(usize, usize)> {
    text.match_indices(label)
        .filter_map(|(a, _)| {
            let b = a + label.len();
            let prev = text[..a].chars().next_back();
            let next = text[b..].chars().next();
            if prev.is_some_and(|c| c.is_ascii_alphanumeric())
                || next.is_some_and(|c| c.is_ascii_alphanumeric())
            {
                return None;
            }
            if prev == Some('.')
                && text[..a]
                    .chars()
                    .rev()
                    .nth(1)
                    .is_some_and(|c| c.is_ascii_digit())
            {
                return None;
            }
            if next == Some('.') && text[b..].chars().nth(1).is_some_and(|c| c.is_ascii_digit()) {
                return None;
            }
            Some((a, b))
        })
        .collect()
}
fn contextual(text: &str, range: (usize, usize), dest: &str) -> bool {
    let prefix: String = text[..range.0]
        .chars()
        .rev()
        .take(80)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect();
    let after: String = text[range.1..].chars().take(12).collect();
    let kind = if dest.starts_with("table.") {
        "(?:表|[Tt]ables?|[Tt]ab\\.)"
    } else if dest.starts_with("figure.") {
        "(?:图|[Ff]igures?|[Ff]igs?\\.)"
    } else if dest.starts_with("equation.") {
        "(?:式|公式|[Ee]quations?|[Ee]qs?\\.)"
    } else if dest.starts_with("appendix.") {
        "(?:附录|[Aa]ppendix|[Aa]ppendices)"
    } else if dest.contains("section.") {
        "(?:节|第|[Ss]ections?|[Ss]ec\\.)"
    } else {
        return false;
    };
    let re = regex::Regex::new(&format!(
        r"{kind}\s*\(?\s*(?:[0-9.]+\s*[,、和及]\s*(?:and\s*)?)*$"
    ))
    .unwrap();
    re.is_match(&prefix) || (dest.contains("section.") && after.trim_start().starts_with('节'))
}

pub(crate) fn prepare(
    para: &Paragraph,
    ir: &PageIR,
    doc: &Document,
    parsed: &ParsedUnit,
) -> Option<Target> {
    prepare_labeled(para, ir, doc, parsed).or_else(|| prepare_whole_block(para, ir, doc, parsed))
}

/// Relocate links by matching a literal source label inside the translation.
fn prepare_labeled(
    para: &Paragraph,
    ir: &PageIR,
    doc: &Document,
    parsed: &ParsedUnit,
) -> Option<Target> {
    let resolved = super::text_atoms::resolve_text(para, parsed)?;
    let mut target = Target {
        para: para.clone(),
        html: resolved.to_html(),
        parsed: resolved,
        links: vec![],
        atom_links: vec![],
        whole: vec![],
    };
    let page = *doc.get_pages().get(&para.id.page)?;
    let page = doc.get_dictionary(page).ok()?;
    let Ok(annots) = page.get(b"Annots") else {
        return Some(target);
    };
    let annots = object(doc, annots)?.as_array().ok()?;
    let glyphs: BTreeMap<_, _> = ir.glyphs().map(|g| (g.id, g)).collect();
    let mut source = String::new();
    let mut spans = Vec::new();
    for span in &para.text_spans {
        let start = source.len();
        source.push_str(&span.text);
        spans.push((start, source.len(), span.glyph_range));
    }
    let mut text = String::new();
    let mut atoms = BTreeMap::new();
    expanded(&parsed.segments, para, &mut text, &mut atoms)?;
    if text != target.parsed.text() {
        return None;
    }
    let mut ranges: Vec<(usize, usize, ObjectId)> = Vec::new();
    let mut bare: BTreeMap<String, Vec<(ObjectId, String)>> = BTreeMap::new();
    for o in annots {
        let d = object(doc, o)?.as_dict().ok()?;
        let r = rect(doc, d.get(b"Rect").ok()?)?;
        let owned: Vec<_> = para
            .glyphs
            .iter()
            .enumerate()
            .filter(|(_, id)| glyphs.get(id).is_some_and(|g| r.contains(g.bbox.center())))
            .map(|(i, _)| i as u32)
            .collect();
        if owned.is_empty() {
            continue;
        }
        if d.get(b"Subtype").ok()?.as_name().ok()? != b"Link" {
            return None;
        }
        // A shared annotation must not be rewritten by two paragraph owners.
        if ir.glyphs().any(|g| {
            r.contains(g.bbox.center())
                && !g.unicode.iter().all(|c| c.is_whitespace())
                && !para.glyphs.contains(&g.id)
        }) {
            return None;
        }
        let id = o.as_reference().ok()?;
        let selected: Vec<_> = spans
            .iter()
            .filter(|(_, _, (s, e))| owned.iter().any(|i| s <= i && i < e))
            .collect();
        let start = selected.first()?.0;
        let end = selected.last()?.1;
        let label =
            source[start..end].trim_matches(|c: char| c.is_whitespace() || "[],().".contains(c));
        if label.is_empty() {
            return None;
        }
        let start = start + source[start..end].find(label)?;
        let end = start + label.len();
        let atom = para.atoms.iter().find(|a| {
            owned
                .iter()
                .all(|i| a.glyph_range.0 <= *i && *i < a.glyph_range.1)
        });
        if let Some(a) = atom {
            if a.source.is_some() {
                target.atom_links.push((id, a.id, r));
                continue;
            }
            let src_start = spans
                .iter()
                .find(|(_, _, (s, e))| *s <= a.glyph_range.0 && a.glyph_range.0 < *e)?
                .0;
            let (t0, t1) = *atoms.get(&a.id)?;
            let dest_start = t0 + start.checked_sub(src_start)?;
            let dest_end = dest_start + end - start;
            if dest_end > t1 || text.get(dest_start..dest_end) != Some(label) {
                return None;
            }
            ranges.push((dest_start, dest_end, id));
        } else {
            let destination = d.get(b"Dest").ok().or_else(|| {
                object(doc, d.get(b"A").ok()?)?
                    .as_dict()
                    .ok()?
                    .get(b"D")
                    .ok()
            });
            let destination = destination
                .and_then(|o| o.as_str().ok().or_else(|| o.as_name().ok()))
                .map(|v| String::from_utf8_lossy(v).into_owned())
                .unwrap_or_else(|| format!("{:?}", d.get(b"A").ok().and_then(|o| object(doc, o))));
            bare.entry(label.into())
                .or_default()
                .push((id, destination));
        }
    }
    for (label, group) in bare {
        let candidates: Vec<_> = matches(&text, &label)
            .into_iter()
            .filter(|(s, e)| !atoms.values().any(|(a, b)| *s < *b && *a < *e))
            .collect();
        let mut destinations: BTreeMap<String, Vec<ObjectId>> = BTreeMap::new();
        for (id, destination) in group {
            destinations.entry(destination).or_default().push(id);
        }
        let multiple = destinations.len() > 1;
        for (destination, ids) in destinations {
            let context: Vec<_> = candidates
                .iter()
                .copied()
                .filter(|r| contextual(&text, *r, &destination))
                .collect();
            let selected = if context.len() == ids.len() {
                context
            } else if !multiple && candidates.len() == ids.len() {
                candidates.clone()
            } else {
                return None;
            };
            for ((s, e), id) in selected.into_iter().zip(ids) {
                ranges.push((s, e, id));
            }
        }
    }
    ranges.sort_by_key(|r| r.0);
    if ranges.windows(2).any(|w| w[0].1 > w[1].0) {
        return None;
    }
    let mut offset = 0;
    let segments = std::mem::take(&mut target.parsed.segments);
    target.parsed.segments = tag(
        &segments,
        StyleId(0),
        &mut offset,
        &ranges,
        &mut target.para,
        &mut target.links,
    );
    Some(target)
}

/// Block-level link: exactly one Link covers every visible non-blank source glyph
/// of the paragraph, no other link claims any of them, and no glyph outside the
/// paragraph falls inside its rect. The translated block is the anchor, so unequal
/// source/target labels (a translated table-of-contents title) are expected.
fn prepare_whole_block(
    para: &Paragraph,
    ir: &PageIR,
    doc: &Document,
    parsed: &ParsedUnit,
) -> Option<Target> {
    let resolved = super::text_atoms::resolve_text(para, parsed)?;
    let mut target = Target {
        para: para.clone(),
        html: resolved.to_html(),
        parsed: resolved,
        links: vec![],
        atom_links: vec![],
        whole: vec![],
    };
    // The same reconstruction invariant as the literal path: a mismatch is a parse
    // inconsistency, never a reason to widen the anchor.
    let mut text = String::new();
    let mut atoms = BTreeMap::new();
    expanded(&target.parsed.segments, para, &mut text, &mut atoms)?;
    if text != target.parsed.text() {
        return None;
    }
    let glyphs: BTreeMap<_, _> = ir.glyphs().map(|g| (g.id, g)).collect();
    let wanted: Vec<usize> = para
        .glyphs
        .iter()
        .enumerate()
        .filter(|(_, id)| glyphs.get(id).is_some_and(|g| visible_nonblank(g)))
        .map(|(i, _)| i)
        .collect();
    if wanted.is_empty() {
        return None;
    }
    let page = *doc.get_pages().get(&para.id.page)?;
    let page = doc.get_dictionary(page).ok()?;
    let Ok(annots) = page.get(b"Annots") else {
        return None;
    };
    let annots = object(doc, annots)?.as_array().ok()?;
    let mut owner: Option<ObjectId> = None;
    for o in annots {
        let d = object(doc, o)?.as_dict().ok()?;
        let r = rect(doc, d.get(b"Rect").ok()?)?;
        let owned: Vec<usize> = para
            .glyphs
            .iter()
            .enumerate()
            .filter(|(_, id)| {
                glyphs
                    .get(id)
                    .is_some_and(|g| visible_nonblank(g) && r.contains(g.bbox.center()))
            })
            .map(|(i, _)| i)
            .collect();
        if owned.is_empty() {
            continue;
        }
        // A glyph from another paragraph inside the rect is shared, not block-level.
        if ir.glyphs().any(|g| {
            r.contains(g.bbox.center()) && visible_nonblank(g) && !para.glyphs.contains(&g.id)
        }) {
            return None;
        }
        if d.get(b"Subtype").ok()?.as_name().ok()? != b"Link" {
            return None;
        }
        let id = o.as_reference().ok()?;
        // Exactly one link may claim the block, and it must claim all of it.
        if owner.is_some() || owned != wanted {
            return None;
        }
        owner = Some(id);
    }
    target.whole.push(owner?);
    Some(target)
}

fn tag(
    segs: &[Segment],
    style: StyleId,
    offset: &mut usize,
    ranges: &[(usize, usize, ObjectId)],
    para: &mut Paragraph,
    links: &mut Vec<(ObjectId, Vec<StyleId>)>,
) -> Vec<Segment> {
    let mut out = Vec::new();
    for s in segs {
        match s {
            Segment::Style { id, inner } => {
                out.extend(tag(inner, *id, offset, ranges, para, links))
            }
            Segment::Text(t) => {
                let end = *offset + t.len();
                let mut cursor = *offset;
                while cursor < end {
                    let active = ranges.iter().find(|r| r.0 <= cursor && cursor < r.1);
                    let stop = active
                        .map_or_else(
                            || {
                                ranges
                                    .iter()
                                    .filter(|r| r.0 > cursor)
                                    .map(|r| r.0)
                                    .min()
                                    .unwrap_or(end)
                            },
                            |r| r.1,
                        )
                        .min(end);
                    let mut id = style;
                    if let Some((_, _, annotation)) = active {
                        let mut run = para
                            .style_runs
                            .iter()
                            .find(|r| r.id == style)
                            .or(para.style_runs.first())
                            .expect("source style")
                            .clone();
                        id = StyleId(para.style_runs.iter().map(|r| r.id.0).max().unwrap_or(0) + 1);
                        run.id = id;
                        // Underline aliases retain their evidenced source range
                        // for pen selection and the candidate-scoped anchor gate.
                        if !run.underline {
                            run.glyph_range = (0, 0);
                        }
                        para.style_runs.push(run);
                        if let Some((_, tags)) = links.iter_mut().find(|(a, _)| a == annotation) {
                            tags.push(id);
                        } else {
                            links.push((*annotation, vec![id]));
                        }
                    }
                    let part = Segment::Text(t[cursor - *offset..stop - *offset].into());
                    out.push(Segment::Style {
                        id,
                        inner: vec![part],
                    });
                    cursor = stop;
                }
                *offset = end;
            }
            _ => out.push(s.clone()),
        }
    }
    out
}

pub(crate) fn geometry(
    target: &Target,
    laid: &TypesetParagraph,
    shaper: &dyn Shaper,
) -> Option<Vec<(ObjectId, Vec<Rect>)>> {
    let mut plans = target
        .links
        .iter()
        .map(|(id, tags)| {
            let mut boxes = Vec::new();
            for line in &laid.lines {
                let mut bbox: Option<Rect> = None;
                for g in line.glyphs.iter().filter(|g| tags.contains(&g.style)) {
                    // Whitespace has no glyph ink; skipping it keeps the label's
                    // remaining click area instead of voiding the whole anchor.
                    if !g.text.is_empty() && g.text.chars().all(char::is_whitespace) {
                        continue;
                    }
                    let b = shaper.glyph_bounds(g.font, g.gid, g.size)?;
                    let b = Rect::new(g.x + b.x0, g.y + b.y0, g.x + b.x1, g.y + b.y1);
                    bbox = Some(bbox.map_or(b, |a| a.union(&b)));
                }
                if let Some(b) = bbox {
                    boxes.push(b);
                }
            }
            if boxes.is_empty() {
                None
            } else {
                Some((*id, boxes))
            }
        })
        .collect::<Option<Vec<_>>>()?;
    for id in &target.whole {
        // The whole translated block is clickable, one ink box per rendered line.
        let mut boxes = Vec::new();
        for line in &laid.lines {
            let mut bbox: Option<Rect> = None;
            for g in &line.glyphs {
                if !g.text.is_empty() && g.text.chars().all(char::is_whitespace) {
                    continue;
                }
                let b = shaper.glyph_bounds(g.font, g.gid, g.size)?;
                let b = Rect::new(g.x + b.x0, g.y + b.y0, g.x + b.x1, g.y + b.y1);
                bbox = Some(bbox.map_or(b, |a| a.union(&b)));
            }
            if let Some(b) = bbox {
                boxes.push(b);
            }
        }
        if boxes.is_empty() {
            return None;
        }
        plans.push((*id, boxes));
    }
    for (annotation, id, source_rect) in &target.atom_links {
        let mut matches = laid
            .lines
            .iter()
            .flat_map(|l| &l.placed_atoms)
            .filter(|a| a.id == *id);
        let atom = matches.next()?;
        if matches.next().is_some() {
            return None;
        }
        let dx = atom.bbox.x0 - atom.source.x0;
        let dy = atom.bbox.y0 - atom.source.y0;
        plans.push((
            *annotation,
            vec![Rect::new(
                source_rect.x0 + dx,
                source_rect.y0 + dy,
                source_rect.x1 + dx,
                source_rect.y1 + dy,
            )],
        ));
    }
    Some(plans)
}

pub(crate) fn apply(
    doc: &mut Document,
    plans: &[(ObjectId, Vec<Rect>)],
) -> Result<(), super::PipelineError> {
    for (id, boxes) in plans {
        let Some(first) = boxes.first() else {
            continue;
        };
        let bbox = boxes.iter().skip(1).fold(*first, |a, b| a.union(b));
        let d = doc
            .get_object_mut(*id)
            .and_then(Object::as_dict_mut)
            .map_err(|e| super::PipelineError::Validation(format!("link update: {e}")))?;
        d.set(
            "Rect",
            vec![
                Object::Real(bbox.x0),
                Object::Real(bbox.y0),
                Object::Real(bbox.x1),
                Object::Real(bbox.y1),
            ],
        );
        let quad: Vec<Object> = boxes
            .iter()
            .flat_map(|b| [b.x0, b.y1, b.x1, b.y1, b.x0, b.y0, b.x1, b.y0])
            .map(Object::Real)
            .collect();
        d.set("QuadPoints", quad);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use syncpdf_core::ir::{LineBox, PlacedGlyph, TypesetParagraph};
    use syncpdf_core::ParagraphId;
    use syncpdf_typeset::shaper::MonoShaper;
    use syncpdf_typeset::{FontMetrics, ShapedGlyph, StyleSpec};

    #[test]
    fn reference_matches_do_not_confuse_numbers_or_sections() {
        assert_eq!(matches("表2，20和12，2.1；2。", "2").len(), 2);
        assert!(contextual("见表 18、19、20", (12, 14), "table.caption.1"));
        assert!(contextual("第5.1.4节", (3, 8), "subsubsection.5.1.4"));
    }

    /// CFF fonts report no bounds for a space glyph, while TrueType reports an
    /// empty box. Both carry no ink; `'X'` models a glyph with no usable bound
    /// and must keep failing closed.
    struct SpaceIsInklessShaper;
    impl Shaper for SpaceIsInklessShaper {
        fn shape(&self, font: u32, text: &str, size: f32, rtl: bool) -> Vec<ShapedGlyph> {
            MonoShaper.shape(font, text, size, rtl)
        }
        fn glyph_bounds(&self, _font: u32, gid: u16, size: f32) -> Option<Rect> {
            match char::from_u32(u32::from(gid)) {
                Some(' ') => None,
                Some('X') => None,
                _ => Some(Rect::new(0.0, 0.0, size * 0.5, size)),
            }
        }
        fn metrics(&self, font: u32) -> FontMetrics {
            MonoShaper.metrics(font)
        }
        fn font_for(&self, style: &StyleSpec) -> u32 {
            MonoShaper.font_for(style)
        }
    }

    fn placed(text: &str, x: f32, y: f32, style: u32) -> PlacedGlyph {
        PlacedGlyph {
            font: 0,
            gid: text.chars().next().expect("glyph text") as u16,
            text: text.into(),
            x,
            y,
            size: 10.0,
            scale_x: 1.0,
            shear_x: 0.0,
            style: StyleId(style),
            color: None,
        }
    }

    fn line(glyphs: Vec<PlacedGlyph>, y: f32) -> LineBox {
        let bbox = glyphs
            .iter()
            .fold(Rect::new(f32::MAX, f32::MAX, f32::MIN, f32::MIN), |a, g| {
                a.union(&Rect::new(g.x, y, g.x + 5.0, y + 10.0))
            });
        LineBox {
            bbox,
            baseline_y: y,
            glyphs,
            kept_atoms: vec![],
            placed_atoms: vec![],
            underlines: vec![],
        }
    }

    fn linked_target(text: &str, lines: Vec<LineBox>) -> (Target, TypesetParagraph) {
        let annotation = (9_u32, 0_u16);
        let para = Paragraph {
            id: "P01-001".parse().unwrap(),
            page: syncpdf_core::PageId(0),
            region: 0,
            kind: syncpdf_core::ir::RegionKind::Text,
            bbox: Rect::new(0.0, 0.0, 200.0, 40.0),
            lines: vec![],
            glyphs: vec![],
            text_spans: vec![],
            decorations: vec![],
            style_runs: vec![syncpdf_core::ir::StyleRun {
                id: StyleId(1),
                glyph_range: (0, 1),
                underline: false,
                font: 0,
                size: 10.0,
                color: syncpdf_core::Color::BLACK,
                bold: false,
                italic: false,
                serif: false,
                mono: false,
            }],
            atoms: vec![],
            text: text.into(),
            align: syncpdf_core::ir::Align::Left,
            first_indent: 0.0,
            line_height: 12.0,
            is_rtl: false,
            translatable: syncpdf_core::ir::Translatable::Yes,
        };
        let parsed = syncpdf_translate::parse_unit_html(&format!(
            "<p id=\"P01-001\"><span data-style=\"1\">{text}</span></p>"
        ))
        .unwrap();
        let target = Target {
            para,
            parsed,
            html: String::new(),
            links: vec![(annotation, vec![StyleId(1)])],
            atom_links: vec![],
            whole: vec![],
        };
        let laid = TypesetParagraph {
            id: ParagraphId::new(syncpdf_core::PageId(0), 1),
            lines,
            font_scale: 1.0,
            line_height: 12.0,
            color: syncpdf_core::Color::BLACK,
            used_bbox: Rect::new(0.0, 0.0, 200.0, 40.0),
            overflow: false,
        };
        (target, laid)
    }

    #[test]
    fn inkless_space_does_not_void_the_whole_link_label() {
        // "Alpha Beta"：空白字形没有 ink，其余字形有；标签仍必须得到点击框。
        let glyphs = "Alpha Beta"
            .chars()
            .enumerate()
            .map(|(i, c)| placed(&c.to_string(), i as f32 * 5.0, 10.0, 1))
            .collect();
        let (target, laid) = linked_target("Alpha Beta", vec![line(glyphs, 10.0)]);
        let plans = geometry(&target, &laid, &SpaceIsInklessShaper).expect("inkless space");
        assert_eq!(plans.len(), 1);
        let (id, boxes) = &plans[0];
        assert_eq!(*id, (9, 0));
        assert_eq!(boxes.len(), 1);
        // 覆盖全部有墨字形（0..50），不再因空白字形返回 None。
        assert!(boxes[0].x0 <= 0.0 && boxes[0].x1 >= 50.0);
    }

    #[test]
    fn empty_text_continuation_glyph_is_not_whitespace() {
        // Only the first glyph of a shaped cluster carries its text; subsequent
        // glyphs may carry accents/marks and must still contribute their ink.
        for whole in [false, true] {
            let mut mark = placed("M", 20.0, 10.0, 1);
            mark.text.clear();
            let (mut target, mut laid) =
                linked_target("A", vec![line(vec![placed("A", 0.0, 10.0, 1), mark], 10.0)]);
            if whole {
                target.whole.push((9, 0));
                target.links.clear();
            }
            let plan = geometry(&target, &laid, &SpaceIsInklessShaper).unwrap();
            assert_eq!(plan[0].1[0].x1, 25.0);
            laid.lines[0].glyphs[1].gid = b'X' as u16;
            assert!(geometry(&target, &laid, &SpaceIsInklessShaper).is_none());
        }
    }

    #[test]
    fn unmeasurable_non_whitespace_glyph_still_fails_closed() {
        let glyphs = vec![placed("A", 0.0, 10.0, 1), placed("X", 5.0, 10.0, 1)];
        let (target, laid) = linked_target("AX", vec![line(glyphs, 10.0)]);
        assert!(geometry(&target, &laid, &SpaceIsInklessShaper).is_none());
    }

    #[test]
    fn multi_line_link_keeps_one_box_per_line_around_its_spaces() {
        let first: Vec<_> = "Team et"
            .chars()
            .enumerate()
            .map(|(i, c)| placed(&c.to_string(), i as f32 * 5.0, 10.0, 1))
            .collect();
        let second: Vec<_> = "al., 2026a"
            .chars()
            .enumerate()
            .map(|(i, c)| placed(&c.to_string(), i as f32 * 5.0, 0.0, 1))
            .collect();
        let (target, laid) = linked_target(
            "Team et al., 2026a",
            vec![line(first, 10.0), line(second, 0.0)],
        );
        let plans = geometry(&target, &laid, &SpaceIsInklessShaper).expect("wrapped label");
        assert_eq!(plans[0].1.len(), 2, "每行一个点击框");
    }

    #[test]
    fn link_without_any_inkless_glyph_is_unchanged() {
        let glyphs = vec![placed("A", 0.0, 10.0, 1), placed("B", 5.0, 10.0, 1)];
        let (target, laid) = linked_target("AB", vec![line(glyphs, 10.0)]);
        let plans = geometry(&target, &laid, &SpaceIsInklessShaper).expect("plain label");
        assert_eq!(plans[0].1.len(), 1);
    }

    // ── 整块 Link 语义分支（TOC 标题） ────────────────────────────────

    use lopdf::dictionary;
    use syncpdf_core::ir::{DisplayItem, Glyph, GlyphFlags, GlyphSource, RegionKind};
    use syncpdf_core::{GlyphId, ObjRef, OpKey};

    /// 一个可见源字形，`ordinal` 同时是其在段落里的下标。
    fn src_glyph(ordinal: u16, text: char, x: f32) -> Glyph {
        Glyph {
            id: GlyphId {
                page: syncpdf_core::PageId(0),
                op: OpKey::new(ObjRef::new(1, 0), 0),
                ordinal,
            },
            unicode: [text].into_iter().collect(),
            code: text as u32,
            font: 0,
            size: 10.0,
            matrix: syncpdf_core::Matrix::new(1.0, 0.0, 0.0, 1.0, x, 10.0),
            bbox: Rect::new(x, 10.0, x + 5.0, 20.0),
            ink: None,
            advance: 5.0,
            fill: syncpdf_core::Color::BLACK,
            render_mode: 0,
            source: GlyphSource {
                element_index: 0,
                string_operand_range: (0, 1),
                decoded_code_range: (0, 1),
            },
            flags: GlyphFlags::default(),
        }
    }

    /// TOC 段：源为英文标题「Introduction」，译文为中文；Link 覆盖部分或全部字形。
    fn toc_case(link_ords: &[u16], rival: bool) -> (Paragraph, PageIR, Document, ParsedUnit) {
        let text = "Introduction";
        let glyphs: Vec<Glyph> = text
            .chars()
            .enumerate()
            .map(|(i, c)| src_glyph(i as u16, c, i as f32 * 5.0))
            .collect();
        let para = Paragraph {
            id: "P01-002".parse().unwrap(),
            page: syncpdf_core::PageId(0),
            region: 0,
            kind: RegionKind::ParagraphTitle,
            bbox: Rect::new(0.0, 10.0, 200.0, 20.0),
            lines: vec![],
            glyphs: glyphs.iter().map(|g| g.id).collect(),
            text_spans: text
                .chars()
                .enumerate()
                .map(|(i, c)| syncpdf_core::ir::SourceTextSpan {
                    text: c.to_string(),
                    glyph_range: (i as u32, i as u32 + 1),
                })
                .collect(),
            decorations: vec![],
            style_runs: vec![syncpdf_core::ir::StyleRun {
                id: StyleId(1),
                glyph_range: (0, text.len() as u32),
                underline: false,
                font: 0,
                size: 10.0,
                color: syncpdf_core::Color::BLACK,
                bold: false,
                italic: false,
                serif: false,
                mono: false,
            }],
            atoms: vec![],
            text: text.into(),
            align: syncpdf_core::ir::Align::Left,
            first_indent: 0.0,
            line_height: 12.0,
            is_rtl: false,
            translatable: syncpdf_core::ir::Translatable::Yes,
        };
        let ir = PageIR {
            page: syncpdf_core::PageId(0),
            media_box: Rect::new(0.0, 0.0, 612.0, 792.0),
            crop_box: Rect::new(0.0, 0.0, 612.0, 792.0),
            rotation: 0,
            fonts: vec![],
            items: vec![DisplayItem::Text { glyphs }],
        };
        let parsed = syncpdf_translate::parse_unit_html(
            r#"<p id="P01-002"><span data-style="1">1 引言</span></p>"#,
        )
        .unwrap();
        let mut doc = Document::new();
        let pages = doc.new_object_id();
        let page = doc.add_object(dictionary! { "Type" => "Page", "Parent" => pages });
        doc.objects.insert(
            pages,
            Object::Dictionary(
                dictionary! {"Type"=>"Pages","Kids"=>vec![Object::Reference(page)],"Count"=>1},
            ),
        );
        let root = doc.add_object(dictionary! {"Type"=>"Catalog","Pages"=>pages});
        doc.trailer.set("Root", root);
        let rect_of = |ords: &[u16]| {
            let sel: Vec<_> = ir
                .glyphs()
                .filter(|g| ords.contains(&g.id.ordinal))
                .collect();
            let b = sel.iter().fold(sel[0].bbox, |a, g| a.union(&g.bbox));
            vec![b.x0.into(), b.y0.into(), b.x1.into(), b.y1.into()]
        };
        let all: Vec<u16> = (0..text.len() as u16).collect();
        // 真实 PDF 的注释是间接对象；`as_reference` 依赖这一点。
        let mut annots = Vec::new();
        if rival {
            let a = doc.add_object(dictionary! {
                "Subtype" => "Link", "Rect" => rect_of(&all),
                "Dest" => Object::string_literal("section.2"),
            });
            annots.push(Object::Reference(a));
        }
        let a = doc.add_object(dictionary! {
            "Subtype" => "Link", "Rect" => rect_of(link_ords),
            "Dest" => Object::string_literal("section.1"),
        });
        annots.push(Object::Reference(a));
        doc.get_dictionary_mut(page).unwrap().set("Annots", annots);
        (para, ir, doc, parsed)
    }

    fn toc_laid(text: &str) -> TypesetParagraph {
        let glyphs: Vec<_> = text
            .chars()
            .enumerate()
            .map(|(i, c)| placed(&c.to_string(), i as f32 * 5.0, 10.0, 1))
            .collect();
        TypesetParagraph {
            id: ParagraphId::new(syncpdf_core::PageId(0), 2),
            lines: vec![line(glyphs, 10.0)],
            font_scale: 1.0,
            line_height: 12.0,
            color: syncpdf_core::Color::BLACK,
            used_bbox: Rect::new(0.0, 0.0, 200.0, 20.0),
            overflow: false,
        }
    }

    #[test]
    fn whole_block_toc_title_relocates_without_literal_label_match() {
        // 源「Introduction」与译文「1 引言」字面不同；整块 Link 仍必须成立。
        let all: Vec<u16> = (0.."Introduction".len() as u16).collect();
        let (para, ir, doc, parsed) = toc_case(&all, false);
        let target = prepare(&para, &ir, &doc, &parsed).expect("whole-block anchor");
        assert_eq!(target.whole.len(), 1);
        assert!(target.links.is_empty(), "不得降级为部分标签锚点");
        let laid = toc_laid("1 引言");
        let plans = geometry(&target, &laid, &SpaceIsInklessShaper).expect("toc geometry");
        assert_eq!(plans.len(), 1);
        assert_eq!(plans[0].1.len(), 1);
    }

    #[test]
    fn partial_link_coverage_does_not_take_the_whole_block_path() {
        // 只覆盖前 3 个字形：既非整块，也不得被当成整段锚点。
        let (para, ir, doc, parsed) = toc_case(&[0, 1, 2], false);
        assert!(
            prepare(&para, &ir, &doc, &parsed).is_none(),
            "部分拥有必须 fail closed"
        );
    }

    #[test]
    fn rival_link_destinations_are_rejected_for_the_whole_block() {
        // 两个 Link 都覆盖整块：目的地冲突，必须拒绝。
        let all: Vec<u16> = (0.."Introduction".len() as u16).collect();
        let (para, ir, doc, parsed) = toc_case(&all, true);
        assert!(
            prepare(&para, &ir, &doc, &parsed).is_none(),
            "多个链接冲突必须拒绝"
        );
    }

    #[test]
    fn whole_block_requires_no_foreign_glyph_inside_the_link_rect() {
        // 段外字形落在 Link 矩形内：共享注释，不得当作整块。
        let all: Vec<u16> = (0.."Introduction".len() as u16).collect();
        let (para, mut ir, mut doc, parsed) = toc_case(&all, false);
        if let Some(DisplayItem::Text { glyphs }) = ir.items.first_mut() {
            let mut foreign = src_glyph(99, 'Z', 100.0);
            foreign.id.ordinal = 99;
            glyphs.push(foreign);
        }
        let page = doc.get_pages()[&1];
        let b = Rect::new(0.0, 10.0, 105.0, 20.0);
        let a = doc.add_object(dictionary! {
            "Subtype" => "Link",
            "Rect" => vec![b.x0.into(), b.y0.into(), b.x1.into(), b.y1.into()],
            "Dest" => Object::string_literal("section.1"),
        });
        doc.get_dictionary_mut(page)
            .unwrap()
            .set("Annots", vec![Object::Reference(a)]);
        assert!(
            prepare(&para, &ir, &doc, &parsed).is_none(),
            "段外字形共享时必须拒绝"
        );
    }
}
