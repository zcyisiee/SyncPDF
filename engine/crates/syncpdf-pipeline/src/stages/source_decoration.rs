//! Claim source underlines that belong to exactly one paragraph.
//!
//! A source underline is a plain `S` stroked two-point segment sitting just
//! below one paragraph's text line. Such ink is a *decoration of that text*, not
//! independent artwork: it must move with the words. Treating it as an obstacle
//! made fixed-size translations fail even though they fit, so it is claimed
//! here, marked as an underline style run, and later redrawn under the
//! translated glyphs while the source line is erased.
//!
//! Claiming is deliberately strict; anything ambiguous stays unclaimed and keeps
//! both its source ink and its original fallback behaviour. A wrong claim would
//! erase or move ink that still belongs to the page.
use syncpdf_core::ir::{
    DisplayItem, Glyph, PageIR, Paragraph, PathStroke, SourceDecoration, Translatable,
};
use syncpdf_core::{GlyphId, Rect};

#[cfg(test)]
#[path = "source_decoration_real.rs"]
mod source_decoration_real;

/// Vertical search window below a baseline, in em of the line's own glyph size.
/// Underlines sit close to their text; a wider window could claim the next line.
const BELOW_MIN: f32 = 0.02;
const BELOW_MAX: f32 = 0.6;
/// Horizontal slack for the line to cover its underlined words.
const X_TOL: f32 = 0.75;

/// Attribute source underlines to their owning paragraph.
///
/// The source IR keeps every path: a failed candidate must still see its own
/// source line, so nothing is removed globally here. Ownership is recorded on
/// the paragraph, and only the owner's own frame/refinement may set it aside.
///
/// Returns the number of attributed lines.
pub(crate) fn claim(ir: &PageIR, paras: &mut [Paragraph]) -> usize {
    let lines = ir
        .items
        .iter()
        .filter_map(|item| match item {
            DisplayItem::Path {
                bbox,
                is_stroke: true,
                is_fill: false,
                stroke: Some(stroke),
                ..
            } => Some((*bbox, *stroke)),
            _ => None,
        })
        // Formula-owned paint is already erased/replayed with its source atom.
        // A fraction bar can sit under a numerator like an underline, but must
        // never gain a second owner or impose a translated style anchor.
        .filter(|(b, _)| {
            !paras
                .iter()
                .flat_map(|p| &p.atoms)
                .filter_map(|a| a.source)
                .any(|a| {
                    a.bbox.x0 <= b.x0 && b.x1 <= a.bbox.x1 && a.bbox.y0 <= b.y0 && b.y1 <= a.bbox.y1
                })
        })
        .collect::<Vec<_>>();
    if lines.is_empty() {
        return 0;
    }
    let glyphs: Vec<&Glyph> = ir
        .glyphs()
        .filter(|g| !g.flags.invisible && !g.flags.outside_clip && !is_space(g))
        .collect();
    let mut claimed = 0;
    for &(bbox, stroke) in &lines {
        if let Some((index, range, baseline)) = owner(&glyphs, paras, bbox) {
            paras[index].decorations.push(SourceDecoration {
                stroke,
                bbox,
                glyph_range: range,
                offset: baseline - bbox.center().y,
            });
            claimed += 1;
        }
    }
    // TeX may paint an inter-word space as a separate stroke, overlapping the
    // two word strokes slightly. It has no glyph of its own. Claim it only as
    // a proven continuous bridge between two already-owned, same-pen strokes;
    // never expand word ownership just because a line is nearby.
    let primary: Vec<_> = paras.iter().map(|p| p.decorations.clone()).collect();
    for &(bbox, stroke) in &lines {
        if primary.iter().flatten().any(|d| d.stroke.op == stroke.op) {
            continue;
        }
        let mut owners = Vec::new();
        for (index, ds) in primary.iter().enumerate() {
            for left in ds {
                for right in ds {
                    if left.stroke.op == right.stroke.op
                        || left.glyph_range.1 != right.glyph_range.0
                        || left.bbox.x0 >= bbox.x0
                        || right.bbox.x1 <= bbox.x1
                        || (left.bbox.x1 - bbox.x0).abs() > X_TOL
                        || (right.bbox.x0 - bbox.x1).abs() > X_TOL
                        || (left.bbox.y0 - bbox.y0).abs() > 0.01
                        || (right.bbox.y0 - bbox.y0).abs() > 0.01
                    {
                        continue;
                    }
                    let d = SourceDecoration {
                        stroke,
                        bbox,
                        glyph_range: (left.glyph_range.0, right.glyph_range.1),
                        offset: left.offset,
                    };
                    if !same_pen(&left.stroke, left.offset, &d)
                        || !same_pen(&right.stroke, right.offset, &d)
                    {
                        continue;
                    }
                    let ids =
                        &paras[index].glyphs[d.glyph_range.0 as usize..d.glyph_range.1 as usize];
                    let baseline = bbox.y0 + d.offset;
                    if glyphs.iter().any(|g| {
                        let b = spread(g);
                        b.x1 > bbox.x0
                            && b.x0 < bbox.x1
                            && (g.matrix.f - baseline).abs() <= g.size * 0.25
                            && !ids.contains(&g.id)
                    }) {
                        continue;
                    }
                    owners.push((index, d));
                }
            }
        }
        if owners.len() == 1 {
            let (index, d) = owners.pop().unwrap();
            paras[index].decorations.push(d);
            claimed += 1;
        }
    }
    claimed
}

/// Source paint ops this paragraph owns, for frame/refinement to set aside.
pub(crate) fn owned_ops(para: &Paragraph) -> impl Iterator<Item = syncpdf_core::OpKey> + '_ {
    para.decorations.iter().map(|d| d.stroke.op)
}

/// Whether the delivered translation still carries the underline anchor.
///
/// A paragraph without claimed decoration is always fine. Otherwise the parsed
/// unit must retain nonblank target text for each distinct underlined source
/// range (internal link aliases count). Missing anchors keep the source line. This
/// is intentionally scoped to decorated candidates: R6 still allows free style
/// omission everywhere else, so it is not a global style validator.
pub(crate) fn anchored(para: &Paragraph, parsed: &syncpdf_translate::ParsedUnit) -> bool {
    if para.decorations.is_empty() {
        return true;
    }
    // Source-drawing atoms currently carry no target underline style. Never
    // erase their line merely because adjacent prose kept an anchor.
    if para.decorations.iter().any(|d| {
        para.atoms.iter().any(|a| {
            a.source.is_some()
                && a.glyph_range.0 < d.glyph_range.1
                && d.glyph_range.0 < a.glyph_range.1
        })
    }) {
        return false;
    }
    let underlined: Vec<_> = para.style_runs.iter().filter(|r| r.underline).collect();
    let mut used = std::collections::BTreeSet::new();
    used_ink_styles(&parsed.segments, None, &mut used);
    !underlined.is_empty()
        && underlined.iter().all(|run| {
            underlined
                .iter()
                .any(|alias| alias.glyph_range == run.glyph_range && used.contains(&alias.id))
        })
}

fn used_ink_styles(
    segs: &[syncpdf_translate::Segment],
    style: Option<syncpdf_core::StyleId>,
    used: &mut std::collections::BTreeSet<syncpdf_core::StyleId>,
) {
    for seg in segs {
        match seg {
            syncpdf_translate::Segment::Style { id, inner } => {
                used_ink_styles(inner, Some(*id), used)
            }
            syncpdf_translate::Segment::Text(text) if text.chars().any(|c| !c.is_whitespace()) => {
                if let Some(id) = style {
                    used.insert(id);
                }
            }
            _ => {}
        }
    }
}

/// Mark exactly the decorated glyph ranges as underlined.
///
/// A paragraph usually has one style run covering the whole sentence, so setting
/// a flag on that run would underline everything. Runs are therefore split at
/// decoration boundaries and only the overlapping pieces are marked. A decoration
/// with no ink evidence is dropped rather than guessed.
pub(crate) fn mark_styles(ir: &PageIR, paras: &mut [Paragraph]) {
    for para in paras.iter_mut() {
        let evidencd = para.decorations.iter().all(|d| {
            let (start, end) = d.glyph_range;
            para.glyphs
                .get(start as usize..end as usize)
                .is_some_and(|ids| ids.iter().any(|id| ink(ir, *id).is_some()))
        });
        let conflicting = para.decorations.iter().any(|a| {
            para.decorations.iter().any(|b| {
                a.glyph_range.0 < b.glyph_range.1
                    && b.glyph_range.0 < a.glyph_range.1
                    && !same_pen(&a.stroke, a.offset, b)
            })
        });
        if !evidencd || conflicting {
            para.decorations.clear();
            continue;
        }
        if para.decorations.is_empty() {
            continue;
        }
        let ranges = merged(para);
        para.style_runs = split_runs(&para.style_runs, &ranges);
    }
}

/// Decoration ranges to mark, merging neighbours that share one stroke style.
///
/// A word underline is often painted as several adjacent segments; splitting a
/// style run at every segment boundary would hand the model many spans that all
/// mean the same decoration, and one dropped span would then lose the line. Only
/// ranges whose stroke width, colour and offset agree are merged, so a
/// decoration set with different pens stays separate.
fn merged(para: &Paragraph) -> Vec<(u32, u32)> {
    let mut sorted: Vec<&SourceDecoration> = para.decorations.iter().collect();
    sorted.sort_by_key(|d| d.glyph_range);
    let mut out: Vec<(u32, u32, PathStroke, f32)> = Vec::new();
    for d in sorted {
        match out.last_mut() {
            Some((_, end, stroke, offset))
                if d.glyph_range.0 <= *end && same_pen(stroke, *offset, d) =>
            {
                *end = (*end).max(d.glyph_range.1);
            }
            _ => out.push((d.glyph_range.0, d.glyph_range.1, d.stroke, d.offset)),
        }
    }
    out.into_iter().map(|(a, b, _, _)| (a, b)).collect()
}

/// Adjacent segments merge only with the same pen and the same baseline distance.
fn same_pen(pen: &PathStroke, offset: f32, next: &SourceDecoration) -> bool {
    pen.color == next.stroke.color
        && (pen.width - next.stroke.width).abs() <= 0.01
        && (offset - next.offset).abs() <= 0.01
}

fn ink(ir: &PageIR, id: GlyphId) -> Option<Rect> {
    ir.glyphs().find(|g| g.id == id).and_then(|g| g.ink)
}

/// Split runs at every decoration boundary, marking only the covered pieces.
/// The first piece keeps its id so untouched spans keep their anchor identity.
fn split_runs(
    runs: &[syncpdf_core::ir::StyleRun],
    ranges: &[(u32, u32)],
) -> Vec<syncpdf_core::ir::StyleRun> {
    let mut out = Vec::new();
    let mut next = runs.iter().map(|r| r.id.0).max().unwrap_or(0) + 1;
    for run in runs {
        let (start, end) = run.glyph_range;
        let mut cuts: Vec<u32> = ranges
            .iter()
            .flat_map(|(a, b)| [*a, *b])
            .filter(|c| *c > start && *c < end)
            .collect();
        cuts.sort_unstable();
        cuts.dedup();
        let mut prev = start;
        let mut first = true;
        for cut in cuts.into_iter().chain(std::iter::once(end)) {
            if prev < cut {
                let id = if first {
                    run.id
                } else {
                    let id = syncpdf_core::StyleId(next);
                    next += 1;
                    id
                };
                out.push(syncpdf_core::ir::StyleRun {
                    id,
                    glyph_range: (prev, cut),
                    underline: ranges.iter().any(|(a, b)| *a < cut && prev < *b),
                    ..*run
                });
            }
            prev = cut;
            first = false;
        }
    }
    out
}

/// Locate the single paragraph owning a stroked line, with the covered glyph
/// range in that paragraph's reading order.
fn owner(glyphs: &[&Glyph], paras: &[Paragraph], rule: Rect) -> Option<(usize, (u32, u32), f32)> {
    let mid_y = rule.center().y;
    let mut candidates = Vec::new();
    for (index, para) in paras.iter().enumerate() {
        if !matches!(para.translatable, Translatable::Yes) || para.glyphs.is_empty() {
            continue;
        }
        let owned: Vec<GlyphId> = para.glyphs.clone();
        let own: Vec<&Glyph> = glyphs
            .iter()
            .copied()
            .filter(|g| owned.contains(&g.id))
            .collect();
        if own.is_empty() {
            continue;
        }
        let Some(line) = own.iter().find(|g| {
            // Same physical line: the rule sits just below the glyph's baseline.
            let size = g.size;
            let baseline = g.matrix.f;
            (baseline - mid_y) > size * BELOW_MIN && (baseline - mid_y) <= size * BELOW_MAX
        }) else {
            continue;
        };
        let baseline = line.matrix.f;
        // Only glyphs on that one physical line may be covered, and the line must
        // not reach into the neighbouring lines' ink bands.
        let band: Vec<&Glyph> = own
            .iter()
            .copied()
            .filter(|g| (g.matrix.f - baseline).abs() <= g.size * 0.25)
            .collect();
        if band.is_empty() {
            continue;
        }
        let covered: Vec<&Glyph> = band
            .iter()
            .copied()
            .filter(|g| spread(g).x1 > rule.x0 && spread(g).x0 < rule.x1)
            .collect();
        if covered.is_empty() {
            continue;
        }
        let Some(first) = covered.first() else {
            continue;
        };
        let Some(last) = covered.last() else { continue };
        let left = spread(first).x0;
        let right = spread(last).x1;
        // The rule must not extend meaningfully past the words it decorates.
        if (rule.x0 - left).abs() > X_TOL || (rule.x1 - right).abs() > X_TOL {
            continue;
        }
        // Every glyph in the same horizontal span and line band must also be in
        // this paragraph (unique ownership), else the rule is shared/ambiguous.
        let crossed: Vec<GlyphId> = glyphs
            .iter()
            .filter(|g| {
                let b = spread(g);
                b.x1 > rule.x0 && b.x0 < rule.x1 && (g.matrix.f - baseline).abs() <= g.size * 0.25
            })
            .map(|g| g.id)
            .collect();
        if crossed.iter().any(|id| !owned.contains(id)) {
            continue;
        }
        let range = reading_range(para, &covered);
        candidates.push((index, range, baseline));
    }
    if candidates.len() != 1 {
        return None;
    }
    candidates.pop()
}

/// Half-open reading-order glyph index range covering the decorated glyphs.
fn reading_range(para: &Paragraph, covered: &[&Glyph]) -> (u32, u32) {
    let mut start = u32::MAX;
    let mut end = 0u32;
    for (index, id) in para.glyphs.iter().enumerate() {
        if covered.iter().any(|g| g.id == *id) {
            start = start.min(index as u32);
            end = end.max(index as u32 + 1);
        }
    }
    (start.min(para.glyphs.len() as u32), end)
}

/// A glyph's tight ink when evidenced, else its loose box (conservative).
fn spread(g: &Glyph) -> Rect {
    g.ink.unwrap_or(g.bbox)
}

fn is_space(g: &Glyph) -> bool {
    g.unicode.iter().all(|c| c.is_whitespace())
}

#[cfg(test)]
mod tests {
    use super::*;
    use syncpdf_core::ir::{
        Align, Atom, FontRef, GlyphFlags, GlyphSource, Line, RegionKind, StyleRun, Translatable,
    };
    use syncpdf_core::{Color, Matrix, ObjRef, OpKey, PageId, StyleId};

    fn glyph(ordinal: u16, text: &str, x: f32, y: f32, size: f32) -> Glyph {
        let w = size * 0.5 * text.chars().count() as f32;
        Glyph {
            id: GlyphId {
                page: PageId(0),
                op: OpKey::new(ObjRef::new(1, 0), u32::from(ordinal)),
                ordinal,
            },
            unicode: text.chars().collect(),
            code: ordinal as u32,
            font: 0,
            size,
            matrix: Matrix::new(1.0, 0.0, 0.0, 1.0, x, y),
            bbox: Rect::new(x, y, x + w, y + size),
            ink: Some(Rect::new(x, y, x + w, y + size * 0.7)),
            advance: w,
            fill: Color::BLACK,
            render_mode: 0,
            source: GlyphSource {
                element_index: 0,
                string_operand_range: (0, 1),
                decoded_code_range: (0, 1),
            },
            flags: GlyphFlags::default(),
        }
    }

    fn rule(x0: f32, x1: f32, y: f32) -> (Rect, PathStroke) {
        rule_at(900, x0, x1, y)
    }

    /// One stroked rule with an explicit paint op; two real rules never share an op.
    fn rule_at(op: u32, x0: f32, x1: f32, y: f32) -> (Rect, PathStroke) {
        (
            Rect::new(x0, y, x1, y),
            PathStroke {
                op: OpKey::new(ObjRef::new(1, 0), op),
                width: 0.398,
                color: Color::BLACK,
            },
        )
    }

    fn page(glyphs: Vec<Glyph>, rules: Vec<(Rect, PathStroke)>) -> PageIR {
        let mut items = vec![DisplayItem::Text { glyphs }];
        items.extend(rules.into_iter().map(|(bbox, stroke)| DisplayItem::Path {
            bbox,
            is_fill: false,
            is_stroke: true,
            stroke: Some(stroke),
        }));
        PageIR {
            page: PageId(0),
            media_box: Rect::new(0.0, 0.0, 612.0, 792.0),
            crop_box: Rect::new(0.0, 0.0, 612.0, 792.0),
            rotation: 0,
            fonts: vec![FontRef {
                resource_name: "F1".into(),
                base_font: "F1".into(),
                is_serif: false,
                is_fixed_pitch: false,
                is_italic: false,
                is_bold: false,
            }],
            items,
        }
    }

    fn paragraph(glyphs: &[&Glyph]) -> Paragraph {
        let ids: Vec<GlyphId> = glyphs.iter().map(|g| g.id).collect();
        let baseline = glyphs[0].matrix.f;
        Paragraph {
            id: "P01-001".parse().unwrap(),
            page: PageId(0),
            region: 0,
            kind: RegionKind::Text,
            bbox: ids
                .iter()
                .map(|id| glyphs.iter().find(|g| g.id == *id).map(|g| g.bbox).unwrap())
                .reduce(|a, b| a.union(&b))
                .unwrap(),
            lines: vec![Line {
                glyphs: ids.clone(),
                baseline_y: baseline,
                bbox: glyphs
                    .iter()
                    .map(|g| g.bbox)
                    .reduce(|a, b| a.union(&b))
                    .unwrap(),
            }],
            glyphs: ids,
            text_spans: Vec::new(),
            style_runs: vec![StyleRun {
                id: StyleId(1),
                glyph_range: (0, glyphs.len() as u32),
                font: 0,
                size: glyphs[0].size,
                color: Color::BLACK,
                bold: false,
                italic: false,
                serif: false,
                mono: false,
                underline: false,
                rise: 0.0,
            }],
            atoms: Vec::<Atom>::new(),
            decorations: Vec::new(),
            text: glyphs
                .iter()
                .map(|g| g.unicode.iter().collect::<String>())
                .collect(),
            align: Align::Left,
            first_indent: 0.0,
            line_height: 14.0,
            is_rtl: false,
            translatable: Translatable::Yes,
        }
    }

    #[test]
    fn underline_below_one_line_is_claimed_without_touching_the_source_ir() {
        let gs: Vec<Glyph> = "what"
            .chars()
            .enumerate()
            .map(|(i, c)| {
                glyph(
                    i as u16,
                    &c.to_string(),
                    100.0 + i as f32 * 5.0,
                    400.0,
                    10.0,
                )
            })
            .collect();
        let ir = page(gs.clone(), vec![rule(100.0, 120.0, 398.5)]);
        let refs: Vec<&Glyph> = gs.iter().collect();
        let mut paras = vec![paragraph(&refs)];
        assert_eq!(claim(&ir, &mut paras), 1);
        mark_styles(&ir, &mut paras);
        assert_eq!(paras[0].decorations.len(), 1);
        assert_eq!(paras[0].decorations[0].glyph_range, (0, 4));
        assert!(paras[0].style_runs.iter().all(|r| r.underline));
        // The source line stays in the IR so a failed candidate still sees it.
        assert!(ir
            .items
            .iter()
            .any(|i| matches!(i, DisplayItem::Path { .. })));
        assert_eq!(owned_ops(&paras[0]).count(), 1);
    }

    #[test]
    fn partial_decoration_splits_the_run_instead_of_underlining_the_sentence() {
        let gs: Vec<Glyph> = "abcdef"
            .chars()
            .enumerate()
            .map(|(i, c)| {
                glyph(
                    i as u16,
                    &c.to_string(),
                    100.0 + i as f32 * 5.0,
                    400.0,
                    10.0,
                )
            })
            .collect();
        let ir = page(gs.clone(), vec![rule(110.0, 120.0, 398.5)]);
        let refs: Vec<&Glyph> = gs.iter().collect();
        let mut paras = vec![paragraph(&refs)];
        assert_eq!(claim(&ir, &mut paras), 1);
        assert_eq!(paras[0].decorations[0].glyph_range, (2, 4));
        mark_styles(&ir, &mut paras);
        let runs = &paras[0].style_runs;
        assert_eq!(runs.len(), 3, "one run must split at both boundaries");
        assert_eq!(
            runs.iter()
                .map(|r| (r.glyph_range, r.underline))
                .collect::<Vec<_>>(),
            vec![((0, 2), false), ((2, 4), true), ((4, 6), false)]
        );
        assert_eq!(
            runs[0].id,
            StyleId(1),
            "the untouched leading span keeps its anchor"
        );
        assert!(runs[1].id != runs[0].id && runs[2].id != runs[0].id && runs[2].id != runs[1].id);
    }

    #[test]
    fn rules_over_unclaimed_ink_or_wrong_band_are_rejected() {
        let gs: Vec<Glyph> = "what"
            .chars()
            .enumerate()
            .map(|(i, c)| {
                glyph(
                    i as u16,
                    &c.to_string(),
                    100.0 + i as f32 * 5.0,
                    400.0,
                    10.0,
                )
            })
            .collect();
        let refs: Vec<&Glyph> = gs.iter().collect();
        for rule_box in [
            // Far below the baseline: belongs to the next line, not this one.
            rule(100.0, 120.0, 390.0),
            // Wider than the words it would decorate: shared/graphic rule.
            rule(60.0, 200.0, 398.5),
            // Above the baseline: strike-through, not an underline.
            rule(100.0, 120.0, 401.5),
            // A fragment through two letters does not prove full ownership.
            rule(112.0, 118.0, 398.5),
        ] {
            let ir = page(gs.clone(), vec![rule_box]);
            let mut paras = vec![paragraph(&refs)];
            assert_eq!(claim(&ir, &mut paras), 0, "{rule_box:?}");
            assert!(paras[0].decorations.is_empty());
        }
    }

    #[test]
    fn rule_shared_with_a_neighbouring_paragraph_stays_unclaimed() {
        let mut gs: Vec<Glyph> = "under"
            .chars()
            .enumerate()
            .map(|(i, c)| {
                glyph(
                    i as u16,
                    &c.to_string(),
                    100.0 + i as f32 * 5.0,
                    400.0,
                    10.0,
                )
            })
            .collect();
        gs.extend("next".chars().enumerate().map(|(i, c)| {
            glyph(
                10 + i as u16,
                &c.to_string(),
                200.0 + i as f32 * 5.0,
                400.0,
                10.0,
            )
        }));
        let left: Vec<&Glyph> = gs[..5].iter().collect();
        let right: Vec<&Glyph> = gs[5..].iter().collect();
        let ir = page(gs.clone(), vec![rule(100.0, 220.0, 398.5)]);
        let mut paras = vec![paragraph(&left), paragraph(&right)];
        assert_eq!(claim(&ir, &mut paras), 0);
        assert!(paras.iter().all(|p| p.decorations.is_empty()));
    }

    #[test]
    fn adjacent_segments_of_the_same_pen_mark_one_span_and_different_pens_do_not() {
        let gs: Vec<Glyph> = "abcd"
            .chars()
            .enumerate()
            .map(|(i, c)| {
                glyph(
                    i as u16,
                    &c.to_string(),
                    100.0 + i as f32 * 5.0,
                    400.0,
                    10.0,
                )
            })
            .collect();
        let refs: Vec<&Glyph> = gs.iter().collect();
        // "ab" and "cd" are painted as two segments with the same pen: one span.
        let same = page(
            gs.clone(),
            vec![
                rule_at(1, 100.0, 110.0, 398.5),
                rule_at(2, 110.0, 120.0, 398.5),
            ],
        );
        let mut paras = vec![paragraph(&refs)];
        assert_eq!(claim(&same, &mut paras), 2);
        mark_styles(&same, &mut paras);
        assert_eq!(
            paras[0]
                .style_runs
                .iter()
                .filter(|r| r.underline)
                .map(|r| r.glyph_range)
                .collect::<Vec<_>>(),
            vec![(0, 4)],
            "one continuous decoration must not fragment into two spans"
        );

        // A different width is a different pen: the segments stay separate.
        let thick = PathStroke {
            width: 1.2,
            ..rule_at(2, 110.0, 120.0, 398.5).1
        };
        let mut ir = page(
            gs.clone(),
            vec![
                rule_at(1, 100.0, 110.0, 398.5),
                rule_at(2, 110.0, 120.0, 398.5),
            ],
        );
        if let Some(DisplayItem::Path { stroke, .. }) = ir.items.last_mut() {
            *stroke = Some(thick);
        }
        let mut paras = vec![paragraph(&refs)];
        assert_eq!(claim(&ir, &mut paras), 2);
        mark_styles(&ir, &mut paras);
        // Every decorated glyph stays underlined, but the two pens are not
        // collapsed into the one span the same-pen frame produces.
        let underlined: Vec<(u32, u32)> = paras[0]
            .style_runs
            .iter()
            .filter(|r| r.underline)
            .map(|r| r.glyph_range)
            .collect();
        assert_ne!(underlined, vec![(0, 4)], "distinct pens are not one span");
        let mut covered: Vec<u32> = underlined.iter().flat_map(|(a, b)| *a..*b).collect();
        covered.dedup();
        assert_eq!(covered, vec![0, 1, 2, 3]);
    }

    #[test]
    fn formula_owned_bar_is_not_claimed_as_a_text_decoration() {
        let gs: Vec<_> = "ab"
            .chars()
            .enumerate()
            .map(|(i, c)| {
                glyph(
                    i as u16,
                    &c.to_string(),
                    100.0 + i as f32 * 5.0,
                    400.0,
                    10.0,
                )
            })
            .collect();
        let ir = page(gs.clone(), vec![rule(100.0, 110.0, 398.5)]);
        let refs: Vec<_> = gs.iter().collect();
        let mut p = paragraph(&refs);
        p.atoms.push(syncpdf_core::ir::Atom {
            id: syncpdf_core::AtomId(1),
            glyph_range: (0, 2),
            kind: syncpdf_core::ir::AtomKind::Formula,
            text: "ab".into(),
            source: Some(syncpdf_core::ir::SourceAtom {
                bbox: Rect::new(99.0, 398.0, 111.0, 409.0),
                baseline: 402.0,
                advance: None,
            }),
        });
        assert_eq!(claim(&ir, &mut [p]), 0);
    }

    #[test]
    fn blank_bridge_requires_both_owned_ends_and_the_same_pen() {
        let gs: Vec<_> = [("a", 100.0), ("b", 110.0)]
            .into_iter()
            .enumerate()
            .map(|(i, (c, x))| glyph(i as u16, c, x, 400.0, 10.0))
            .collect();
        let refs: Vec<_> = gs.iter().collect();
        let mut ir = page(
            gs.clone(),
            vec![
                rule_at(1, 100.0, 105.0, 398.5),
                rule_at(2, 104.6, 110.4, 398.5),
                rule_at(3, 110.0, 115.0, 398.5),
            ],
        );
        let mut ps = vec![paragraph(&refs)];
        assert_eq!(claim(&ir, &mut ps), 3);
        mark_styles(&ir, &mut ps);
        assert_eq!(ps[0].style_runs.len(), 1);
        assert!(ps[0].style_runs[0].underline);
        if let DisplayItem::Path {
            stroke: Some(s), ..
        } = &mut ir.items[2]
        {
            s.width = 2.0;
        }
        let mut ps = vec![paragraph(&refs)];
        assert_eq!(
            claim(&ir, &mut ps),
            2,
            "different-pen bridge is not text decoration"
        );
        ir.items.pop();
        let mut ps = vec![paragraph(&refs)];
        assert_eq!(claim(&ir, &mut ps), 1, "one owned end is insufficient");
    }

    #[test]
    fn every_underlined_range_requires_nonblank_target_ink_anchor() {
        let gs: Vec<Glyph> = "abcdef"
            .chars()
            .enumerate()
            .map(|(i, c)| {
                glyph(
                    i as u16,
                    &c.to_string(),
                    100.0 + i as f32 * 5.0,
                    400.0,
                    10.0,
                )
            })
            .collect();
        let ir = page(
            gs.clone(),
            vec![
                rule_at(1, 100.0, 110.0, 398.5),
                rule_at(2, 120.0, 130.0, 398.5),
            ],
        );
        let refs: Vec<&Glyph> = gs.iter().collect();
        let mut paras = vec![paragraph(&refs)];
        claim(&ir, &mut paras);
        mark_styles(&ir, &mut paras);
        let p = &mut paras[0];
        let runs: Vec<_> = p
            .style_runs
            .iter()
            .filter(|r| r.underline)
            .cloned()
            .collect();
        assert_eq!(runs.len(), 2);
        let parse = |body: String| {
            syncpdf_translate::parse_unit_html(&format!("<p id=\"P01-001\">{body}</p>")).unwrap()
        };
        let span = |id: syncpdf_core::StyleId, text: &str| {
            format!("<span data-style=\"{}\">{text}</span>", id.0)
        };
        assert!(!anchored(p, &parse(span(runs[0].id, "甲"))));
        for missing in ["", " "] {
            assert!(!anchored(
                p,
                &parse(span(runs[0].id, "甲") + &span(runs[1].id, missing))
            ));
        }
        assert!(anchored(
            p,
            &parse(span(runs[0].id, "甲") + &span(runs[1].id, "乙"))
        ));
        // Internal link tags alias the same source range; they do not require
        // both the original ID and its tagged copy to appear.
        let tagged = syncpdf_core::ir::StyleRun {
            id: StyleId(100),
            ..runs[0]
        };
        p.style_runs.push(tagged.clone());
        assert!(anchored(
            p,
            &parse(span(tagged.id, "甲") + &span(runs[1].id, "乙"))
        ));
    }

    #[test]
    fn anchor_absence_only_refuses_the_decorated_candidate() {
        let gs: Vec<Glyph> = "abcdef"
            .chars()
            .enumerate()
            .map(|(i, c)| {
                glyph(
                    i as u16,
                    &c.to_string(),
                    100.0 + i as f32 * 5.0,
                    400.0,
                    10.0,
                )
            })
            .collect();
        let ir = page(gs.clone(), vec![rule(110.0, 120.0, 398.5)]);
        let refs: Vec<&Glyph> = gs.iter().collect();
        let mut paras = vec![paragraph(&refs)];
        claim(&ir, &mut paras);
        mark_styles(&ir, &mut paras);
        let underlined = paras[0].style_runs.iter().find(|r| r.underline).unwrap().id;
        let kept = syncpdf_translate::parse_unit_html(&format!(
            "<p id=\"P01-001\"><span data-style=\"{}\">xy</span></p>",
            underlined.0
        ))
        .unwrap();
        assert!(anchored(&paras[0], &kept));
        let dropped = syncpdf_translate::parse_unit_html("<p id=\"P01-001\">xy</p>").unwrap();
        assert!(!anchored(&paras[0], &dropped));
        // An undecorated paragraph is never blocked by style omission (R6).
        let plain = paragraph(&refs[..2]);
        assert!(anchored(&plain, &dropped));
    }
}
