//! Preserve link identity while relocating clickable geometry to translated glyphs.
//! KEEP anchors are exact. Legacy unmarked references require unambiguous matches;
//! repeated unmarked labels may map by occurrence only when destinations agree.
use lopdf::{Document, Object, ObjectId};
use std::collections::BTreeMap;
use syncpdf_core::ir::{PageIR, Paragraph, TypesetParagraph};
use syncpdf_core::{AtomId, Rect, StyleId};
use syncpdf_translate::{ParsedUnit, Segment};
use syncpdf_typeset::Shaper;

#[derive(Debug, Clone)]
pub(crate) struct Target {
    pub para: Paragraph,
    pub parsed: ParsedUnit,
    pub html: String,
    links: Vec<(ObjectId, Vec<StyleId>)>,
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
    let resolved = super::text_atoms::resolve_text(para, parsed)?;
    let mut target = Target {
        para: para.clone(),
        html: resolved.to_html(),
        parsed: resolved,
        links: vec![],
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
                        run.glyph_range = (0, 0);
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
    target
        .links
        .iter()
        .map(|(id, tags)| {
            let mut boxes = Vec::new();
            for line in &laid.lines {
                let mut bbox: Option<Rect> = None;
                for g in line.glyphs.iter().filter(|g| tags.contains(&g.style)) {
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
        .collect()
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
    #[test]
    fn reference_matches_do_not_confuse_numbers_or_sections() {
        assert_eq!(matches("表2，20和12，2.1；2。", "2").len(), 2);
        assert!(contextual("见表 18、19、20", (12, 14), "table.caption.1"));
        assert!(contextual("第5.1.4节", (3, 8), "subsubsection.5.1.4"));
    }
}
