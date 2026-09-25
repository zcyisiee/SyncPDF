//! Bounded local refinement against immutable source + accepted target ink.
//! No raster redetection, cross-page move, or fit-driven font shrinking.
use super::frame::LayoutFrame;
use std::collections::{BTreeMap, BTreeSet};
use syncpdf_core::ir::{DisplayItem, PageIR, Paragraph, TypesetParagraph};
use syncpdf_core::{GlyphId, OpKey, ParagraphId, Rect};
use syncpdf_typeset::Shaper;

pub(crate) fn obstacles(
    ir: &PageIR,
    moving: &[&Paragraph],
    paragraphs: &BTreeMap<ParagraphId, Paragraph>,
    placed: &[TypesetParagraph],
    shaper: &dyn Shaper,
    displaced: &[&ParagraphId],
) -> Vec<Rect> {
    // Moving paragraphs (placed or not) release their source ink to the layout.
    let mut removed: BTreeSet<GlyphId> = moving
        .iter()
        .flat_map(|p| p.glyphs.iter().copied())
        .collect();
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
    let own_ops: BTreeSet<OpKey> = moving
        .iter()
        .copied()
        .chain(placed.iter().filter_map(|p| paragraphs.get(&p.id)))
        .flat_map(super::source_decoration::owned_ops)
        .collect();
    let moved_atoms: Vec<_> = moving
        .iter()
        .copied()
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
                    stroke,
                    ..
                } if *is_fill || *is_stroke => {
                    // Only an owner that already holds its underline anchor may
                    // set its own source line aside; every other line is paint.
                    if stroke.is_some_and(|s| own_ops.contains(&s.op)) {
                        return None;
                    }
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
        if displaced.contains(&&laid.id) {
            continue;
        }
        for line in &laid.lines {
            obstacles.extend(line.placed_atoms.iter().map(|a| a.bbox));
            obstacles.extend(line.underlines.iter().map(|u| u.bbox));
            for g in &line.glyphs {
                if !g.text.is_empty() && g.text.chars().all(char::is_whitespace) {
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

/// Target ink gap between two stacked paragraphs: the source gap, widened by
/// how much looser the target line pitch is than the source's (`upper`/`lower`).
pub(crate) fn separation(source_gap: f32, upper: f32, lower: f32) -> f32 {
    source_gap.max(0.25) + upper.max(lower).max(0.0)
}

/// Repack a contiguous group in its existing column. Retain each measure,
/// paragraph separation and reading order, and never jump across a fixed obstacle.
///
/// `leading[i]` is how much member `i`'s target line pitch exceeds its source
/// pitch. A source paragraph gap is read relative to the source leading, so the
/// repacked ink gap grows by the same amount; otherwise a looser target line
/// pitch makes the paragraph break indistinguishable from a line break.
pub(crate) fn group_frames(
    group: &[(&Paragraph, &LayoutFrame, Rect)],
    leading: &[f32],
    crop: Rect,
    obstacles: &[Rect],
) -> Vec<Vec<LayoutFrame>> {
    let Some(&(para, initial, used)) = group.first() else {
        return Vec::new();
    };
    let gap = |index: usize| {
        separation(
            group[index - 1].0.bbox.y0 - group[index].0.bbox.y1,
            leading.get(index - 1).copied().unwrap_or(0.0),
            leading.get(index).copied().unwrap_or(0.0),
        )
    };
    let mut measure = initial.clone();
    let mut height = used.height();
    for (index, (_, frame, ink)) in group.iter().enumerate().skip(1) {
        measure.bbox.x0 = measure.bbox.x0.min(frame.bbox.x0);
        measure.bbox.x1 = measure.bbox.x1.max(frame.bbox.x1);
        height += gap(index) + ink.height();
    }
    let combined = Rect::new(measure.bbox.x0, used.y1 - height, measure.bbox.x1, used.y1);
    free_frames(para, &measure, combined, crop, obstacles)
        .into_iter()
        .filter(|space| {
            group
                .iter()
                .all(|(p, _, _)| space.bbox.y1 > p.bbox.y0 && space.bbox.y0 < p.bbox.y1)
        })
        .map(|space| {
            let mut top = used.y1 + space.first_baseline - initial.first_baseline;
            group
                .iter()
                .enumerate()
                .map(|(index, (_, frame, ink))| {
                    if index > 0 {
                        top -= gap(index);
                    }
                    let mut placed = space.clone();
                    placed.bbox.x0 = frame.bbox.x0;
                    placed.bbox.x1 = frame.bbox.x1;
                    placed.first_baseline = top - (ink.y1 - frame.first_baseline);
                    top -= ink.height();
                    placed
                })
                .collect()
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
            decorations: Vec::new(),
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
        let pairs = group_frames(
            &[(&para, &initial, used), (&neighbor, &next, next_used)],
            &[],
            crop,
            &obstacles,
        );
        assert_eq!(pairs.len(), 1);
        let (a, b) = (&pairs[0][0], &pairs[0][1]);
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
        assert!(group_frames(
            &[(&para, &initial, used), (&neighbor, &next, next_used)],
            &[],
            crop,
            &blocked
        )
        .is_empty());
        // Insufficient total clearance also fails without changing either input.
        assert!(group_frames(
            &[(&para, &initial, used), (&neighbor, &next, next_used)],
            &[],
            Rect::new(0., 50., 200., 200.),
            &obstacles
        )
        .is_empty());
        assert_eq!(next.first_baseline, 65.);
        // Even abundant space above a separator cannot justify dragging a source
        // paragraph from below that separator into the preceding section.
        assert!(group_frames(
            &[(&para, &initial, used), (&neighbor, &next, next_used)],
            &[],
            Rect::new(0., 0., 200., 500.),
            &[Rect::new(20., 75., 80., 75.5)],
        )
        .is_empty());
    }

    #[test]
    fn looser_target_leading_widens_paragraph_gap_and_tighter_never_narrows_it() {
        let (para, initial) = fixture();
        let mut neighbor = para.clone();
        neighbor.bbox = Rect::new(20., 30., 80., 70.);
        let next = LayoutFrame {
            first_baseline: 65.,
            ..initial.clone()
        };
        let crop = Rect::new(0., 0., 200., 200.);
        let obstacles = [Rect::new(20., 120., 80., 130.), Rect::new(20., 0., 80., 5.)];
        let used = Rect::new(20., 60., 80., 119.);
        let next_used = Rect::new(20., 45., 80., 70.);
        let group = [(&para, &initial, used), (&neighbor, &next, next_used)];
        let ink_gap = |leading: &[f32]| {
            let frames = group_frames(&group, leading, crop, &obstacles);
            let (a, b) = (&frames[0][0], &frames[0][1]);
            let top = used.y0 + a.first_baseline - initial.first_baseline;
            top - (next_used.y1 + b.first_baseline - next.first_baseline)
        };
        // Source gap 10pt; the lower paragraph's lines are 3pt looser.
        assert_eq!(ink_gap(&[0., 3.]), 13.);
        assert_eq!(ink_gap(&[3., -2.]), 13.);
        assert_eq!(ink_gap(&[-2., -2.]), 10.);
        assert_eq!(separation(-1., 0., 0.), 0.25);
    }

    #[test]
    fn longer_group_can_reuse_space_after_all_successive_neighbors() {
        let (para, initial) = fixture();
        let mut middle = para.clone();
        middle.bbox = Rect::new(20., 30., 80., 70.);
        let middle_frame = LayoutFrame {
            first_baseline: 65.,
            ..initial.clone()
        };
        let mut last = para.clone();
        last.bbox = Rect::new(20., 0., 80., 20.);
        let last_frame = LayoutFrame {
            first_baseline: 15.,
            ..initial.clone()
        };
        let used = Rect::new(20., 60., 80., 119.);
        let middle_used = Rect::new(20., 45., 80., 70.);
        let last_used = Rect::new(20., 10., 80., 20.);
        let crop = Rect::new(0., -20., 200., 200.);
        let fixed = [Rect::new(20., 120., 80., 130.)];
        let first_two = [
            (&para, &initial, used),
            (&middle, &middle_frame, middle_used),
        ];
        let mut occupied = fixed.to_vec();
        occupied.push(Rect::new(20., 22., 80., 32.));
        assert!(group_frames(&first_two, &[], crop, &occupied).is_empty());
        let frames = group_frames(
            &[first_two[0], first_two[1], (&last, &last_frame, last_used)],
            &[],
            crop,
            &fixed,
        );
        assert_eq!(frames.len(), 1);
        assert_eq!(frames[0].len(), 3);
        let group = &frames[0];
        assert!(group[0].first_baseline > group[1].first_baseline);
        assert!(group[1].first_baseline > group[2].first_baseline);
        let middle_top = middle_used.y1 + group[1].first_baseline - middle_frame.first_baseline;
        let last_top = last_used.y1 + group[2].first_baseline - last_frame.first_baseline;
        assert!((used.y0 - middle_top - 10.).abs() < 0.001);
        assert!((middle_top - middle_used.height() - last_top - 10.).abs() < 0.001);
    }
}
