//! Formula regions are atomic source drawings inside prose, not a reason to skip it.
use std::collections::BTreeSet;
use syncpdf_core::ir::{
    Atom, AtomKind, DisplayItem, Glyph, PageIR, Paragraph, Region, RegionKind, SourceAtom,
};
use syncpdf_core::{AtomId, GlyphId, Rect};

pub(super) struct Formula {
    ids: BTreeSet<GlyphId>,
    /// Prose glyphs the detected box swallowed but which stay translatable.
    pub(super) released: BTreeSet<GlyphId>,
    /// Context proving the punctuation is prose must belong to its final paragraph too.
    release_context: BTreeSet<GlyphId>,
    /// The released glyph must also be the final paragraph's last glyph.
    release_tail: bool,
    source: SourceAtom,
    row: Rect,
}

pub(super) fn sources(ir: &PageIR, regions: &[&Region]) -> Vec<Formula> {
    let glyphs: Vec<_> = ir
        .glyphs()
        .filter(|g| !g.flags.invisible && !g.flags.outside_clip)
        .collect();
    let mut out = Vec::new();
    for r in regions.iter().filter(|r| r.kind == RegionKind::Formula) {
        let owned: Vec<_> = glyphs
            .iter()
            .copied()
            .filter(|g| r.bbox.contains(g.bbox.center()))
            .collect();
        if owned.is_empty() {
            continue;
        }
        // A formula can only move with a prose region that contains all its glyphs.
        let Some(parent) = regions
            .iter()
            .filter(|p| p.kind.translatable())
            .filter(|p| owned.iter().all(|g| p.bbox.contains(g.bbox.center())))
            .min_by(|a, b| {
                (a.bbox.width() * a.bbox.height()).total_cmp(&(b.bbox.width() * b.bbox.height()))
            })
        else {
            continue;
        };
        if owned.iter().any(|g| {
            regions.iter().any(|p| {
                !p.kind.translatable()
                    && p.kind != RegionKind::Formula
                    && p.bbox.contains(g.bbox.center())
            })
        }) {
            continue;
        }
        // A detected Formula box is often wider than the real math and swallows a
        // prose comma after the last math glyph; freezing it into the KEEP atom
        // leaves punctuation the translation cannot reorder ("在H,中"). The same
        // happens to the `)` closing a prose paren pair whose `(` and words sit
        // outside the box, and to the `:` introducing a display equation below.
        // Release any of them only with the full evidence chain; anything
        // ambiguous keeps the old protection.
        let (candidate, mut release_tail) =
            match prose_trailing_comma(&owned, r, parent, regions, &glyphs)
                .map(|g| (g, BTreeSet::new()))
                .or_else(|| prose_trailing_close_paren(&owned, r, parent, regions, &glyphs))
            {
                Some(c) => (Some(c), false),
                None => (
                    prose_trailing_colon(ir, &owned, r, parent, regions, &glyphs),
                    true,
                ),
            };
        let mut released: BTreeSet<GlyphId> =
            candidate.as_ref().map(|(g, _)| g.id).into_iter().collect();
        let mut release_context = candidate.map(|(_, context)| context).unwrap_or_default();
        loop {
            let kept: Vec<&Glyph> = owned
                .iter()
                .copied()
                .filter(|g| !released.contains(&g.id))
                .collect();
            let Some(seed) = kept.first().copied() else {
                break;
            };
            let ids: BTreeSet<_> = kept.iter().map(|g| g.id).collect();
            let bbox = kept.iter().fold(seed.bbox, |b, g| b.union(&g.bbox));
            // 每个字形必须有完整墨迹证据，否则该字形退回保守 loose 盒。
            let formula_ink = kept
                .iter()
                .fold(ink_or_box(seed), |b: Rect, g| b.union(&ink_or_box(g)));
            let Some(neighbor) = glyphs
                .iter()
                .filter(|g| {
                    !ids.contains(&g.id)
                        && parent.bbox.contains(g.bbox.center())
                        && !regions.iter().any(|r| {
                            r.kind == RegionKind::Formula && r.bbox.contains(g.bbox.center())
                        })
                        && g.unicode.iter().any(|c| c.is_alphabetic())
                        && (g.bbox.center().y - bbox.center().y).abs() < g.size
                })
                .min_by(|a, b| {
                    let distance = |g: &&&Glyph| {
                        (g.bbox.center().y - bbox.center().y).abs() * 20.0
                            + (g.bbox.center().x - bbox.center().x).abs()
                    };
                    distance(a).total_cmp(&distance(b))
                })
            else {
                break;
            };
            // Erase, replay and collision must use the SAME evidenced rectangle.
            // A larger loose erase clip could cut neighboring ink even when the
            // tighter collision box does not intersect it.
            let mut clip = formula_ink;
            // Include fraction bars and other local vector ink, never a crossing rule/image.
            for item in &ir.items {
                if let DisplayItem::Path {
                    bbox,
                    is_fill,
                    is_stroke,
                    ..
                } = item
                {
                    if (*is_fill || *is_stroke)
                        && r.bbox.contains(bbox.center())
                        && bbox.width() <= r.bbox.width() + 1.0
                        && bbox.height() <= r.bbox.height() + 1.0
                    {
                        let pad = if *is_stroke { 0.5 } else { 0.05 };
                        let path =
                            Rect::new(bbox.x0 - pad, bbox.y0 - pad, bbox.x1 + pad, bbox.y1 + pad);
                        clip = clip.union(&path);
                    }
                }
            }
            // The released comma is foreign ink now: if the shrunk clip still
            // touches it, restore the full ownership instead of cutting ink.
            let blocked = glyphs.iter().any(|g| {
                !ids.contains(&g.id)
                    && !g.unicode.iter().all(|c| c.is_whitespace())
                    && overlaps(clip, ink_or_box(g))
            }) || ir.items.iter().any(|item| match item {
                DisplayItem::Image { bbox } | DisplayItem::InlineImage { bbox } => {
                    overlaps(clip, *bbox)
                }
                DisplayItem::Path {
                    bbox,
                    is_fill,
                    is_stroke,
                    ..
                } if *is_fill || *is_stroke => {
                    overlaps(clip, *bbox)
                        && !(clip.contains(syncpdf_core::Point::new(bbox.x0, bbox.y0))
                            && clip.contains(syncpdf_core::Point::new(bbox.x1, bbox.y1)))
                }
                _ => false,
            });
            if blocked {
                if released.is_empty() {
                    break;
                }
                released.clear();
                release_context.clear();
                release_tail = false;
                continue;
            }
            out.push(Formula {
                ids,
                released: std::mem::take(&mut released),
                release_context: std::mem::take(&mut release_context),
                release_tail,
                source: SourceAtom {
                    bbox: clip,
                    baseline: neighbor.matrix.f,
                },
                row: neighbor.bbox,
            });
            break;
        }
    }
    radical_sources(ir, regions, &glyphs, &mut out);
    out
}

/// A detected Formula box is often wider than the real math: it can swallow the
/// prose comma that follows the last math glyph. The comma is released back to
/// prose only when every piece of evidence agrees, otherwise the caller keeps
/// it inside the atom:
/// - it is the rightmost owned glyph, a lone `,` with tight-ink evidence, on
///   the formula's main baseline;
/// - it keeps the body font while the math glyph it follows does not (font
///   mixing alone is not proof, but without a boundary there is none);
/// - the remaining glyphs have no unclosed `(`/`[`/`{` that would make the
///   comma a separator;
/// - no other preserved region also claims it;
/// - a two-word body-text successor follows it in reading order, on its own
///   line or at the start of the wrapped row below; final paragraph ownership
///   is checked again after attachment.
fn prose_trailing_comma<'a>(
    owned: &[&'a Glyph],
    region: &Region,
    parent: &Region,
    regions: &[&Region],
    glyphs: &[&'a Glyph],
) -> Option<&'a Glyph> {
    let comma = owned.iter().copied().max_by(|a, b| {
        a.bbox
            .x0
            .total_cmp(&b.bbox.x0)
            .then(a.bbox.x1.total_cmp(&b.bbox.x1))
    })?;
    let ink = comma.ink?;
    if comma.unicode.as_slice() != [',']
        || ![ink.x0, ink.y0, ink.x1, ink.y1, comma.matrix.f, comma.size]
            .into_iter()
            .all(f32::is_finite)
        || ink.width() <= 0.0
        || ink.height() <= 0.0
        || comma.size <= 0.0
    {
        return None;
    }
    let rest: Vec<&Glyph> = owned.iter().copied().filter(|g| g.id != comma.id).collect();
    if rest.is_empty() {
        return None;
    }
    // Same baseline as the largest (main-row) glyph, not a sub/superscript tail.
    let base = rest.iter().max_by(|a, b| a.size.total_cmp(&b.size))?;
    if (comma.matrix.f - base.matrix.f).abs() > 0.5 {
        return None;
    }
    // The glyph the comma attaches to must carry a different (math) font.
    let prev = rest.iter().max_by(|a, b| a.bbox.x0.total_cmp(&b.bbox.x0))?;
    if comma.font == prev.font {
        return None;
    }
    // An unclosed math delimiter inside makes the comma a separator, not prose.
    let mut sorted: Vec<&Glyph> = rest.clone();
    sorted.sort_by(|a, b| a.bbox.x0.total_cmp(&b.bbox.x0));
    if !balanced_delimiters(&sorted) {
        return None;
    }
    // A detector may omit the opening delimiter entirely, e.g. f(H, max(...)).
    // Inspect the owner's preceding rows as well as the current row prefix.
    let mut prefix: Vec<_> = glyphs
        .iter()
        .copied()
        .filter(|g| {
            parent.bbox.contains(g.bbox.center())
                && (g.matrix.f > comma.matrix.f + 0.5
                    || ((g.matrix.f - comma.matrix.f).abs() <= 0.5 && g.bbox.x0 < comma.bbox.x0))
        })
        .collect();
    prefix.sort_by(|a, b| {
        b.matrix
            .f
            .total_cmp(&a.matrix.f)
            .then(a.bbox.x0.total_cmp(&b.bbox.x0))
    });
    if !balanced_delimiters(&prefix) {
        return None;
    }
    // No other preserved region may also claim the comma.
    if regions.iter().any(|p| {
        !p.kind.translatable() && p.index != region.index && p.bbox.contains(comma.bbox.center())
    }) {
        return None;
    }
    prose_successor(comma, parent, regions, glyphs)?;
    Some(comma)
}

/// The same swallow happens to a prose `)`: "(i.e., the decoder, 𝑙>𝐿/2)" keeps
/// the real math but its closing paren lands inside the detected box, so the
/// atom reads "𝑙>𝐿/2)" and the paragraph adds a second prose `)`. Release it
/// only when the pairing is proven end to end; anything ambiguous keeps it:
/// - it is the rightmost owned glyph, a lone `)` with finite positive tight
///   ink, on the formula's main baseline;
/// - the remaining glyphs still balance their own delimiters;
/// - walking the owner's reading order backwards pairs it with a `(` that no
///   Formula or preserved region claims (a math `f(x)`/`(x,y)` opener inside
///   the box, or one owned by an earlier formula atom, is not a prose opener);
/// - the pair encloses at least two complete prose words outside every Formula
///   region, in the close paren's own font and size, with no unknown glyphs;
/// - no other preserved region also claims the close paren.
fn prose_trailing_close_paren<'a>(
    owned: &[&'a Glyph],
    region: &Region,
    parent: &Region,
    regions: &[&Region],
    glyphs: &[&'a Glyph],
) -> Option<(&'a Glyph, BTreeSet<GlyphId>)> {
    let close = owned.iter().copied().max_by(|a, b| {
        a.bbox
            .x0
            .total_cmp(&b.bbox.x0)
            .then(a.bbox.x1.total_cmp(&b.bbox.x1))
    })?;
    let ink = close.ink?;
    if close.unicode.as_slice() != [')']
        || ![ink.x0, ink.y0, ink.x1, ink.y1, close.matrix.f, close.size]
            .into_iter()
            .all(f32::is_finite)
        || ink.width() <= 0.0
        || ink.height() <= 0.0
        || close.size <= 0.0
    {
        return None;
    }
    let rest: Vec<&Glyph> = owned.iter().copied().filter(|g| g.id != close.id).collect();
    if rest.is_empty() {
        return None;
    }
    // Same baseline as the largest (main-row) glyph, not a sub/superscript tail.
    let base = rest.iter().max_by(|a, b| a.size.total_cmp(&b.size))?;
    if (close.matrix.f - base.matrix.f).abs() > 0.5 {
        return None;
    }
    // The remaining math must still balance its own delimiters.
    let mut sorted: Vec<&Glyph> = rest.clone();
    sorted.sort_by(|a, b| a.bbox.x0.total_cmp(&b.bbox.x0));
    if !balanced_delimiters(&sorted) {
        return None;
    }
    // The pair proof: a prose `(` opener and real prose words inside.
    let context = prose_paren_pair(close, parent, regions, glyphs)?;
    // No other preserved region may also claim the close paren.
    if regions.iter().any(|p| {
        !p.kind.translatable() && p.index != region.index && p.bbox.contains(close.bbox.center())
    }) {
        return None;
    }
    Some((close, context))
}

/// Walk the owner's reading order backwards from `close` to the opener it
/// pairs with. Every glyph inside the pair must have known unicode and every
/// nested pair must type-match, or the pairing is unprovable. The opener must
/// be a `(` that no Formula or preserved region claims, in the same body font
/// and size as the close paren, and the pair must enclose at least two
/// complete prose words outside every Formula region.
fn prose_paren_pair(
    close: &Glyph,
    parent: &Region,
    regions: &[&Region],
    glyphs: &[&Glyph],
) -> Option<BTreeSet<GlyphId>> {
    // Unknown sub/superscript glyphs must not disappear through baseline filtering.
    if glyphs
        .iter()
        .any(|g| parent.bbox.contains(g.bbox.center()) && g.unicode.is_empty())
    {
        return None;
    }
    let mut prefix: Vec<&Glyph> = glyphs
        .iter()
        .copied()
        .filter(|g| {
            g.id != close.id
                && parent.bbox.contains(g.bbox.center())
                && (g.matrix.f > close.matrix.f + 0.5
                    || ((g.matrix.f - close.matrix.f).abs() <= 0.5 && g.bbox.x0 < close.bbox.x0))
        })
        .collect();
    prefix.sort_by(|a, b| {
        b.matrix
            .f
            .total_cmp(&a.matrix.f)
            .then(a.bbox.x0.total_cmp(&b.bbox.x0))
    });
    let mut stack: Vec<char> = Vec::new();
    let mut open_index = None;
    for (i, g) in prefix.iter().enumerate().rev() {
        if g.unicode.is_empty() {
            return None;
        }
        for &c in g.unicode.iter().rev() {
            match c {
                ')' | ']' | '}' => stack.push(c),
                '(' | '[' | '{' => match stack.pop() {
                    None => {
                        let ink = g.ink?;
                        if ![ink.x0, ink.y0, ink.x1, ink.y1, g.matrix.f, g.size]
                            .into_iter()
                            .all(f32::is_finite)
                            || ink.width() <= 0.0
                            || ink.height() <= 0.0
                            || g.unicode.as_slice() != ['(']
                            || c != '('
                            || g.font != close.font
                            || (g.size - close.size).abs() > 0.05
                            || regions
                                .iter()
                                .any(|r| !r.kind.translatable() && r.bbox.contains(g.bbox.center()))
                        {
                            return None;
                        }
                        open_index = Some(i);
                        break;
                    }
                    Some(closer) => {
                        let want = match closer {
                            ')' => '(',
                            ']' => '[',
                            _ => '{',
                        };
                        if c != want {
                            return None;
                        }
                    }
                },
                _ => {}
            }
        }
        if open_index.is_some() {
            break;
        }
    }
    let i = open_index?;
    // Check the WHOLE pair before accepting any word witnesses. Font changes
    // do not create word boundaries: reuse the strict body-word proof, starting
    // only after evidenced whitespace/punctuation or a geometric word gap.
    let inside = &prefix[i + 1..];
    if inside.iter().any(|g| {
        g.unicode.is_empty()
            || regions.iter().any(|r| {
                !r.kind.translatable()
                    && r.kind != RegionKind::Formula
                    && r.bbox.contains(g.bbox.center())
            })
    }) {
        return None;
    }
    let prose = prefix[i..].windows(2).any(|pair| {
        let (prev, start) = (pair[0], pair[1]);
        let boundary = prev
            .unicode
            .iter()
            .all(|c| c.is_whitespace() || matches!(c, '(' | '.' | ',' | ';' | ':' | '!' | '?'))
            || ((prev.matrix.f - start.matrix.f).abs() <= 0.5
                && start.bbox.x0 - prev.bbox.x1 > 0.15 * close.size);
        boundary && body_word_pair(start, close, parent, regions, inside)
    });
    prose.then(|| {
        prefix[i..]
            .iter()
            .map(|g| g.id)
            .chain(std::iter::once(close.id))
            .collect()
    })
}

/// A detected box can also swallow the body colon that introduces a display
/// equation: "...response 𝑧_b,j:" keeps `:` inside the KEEP atom, so the
/// paragraph can never end with it. Release it only when the whole evidence
/// chain holds; anything ambiguous keeps the old protection:
/// - the same rightmost/lone-glyph, finite tight-ink, main-baseline,
///   font-boundary and balanced-delimiter evidence as the other releases;
/// - no unknown glyph inside the owner region, and two complete body words in
///   the colon's own font and size precede it on its own row;
/// - it is the last visible glyph of its owner region and — checked again in
///   `released_for` through `release_tail` — of the final paragraph;
/// - the nearest region directly below its row, inside the parent's reading
///   column, is a unique Formula region separated by an empty gap and holding
///   a verifiable `=` with real glyphs on both sides: the display equation the
///   colon introduces. Internal math colons (maps, ratios, slices) never
///   reach this point because they are not the paragraph's last glyph.
fn prose_trailing_colon<'a>(
    ir: &PageIR,
    owned: &[&'a Glyph],
    region: &Region,
    parent: &Region,
    regions: &[&Region],
    glyphs: &[&'a Glyph],
) -> Option<(&'a Glyph, BTreeSet<GlyphId>)> {
    let colon = owned.iter().copied().max_by(|a, b| {
        a.bbox
            .x0
            .total_cmp(&b.bbox.x0)
            .then(a.bbox.x1.total_cmp(&b.bbox.x1))
    })?;
    let ink = colon.ink?;
    if colon.unicode.as_slice() != [':']
        || ![ink.x0, ink.y0, ink.x1, ink.y1, colon.matrix.f, colon.size]
            .into_iter()
            .all(f32::is_finite)
        || ink.width() <= 0.0
        || ink.height() <= 0.0
        || colon.size <= 0.0
    {
        return None;
    }
    let rest: Vec<&Glyph> = owned.iter().copied().filter(|g| g.id != colon.id).collect();
    if rest.is_empty() || rest.iter().any(|g| g.unicode.is_empty()) {
        return None;
    }
    // Same baseline as the largest (main-row) glyph, not a sub/superscript tail.
    let base = rest.iter().max_by(|a, b| a.size.total_cmp(&b.size))?;
    if (colon.matrix.f - base.matrix.f).abs() > 0.5 {
        return None;
    }
    // The glyph the colon attaches to must carry a different (math) font.
    let prev = rest.iter().max_by(|a, b| a.bbox.x0.total_cmp(&b.bbox.x0))?;
    if colon.font == prev.font {
        return None;
    }
    // The remaining math must still balance its own delimiters.
    let mut sorted: Vec<&Glyph> = rest.clone();
    sorted.sort_by(|a, b| a.bbox.x0.total_cmp(&b.bbox.x0));
    if !balanced_delimiters(&sorted) {
        return None;
    }
    let in_formula = |c: syncpdf_core::Point| {
        regions
            .iter()
            .any(|r| r.kind == RegionKind::Formula && r.bbox.contains(c))
    };
    // Unknown glyphs inside the owner region make the boundary unprovable;
    // formula-owned glyphs keep their own protection instead.
    if glyphs.iter().any(|g| {
        parent.bbox.contains(g.bbox.center())
            && !in_formula(g.bbox.center())
            && g.unicode.is_empty()
    }) {
        return None;
    }
    let visible = |g: &Glyph| g.unicode.is_empty() || g.unicode.iter().any(|c| !c.is_whitespace());
    // The colon must be the owner's last visible glyph: nothing to its right on
    // its own row and no lower row inside the parent region. Formula-owned
    // glyphs (e.g. another atom's subscripts below the baseline) are not prose
    // continuation and do not count.
    if glyphs.iter().any(|g| {
        g.id != colon.id
            && parent.bbox.contains(g.bbox.center())
            && !in_formula(g.bbox.center())
            && visible(g)
            && (g.matrix.f < colon.matrix.f - 0.5
                || ((g.matrix.f - colon.matrix.f).abs() <= 0.5 && g.bbox.x0 > colon.bbox.x0))
    }) {
        return None;
    }
    // Two complete body words in the colon's own font and size must precede it
    // on its row; a font-change fragment is not a word boundary.
    let mut prefix: Vec<&Glyph> = glyphs
        .iter()
        .copied()
        .filter(|g| {
            parent.bbox.contains(g.bbox.center())
                && (g.matrix.f - colon.matrix.f).abs() <= 0.5
                && g.bbox.x0 < colon.bbox.x0
        })
        .collect();
    prefix.sort_by(|a, b| a.bbox.x0.total_cmp(&b.bbox.x0));
    let prose = prefix.windows(2).any(|pair| {
        let (prev, start) = (pair[0], pair[1]);
        let boundary = prev
            .unicode
            .iter()
            .all(|c| c.is_whitespace() || matches!(c, '(' | '.' | ',' | ';' | ':' | '!' | '?'))
            || ((prev.matrix.f - start.matrix.f).abs() <= 0.5
                && start.bbox.x0 - prev.bbox.x1 > 0.15 * colon.size);
        boundary && body_word_pair(start, colon, parent, regions, glyphs)
    });
    if !prose {
        return None;
    }
    // The colon must introduce the nearest region directly below its row: a
    // unique display formula in the parent's reading column. Another region at
    // the same depth, or anything else nearer, keeps it protected.
    let row_bottom = prefix.iter().fold(colon.bbox, |b, g| b.union(&g.bbox)).y0;
    let in_column = |r: &&&Region| {
        let c = r.bbox.center();
        parent.bbox.x0 <= c.x && c.x <= parent.bbox.x1
    };
    let below = regions
        .iter()
        .filter(|r| r.index != region.index && in_column(r) && r.bbox.y1 <= row_bottom + 0.5)
        .max_by(|a, b| a.bbox.y1.total_cmp(&b.bbox.y1))?;
    if below.kind != RegionKind::Formula
        || parent.bbox.contains(below.bbox.center())
        || row_bottom - below.bbox.y1 > 2.5 * colon.size
        || regions.iter().any(|r| {
            r.index != below.index && in_column(&r) && (r.bbox.y1 - below.bbox.y1).abs() <= 0.5
        })
    {
        return None;
    }
    // Nothing may sit in the gap between the row and the equation. Exempt only
    // what is already proven: the equation below and the same-line formulas —
    // detected boxes that own a glyph on the colon's own row (the owner and
    // neighbouring atoms whose subscripts legitimately dip into the gap).
    // Content inside any other detected formula is still foreign.
    let same_line: BTreeSet<u32> = regions
        .iter()
        .filter(|r| r.kind == RegionKind::Formula)
        .filter(|r| {
            glyphs.iter().any(|g| {
                r.bbox.contains(g.bbox.center()) && (g.matrix.f - colon.matrix.f).abs() <= 0.5
            })
        })
        .map(|r| r.index)
        .collect();
    let proven = |c: syncpdf_core::Point| {
        below.bbox.contains(c)
            || regions
                .iter()
                .any(|r| same_line.contains(&r.index) && r.bbox.contains(c))
    };
    let band = Rect::new(parent.bbox.x0, below.bbox.y1, parent.bbox.x1, row_bottom);
    if glyphs.iter().any(|g| {
        !owned.iter().any(|o| o.id == g.id)
            && (g.matrix.f - colon.matrix.f).abs() > 0.5
            && !proven(g.bbox.center())
            && overlaps(band, ink_or_box(g))
    }) || ir.items.iter().any(|item| match item {
        DisplayItem::Image { bbox } | DisplayItem::InlineImage { bbox } => {
            !proven(bbox.center()) && overlaps(band, *bbox)
        }
        DisplayItem::Path {
            bbox,
            is_fill,
            is_stroke,
            ..
        } if *is_fill || *is_stroke => !proven(bbox.center()) && overlaps(band, *bbox),
        _ => false,
    }) {
        return None;
    }
    // The equation must be verifiable: a `=` carrying real ink with a complete
    // (non-empty unicode, finite positive ink) glyph on each side. Other
    // equation glyphs may be control codes that print nothing; they are not
    // required as witnesses.
    let solid = |g: &Glyph| {
        !g.unicode.is_empty()
            && g.ink.is_some_and(|i| {
                [i.x0, i.y0, i.x1, i.y1].into_iter().all(f32::is_finite)
                    && i.width() > 0.0
                    && i.height() > 0.0
            })
    };
    let equation: Vec<&Glyph> = glyphs
        .iter()
        .copied()
        .filter(|g| below.bbox.contains(g.bbox.center()) && visible(g))
        .collect();
    if !equation.iter().any(|e| {
        e.unicode.as_slice() == ['=']
            && solid(e)
            && equation
                .iter()
                .any(|l| l.id != e.id && solid(l) && l.bbox.x1 <= e.bbox.x0 + 0.5)
            && equation
                .iter()
                .any(|r| r.id != e.id && solid(r) && r.bbox.x0 >= e.bbox.x1 - 0.5)
    }) {
        return None;
    }
    // No other preserved region may also claim the colon.
    if regions.iter().any(|p| {
        !p.kind.translatable() && p.index != region.index && p.bbox.contains(colon.bbox.center())
    }) {
        return None;
    }
    let context = prefix
        .iter()
        .map(|g| g.id)
        .chain(std::iter::once(colon.id))
        .collect();
    Some((colon, context))
}

fn balanced_delimiters(glyphs: &[&Glyph]) -> bool {
    let mut stack = Vec::new();
    for g in glyphs {
        if g.unicode.is_empty() {
            return false;
        }
        for &c in &g.unicode {
            match c {
                '(' | '[' | '{' => stack.push(c),
                ')' | ']' | '}' => {
                    let open = match c {
                        ')' => '(',
                        ']' => '[',
                        _ => '{',
                    };
                    if stack.pop() != Some(open) {
                        return false;
                    }
                }
                _ => {}
            }
        }
    }
    stack.is_empty()
}

/// A letter alone may be an upright math variable. Require two complete body
/// words (each at least two letters), with normal spacing and no intervening
/// unknown/preserved glyph. This is intentionally a narrow prose proof.
fn body_word_pair(
    start: &Glyph,
    comma: &Glyph,
    parent: &Region,
    regions: &[&Region],
    glyphs: &[&Glyph],
) -> bool {
    let mut row: Vec<_> = glyphs
        .iter()
        .copied()
        .filter(|g| {
            parent.bbox.contains(g.bbox.center())
                && (g.matrix.f - start.matrix.f).abs() <= 0.5
                && g.bbox.x0 >= start.bbox.x0
        })
        .collect();
    row.sort_by(|a, b| a.bbox.x0.total_cmp(&b.bbox.x0));
    let mut words = 0;
    let mut letters = 0;
    let mut right = start.bbox.x0;
    for g in row {
        let space = !g.unicode.is_empty() && g.unicode.iter().all(|c| c.is_whitespace());
        let gap = g.bbox.x0 - right;
        if gap > comma.size {
            return false;
        }
        if (space || gap > 0.15 * comma.size) && letters > 0 {
            if letters < 2 {
                return false;
            }
            words += 1;
            letters = 0;
            if words == 2 {
                return true;
            }
        }
        if !space {
            if g.unicode.is_empty()
                || !g.unicode.iter().all(|c| c.is_ascii_alphabetic())
                || g.font != comma.font
                || (g.size - comma.size).abs() > 0.05
                || regions
                    .iter()
                    .any(|r| !r.kind.translatable() && r.bbox.contains(g.bbox.center()))
            {
                return words == 1
                    && letters >= 2
                    && !g.unicode.is_empty()
                    && g.unicode
                        .iter()
                        .all(|c| matches!(c, '.' | ',' | ';' | ':' | '!' | '?'));
            }
            letters += g.unicode.len();
        }
        right = g.bbox.x1;
    }
    words == 1 && letters >= 2
}

/// The released comma must be followed by prose of the same owner and style:
/// the next glyph on its own line, or the first glyph of the wrapped row below.
fn prose_successor(
    comma: &Glyph,
    parent: &Region,
    regions: &[&Region],
    glyphs: &[&Glyph],
) -> Option<()> {
    let in_parent = |g: &Glyph| parent.bbox.contains(g.bbox.center());
    let visible = |g: &Glyph| g.unicode.is_empty() || g.unicode.iter().any(|c| !c.is_whitespace());
    // The nearest non-space glyph to the right on the same baseline must sit
    // within a normal word gap and be prose; a math neighbour keeps the comma.
    if let Some(next) = glyphs
        .iter()
        .copied()
        .filter(|g| {
            g.id != comma.id
                && in_parent(g)
                && (g.matrix.f - comma.matrix.f).abs() <= 0.5
                && g.bbox.x0 >= comma.bbox.x1 - 0.5
                && visible(g)
        })
        .min_by(|a, b| a.bbox.x0.total_cmp(&b.bbox.x0))
    {
        if next.bbox.x0 - comma.bbox.x1 > comma.size {
            return None;
        }
        return body_word_pair(next, comma, parent, regions, glyphs).then_some(());
    }
    // Otherwise the comma ends its line; the wrapped row below must open with
    // prose that starts left of it.
    let next_row = glyphs
        .iter()
        .copied()
        .filter(|g| in_parent(g) && g.matrix.f < comma.matrix.f - 0.5 && visible(g))
        .map(|g| g.matrix.f)
        .fold(f32::NEG_INFINITY, f32::max);
    if !next_row.is_finite() || comma.matrix.f - next_row > super::PARAGRAPH_GAP_RATIO * comma.size
    {
        return None;
    }
    let first = glyphs
        .iter()
        .copied()
        .filter(|g| in_parent(g) && (g.matrix.f - next_row).abs() <= 0.5 && visible(g))
        .min_by(|a, b| a.bbox.x0.total_cmp(&b.bbox.x0))?;
    (first.bbox.x0 <= comma.bbox.x0 && body_word_pair(first, comma, parent, regions, glyphs))
        .then_some(())
}

/// Undetected inline radicals whose loose box overflows its own line.
///
/// A `√` glyph can carry a glyph box that is materially taller than its real ink.
/// When that box spans the gap to the next line, `group_lines` bridges the two
/// rows and x-ordering interleaves them, corrupting the whole paragraph's reading
/// order. A real radical is proved by geometry alone: the `√` ink, an adjoining
/// horizontal stroke-only overbar at its top, and the whole radicand as the
/// nearest source line strictly below that bar. Reusing the detected-formula
/// ownership, ink and conflict gates means a false candidate is simply skipped,
/// so nothing is erased and the original text is preserved.
fn radical_sources(ir: &PageIR, regions: &[&Region], glyphs: &[&Glyph], out: &mut Vec<Formula>) {
    const RADICAL: char = '\u{221a}';
    // Only plain stroked horizontal paint can prove an overbar.
    let bars: Vec<_> = ir
        .items
        .iter()
        .filter_map(|item| match item {
            DisplayItem::Path {
                bbox,
                is_fill: false,
                is_stroke: true,
                stroke: Some(stroke),
                ..
            } => Some((bbox, stroke)),
            _ => None,
        })
        .collect();
    for root in glyphs.iter().copied() {
        if root.unicode.len() != 1 || root.unicode[0] != RADICAL {
            continue;
        }
        // The tight box is the only reliable evidence of the real ink; without it
        // the loose box cannot be told apart from a genuinely tall glyph.
        let Some(ink) = root.ink else { continue };
        // A detected Formula region or an earlier candidate already owns it.
        if regions
            .iter()
            .any(|r| r.kind == RegionKind::Formula && r.bbox.contains(root.bbox.center()))
            || out.iter().any(|f| f.ids.contains(&root.id))
        {
            continue;
        }
        if root.bbox.height() <= ink.height() * 1.2 {
            continue;
        }
        let span = root.size * 0.25;
        let max_bar = (root.size * 0.08).max(0.75);
        let mut candidates = bars.iter().copied().filter(|(b, stroke)| {
            b.height() <= max_bar
                && b.width() > 0.0
                && stroke.width.is_finite()
                && stroke.width > 0.0
                && stroke.width <= max_bar
                && (b.y0 - ink.y1).abs() <= span
                && ink.x0 <= b.x0
                && b.x0 <= ink.x1
        });
        let Some((bar, pen)) = candidates.next() else {
            continue;
        };
        if candidates.next().is_some() {
            continue; // Similar geometry cannot choose between different paint owners.
        }
        // The radicand is the nearest source line strictly below the bar; a line
        // touched by the bar itself is not an under-bar radicand.
        let line = glyphs
            .iter()
            .filter(|g| bar.x0 - 0.5 <= g.bbox.center().x && g.bbox.center().x <= bar.x1 + 0.5)
            .map(|g| g.matrix.f)
            .filter(|y| y.is_finite() && *y < bar.y0 - 0.5)
            .fold(f32::NEG_INFINITY, f32::max);
        if !line.is_finite() || bar.y0 - line > root.size * 1.5 {
            continue;
        }
        let mut radicand: Vec<&Glyph> = glyphs
            .iter()
            .copied()
            .filter(|g| {
                (g.matrix.f - line).abs() <= 0.01
                    && bar.x0 - 0.5 <= g.bbox.center().x
                    && g.bbox.center().x <= bar.x1 + 0.5
            })
            .collect();
        if radicand.is_empty() {
            continue;
        }
        // Every radicand glyph must sit fully under the bar at the same size, or the
        // bar is not this radical's overbar and the clip could cut neighbouring ink.
        if radicand.iter().any(|g| {
            g.ink.is_none_or(|ink| {
                ink.y1 > bar.y0 + 0.5 || ink.x0 < bar.x0 - 0.5 || ink.x1 > bar.x1 + 0.5
            }) || (g.size - root.size).abs() > 0.01
        }) {
            continue;
        }
        radicand.sort_by(|a, b| a.bbox.x0.total_cmp(&b.bbox.x0));
        let ids: BTreeSet<GlyphId> = std::iter::once(root.id)
            .chain(radicand.iter().map(|g| g.id))
            .collect();
        // A unique translatable region must own the radical and the whole radicand;
        // no preserved region may also claim any of them.
        let owned = || std::iter::once(root).chain(radicand.iter().copied());
        if regions
            .iter()
            .filter(|p| p.kind.translatable())
            .filter(|p| owned().all(|g| p.bbox.contains(g.bbox.center())))
            .count()
            != 1
            || out.iter().any(|f| !f.ids.is_disjoint(&ids))
        {
            continue;
        }
        if owned().any(|g| {
            regions.iter().any(|p| {
                !p.kind.translatable()
                    && p.kind != RegionKind::Formula
                    && p.bbox.contains(g.bbox.center())
            })
        }) {
            continue;
        }
        // Same evidence chain as a detected formula: real ink plus the overbar, and
        // nothing else may cross it.
        let pad = (pen.width * 0.5).max(0.5);
        let bar_box = Rect::new(bar.x0 - pad, bar.y0 - pad, bar.x1 + pad, bar.y1 + pad);
        let mut clip = Rect::new(ink.x0, ink.y0, ink.x1, ink.y1).union(&bar_box);
        for g in &radicand {
            clip = clip.union(&ink_or_box(g));
        }
        if glyphs.iter().any(|g| {
            !ids.contains(&g.id)
                && (g.unicode.is_empty() || !g.unicode.iter().all(|c| c.is_whitespace()))
                && overlaps(clip, ink_or_box(g))
        }) {
            continue;
        }
        if ir.items.iter().any(|item| match item {
            DisplayItem::Image { bbox } | DisplayItem::InlineImage { bbox } => {
                overlaps(clip, *bbox)
            }
            DisplayItem::Path {
                bbox,
                is_fill,
                is_stroke,
                stroke,
            } if *is_fill || *is_stroke => {
                let overbar = stroke.as_ref().is_some_and(|s| s.op == pen.op);
                let pad = if *is_stroke {
                    stroke.as_ref().map_or(0.5, |s| s.width * 0.5)
                } else {
                    0.0
                };
                !overbar
                    && overlaps(
                        clip,
                        Rect::new(bbox.x0 - pad, bbox.y0 - pad, bbox.x1 + pad, bbox.y1 + pad),
                    )
            }
            _ => false,
        }) {
            continue;
        }
        let row = radicand
            .iter()
            .fold(radicand[0].bbox, |b, g| b.union(&g.bbox));
        out.push(Formula {
            ids,
            released: BTreeSet::new(),
            release_context: BTreeSet::new(),
            release_tail: false,
            source: SourceAtom {
                bbox: clip,
                baseline: line,
            },
            row,
        });
    }
}

fn overlaps(a: Rect, b: Rect) -> bool {
    a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1
}

/// 邻接判定用的实际墨迹：pdfium tight box 有值就用它，否则退回 loose bbox。
///
/// loose box 含字体上下沿，会让同一行的邻字、甚至上下行看似相交；tight box
/// 只覆盖真实墨迹，是「是否真的挡到内容」的可靠证据。没有 tight 证据时
/// 必须继续保守（沿用 loose），不能把未知当成无碰撞。
fn ink_or_box(g: &Glyph) -> Rect {
    g.ink.unwrap_or(g.bbox)
}

pub(super) fn line_box(g: &Glyph, formulas: &[Formula]) -> Rect {
    formulas
        .iter()
        .find(|f| f.ids.contains(&g.id))
        .map_or(g.bbox, |f| {
            Rect::new(g.bbox.x0, f.row.y0, g.bbox.x1, f.row.y1)
        })
}

/// An exemption belongs only to the final paragraph that actually acquired the
/// associated source atom; a region-level candidate is not paragraph ownership.
pub(super) fn released_for(p: &Paragraph, formulas: &[Formula]) -> BTreeSet<GlyphId> {
    formulas
        .iter()
        .filter(|f| f.release_context.iter().all(|id| p.glyphs.contains(id)))
        .filter(|f| !f.release_tail || p.glyphs.last().is_some_and(|id| f.released.contains(id)))
        .filter(|f| {
            p.atoms.iter().any(|a| {
                a.kind == AtomKind::Formula
                    && a.source == Some(f.source)
                    && p.glyphs
                        .get(a.glyph_range.0 as usize..a.glyph_range.1 as usize)
                        .is_some_and(|ids| {
                            ids.len() == f.ids.len() && ids.iter().all(|id| f.ids.contains(id))
                        })
            })
        })
        .flat_map(|f| f.released.iter().copied())
        .filter(|id| p.glyphs.contains(id))
        .collect()
}

pub(super) fn attach(p: &mut Paragraph, formulas: &[Formula]) {
    if !p.kind.translatable() {
        return;
    }
    for f in formulas {
        let indices: Vec<_> = p
            .glyphs
            .iter()
            .enumerate()
            .filter(|(_, id)| f.ids.contains(id))
            .map(|(i, _)| i as u32)
            .collect();
        if indices.len() != f.ids.len() || indices.is_empty() {
            continue;
        }
        let start = indices[0];
        let end = indices[indices.len() - 1] + 1;
        if (end - start) as usize != indices.len() {
            continue;
        }
        let text: String = p
            .text_spans
            .iter()
            .filter(|s| {
                let (a, b) = s.glyph_range;
                if a == b {
                    start < a && a < end
                } else {
                    start <= a && b <= end
                }
            })
            .map(|s| s.text.as_str())
            .collect();
        p.atoms
            .retain(|a| a.glyph_range.1 <= start || end <= a.glyph_range.0);
        p.atoms.push(Atom {
            id: AtomId(0),
            glyph_range: (start, end),
            kind: AtomKind::Formula,
            text,
            source: Some(f.source),
        });
    }
    p.atoms.sort_by_key(|a| a.glyph_range.0);
    for (i, a) in p.atoms.iter_mut().enumerate() {
        a.id = AtomId(i as u32 + 1);
    }
}
