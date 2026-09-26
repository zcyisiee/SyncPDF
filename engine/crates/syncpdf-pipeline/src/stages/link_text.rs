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
//! otherwise the link is not placed.
//!
//! An unplaceable link does not cost the paragraph its translation: `prepare`
//! first tries every owned link strictly; if that fails, it keeps each link
//! that places on its own and still co-places, and records the rest in
//! `Target::dropped` with a reason (label absent, repeated, or in conflict).
//! A dropped link is hidden (annotation flag Hidden) rather than removed, so
//! the page's annotation count is unchanged and nothing points at wrong text.
//!
//! The label is the exact text of the link's own glyphs — whitespace trimmed,
//! punctuation kept ("(1)", "2018)", "Jeong et al.,") for the literal match;
//! the category/number structure (`decompose`) ignores surrounding punctuation.
//! Appendix-numbered tails ("A1", "B.2", "S3") decompose like numeric tails.
//! A line-wrapped citation split into several same-destination links is one
//! label: fragments contiguous in reading order (joined across whitespace
//! only) match their merged literal once, and the anchor is split between
//! them. A repeated label — bare or merged — pairs by source-occurrence
//! ordinal when it occurs equally often in source and translation and every
//! annotation covers a distinct occurrence; count mismatches and duplicate
//! claims on one occurrence keep falling back. A line-end hyphen at a
//! fragment boundary whose continuation starts lowercase is a typesetting
//! artifact of the wrap; the de-hyphenated spelling is a fallback label,
//! tried only after the joined label itself fails to place.
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
use std::collections::{BTreeMap, BTreeSet};
use syncpdf_core::ir::{Glyph, PageIR, Paragraph, TypesetParagraph};
use syncpdf_core::{AtomId, Rect, StyleId};
use syncpdf_translate::{ParsedUnit, Segment};
use syncpdf_typeset::Shaper;

#[derive(Debug, Clone)]
pub(crate) struct Target {
    pub para: Paragraph,
    pub parsed: ParsedUnit,
    /// Model-space HTML (`{{KEEP_n}}` intact): what editors show and what a
    /// manual override is validated against.
    pub html: String,
    links: Vec<(ObjectId, Vec<StyleId>)>,
    atom_links: Vec<(ObjectId, AtomId, Rect)>,
    /// Block-level anchors: clickable over the whole translated paragraph.
    whole: Vec<ObjectId>,
    /// Links that could not be placed uniquely in the translation: hidden in
    /// the output (the page keeps its annotation count), each with its reason.
    pub dropped: Vec<(ObjectId, String)>,
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
    /// with an enumeration list ("表 18、19、20") or an appendix letter prefix
    /// ("表 A1") in between.
    fn precedes_number(self, prefix: &str) -> bool {
        regex::Regex::new(&format!(
            r"{}\s*\(?\s*(?:[A-Za-z]{{1,2}}\s*)?(?:[0-9.]+\s*[,、和及]\s*(?:and\s*)?)*$",
            self.word_pattern()
        ))
        .expect("category prefix regex")
        .is_match(prefix)
    }
}

/// Split a bare label into cross-reference structure: a leading category word
/// plus a numeric ("15", "4.2"), single-letter ("B"), or appendix-numbered
/// ("A1", "B.2", "S3") tail — a letter prefix followed by digits/dots. Labels
/// without that structure (pure numbers, footnotes, "§I", stray "1A") keep the
/// literal-only path.
fn decompose(label: &str) -> Option<(RefKind, &str)> {
    // Structure ignores the link's surrounding punctuation ("(Fig. 3)",
    // "Section 3.", "Table 2,"); the literal path keeps it.
    let label = label.trim_matches(|c: char| c.is_whitespace() || "[](),.;:".contains(c));
    let word_end = label
        .find(|c: char| !c.is_ascii_alphabetic() && c != '.')
        .unwrap_or(label.len());
    let kind = RefKind::from_label_word(&label[..word_end])?;
    let tail = label[word_end..].trim_start();
    let digits = |t: &str| {
        t.chars().any(|c| c.is_ascii_digit())
            && t.chars().all(|c| c.is_ascii_digit() || c == '.')
            && !t.starts_with('.')
            && !t.ends_with('.')
    };
    let numeric = digits(tail);
    let letter = tail.len() == 1 && tail.chars().all(|c| c.is_ascii_alphabetic());
    // Appendix/supplementary numbering: one or two uppercase letters then a
    // digit/dot tail ("A1", "B.2"). Trailing letters ("1A") or repeated dots
    // ("A..1") are not that structure.
    let appendix = {
        let letters: String = tail
            .chars()
            .take_while(|c| c.is_ascii_uppercase())
            .collect();
        let rest = &tail[letters.len()..];
        !letters.is_empty()
            && letters.len() <= 2
            && rest.chars().any(|c| c.is_ascii_digit())
            && rest.chars().all(|c| c.is_ascii_digit() || c == '.')
            && !rest.ends_with('.')
            && !rest.contains("..")
    };
    (numeric || letter || appendix).then_some((kind, tail))
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

/// One line-wrapped label: its joined text, the de-hyphenated fallback for a
/// line-end wrap hyphen, the participating annotations, their fragment
/// weights, and the label's byte range in the source text (the identity its
/// source-occurrence ordinal is measured against).
struct MergedLabel {
    label: String,
    dehyphenated: Option<String>,
    ids: Vec<ObjectId>,
    destinations: BTreeSet<String>,
    weights: Vec<usize>,
    source_range: (usize, usize),
}

/// Repeated-label fallback: pair the k-th source occurrence of the label with
/// the k-th translated occurrence. Valid only when the label occurs exactly as
/// often in the translation as in the source and every annotation claims a
/// distinct source occurrence — the ordinal is evidenced by the annotation's
/// own source range, not by a count coincidence. A translation may reorder
/// clauses, so the pairing is only order-invariant when every annotation in
/// the group shares one destination: distinct destinations under reordering
/// would silently swap links, so they fail closed. Anything else returns None.
fn pair_by_occurrence(
    label: &str,
    group: &[(ObjectId, String, (usize, usize))],
    literal: &[(usize, usize)],
    source: &str,
) -> Option<Vec<((usize, usize), ObjectId)>> {
    if literal.is_empty() {
        return None;
    }
    // Order-invariance gate: one shared destination for the whole group.
    if group.windows(2).any(|w| w[0].1 != w[1].1) {
        return None;
    }
    let occurrences: Vec<_> = matches(source, label);
    if occurrences.len() != literal.len() {
        return None;
    }
    let mut claimed = vec![false; occurrences.len()];
    let mut out = Vec::with_capacity(group.len());
    for (id, _, range) in group {
        // The annotation's trimmed label range must be exactly one source
        // occurrence; a duplicate claim on the same occurrence is ambiguous.
        let ord = occurrences
            .iter()
            .position(|&(s, e)| (s, e) == *range)
            .filter(|&o| !claimed[o]);
        let ord = ord?;
        claimed[ord] = true;
        out.push((literal[ord], *id));
    }
    Some(out)
}

/// Partition a matched translated range between one merged label's
/// annotations by fragment weight. Each annotation needs at least one
/// character of the anchor, so an unsplittable range fails closed. Returns
/// whether the split succeeded (appending to `ranges` on success).
fn split_anchor(
    text: &str,
    (s, e): (usize, usize),
    ids: &[ObjectId],
    weights: &[usize],
    ranges: &mut Vec<(usize, usize, ObjectId)>,
) -> bool {
    let chars: Vec<(usize, usize)> = {
        let mut v = Vec::new();
        let mut a = s;
        for c in text[s..e].chars() {
            v.push((a, a + c.len_utf8()));
            a += c.len_utf8();
        }
        v
    };
    let total: usize = weights.iter().sum();
    let count = chars.len();
    // Each annotation needs at least one character of the anchor.
    if ids.len() > count || count < 1 {
        return false;
    }
    // Weighted split at char boundaries: each annotation gets its share of
    // the matched range, so the pieces cover it exactly. Bounds stay in
    // 0..count and strictly increase even when a fragment's weight exceeds
    // the whole matched range (a compressed translation).
    let mut bounds: Vec<usize> = Vec::with_capacity(ids.len() + 1);
    bounds.push(0);
    let mut acc = 0;
    for (k, weight) in weights.iter().enumerate() {
        acc += weight;
        if k + 1 == weights.len() {
            bounds.push(count);
        } else {
            let b = ((acc * count) / total).min(count - 1);
            bounds.push(b.max(bounds[k] + 1).min(count - 1));
        }
    }
    let mut cursor = e;
    for (k, id) in ids.iter().enumerate() {
        let start = chars[bounds[k]].0;
        let end = if k + 1 == ids.len() {
            e
        } else {
            chars[bounds[k + 1]].0
        };
        ranges.push((start, end, *id));
        cursor = end;
    }
    debug_assert_eq!(cursor, e);
    true
}

/// Keep the translation when a link cannot be placed: every link that places
/// on its own is kept, the rest are dropped with a recorded reason. Only
/// failures unrelated to link placement (atoms, parse invariants, non-Link
/// annotations over the text) still return None and keep the source.
pub(crate) fn prepare(
    para: &Paragraph,
    ir: &PageIR,
    doc: &Document,
    parsed: &ParsedUnit,
) -> Option<Target> {
    if let Some(target) = prepare_strict(para, ir, doc, parsed) {
        return Some(target);
    }
    let links = owned_links(para, ir, doc)?;
    let kept: BTreeSet<ObjectId> = links
        .iter()
        .filter(|(id, _)| {
            prepare_labeled(para, ir, doc, parsed, Some(&BTreeSet::from([*id]))).is_some()
        })
        .map(|(id, _)| *id)
        .collect();
    let mut target = prepare_labeled(para, ir, doc, parsed, Some(&kept))
        .or_else(|| prepare_labeled(para, ir, doc, parsed, Some(&BTreeSet::new())))?;
    let placed: BTreeSet<ObjectId> = target
        .links
        .iter()
        .map(|(id, _)| *id)
        .chain(target.atom_links.iter().map(|(id, _, _)| *id))
        .collect();
    let text = target.parsed.text();
    target.dropped = links
        .into_iter()
        .filter(|(id, _)| !placed.contains(id))
        .map(|(id, label)| {
            let reason = match matches(&text, &label).len() {
                0 => format!("「{label}」：译文中找不到链接文字"),
                1 => format!("「{label}」：与段内其他链接冲突，无法唯一定位"),
                n => format!("「{label}」：链接文字在译文中出现 {n} 次，无法唯一定位"),
            };
            (id, reason)
        })
        .collect();
    Some(target)
}

/// All links must place uniquely, else None (the fail-closed placement rules).
fn prepare_strict(
    para: &Paragraph,
    ir: &PageIR,
    doc: &Document,
    parsed: &ParsedUnit,
) -> Option<Target> {
    prepare_labeled(para, ir, doc, parsed, None)
        .or_else(|| prepare_whole_block(para, ir, doc, parsed))
}

/// The Link annotations covering this paragraph's glyphs, with their source
/// label (owned glyph text, whitespace trimmed).
fn owned_links(para: &Paragraph, ir: &PageIR, doc: &Document) -> Option<Vec<(ObjectId, String)>> {
    let page = *doc.get_pages().get(&para.id.page)?;
    let page = doc.get_dictionary(page).ok()?;
    let Ok(annots) = page.get(b"Annots") else {
        return Some(vec![]);
    };
    let glyphs: BTreeMap<_, _> = ir.glyphs().map(|g| (g.id, g)).collect();
    let mut out = Vec::new();
    for o in object(doc, annots)?.as_array().ok()? {
        let Ok(id) = o.as_reference() else { continue };
        let Some(d) = object(doc, o).and_then(|o| o.as_dict().ok()) else {
            continue;
        };
        if d.get(b"Subtype").ok().and_then(|s| s.as_name().ok()) != Some(b"Link") {
            continue;
        }
        let Some(r) = d.get(b"Rect").ok().and_then(|r| rect(doc, r)) else {
            continue;
        };
        let label: String = para
            .glyphs
            .iter()
            .filter_map(|id| glyphs.get(id))
            .filter(|g| r.contains(g.bbox.center()))
            .flat_map(|g| g.unicode.iter())
            .collect();
        let label = label.trim();
        if !label.is_empty() {
            out.push((id, label.to_string()));
        }
    }
    Some(out)
}

/// Relocate links by matching a literal source label inside the translation.
/// `only` restricts relocation to the given links; the others are ignored
/// here and reported as dropped by `prepare`.
fn prepare_labeled(
    para: &Paragraph,
    ir: &PageIR,
    doc: &Document,
    parsed: &ParsedUnit,
    only: Option<&BTreeSet<ObjectId>>,
) -> Option<Target> {
    let resolved = super::text_atoms::resolve_text(para, parsed)?;
    let mut target = Target {
        para: para.clone(),
        html: parsed.to_html(),
        parsed: resolved,
        links: vec![],
        atom_links: vec![],
        whole: vec![],
        dropped: vec![],
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
    // Bare-path annotations: (id, destination, owned glyph indices, label byte
    // range in `source`, label). A line-wrapped citation is split by the PDF
    // producer into adjacent Links to one destination; such fragments form one
    // run whose label is the joined text, not either ambiguous fragment.
    #[allow(clippy::type_complexity)]
    let mut annotations: Vec<(ObjectId, String, Vec<u32>, (usize, usize), String)> = Vec::new();
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
        let id = o.as_reference().ok()?;
        if only.is_some_and(|only| !only.contains(&id)) {
            continue;
        }
        // A shared annotation must not be rewritten by two paragraph owners.
        if ir.glyphs().any(|g| {
            r.contains(g.bbox.center())
                && !g.unicode.iter().all(|c| c.is_whitespace())
                && !para.glyphs.contains(&g.id)
        }) {
            return None;
        }
        let selected: Vec<_> = spans
            .iter()
            .filter(|(_, _, (s, e))| owned.iter().any(|i| s <= i && i < e))
            .collect();
        let start = selected.first()?.0;
        let end = selected.last()?.1;
        // The label is the exact text of the link's own glyphs: trim
        // whitespace only, keep punctuation ("(1)", "2018)", "Jeong et al.,").
        // A paren-stripped form would be ambiguous whenever the same bare
        // number occurs elsewhere in the paragraph.
        let label = source[start..end].trim_matches(|c: char| c.is_whitespace());
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
            annotations.push((id, destination, owned, (start, end), label.into()));
        }
    }
    // Partition same-destination annotations into runs: fragments whose owned
    // glyph indices are contiguous in reading order (adjacent or joined only
    // by separator spans) are one line-wrapped label. Others are rival
    // occurrences and keep the fail-closed literal path.
    let mut runs: Vec<Vec<usize>> = Vec::new();
    {
        let mut by_dest: BTreeMap<&str, Vec<usize>> = BTreeMap::new();
        for (i, (_, destination, _, _, _)) in annotations.iter().enumerate() {
            by_dest.entry(destination).or_default().push(i);
        }
        for (_, mut indices) in by_dest {
            if indices.len() < 2 {
                runs.push(indices);
                continue;
            }
            indices.sort_by_key(|&i| annotations[i].2.first().copied().unwrap_or(u32::MAX));
            let mut run: Vec<usize> = Vec::new();
            for &i in &indices {
                let adjacent = run.last().is_some_and(|&j| {
                    // The glyphs between the two fragments must all be
                    // whitespace (the line-wrap separator): anything with ink
                    // between them means these are not one wrapped label.
                    let prev_last = annotations[j].2.last().copied().unwrap_or(u32::MAX);
                    let next_first = annotations[i].2.first().copied().unwrap_or(u32::MAX);
                    if next_first <= prev_last {
                        return false;
                    }
                    (prev_last + 1..next_first).all(|k| {
                        para.glyphs
                            .get(k as usize)
                            .and_then(|id| glyphs.get(id))
                            .is_some_and(|g| {
                                !g.unicode.is_empty()
                                    && g.unicode.iter().all(|c: &char| c.is_whitespace())
                            })
                    })
                });
                if adjacent {
                    run.push(i);
                } else {
                    if !run.is_empty() {
                        runs.push(std::mem::take(&mut run));
                    }
                    run.push(i);
                }
            }
            runs.push(run);
        }
    }
    /// A bare label's annotation: object id, destination, trimmed source range.
    type BareAnnotation = (ObjectId, String, (usize, usize));
    let mut bare: BTreeMap<String, Vec<BareAnnotation>> = BTreeMap::new();
    let mut merged_labels: Vec<MergedLabel> = Vec::new();
    for run in &runs {
        if run.len() == 1 {
            let (id, destination, _, range, label) = &annotations[run[0]];
            bare.entry(label.clone())
                .or_default()
                .push((*id, destination.clone(), *range));
            continue;
        }
        // One label: the joined text of every fragment's owned spans.
        let first = run.iter().map(|&i| annotations[i].3 .0).min().unwrap();
        let last = run.iter().map(|&i| annotations[i].3 .1).max().unwrap();
        let joined = &source[first..last];
        let label = joined.trim_matches(|c: char| c.is_whitespace());
        if label.is_empty() {
            return None;
        }
        // A line-end hyphen at a fragment boundary whose continuation starts
        // lowercase is a wrap artifact ("Sac-" + "ramento"): the de-hyphenated
        // spelling is the label's fallback form. Uppercase continuations are
        // real compound words ("X-" + "Ray") and keep the hyphen.
        let mut dehyphenated: Option<String> = None;
        let mut removed = 0usize;
        for w in run.windows(2) {
            let a_end = annotations[w[0]].3 .1;
            let next = &source[annotations[w[1]].3 .0..annotations[w[1]].3 .1];
            let wraps_word = next.chars().next().is_some_and(|c| c.is_ascii_lowercase())
                && a_end > first
                && source.as_bytes()[a_end - 1] == b'-';
            if wraps_word {
                // '-' is ASCII, so the byte position is a char boundary.
                let pos = a_end - 1 - first - removed;
                dehyphenated
                    .get_or_insert_with(|| joined.to_string())
                    .remove(pos);
                removed += 1;
            }
        }
        let dehyphenated = dehyphenated
            .map(|v| v.trim_matches(|c: char| c.is_whitespace()).to_string())
            .filter(|v| !v.is_empty() && v != label);
        // The label's own byte range in the source: the identity its
        // source-occurrence ordinal is measured against.
        let lead = joined.find(label)?;
        let source_range = (first + lead, first + lead + label.len());
        let ids: Vec<ObjectId> = run.iter().map(|&i| annotations[i].0).collect();
        let destinations: BTreeSet<String> =
            run.iter().map(|&i| annotations[i].1.clone()).collect();
        // Fragment weights for partitioning the matched translated range.
        let weights: Vec<usize> = run
            .iter()
            .map(|&i| annotations[i].4.chars().count())
            .collect();
        merged_labels.push(MergedLabel {
            label: label.to_string(),
            dehyphenated,
            ids,
            destinations,
            weights,
            source_range,
        });
    }
    for (label, group) in bare {
        let literal: Vec<_> = matches(&text, &label)
            .into_iter()
            .filter(|(s, e)| !atoms.values().any(|(a, b)| *s < *b && *a < *e))
            .collect();
        let mut destinations: BTreeMap<String, Vec<ObjectId>> = BTreeMap::new();
        for (id, destination, _) in &group {
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
                    .all(|(_, dest, _)| RefKind::from_dest(dest).is_none_or(|d| d == *kind))
            });
            if let Some((kind, number)) = decomposed {
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
                    select_by_destination(&all, &text, &destinations, multiple, true, |_| {
                        Some(kind)
                    });
            }
        }
        if selected.is_none() {
            // A repeated bare label pairs by source-occurrence ordinal: the
            // label occurs exactly as often in the translation as in the
            // source, and every annotation covers a distinct source
            // occurrence — the k-th source occurrence anchors the k-th
            // translated occurrence. Count mismatches and duplicate claims on
            // one occurrence keep failing closed.
            selected = pair_by_occurrence(&label, &group, &literal, &source);
        }
        ranges.extend(selected?.into_iter().map(|((s, e), id)| (s, e, id)));
    }
    // Place each merged label and partition its matched range between the
    // label's annotations by fragment weight: `tag` styles only the first
    // active range at any cursor, so annotations that share a range would
    // silently lose their anchors. A merged label that is unique in the
    // translation anchors there (a whole citation matching twice is
    // ambiguous); a repeated one pairs by source-occurrence ordinal like the
    // bare path, and a line-end hyphen falls back to the de-hyphenated
    // spelling when the joined label itself fails to place.
    let mut merged_groups: BTreeMap<String, Vec<usize>> = BTreeMap::new();
    for (i, m) in merged_labels.iter().enumerate() {
        merged_groups.entry(m.label.clone()).or_default().push(i);
    }
    for (label, idxs) in merged_groups {
        let literal: Vec<_> = matches(&text, &label)
            .into_iter()
            .filter(|(s, e)| !atoms.values().any(|(a, b)| *s < *b && *a < *e))
            .collect();
        let mut anchors: Option<Vec<(usize, usize)>> = None;
        if idxs.len() == 1 && literal.len() == 1 {
            anchors = Some(vec![literal[0]]);
        }
        if anchors.is_none() && idxs.len() == 1 {
            if let Some(v) = merged_labels[idxs[0]].dehyphenated.clone() {
                let dehyphenated: Vec<_> = matches(&text, &v)
                    .into_iter()
                    .filter(|(s, e)| !atoms.values().any(|(a, b)| *s < *b && *a < *e))
                    .collect();
                if dehyphenated.len() == 1 {
                    anchors = Some(vec![dehyphenated[0]]);
                }
            }
        }
        if anchors.is_none() {
            // Repeated merged label: pair by source-occurrence ordinal. The
            // label occurs equally often in source and translation and every
            // merged entry covers a distinct source occurrence; anything else
            // stays ambiguous and fails closed. Reordering safety mirrors the
            // bare path: every merged entry must carry the same destination
            // set, else a reordered translation would swap links.
            let occurrences: Vec<_> = matches(&source, &label);
            let same_destinations = idxs
                .windows(2)
                .all(|w| merged_labels[w[0]].destinations == merged_labels[w[1]].destinations);
            if same_destinations && !literal.is_empty() && literal.len() == occurrences.len() {
                let mut claimed = vec![false; occurrences.len()];
                let mut per_entry = Vec::with_capacity(idxs.len());
                for &i in &idxs {
                    let ord = occurrences
                        .iter()
                        .position(|&(s, e)| (s, e) == merged_labels[i].source_range)
                        .filter(|&o| !claimed[o]);
                    if let Some(o) = ord {
                        claimed[o] = true;
                        per_entry.push(Some(literal[o]));
                    } else {
                        per_entry.push(None);
                    }
                }
                if per_entry.iter().all(|a| a.is_some()) {
                    anchors = Some(per_entry.into_iter().map(|a| a.unwrap()).collect());
                }
            }
        }
        let anchors = anchors?;
        for (&i, &(s, e)) in idxs.iter().zip(&anchors) {
            let m = &merged_labels[i];
            if !split_anchor(&text, (s, e), &m.ids, &m.weights, &mut ranges) {
                return None;
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
        html: parsed.to_html(),
        parsed: resolved,
        links: vec![],
        atom_links: vec![],
        whole: vec![],
        dropped: vec![],
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

/// Hide dropped links (annotation flag bit 2, Hidden): no ink, no click, and
/// the page keeps its annotation count.
pub(crate) fn hide_dropped(
    doc: &mut Document,
    target: &Target,
) -> Result<(), super::PipelineError> {
    for (id, _) in &target.dropped {
        let d = doc
            .get_object_mut(*id)
            .and_then(Object::as_dict_mut)
            .map_err(|e| super::PipelineError::Validation(format!("link hide: {e}")))?;
        let flags = d.get(b"F").ok().and_then(|f| f.as_i64().ok()).unwrap_or(0);
        d.set("F", flags | 2);
    }
    Ok(())
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
        // 附录/补充材料的编号结构：字母前缀 + 数字/点尾（A1、B.2、S3）。
        assert_eq!(decompose("Fig. A1"), Some((RefKind::Figure, "A1")));
        assert_eq!(decompose("Table B.2"), Some((RefKind::Table, "B.2")));
        assert_eq!(decompose("Appendix S3"), Some((RefKind::Appendix, "S3")));
        assert_eq!(decompose("(Fig. 3)"), Some((RefKind::Figure, "3")));
        assert_eq!(decompose("Section 3."), Some((RefKind::Section, "3")));
        assert_eq!(decompose("Table A1,"), Some((RefKind::Table, "A1")));
        // 无类别结构的标签不分解，保持纯字面路径。
        for bare in [
            "15",
            "8",
            "B",
            "§I",
            "NarraBench",
            "A1",
            "1A",
            "A.1.",
            "A..1",
        ] {
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
                rise: 0.0,
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
            dropped: vec![],
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
                rise: 0.0,
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
        let target = prepare_strict(&para, &ir, &doc, &parsed).expect("whole-block anchor");
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
            prepare_strict(&para, &ir, &doc, &parsed).is_none(),
            "部分拥有必须 fail closed"
        );
    }

    #[test]
    fn rival_link_destinations_are_rejected_for_the_whole_block() {
        // 两个 Link 都覆盖整块：目的地冲突，必须拒绝。
        let all: Vec<u16> = (0.."Introduction".len() as u16).collect();
        let (para, ir, doc, parsed) = toc_case(&all, true);
        assert!(
            prepare_strict(&para, &ir, &doc, &parsed).is_none(),
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
            prepare_strict(&para, &ir, &doc, &parsed).is_none(),
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
                rise: 0.0,
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
        let target = prepare_strict(&para, &ir, &doc, &parsed).expect("按类别+编号重定位");
        assert!(target.whole.is_empty());
        let ann = annotated_texts(&target);
        assert_eq!(ann.len(), 1);
        assert_eq!(ann[0].0, placed[0].0);
        assert_eq!(ann[0].1, "15");
    }

    #[test]
    fn unplaceable_link_is_dropped_with_reason_and_others_kept() {
        // 译文漏掉 Table 15：严格放回失败；放宽后保留 Table 14 的锚点，
        // Table 15 记入 dropped 并带原因，而不是整段回退。
        let text = "Compare Table 14 and Table 15.";
        let links: Vec<_> = [("Table 14", "table.14"), ("Table 15", "table.15")]
            .iter()
            .map(|(w, d)| {
                let at = text.find(w).unwrap();
                (at, at + w.len(), *d)
            })
            .collect();
        let (para, ir, doc, parsed, placed) =
            ref_case("P01-013", text, &links, "比较表 14 与附表。");
        assert!(prepare_strict(&para, &ir, &doc, &parsed).is_none());
        let target = prepare(&para, &ir, &doc, &parsed).expect("保留译文");
        let ann = annotated_texts(&target);
        assert_eq!(ann.len(), 1);
        assert_eq!(ann[0].0, placed[0].0);
        assert_eq!(target.dropped.len(), 1);
        assert_eq!(target.dropped[0].0, placed[1].0);
        assert!(
            target.dropped[0].1.contains("找不到"),
            "{}",
            target.dropped[0].1
        );
    }

    #[test]
    fn placeable_links_drop_nothing() {
        // 反例：全部可放回时放宽路径不得丢任何链接。
        let text = "See Table 15 for details.";
        let at = text.find("Table 15").unwrap();
        let (para, ir, doc, parsed, _) = ref_case(
            "P01-014",
            text,
            &[(at, at + "Table 15".len(), "table.15")],
            "见表 15 的细节。",
        );
        let target = prepare(&para, &ir, &doc, &parsed).unwrap();
        assert!(target.dropped.is_empty());
        assert_eq!(annotated_texts(&target).len(), 1);
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
        let target = prepare_strict(&para, &ir, &doc, &parsed).expect("三链接按目标配对");
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
            let target = prepare_strict(&para, &ir, &doc, &parsed)
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
            prepare_strict(&para, &ir, &doc, &parsed).is_none(),
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
            prepare_strict(&para, &ir, &doc, &parsed).is_none(),
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
            prepare_strict(&para, &ir, &doc, &parsed).is_none(),
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
        let target = prepare_strict(&para, &ir, &doc, &parsed).expect("字面在时走原路径");
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
        let target = prepare_strict(&para, &ir, &doc, &parsed).expect("编号锚定在类别词之后");
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
            prepare_strict(&para, &ir, &doc, &parsed).is_none(),
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
            prepare_strict(&para, &ir, &doc, &parsed).is_none(),
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
            prepare_strict(&para, &ir, &doc, &parsed).is_none(),
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
            prepare_strict(&para, &ir, &doc, &parsed).is_none(),
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
            prepare_strict(&para, &ir, &doc, &parsed).is_none(),
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
            let target = prepare_strict(&para, &ir, &doc, &parsed)
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
        let target = prepare_strict(&para, &ir, &doc, &parsed).expect("按各自类别配对");
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

    // ── 附录字母数字尾（Fig. A1 → 「图 A1」） ─────────────────────────

    #[test]
    fn appendix_letter_digit_tails_relocate() {
        // 附录编号是「字母前缀 + 数字/点」的结构（A1、B.2）；类别词被译掉后
        // 尾部必须仍能按类别+编号重定位。非 hyperref 目的地（f0075 这类），
        // 类别证据只来自源标签自身的类别词。
        for (source, label, dest, translated, anchored) in [
            (
                "as shown in Fig. A1",
                "Fig. A1",
                "f0075",
                "如图 A1 所示",
                "A1",
            ),
            ("see Table B.2", "Table B.2", "t0045", "见表 B.2。", "B.2"),
            (
                "in Appendix S3",
                "Appendix S3",
                "sec.S3",
                "在附录 S3 中",
                "S3",
            ),
        ] {
            let at = source.find(label).unwrap();
            let (para, ir, doc, parsed, placed) = ref_case(
                "P01-026",
                source,
                &[(at, at + label.len(), dest)],
                translated,
            );
            let target = prepare_strict(&para, &ir, &doc, &parsed)
                .unwrap_or_else(|| panic!("{label} 必须能按类别+字母数字尾重定位"));
            let ann = annotated_texts(&target);
            assert_eq!(ann.len(), 1);
            assert_eq!(ann[0].0, placed[0].0);
            assert_eq!(ann[0].1, anchored);
        }
    }

    #[test]
    fn stray_letter_digit_tail_without_category_falls_back() {
        // 译文里只有孤立 A1（无类别词）且数量恰等：不得因数量巧合认领。
        let text = "as shown in Fig. A1";
        let at = text.find("Fig. A1").unwrap();
        let (para, ir, doc, parsed, _) = ref_case(
            "P01-027",
            text,
            &[(at, at + "Fig. A1".len(), "f0075")],
            "特征 A1 与模式 A1 的对比。",
        );
        assert!(
            prepare_strict(&para, &ir, &doc, &parsed).is_none(),
            "无类别语境的孤立字母数字尾即使数量恰等也必须回退"
        );
    }

    #[test]
    fn mismatched_category_for_letter_digit_tail_falls_back() {
        // 源标签是 Fig. A1，译文却写成「表 A1」：类别不符，不得借用。
        let text = "as shown in Fig. A1";
        let at = text.find("Fig. A1").unwrap();
        let (para, ir, doc, parsed, _) = ref_case(
            "P01-028",
            text,
            &[(at, at + "Fig. A1".len(), "f0075")],
            "如表 A1 所示。",
        );
        assert!(
            prepare_strict(&para, &ir, &doc, &parsed).is_none(),
            "类别词与源类别不符必须回退"
        );
    }

    // ── 标签取链接自身字形的精确文本（保留括号等标点） ─────────────────

    #[test]
    fn owned_punctuation_label_relocates() {
        // 链接只盖住 "(1)"：标签必须精确取被盖住字形的文本，括号不再被剥掉；
        // 译文「式(1)」中 "(1)" 唯一出现，字面路径即成立。
        let text = "the objective in (1) is minimized.";
        let at = text.find("(1)").unwrap();
        let (para, ir, doc, parsed, placed) = ref_case(
            "P01-029",
            text,
            &[(at, at + "(1)".len(), "e0005")],
            "式(1) 中的目标函数被最小化。",
        );
        let target = prepare_strict(&para, &ir, &doc, &parsed).expect("精确标签含括号");
        let ann = annotated_texts(&target);
        assert_eq!(ann.len(), 1);
        assert_eq!(ann[0].0, placed[0].0);
        assert_eq!(ann[0].1, "(1)");
    }

    #[test]
    fn owned_punctuation_label_ambiguous_falls_back() {
        // 同一精确标签 "(1)" 在译文中出现两次而链接只有一条：无法唯一定位，回退。
        let text = "the objective in (1) is minimized.";
        let at = text.find("(1)").unwrap();
        let (para, ir, doc, parsed, _) = ref_case(
            "P01-030",
            text,
            &[(at, at + "(1)".len(), "e0005")],
            "式(1) 与式(1) 一致，二者均为枚举。",
        );
        assert!(
            prepare_strict(&para, &ir, &doc, &parsed).is_none(),
            "精确标签多次出现且无类别语境时必须回退"
        );
    }

    // ── 跨行折断的同目的地链接合并（一条引文被折成两条注释） ─────────────

    #[test]
    fn contiguous_same_destination_links_merge() {
        // 一条 "Ropke and" + "Pisinger, 2006a)" 引文被行折断成两条同目的地
        // 注释；被盖住的字形在阅读顺序上连续（中间只有空白），合并后的
        // 完整标签在译文中逐字存在：两条注释都锚定到这一处。
        let text = "compare (Ropke and Pisinger, 2006a) here.";
        let a = text.find("Ropke and").unwrap();
        let b = a + "Ropke and".len();
        let c = b + " ".len();
        let d = text.find("2006a)").unwrap() + "2006a)".len();
        let (para, ir, doc, parsed, placed) = ref_case(
            "P01-031",
            text,
            &[(a, b, "b1170"), (c, d, "b1170")],
            "比较 (Ropke and Pisinger, 2006a) 这里。",
        );
        let target = prepare_strict(&para, &ir, &doc, &parsed).expect("折断引文按合并标签重定位");
        let ann = annotated_texts(&target);
        assert_eq!(ann.len(), 2, "两条注释都要有锚点");
        let mut ids: Vec<_> = ann.iter().map(|(o, _)| *o).collect();
        ids.sort();
        let mut wanted: Vec<_> = placed.iter().map(|(o, _)| *o).collect();
        wanted.sort();
        assert_eq!(ids, wanted, "两条注释都要有锚点");
        // 两个锚点共同覆盖合并标签的完整文本（按片段权重切分）。
        let joined: String = {
            let mut v: Vec<&(ObjectId, String)> = ann.iter().collect();
            v.sort_by_key(|(o, _)| placed.iter().position(|(p, _)| p == o).unwrap());
            v.iter().map(|(_, t)| t.as_str()).collect()
        };
        assert_eq!(joined, "Ropke and Pisinger, 2006a)");
    }

    #[test]
    fn noncontiguous_same_destination_links_do_not_merge() {
        // 同目的地两条链接之间隔着其他文字：不属于同一条折断引文，不合并；
        // 各自的标签在译文中无法唯一定位时必须回退。
        let text = "see Ropke and also 2006a again.";
        let a = text.find("Ropke and").unwrap();
        let b = a + "Ropke and".len();
        let c = text.find("2006a").unwrap();
        let (para, ir, doc, parsed, _) = ref_case(
            "P01-032",
            text,
            &[(a, b, "b1170"), (c, c + "2006a".len(), "b1170")],
            "参见 Ropke 和另见 2006a 再说。",
        );
        assert!(
            prepare_strict(&para, &ir, &doc, &parsed).is_none(),
            "不连续的同目的地链接不得合并"
        );
    }

    #[test]
    fn merged_label_absent_from_translation_falls_back() {
        // 折断引文合并后，其合并文本被译文改写（不再逐字存在）：回退。
        let text = "compare (Ropke and Pisinger, 2006a) here.";
        let a = text.find("Ropke and").unwrap();
        let b = a + "Ropke and".len();
        let c = b + " ".len();
        let d = text.find("2006a)").unwrap() + "2006a)".len();
        let (para, ir, doc, parsed, _) = ref_case(
            "P01-033",
            text,
            &[(a, b, "b1170"), (c, d, "b1170")],
            "比较 (Ropke 与 Pisinger，2006a) 这里。",
        );
        assert!(
            prepare_strict(&para, &ir, &doc, &parsed).is_none(),
            "合并标签不在译文中逐字存在时必须回退"
        );
    }

    // ── 同标签多次出现：按源文与译文的阅读顺序配对 ───────────────────

    /// 第 occurrence 条注释（按 `ref_case` 的 links 顺序）锚定的译文范围，
    /// 无法锚定时为 None。位置断言用：相同文本无法区分时看字节范围。
    fn anchored_range_at(
        target: &Target,
        placed: &[(ObjectId, String)],
        occurrence: usize,
    ) -> Option<(usize, usize)> {
        // Walk the tagged segments in the same order `tag` did and record each
        // styled span's byte range in the reconstructed translated text.
        let mut spans: Vec<(StyleId, usize, usize)> = Vec::new();
        fn walk(
            segs: &[Segment],
            style: Option<StyleId>,
            cursor: &mut usize,
            out: &mut Vec<(StyleId, usize, usize)>,
        ) {
            for s in segs {
                match s {
                    Segment::Text(t) => {
                        if let Some(id) = style {
                            let start = *cursor;
                            out.push((id, start, start + t.len()));
                        }
                        *cursor += t.len();
                    }
                    Segment::Style { id, inner } => walk(inner, Some(*id), cursor, out),
                    _ => {}
                }
            }
        }
        let mut cursor = 0;
        walk(&target.parsed.segments, None, &mut cursor, &mut spans);
        let (_ann, tags) = target
            .links
            .iter()
            .find(|(a, _)| placed[occurrence].0 == *a)?;
        let range = spans.iter().find(|(id, ..)| tags.contains(id))?;
        Some((range.1, range.2))
    }

    #[test]
    fn repeated_label_pairs_by_source_occurrence_ordinal() {
        // 同一引文标签在源文出现两次、译文也逐字出现两次：两条注释按各自
        // 覆盖的源文出现序号配对同序号的译文出现——序号证据来自注释覆盖
        // 的源文范围本身，不是数量巧合。
        let text = "First (Santini et al., 2018) then again (Santini et al., 2018) end.";
        let label = "Santini et al., 2018";
        let tr = "首次引用 (Santini et al., 2018) 之后又引用 (Santini et al., 2018) 结束。";
        let a1 = text.find(label).unwrap();
        let a2 = text.rfind(label).unwrap();
        let (para, ir, doc, parsed, placed) = ref_case(
            "P01-034",
            text,
            &[
                (a1, a1 + label.len(), "b1205"),
                (a2, a2 + label.len(), "b1205"),
            ],
            tr,
        );
        let target = prepare_strict(&para, &ir, &doc, &parsed).expect("同数出现按序号配对");
        let first = anchored_range_at(&target, &placed, 0).expect("第一条注释有锚点");
        assert_eq!(first.0, tr.find(label).unwrap(), "第一条注释锚定第一次出现");
        let second = anchored_range_at(&target, &placed, 1).expect("第二条注释有锚点");
        assert_eq!(
            second.0,
            tr.rfind(label).unwrap(),
            "第二条注释锚定第二次出现"
        );
    }

    #[test]
    fn repeated_label_count_mismatch_falls_back() {
        // 反例：源文出现两次、译文只出现一次：无法一一对应，回退。
        let text = "First (Santini et al., 2018) then again (Santini et al., 2018) end.";
        let label = "Santini et al., 2018";
        let a1 = text.find(label).unwrap();
        let a2 = text.rfind(label).unwrap();
        let (para, ir, doc, parsed, _) = ref_case(
            "P01-035",
            text,
            &[
                (a1, a1 + label.len(), "b1205"),
                (a2, a2 + label.len(), "b1205"),
            ],
            "首次引用 (Santini et al., 2018) 之后又引用一次，结束。",
        );
        assert!(
            prepare_strict(&para, &ir, &doc, &parsed).is_none(),
            "出现次数不一致时必须回退"
        );
    }

    #[test]
    fn repeated_label_duplicate_annotation_on_same_occurrence_falls_back() {
        // 反例：两条注释覆盖源文的同一次出现（重叠重复注，如 P01-007 的
        // 脚注星标）：字面出现数不等于注释数，序号配对中第二条注释无处
        // 认领，回退。
        let text = "First (Santini et al., 2018) then again (Santini et al., 2018) end.";
        let label = "Santini et al., 2018";
        let a1 = text.find(label).unwrap();
        let (para, ir, doc, parsed, _) = ref_case(
            "P01-036",
            text,
            &[
                (a1, a1 + label.len(), "b1205"),
                (a1, a1 + label.len(), "b1205"),
            ],
            "首次引用 (Santini et al., 2018)，结束。",
        );
        assert!(
            prepare_strict(&para, &ir, &doc, &parsed).is_none(),
            "同一源文出现上的重复注释无法按序号配对，必须回退"
        );
    }

    #[test]
    fn wrapped_label_repeated_pairs_by_occurrence_ordinal() {
        // 折断成两条同目的地注释的引文标签在源文出现两次（共 4 条注释、
        // 两个合并 run），译文逐字出现两次：每个 run 按其源文出现序号
        // 锚定同序号的译文出现，run 内仍按片段权重切分。
        let text = "A (Ropke and Pisinger, 2006a) and B (Ropke and Pisinger, 2006a) done.";
        let label = "Ropke and Pisinger, 2006a";
        let tr = "甲 (Ropke and Pisinger, 2006a) 与乙 (Ropke and Pisinger, 2006a) 完成。";
        let o1 = text.find(label).unwrap();
        let o2 = text.rfind(label).unwrap();
        let split = o1 + "Ropke and".len();
        let split2 = o2 + "Ropke and".len();
        let (para, ir, doc, parsed, placed) = ref_case(
            "P01-037",
            text,
            &[
                (o1, split, "b1170"),
                (split + 1, o1 + label.len(), "b1170"),
                (o2, split2, "b1170"),
                (split2 + 1, o2 + label.len(), "b1170"),
            ],
            tr,
        );
        let target = prepare_strict(&para, &ir, &doc, &parsed).expect("重复折断引文按序号配对");
        let r0 = anchored_range_at(&target, &placed, 0).expect("第一个链接有锚点");
        let w1 = tr.find(label).unwrap();
        assert!(
            r0.0 >= w1 && r0.0 < w1 + label.len(),
            "第一个 run 锚定译文的第一次出现"
        );
        let r2 = anchored_range_at(&target, &placed, 2).expect("第三个链接有锚点");
        let w2 = tr.rfind(label).unwrap();
        assert!(
            r2.0 >= w2 && r2.0 < w2 + label.len(),
            "第二个 run 锚定译文的第二次出现"
        );
    }

    #[test]
    fn wrapped_label_repeated_count_mismatch_falls_back() {
        // 反例：折断引文出现两次但译文只出现一次：回退。
        let text = "A (Ropke and Pisinger, 2006a) and B (Ropke and Pisinger, 2006a) done.";
        let label = "Ropke and Pisinger, 2006a";
        let o1 = text.find(label).unwrap();
        let o2 = text.rfind(label).unwrap();
        let split = o1 + "Ropke and".len();
        let split2 = o2 + "Ropke and".len();
        let (para, ir, doc, parsed, _) = ref_case(
            "P01-038",
            text,
            &[
                (o1, split, "b1170"),
                (split + 1, o1 + label.len(), "b1170"),
                (o2, split2, "b1170"),
                (split2 + 1, o2 + label.len(), "b1170"),
            ],
            "甲 (Ropke and Pisinger, 2006a) 与乙的引用完成。",
        );
        assert!(
            prepare_strict(&para, &ir, &doc, &parsed).is_none(),
            "折断引文出现数不一致时必须回退"
        );
    }

    // ── 片段边界上的软连字符（行末断词） ─────────────────────────────

    #[test]
    fn soft_hyphen_at_fragment_boundary_relocates() {
        // 行末断词："Sac-" + "ramento et al., 2019" 两片字节相邻，软连字符
        // 只是排版产物；译文里完整拼写 "Sacramento et al., 2019" 存在时，
        // 按去连字符变体重定位，两片注释仍分摊锚点。
        let text = "cited in (Sac-ramento et al., 2019) before.";
        let hyphen = text.find("Sac-").unwrap();
        let rest = hyphen + "Sac-".len();
        let end = rest + "ramento et al., 2019".len();
        let (para, ir, doc, parsed, placed) = ref_case(
            "P01-039",
            text,
            &[(hyphen, rest, "b0305"), (rest, end, "b0305")],
            "引用 (Sacramento et al., 2019) 于前文。",
        );
        let target =
            prepare_strict(&para, &ir, &doc, &parsed).expect("软连字符按去连字符变体重定位");
        let ann = annotated_texts(&target);
        assert_eq!(ann.len(), 2, "两条注释都要有锚点");
        let mut ids: Vec<_> = ann.iter().map(|(o, _)| *o).collect();
        ids.sort();
        let mut wanted: Vec<_> = placed.iter().map(|(o, _)| *o).collect();
        wanted.sort();
        assert_eq!(ids, wanted);
        let joined: String = {
            let mut v: Vec<&(ObjectId, String)> = ann.iter().collect();
            v.sort_by_key(|(o, _)| placed.iter().position(|(p, _)| p == o).unwrap());
            v.iter().map(|(_, t)| t.as_str()).collect()
        };
        assert_eq!(joined, "Sacramento et al., 2019");
    }

    #[test]
    fn hard_hyphenated_word_keeps_hyphen() {
        // 反例：片段边界的连字符后继片段以大写字母开头（真复合词的折断
        // 形态，如 "X-Ray" 折成 "X-" + "Ray"）：不构造去连字符变体，按原
        // 标签逐字匹配，失败则回退。
        let text = "cited in (Con-Tardo et al., 2012) before.";
        let hyphen = text.find("Con-").unwrap();
        let rest = hyphen + "Con-".len();
        let end = rest + "Tardo et al., 2012".len();
        let (para, ir, doc, parsed, _) = ref_case(
            "P01-040",
            text,
            &[(hyphen, rest, "b0310"), (rest, end, "b0310")],
            "引用 (ConTardo et al., 2012) 于前文。",
        );
        // 译文中既无逐字 "Con-Tardo"（字面路径失败）也无 "Contardo"
        // （大写后继不构造变体）：必须回退。
        assert!(
            prepare_strict(&para, &ir, &doc, &parsed).is_none(),
            "连字符后为大写字母时不得构造去连字符变体"
        );
    }
}
