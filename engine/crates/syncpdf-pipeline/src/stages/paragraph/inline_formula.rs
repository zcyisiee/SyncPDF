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
        let mut clip = bbox;
        // Include fraction bars and other local vector ink, never a crossing rule/image.
        for item in &ir.items {
            if let DisplayItem::Path {
                bbox,
                is_fill,
                is_stroke,
            } = item
            {
                if (*is_fill || *is_stroke)
                    && r.bbox.contains(bbox.center())
                    && bbox.width() <= r.bbox.width() + 1.0
                    && bbox.height() <= r.bbox.height() + 1.0
                {
                    let pad = if *is_stroke { 0.5 } else { 0.05 };
                    clip = clip.union(&Rect::new(
                        bbox.x0 - pad,
                        bbox.y0 - pad,
                        bbox.x1 + pad,
                        bbox.y1 + pad,
                    ));
                }
            }
        }
        if glyphs.iter().any(|g| {
            !ids.contains(&g.id)
                && !g.unicode.iter().all(|c| c.is_whitespace())
                && overlaps(clip, g.bbox)
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
    out
}

fn overlaps(a: Rect, b: Rect) -> bool {
    a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1
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
