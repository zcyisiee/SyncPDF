//! Formula regions are atomic source drawings inside prose, not a reason to skip it.
use std::collections::BTreeSet;
use syncpdf_core::ir::{
    Atom, AtomKind, DisplayItem, Glyph, PageIR, Paragraph, Region, RegionKind, SourceAtom,
};
use syncpdf_core::{AtomId, GlyphId, Rect};

pub(super) struct Formula {
    ids: BTreeSet<GlyphId>,
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
        let Some(first) = owned.first() else { continue };
        let ids: BTreeSet<_> = owned.iter().map(|g| g.id).collect();
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
        let bbox = owned.iter().fold(first.bbox, |b, g| b.union(&g.bbox));
        // 每个字形必须有完整墨迹证据，否则该字形退回保守 loose 盒。
        let formula_ink = owned
            .iter()
            .fold(ink_or_box(first), |b: Rect, g| b.union(&ink_or_box(g)));
        let Some(neighbor) = glyphs
            .iter()
            .filter(|g| {
                !ids.contains(&g.id)
                    && parent.bbox.contains(g.bbox.center())
                    && !regions
                        .iter()
                        .any(|r| r.kind == RegionKind::Formula && r.bbox.contains(g.bbox.center()))
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
            continue;
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
        if glyphs.iter().any(|g| {
            !ids.contains(&g.id)
                && !g.unicode.iter().all(|c| c.is_whitespace())
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
                ..
            } if *is_fill || *is_stroke => {
                overlaps(clip, *bbox)
                    && !(clip.contains(syncpdf_core::Point::new(bbox.x0, bbox.y0))
                        && clip.contains(syncpdf_core::Point::new(bbox.x1, bbox.y1)))
            }
            _ => false,
        }) {
            continue;
        }
        out.push(Formula {
            ids,
            source: SourceAtom {
                bbox: clip,
                baseline: neighbor.matrix.f,
            },
            row: neighbor.bbox,
        });
    }
    radical_sources(ir, regions, &glyphs, &mut out);
    out
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
