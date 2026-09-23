//! Bounded local refinement against immutable source + accepted target ink.
//! No raster redetection, cross-page move, or fit-driven font shrinking.
use super::frame::LayoutFrame;
use std::collections::{BTreeMap, BTreeSet};
use syncpdf_core::ir::{DisplayItem, PageIR, Paragraph, TypesetParagraph};
use syncpdf_core::{GlyphId, ParagraphId, Rect};
use syncpdf_typeset::Shaper;

pub(crate) fn obstacles(
    ir: &PageIR,
    para: &Paragraph,
    paragraphs: &BTreeMap<ParagraphId, Paragraph>,
    placed: &[TypesetParagraph],
    shaper: &dyn Shaper,
    displaced: Option<&ParagraphId>,
) -> Vec<Rect> {
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
        if displaced == Some(&laid.id) {
            continue;
        }
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
    obstacles
}
pub(crate) fn free_frames(
    para: &Paragraph,
    initial: &LayoutFrame,
    used: Rect,
    crop: Rect,
    obstacles: &[Rect],
) -> Vec<LayoutFrame> {
    // Reclaim vertical space for the chosen horizontal measure, including space
    // released by translated neighbors. Retained tables/rules stay solid.
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
/// Keep the left anchor and extend to the first visible obstacle on the right.
/// Recompute vertical clearance at this width before accepting any layout.
pub(crate) fn wider_measure(
    para: &Paragraph,
    initial: &LayoutFrame,
    crop: Rect,
    obstacles: &[Rect],
) -> Option<LayoutFrame> {
    let right = obstacles
        .iter()
        .filter(|b| b.y1 > para.bbox.y0 && b.y0 < para.bbox.y1 && b.x0 >= para.bbox.x1)
        .map(|b| b.x0 - 0.25)
        .fold(crop.x1, f32::min);
    if right <= initial.bbox.x1 + 0.01 {
        return None;
    }
    let mut frame = initial.clone();
    frame.bbox.x1 = right;
    Some(frame)
}

/// Allocate a failed paragraph and its immediately following accepted neighbor
/// as one vertical group. Preserve their reading order, widths and source gap.
pub(crate) fn pair_frames(
    first: (&Paragraph, &LayoutFrame, Rect),
    next: (&Paragraph, &LayoutFrame, Rect),
    crop: Rect,
    obstacles: &[Rect],
) -> Vec<(LayoutFrame, LayoutFrame)> {
    let (para, initial, used) = first;
    let (neighbor, next_initial, next_used) = next;
    let gap = (para.bbox.y0 - neighbor.bbox.y1).max(0.25);
    let mut group = initial.clone();
    group.bbox.x0 = initial.bbox.x0.min(next_initial.bbox.x0);
    group.bbox.x1 = initial.bbox.x1.max(next_initial.bbox.x1);
    let group_used = Rect::new(
        group.bbox.x0,
        used.y0 - gap - next_used.height(),
        group.bbox.x1,
        used.y1,
    );
    free_frames(para, &group, group_used, crop, obstacles)
        .into_iter()
        .map(|space| {
            let shift = space.first_baseline - initial.first_baseline;
            let mut a = space.clone();
            a.bbox.x0 = initial.bbox.x0;
            a.bbox.x1 = initial.bbox.x1;
            let mut b = space;
            b.bbox.x0 = next_initial.bbox.x0;
            b.bbox.x1 = next_initial.bbox.x1;
            b.first_baseline = used.y0 + shift - gap - (next_used.y1 - next_initial.first_baseline);
            (a, b)
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture() -> (Paragraph, LayoutFrame) {
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
        (para, initial)
    }
    #[test]
    fn reclaimed_space_moves_baseline_without_crossing_column_or_retained_rule() {
        let (para, initial) = fixture();
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

    #[test]
    fn horizontal_gap_stops_at_neighbor_and_checks_new_width_vertical_clearance() {
        let (para, initial) = fixture();
        let crop = Rect::new(0., 0., 200., 200.);
        let obstacles = [
            Rect::new(110., 50., 180., 150.),
            // A retained image above the paragraph, outside the old width.
            Rect::new(90., 130., 100., 180.),
        ];
        let wider = wider_measure(&para, &initial, crop, &obstacles).unwrap();
        assert_eq!(wider.bbox.x0, initial.bbox.x0);
        assert_eq!(wider.bbox.x1, 109.75);
        let frames = free_frames(
            &para,
            &wider,
            Rect::new(20., 85., 108., 119.),
            crop,
            &obstacles,
        );
        assert_eq!(frames.len(), 1);
        assert_eq!(frames[0].bbox.y1, 129.75);
        assert_eq!(frames[0].obstacles, obstacles);
    }

    #[test]
    fn adjacent_paragraphs_share_vertical_space_but_preserve_order_and_gap() {
        let (para, initial) = fixture();
        let mut neighbor = para.clone();
        neighbor.bbox = Rect::new(20., 30., 80., 70.);
        let next = LayoutFrame {
            first_baseline: 65.,
            ..initial.clone()
        };
        let crop = Rect::new(0., 0., 200., 200.);
        let obstacles = [
            Rect::new(20., 120., 80., 130.),
            Rect::new(20., 10., 80., 20.),
        ];
        let used = Rect::new(20., 60., 80., 119.);
        let next_used = Rect::new(20., 45., 80., 70.);
        let pairs = pair_frames(
            (&para, &initial, used),
            (&neighbor, &next, next_used),
            crop,
            &obstacles,
        );
        assert_eq!(pairs.len(), 1);
        let (a, b) = &pairs[0];
        assert_eq!(a.first_baseline, 110.);
        assert_eq!(b.first_baseline, 45.);
        assert_eq!(
            used.y0 - (next_used.y1 + b.first_baseline - next.first_baseline),
            10.
        );
        assert_eq!(a.bbox.x0, initial.bbox.x0);
        assert_eq!(b.bbox.x1, next.bbox.x1);

        // An intervening protected rule prevents the group from being repacked.
        let mut blocked = obstacles.to_vec();
        blocked.push(Rect::new(20., 75., 80., 75.5));
        assert!(pair_frames(
            (&para, &initial, used),
            (&neighbor, &next, next_used),
            crop,
            &blocked
        )
        .is_empty());
        // Insufficient total clearance also fails without changing either input.
        assert!(pair_frames(
            (&para, &initial, used),
            (&neighbor, &next, next_used),
            Rect::new(0., 50., 200., 200.),
            &obstacles
        )
        .is_empty());
        assert_eq!(next.first_baseline, 65.);
    }
}
