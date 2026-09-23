//! Bounded same-column refinement against immutable source + accepted target ink.
//! No raster redetection, cross-column move, or fit-driven font shrinking.
use super::frame::LayoutFrame;
use std::collections::{BTreeMap, BTreeSet};
use syncpdf_core::ir::{DisplayItem, PageIR, Paragraph, TypesetParagraph};
use syncpdf_core::{GlyphId, ParagraphId, Rect};
use syncpdf_typeset::Shaper;

pub(crate) fn frames(
    ir: &PageIR,
    para: &Paragraph,
    initial: &LayoutFrame,
    used: Rect,
    paragraphs: &BTreeMap<ParagraphId, Paragraph>,
    placed: &[TypesetParagraph],
    shaper: &dyn Shaper,
) -> Vec<LayoutFrame> {
    let mut removed: BTreeSet<GlyphId> = para.glyphs.iter().copied().collect();
    for laid in placed {
        if let Some(p) = paragraphs.get(&laid.id) {
            removed.extend(p.glyphs.iter().copied());
        }
    }
    let mut obstacles: Vec<_> = ir
        .glyphs()
        .filter(|g| {
            !removed.contains(&g.id)
                && !g.flags.invisible
                && !g.flags.outside_clip
                && (g.unicode.is_empty() || g.unicode.iter().any(|c| !c.is_whitespace()))
        })
        .map(|g| g.bbox)
        .collect();
    let moved_atoms: Vec<_> = std::iter::once(para)
        .chain(placed.iter().filter_map(|p| paragraphs.get(&p.id)))
        .flat_map(|p| &p.atoms)
        .filter_map(|a| a.source)
        .collect();
    obstacles.extend(
        ir.items
            .iter()
            .filter_map(|i| match i {
                DisplayItem::Image { bbox } | DisplayItem::InlineImage { bbox } => Some(*bbox),
                DisplayItem::Path {
                    bbox,
                    is_fill,
                    is_stroke,
                } if *is_fill || *is_stroke => {
                    let pad = if *is_stroke { 0.5 } else { 0.0 };
                    Some(Rect::new(
                        bbox.x0 - pad,
                        bbox.y0 - pad,
                        bbox.x1 + pad,
                        bbox.y1 + pad,
                    ))
                }
                _ => None,
            })
            .filter(|b| {
                !moved_atoms.iter().any(|s| {
                    s.bbox.x0 <= b.x0 && b.x1 <= s.bbox.x1 && s.bbox.y0 <= b.y0 && b.y1 <= s.bbox.y1
                })
            }),
    );
    for laid in placed {
        for line in &laid.lines {
            obstacles.extend(line.placed_atoms.iter().map(|a| a.bbox));
            for g in &line.glyphs {
                if g.text.chars().all(char::is_whitespace) {
                    continue;
                }
                let b = shaper
                    .glyph_bounds(g.font, g.gid, g.size)
                    .map(|b| {
                        Rect::new(
                            g.x + b.x0 * g.scale_x,
                            g.y + b.y0,
                            g.x + b.x1 * g.scale_x,
                            g.y + b.y1,
                        )
                    })
                    .unwrap_or(line.bbox);
                obstacles.push(b);
            }
        }
    }
    free_frames(para, initial, used, ir.crop_box, &obstacles)
}
fn free_frames(
    para: &Paragraph,
    initial: &LayoutFrame,
    used: Rect,
    crop: Rect,
    obstacles: &[Rect],
) -> Vec<LayoutFrame> {
    // Preserve horizontal ownership. Reclaim vertical space only, including space
    // released by shorter translated neighbors. Retained tables/rules stay solid.
    let mut blocked: Vec<_> = obstacles
        .iter()
        .filter(|b| b.x1 > initial.bbox.x0 && b.x0 < initial.bbox.x1)
        .map(|b| (b.y0 - 0.25, b.y1 + 0.25))
        .collect();
    blocked.sort_by(|a, b| a.0.total_cmp(&b.0));
    let mut bands = Vec::new();
    let mut floor = crop.y0;
    for (lo, hi) in blocked {
        if lo > floor {
            bands.push((floor, lo.min(crop.y1)));
        }
        floor = floor.max(hi);
    }
    if floor < crop.y1 {
        bands.push((floor, crop.y1));
    }
    let above = used.y1 - initial.first_baseline;
    let below = initial.first_baseline - used.y0;
    let mut frames = Vec::new();
    for (lo, hi) in bands {
        if hi <= para.bbox.y0 || lo >= para.bbox.y1 || hi - lo < used.height() {
            continue;
        }
        let min = lo + below;
        let max = hi - above;
        if min > max {
            continue;
        }
        frames.push(LayoutFrame {
            bbox: Rect::new(initial.bbox.x0, lo, initial.bbox.x1, hi),
            first_baseline: initial.first_baseline.clamp(min, max),
            obstacles: obstacles.to_vec(),
        });
    }
    frames.sort_by(|a, b| {
        (a.first_baseline - initial.first_baseline)
            .abs()
            .total_cmp(&(b.first_baseline - initial.first_baseline).abs())
    });
    frames
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn reclaimed_space_moves_baseline_without_crossing_column_or_retained_rule() {
        let para = Paragraph {
            id: ParagraphId { page: 1, seq: 1 },
            page: syncpdf_core::PageId(0),
            region: 0,
            kind: syncpdf_core::ir::RegionKind::Text,
            bbox: Rect::new(20., 80., 80., 120.),
            lines: vec![],
            glyphs: vec![],
            text_spans: vec![],
            style_runs: vec![],
            atoms: vec![],
            text: "x".into(),
            align: syncpdf_core::ir::Align::Left,
            first_indent: 0.,
            line_height: 12.,
            is_rtl: false,
            translatable: syncpdf_core::ir::Translatable::Yes,
        };
        let initial = LayoutFrame {
            bbox: Rect::new(20., 80., 80., 130.),
            first_baseline: 110.,
            obstacles: vec![],
        };
        let obstacles = [
            Rect::new(20., 60., 80., 70.),
            Rect::new(20., 140., 80., 150.),
            Rect::new(110., 70., 180., 150.),
        ];
        let result = free_frames(
            &para,
            &initial,
            Rect::new(20., 65., 80., 119.),
            Rect::new(0., 0., 200., 200.),
            &obstacles,
        );
        assert_eq!(result.len(), 1);
        assert_eq!(result[0].bbox, Rect::new(20., 70.25, 80., 139.75));
        assert_eq!(result[0].first_baseline, 115.25);
        assert_eq!(result[0].obstacles.len(), 3);
    }
}
