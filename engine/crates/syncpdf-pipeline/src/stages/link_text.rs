//! Preserve link identity while relocating clickable geometry to translated glyphs.
//! KEEP anchors are exact. Legacy unmarked references require unambiguous matches;
//! repeated unmarked labels may map by occurrence only when destinations agree.
//!
//! A translated cross-reference keeps its identity as "category + number": the
//! literal label disappears when its category word is translated ("Table 15" →
//! 「表 15」), so the surviving number tail relocates the anchor. A tail candidate
//! must sit where the translated category word precedes it ("表 15", "见表15",
//! "第4.2节" — the space is the translator's choice, not a signal); a bare
//! same-number is never claimed just because the count matches. The fail-closed
//! selection rules are unchanged: per-destination grouping, exact counts,
//! otherwise the paragraph keeps its source text.
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
fn contextual(text: &str, range: (usize, usize), kind: Option<RefKind>) -> bool {
    let Some(kind) = kind else {
        return false;
    };
    let prefix = prefix_of(text, range);
    let after: String = text[range.1..].chars().take(12).collect();
    kind.precedes_number(&prefix)
        || (kind == RefKind::Section && after.trim_start().starts_with('节'))
}

/// Up to 80 chars of translated text right before a candidate range.
fn prefix_of(text: &str, range: (usize, usize)) -> String {
    text[..range.0]
        .chars()
        .rev()
        .take(80)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect()
}

/// A cross-reference category: the generic structure of a labeled anchor like
/// "Table 15" or "Figure 6" — a category word plus a number/letter tail. One
/// concept with two evidence sources: the source label's own category word
/// (works without hyperref-named destinations) and the destination namespace
/// (`table.`/`figure.`/…). Conflicting sources suppress label decomposition.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum RefKind {
    Table,
    Figure,
    Equation,
    Appendix,
    Section,
}

impl RefKind {
    /// hyperref 风格命名目的地（section 允许 subsubsection 等包含形式，
    /// 以及 `\section*` 生成的 `section*.` 星号变体）。
    fn from_dest(dest: &str) -> Option<Self> {
        if dest.starts_with("table.") {
            Some(Self::Table)
        } else if dest.starts_with("figure.") {
            Some(Self::Figure)
        } else if dest.starts_with("equation.") {
            Some(Self::Equation)
        } else if dest.starts_with("appendix.") {
            Some(Self::Appendix)
        } else if dest.contains("section.") || dest.contains("section*.") {
            Some(Self::Section)
        } else {
            None
        }
    }

    /// 源标签的类别词（学术引用惯例词与缩写，与译文侧词表同属一个类别概念）。
    fn from_label_word(word: &str) -> Option<Self> {
        match word.trim_end_matches('.').to_ascii_lowercase().as_str() {
            "table" | "tables" | "tab" => Some(Self::Table),
            "figure" | "figures" | "fig" | "figs" => Some(Self::Figure),
            "equation" | "equations" | "eq" | "eqs" => Some(Self::Equation),
            "appendix" | "appendices" => Some(Self::Appendix),
            "section" | "sections" | "sec" => Some(Self::Section),
            _ => None,
        }
    }

    /// 类别词在译文中可能的形式（中文或保留英文）。
    fn word_pattern(self) -> &'static str {
        match self {
            Self::Table => r"(?:表|[Tt]ables?|[Tt]ab\.)",
            Self::Figure => r"(?:图|[Ff]igures?|[Ff]igs?\.)",
            Self::Equation => r"(?:式|公式|[Ee]quations?|[Ee]qs?\.)",
            Self::Appendix => r"(?:附录|[Aa]ppendix|[Aa]ppendices)",
            Self::Section => r"(?:节|第|[Ss]ections?|[Ss]ec\.)",
        }
    }

    /// Whether a word of this category sits right before the number, possibly
    /// with an enumeration list ("表 18、19、20") in between.
    fn precedes_number(self, prefix: &str) -> bool {
        regex::Regex::new(&format!(
            r"{}\s*\(?\s*(?:[0-9.]+\s*[,、和及]\s*(?:and\s*)?)*$",
            self.word_pattern()
        ))
        .expect("category prefix regex")
        .is_match(prefix)
    }
}

/// Split a bare label into cross-reference structure: a leading category word
/// plus a numeric ("15", "4.2") or single-letter ("B") tail. Labels without
/// that structure (pure numbers, footnotes, "§I") keep the literal-only path.
fn decompose(label: &str) -> Option<(RefKind, &str)> {
    let label = label.trim_start();
    let word_end = label
        .find(|c: char| !c.is_ascii_alphabetic() && c != '.')
        .unwrap_or(label.len());
    let kind = RefKind::from_label_word(&label[..word_end])?;
    let tail = label[word_end..].trim_start();
    let numeric = tail.chars().any(|c| c.is_ascii_digit())
        && tail.chars().all(|c| c.is_ascii_digit() || c == '.')
        && !tail.starts_with('.')
        && !tail.ends_with('.');
    let letter = tail.len() == 1 && tail.chars().all(|c| c.is_ascii_alphabetic());
    (numeric || letter).then_some((kind, tail))
}

/// Fail-closed selection per destination group: context-matched candidates
/// when their count is exact, otherwise every candidate when the label has a
/// single destination and counts still match exactly. `strict` drops the
/// count-only fallback: candidates must carry the category context — used for
/// number tails, where a bare same-number ("15 项特征") must never be claimed.
fn select_by_destination(
    candidates: &[(usize, usize)],
    text: &str,
    destinations: &BTreeMap<String, Vec<ObjectId>>,
    multiple: bool,
    strict: bool,
    kind_of: impl Fn(&str) -> Option<RefKind>,
) -> Option<Vec<((usize, usize), ObjectId)>> {
    let mut out = Vec::new();
    for (destination, ids) in destinations {
        let context: Vec<_> = candidates
            .iter()
            .copied()
            .filter(|r| contextual(text, *r, kind_of(destination)))
            .collect();
        let selected = if context.len() == ids.len() {
            context
        } else if !strict && !multiple && candidates.len() == ids.len() {
            candidates.to_vec()
        } else {
            return None;
        };
        for (r, id) in selected.into_iter().zip(ids) {
            out.push((r, *id));
        }
    }
    Some(out)
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
        let literal: Vec<_> = matches(&text, &label)
            .into_iter()
            .filter(|(s, e)| !atoms.values().any(|(a, b)| *s < *b && *a < *e))
            .collect();
        let mut destinations: BTreeMap<String, Vec<ObjectId>> = BTreeMap::new();
        for (id, destination) in &group {
            destinations
                .entry(destination.clone())
                .or_default()
                .push(*id);
        }
        let multiple = destinations.len() > 1;
        let mut selected = select_by_destination(
            &literal,
            &text,
            &destinations,
            multiple,
            false,
            RefKind::from_dest,
        );
        if selected.is_none() {
            // The whole literal may be gone because its category word was
            // translated: relocate by the surviving number tail, gated by the
            // category concept. A destination namespace that disagrees with the
            // label's category suppresses this — don't guess. Tail candidates
            // must carry the category context (strict); a bare same-number is
            // never claimed just because the count happens to match.
            let decomposed = decompose(&label).filter(|(kind, _)| {
                group
                    .iter()
                    .all(|(_, dest)| RefKind::from_dest(dest).is_none_or(|d| d == *kind))
            });
            let (kind, number) = decomposed?;
            let tail: Vec<_> = matches(&text, number)
                .into_iter()
                .filter(|(s, e)| !atoms.values().any(|(a, b)| *s < *b && *a < *e))
                .filter(|r| !literal.contains(r))
                .collect();
            let mut all = literal.clone();
            all.extend(tail);
            all.sort_unstable();
            all.dedup();
            selected =
                select_by_destination(&all, &text, &destinations, multiple, true, |_| Some(kind));
        }
        ranges.extend(selected?.into_iter().map(|((s, e), id)| (s, e, id)));
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
        assert!(contextual(
            "见表 18、19、20",
            (12, 14),
            RefKind::from_dest("table.caption.1")
        ));
        assert!(contextual(
            "第5.1.4节",
            (3, 8),
            RefKind::from_dest("subsubsection.5.1.4")
        ));
        // `\section*` 的星号命名空间同属 section 类别。
        assert_eq!(RefKind::from_dest("section*.57"), Some(RefKind::Section));
        // 类别概念的标签侧来源：没有 hyperref 命名 dest 也能推出类别。
        assert_eq!(decompose("Table 15"), Some((RefKind::Table, "15")));
        assert_eq!(decompose("Appendix B"), Some((RefKind::Appendix, "B")));
        assert_eq!(decompose("Section 4.2"), Some((RefKind::Section, "4.2")));
        assert_eq!(decompose("Fig. 6"), Some((RefKind::Figure, "6")));
        // 无类别结构的标签不分解，保持纯字面路径。
        for bare in ["15", "8", "B", "§I", "NarraBench"] {
            assert_eq!(decompose(bare), None, "{bare} 不应分解");
        }
        // 类别语境只看编号前是否紧邻本类别词，与空格无关：真实模型常写
        // 「见表15」「第4.2节」。枚举 "(1)" 与无类别词的孤立编号不算。
        const T: Option<RefKind> = Some(RefKind::Table);
        assert!(contextual("见表15", (6, 8), T));
        assert!(contextual("见表 15", (7, 9), T));
        assert!(contextual("第4.2节", (3, 6), Some(RefKind::Section)));
        assert!(!contextual("三阶段(1)", (10, 11), T));
        assert!(!contextual("这里 15 项特征", (7, 9), T));
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

    // ── 裸引用「类别 + 编号」重定位（Table 15 → 「表 15」） ────────────────

    /// 裸引用段：源文本 `text`（每字符一个 span），每个 Link 覆盖 `links` 给出的
    /// 字符区间并指向命名目的地，译文 HTML 为 `translated`。返回 prepare 四元组
    /// 与 (注释 id, dest) 列表。
    #[allow(clippy::type_complexity)]
    fn ref_case(
        id: &str,
        text: &str,
        links: &[(usize, usize, &str)],
        translated: &str,
    ) -> (
        Paragraph,
        PageIR,
        Document,
        ParsedUnit,
        Vec<(ObjectId, String)>,
    ) {
        let glyphs: Vec<Glyph> = text
            .chars()
            .enumerate()
            .map(|(i, c)| src_glyph(i as u16, c, i as f32 * 5.0))
            .collect();
        let para = Paragraph {
            id: id.parse().unwrap(),
            page: syncpdf_core::PageId(0),
            region: 0,
            kind: RegionKind::Text,
            bbox: Rect::new(0.0, 10.0, text.chars().count() as f32 * 5.0 + 10.0, 20.0),
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
                glyph_range: (0, text.chars().count() as u32),
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
        let parsed = syncpdf_translate::parse_unit_html(&format!(
            r#"<p id="{id}"><span data-style="1">{translated}</span></p>"#
        ))
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
        let mut annots = Vec::new();
        let mut placed = Vec::new();
        for (a, b, dest) in links {
            let id = doc.add_object(dictionary! {
                "Subtype" => "Link",
                "Rect" => vec![(*a as f32 * 5.0).into(), 10.0.into(), (*b as f32 * 5.0).into(), 20.0.into()],
                "Dest" => Object::string_literal(*dest),
            });
            annots.push(Object::Reference(id));
            placed.push((id, (*dest).to_string()));
        }
        doc.get_dictionary_mut(page).unwrap().set("Annots", annots);
        (para, ir, doc, parsed, placed)
    }

    /// 每个链接注释在译文中得到的锚定文本（按样式段收集）。
    fn annotated_texts(target: &Target) -> Vec<(ObjectId, String)> {
        fn walk(segs: &[Segment], style: Option<StyleId>, out: &mut Vec<(StyleId, String)>) {
            for s in segs {
                match s {
                    Segment::Text(t) => {
                        if let Some(id) = style {
                            out.push((id, t.clone()));
                        }
                    }
                    Segment::Style { id, inner } => walk(inner, Some(*id), out),
                    _ => {}
                }
            }
        }
        let mut spans = Vec::new();
        walk(&target.parsed.segments, None, &mut spans);
        target
            .links
            .iter()
            .map(|(ann, tags)| {
                let text: String = spans
                    .iter()
                    .filter(|(id, _)| tags.contains(id))
                    .map(|(_, t)| t.as_str())
                    .collect();
                (*ann, text)
            })
            .collect()
    }

    #[test]
    fn translated_category_word_relocates_bare_reference() {
        // "Table 15"→「表 15」：类别词按正常翻译被译掉，整串字面消失；
        // 锚点身份是「类别 + 编号」，按存活下来的编号重定位。
        let text = "See Table 15 for details.";
        let at = text.find("Table 15").unwrap();
        let (para, ir, doc, parsed, placed) = ref_case(
            "P01-011",
            text,
            &[(at, at + "Table 15".len(), "table.15")],
            "见表 15 的细节。",
        );
        let target = prepare(&para, &ir, &doc, &parsed).expect("按类别+编号重定位");
        assert!(target.whole.is_empty());
        let ann = annotated_texts(&target);
        assert_eq!(ann.len(), 1);
        assert_eq!(ann[0].0, placed[0].0);
        assert_eq!(ann[0].1, "15");
    }

    #[test]
    fn same_paragraph_references_pair_by_destination() {
        // 同段三链接 Table 14/15/16 → 按各自目标正确配对。
        let text = "Compare Table 14, Table 15 and Table 16.";
        let words = ["Table 14", "Table 15", "Table 16"];
        let dests = ["table.14", "table.15", "table.16"];
        let links: Vec<_> = words
            .iter()
            .zip(dests)
            .map(|(w, d)| {
                let at = text.find(w).unwrap();
                (at, at + w.len(), d)
            })
            .collect();
        let (para, ir, doc, parsed, placed) =
            ref_case("P01-012", text, &links, "比较表 14、表 15 和表 16。");
        let target = prepare(&para, &ir, &doc, &parsed).expect("三链接按目标配对");
        let ann = annotated_texts(&target);
        assert_eq!(ann.len(), 3);
        for (obj, dest) in &placed {
            let anchored = ann
                .iter()
                .find(|(o, _)| o == obj)
                .unwrap_or_else(|| panic!("{dest} 没有锚点"));
            let number = dest.trim_start_matches("table.");
            assert_eq!(anchored.1, number, "{dest} 必须锚定在编号 {number} 上");
        }
    }

    #[test]
    fn figure_appendix_and_dotted_section_tails_relocate() {
        // 不同形态的编号尾：图、附录字母尾、带点节号。
        for (source, label, dest, translated) in [
            (
                "as shown in Figure 6",
                "Figure 6",
                "figure.6",
                "如图 6 所示",
            ),
            ("see Appendix B", "Appendix B", "appendix.B", "见附录 B"),
            (
                "in Section 4.2",
                "Section 4.2",
                "section.4.2",
                "在第 4.2 节",
            ),
        ] {
            let at = source.find(label).unwrap();
            let (para, ir, doc, parsed, _) = ref_case(
                "P01-013",
                source,
                &[(at, at + label.len(), dest)],
                translated,
            );
            let target = prepare(&para, &ir, &doc, &parsed)
                .unwrap_or_else(|| panic!("{label} 必须能按类别+编号重定位"));
            assert_eq!(annotated_texts(&target).len(), 1);
        }
    }

    #[test]
    fn ambiguous_isolated_numbers_without_category_fall_back() {
        // 译文中只有孤立的同号数字（无类别词）且数量对不上：不得抢作锚点。
        let text = "See Table 15 for details.";
        let at = text.find("Table 15").unwrap();
        let (para, ir, doc, parsed, _) = ref_case(
            "P01-014",
            text,
            &[(at, at + "Table 15".len(), "table.15")],
            "前 15 项与后 15 项特征都被保留。",
        );
        assert!(
            prepare(&para, &ir, &doc, &parsed).is_none(),
            "两个孤立 15 无法唯一定位，必须整段回退"
        );
    }

    #[test]
    fn foreign_category_word_rejects_the_candidate() {
        // 源标签是 Table，译文却写成「图 15」：类别不符，不得借用。
        let text = "See Table 15 for details.";
        let at = text.find("Table 15").unwrap();
        let (para, ir, doc, parsed, _) = ref_case(
            "P01-015",
            text,
            &[(at, at + "Table 15".len(), "table.15")],
            "见图 15 的细节。",
        );
        assert!(
            prepare(&para, &ir, &doc, &parsed).is_none(),
            "类别词与源类别不符必须回退"
        );
    }

    #[test]
    fn tail_count_mismatch_falls_back() {
        // 同一标签两条链接、译文只有一个编号：数量不恰等，回退。
        let text = "See Table 15 and again Table 15 here.";
        let first = text.find("Table 15").unwrap();
        let last = text.rfind("Table 15").unwrap();
        let (para, ir, doc, parsed, _) = ref_case(
            "P01-016",
            text,
            &[
                (first, first + "Table 15".len(), "table.15"),
                (last, last + "Table 15".len(), "table.15"),
            ],
            "见表 15，另见上文。",
        );
        assert!(
            prepare(&para, &ir, &doc, &parsed).is_none(),
            "编号数量与链接数不等必须回退"
        );
    }

    #[test]
    fn literal_label_still_present_uses_the_original_path() {
        // 整串字面仍在：走原路径锚定整串字面，不启用编号分解候选。
        let text = "See Table 15 for details.";
        let at = text.find("Table 15").unwrap();
        let (para, ir, doc, parsed, placed) = ref_case(
            "P01-017",
            text,
            &[(at, at + "Table 15".len(), "table.15")],
            "See Table 15 for details. 另有 15 项特征。",
        );
        let target = prepare(&para, &ir, &doc, &parsed).expect("字面在时走原路径");
        let ann = annotated_texts(&target);
        assert_eq!(ann.len(), 1);
        assert_eq!(ann[0].0, placed[0].0);
        assert_eq!(ann[0].1, "Table 15", "锚定整串字面而不是孤立编号");
    }

    #[test]
    fn enumerated_numbers_are_not_reference_candidates() {
        // 编号候选不得落在枚举 "(1)" 上；有类别语境「表 1」时锚定紧邻类别词
        // 的编号，枚举数字不参与。
        let text = "The pipeline has three stages; see Table 1.";
        let at = text.find("Table 1").unwrap();
        let (para, ir, doc, parsed, placed) = ref_case(
            "P01-018",
            text,
            &[(at, at + "Table 1".len(), "table.1")],
            "三个阶段（见表 1）：(1)第一步；(2)第二步。",
        );
        let target = prepare(&para, &ir, &doc, &parsed).expect("编号锚定在类别词之后");
        let ann = annotated_texts(&target);
        assert_eq!(ann.len(), 1);
        assert_eq!(ann[0].0, placed[0].0);
        assert_eq!(ann[0].1, "1");
    }

    #[test]
    fn unrecognized_category_word_falls_back_even_with_exact_count() {
        // 译文编号前是不属于任何类别词表的普通词（fake:cjk 的随机汉字）：
        // 数量恰等也不选——类别语境不可辨认就回退。
        let text = "The pipeline has three stages; see Table 1.";
        let at = text.find("Table 1").unwrap();
        let (para, ir, doc, parsed, _) = ref_case(
            "P01-025",
            text,
            &[(at, at + "Table 1".len(), "table.1")],
            "三个阶段（汉词 1）：(1)第一步；(2)第二步。",
        );
        assert!(
            prepare(&para, &ir, &doc, &parsed).is_none(),
            "类别词不可辨认时数量恰等也必须回退"
        );
    }

    #[test]
    fn conflicting_destination_category_suppresses_decomposition() {
        // 标签类别（Table）与 dest 命名空间（figure.）冲突：不猜，回退。
        let text = "See Table 15 for details.";
        let at = text.find("Table 15").unwrap();
        let (para, ir, doc, parsed, _) = ref_case(
            "P01-019",
            text,
            &[(at, at + "Table 15".len(), "figure.6")],
            "见表 15 的细节。",
        );
        assert!(
            prepare(&para, &ir, &doc, &parsed).is_none(),
            "类别来源冲突必须回退"
        );
    }

    #[test]
    fn isolated_same_number_with_exact_count_falls_back() {
        // 反例：译文只有孤立的「15 项特征」，编号数量与链接数恰好相等，
        // 但编号前没有类别词——数量巧合不构成锚点身份，回退。
        let text = "See Table 15 for details.";
        let at = text.find("Table 15").unwrap();
        let (para, ir, doc, parsed, _) = ref_case(
            "P01-020",
            text,
            &[(at, at + "Table 15".len(), "table.15")],
            "这里有 15 项特征。",
        );
        assert!(
            prepare(&para, &ir, &doc, &parsed).is_none(),
            "无类别语境的孤立编号即使数量恰等也必须回退"
        );
    }

    #[test]
    fn section_sign_letter_without_literal_match_falls_back() {
        // "§G"：链接只盖住字母。真实译文原样保留 §+字母时走整串字面路径；
        // 字母被改写而字面消失时没有可辨认的编号结构，fail-closed 回退。
        let text = "Details appear in appendix(see§G) here.";
        let g = text.char_indices().position(|(_, c)| c == 'G').unwrap();
        let (para, ir, doc, parsed, _) = ref_case(
            "P01-021",
            text,
            &[(g, g + 1, "appendix.G")],
            "细节见(果§深)。",
        );
        assert!(
            prepare(&para, &ir, &doc, &parsed).is_none(),
            "§ 字母被改写且无字面匹配时必须回退"
        );
    }

    #[test]
    fn same_tail_without_recognizable_category_words_falls_back() {
        // 同段 "Table 4" 与 "Figure 4" 共用编号 4，译文类别词不可辨认：
        // 按阅读顺序配对等于猜测（语序一换就配错目标），回退。
        let text = "Compare Table 4 with Figure 4 here.";
        let t4 = text.find("Table 4").unwrap();
        let f4 = text.find("Figure 4").unwrap();
        let (para, ir, doc, parsed, _) = ref_case(
            "P01-022",
            text,
            &[
                (t4, t4 + "Table 4".len(), "table.4"),
                (f4, f4 + "Figure 4".len(), "figure.4"),
            ],
            "比较 某某 4 与 另某 4 这里。",
        );
        assert!(
            prepare(&para, &ir, &doc, &parsed).is_none(),
            "类别词不可辨认时不得按顺序猜测配对"
        );
    }

    #[test]
    fn category_word_glued_to_the_number_relocates() {
        // 真实模型常不写空格：「见表15」。类别语境只看编号前是否紧邻本类别词，
        // 与空格无关，必须仍然重定位。
        for (source, label, dest, translated, anchored) in [
            (
                "See Table 15 for details.",
                "Table 15",
                "table.15",
                "见表15的细节。",
                "15",
            ),
            (
                "in Section 4.2",
                "Section 4.2",
                "section.4.2",
                "在第4.2节",
                "4.2",
            ),
        ] {
            let at = source.find(label).unwrap();
            let (para, ir, doc, parsed, placed) = ref_case(
                "P01-023",
                source,
                &[(at, at + label.len(), dest)],
                translated,
            );
            let target = prepare(&para, &ir, &doc, &parsed)
                .unwrap_or_else(|| panic!("{label} 无空格写法必须能重定位"));
            let ann = annotated_texts(&target);
            assert_eq!(ann.len(), 1);
            assert_eq!(ann[0].0, placed[0].0);
            assert_eq!(ann[0].1, anchored);
        }
    }

    #[test]
    fn swapped_category_references_pair_by_their_own_category() {
        // 同段 "Table 4" 与 "Figure 4" 共用编号 4，译文语序互换：
        // 每个编号由自己紧邻的类别词认领，各配自己的目的地。
        let text = "Compare Table 4 with Figure 4 here.";
        let t4 = text.find("Table 4").unwrap();
        let f4 = text.find("Figure 4").unwrap();
        let (para, ir, doc, parsed, placed) = ref_case(
            "P01-024",
            text,
            &[
                (t4, t4 + "Table 4".len(), "table.4"),
                (f4, f4 + "Figure 4".len(), "figure.4"),
            ],
            "比较图 4 与表 4 这里。",
        );
        let target = prepare(&para, &ir, &doc, &parsed).expect("按各自类别配对");
        let ann = annotated_texts(&target);
        assert_eq!(ann.len(), 2);
        // 按段落阅读顺序排列：译文里「图 4」在前、「表 4」在后，因此
        // figure 的目标必须排在 table 的目标之前，两个编号各自认领。
        assert_eq!(
            ann.iter().map(|(o, _)| *o).collect::<Vec<_>>(),
            vec![placed[1].0, placed[0].0],
            "「图 4」的链接必须锚在译文中更靠前的「4」上"
        );
        assert!(ann.iter().all(|(_, t)| t == "4"));
    }
}
