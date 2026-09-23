use super::*;
use stages::frame::LayoutFrame;
use syncpdf_core::Rect;
use syncpdf_typeset::Shaper;

/// Measure at the requested size, without retaining the old vertical frame or
/// treating source/neighbor ink as a reason to fail this geometry-only probe.
fn probe(
    target: &stages::link_text::Target,
    frame: &LayoutFrame,
    crop: Rect,
    shaper: &dyn Shaper,
    typography: stages::typeset::Typography,
) -> Option<TypesetParagraph> {
    let mut measure = frame.clone();
    measure.bbox.y0 = crop.y0;
    measure.bbox.y1 = crop.y1;
    measure.obstacles.clear();
    let result = stages::typeset::typeset_with_typography(
        shaper,
        &target.para,
        &target.parsed,
        &Obstacles::default(),
        Some(&measure),
        typography,
    );
    let p = result.paragraph;
    (!p.lines.is_empty()
        // A vertical overflow is precisely what moving the baseline can repair.
        // Reject an infeasible width here; the final placement checks all overflow.
        && p.used_bbox.x0 >= measure.bbox.x0 - 0.01
        && p.used_bbox.x1 <= measure.bbox.x1 + 0.01
        && p.lines.iter().flat_map(|l| &l.glyphs).all(|g| g.gid != 0))
    .then_some(p)
}

fn fit(
    target: &stages::link_text::Target,
    frame: &LayoutFrame,
    shaper: &dyn Shaper,
    typography: stages::typeset::Typography,
) -> Option<TypesetParagraph> {
    let result = stages::typeset::typeset_with_typography(
        shaper,
        &target.para,
        &target.parsed,
        &Obstacles::default(),
        Some(frame),
        typography,
    );
    (!result.paragraph.overflow
        && stages::link_text::geometry(target, &result.paragraph, shaper).is_some())
    .then_some(result.paragraph)
}

/// Refine only unsaved pages. All candidates are laid out from immutable source
/// and cached parsed translations; publication still uses one cloned transaction.
pub(super) fn refine_page(state: &mut RunState, page: u32, sink: &SharedSink) {
    let Some(bound) = state.bound.get(&page) else {
        return;
    };
    let crop = bound.ir.crop_box;
    // Whitespace between columns can be reused; the paper's outer text margin
    // remains a boundary even when the CropBox extends to the sheet edge.
    let mut text_area = crop;
    text_area.x1 = bound
        .ir
        .glyphs()
        .filter(|g| {
            !g.flags.invisible
                && !g.flags.outside_clip
                && (g.unicode.is_empty() || g.unicode.iter().any(|c| !c.is_whitespace()))
        })
        .map(|g| g.bbox.x1)
        .reduce(f32::max)
        .unwrap_or(crop.x1)
        .min(crop.x1);
    let shaper = StoreShaper::new(&state.font_store, &state.font_profile).with_role(Role::Body);
    let candidates: Vec<_> = state
        .targets
        .keys()
        .filter(|id| id.page == page + 1)
        .cloned()
        .collect();
    for round in 1..=3 {
        let mut improved = false;
        for id in &candidates {
            let placed: &[TypesetParagraph] =
                state.typeset_by_page.get(&page).map_or(&[], Vec::as_slice);
            if placed.iter().any(|p| p.id == *id) {
                continue;
            }
            let target = &state.targets[id];
            let para = &state.pars[id];
            let Some(initial) = state.frames.get(id) else {
                continue;
            };
            let obstacles =
                stages::refine::obstacles(&bound.ir, para, &state.pars, placed, &shaper, &[]);
            let wider = stages::refine::wider_measure(para, initial, text_area, &obstacles);
            let mut accepted = Vec::new();
            for measure in std::iter::once(initial).chain(wider.as_ref()) {
                let Some(measured) = probe(target, measure, crop, &shaper, state.typography) else {
                    continue;
                };
                for frame in
                    stages::refine::free_frames(para, measure, measured.used_bbox, crop, &obstacles)
                {
                    if let Some(result) = fit(target, &frame, &shaper, state.typography) {
                        accepted.push((frame, result));
                        break;
                    }
                }
                if !accepted.is_empty() {
                    break;
                }
            }
            if accepted.is_empty() {
                // Grow the contiguous same-column group only while needed.
                // The page's finite accepted paragraphs bound this search; fixed
                // source content still prevents a group from crossing an obstacle.
                let mut neighbors: Vec<_> = placed
                    .iter()
                    .filter(|p| {
                        let other = &state.pars[&p.id];
                        other.bbox.y1 <= para.bbox.y0
                            && (other.bbox.x0 - para.bbox.x0).abs() <= 4.0
                            && (other.bbox.x1.min(para.bbox.x1) - other.bbox.x0.max(para.bbox.x0))
                                > 0.8 * other.bbox.width().min(para.bbox.width())
                    })
                    .collect();
                neighbors.sort_by(|a, b| {
                    state.pars[&b.id]
                        .bbox
                        .y1
                        .total_cmp(&state.pars[&a.id].bbox.y1)
                });
                if let Some(measured) = probe(target, initial, crop, &shaper, state.typography) {
                    let mut group = vec![(para, initial, measured.used_bbox)];
                    let mut displaced = Vec::new();
                    for neighbor in neighbors {
                        let Some(frame) = state.frames.get(&neighbor.id) else {
                            break;
                        };
                        if !state.targets.contains_key(&neighbor.id) {
                            break;
                        }
                        group.push((&state.pars[&neighbor.id], frame, neighbor.used_bbox));
                        displaced.push(&neighbor.id);
                        let obstacles = stages::refine::obstacles(
                            &bound.ir,
                            para,
                            &state.pars,
                            placed,
                            &shaper,
                            &displaced,
                        );
                        for frames in stages::refine::group_frames(&group, crop, &obstacles) {
                            let results: Option<Vec<_>> = group
                                .iter()
                                .zip(frames)
                                .map(|((p, _, _), frame)| {
                                    fit(&state.targets[&p.id], &frame, &shaper, state.typography)
                                        .map(|laid| (frame, laid))
                                })
                                .collect();
                            let Some(results) = results else {
                                continue;
                            };
                            if results
                                .windows(2)
                                .any(|p| p[0].1.used_bbox.y0 - p[1].1.used_bbox.y1 < 0.24)
                            {
                                continue;
                            }
                            accepted = results;
                            break;
                        }
                        if !accepted.is_empty() {
                            break;
                        }
                    }
                }
            }
            if accepted.is_empty() {
                continue;
            }
            let group_size = accepted.len();
            for (frame, paragraph) in accepted {
                let changed_id = paragraph.id.clone();
                let old = &state.frames[&changed_id];
                let boxes = paragraph.lines.iter().map(|l| l.bbox).collect();
                let placements = state.typeset_by_page.entry(page).or_default();
                placements.retain(|p| p.id != changed_id);
                placements.push(paragraph);
                sink.emit(Event::Issue {
                    severity: Severity::Info,
                    code: "layout_refined".into(),
                    paragraph_id: Some(changed_id.clone()), page: Some(page + 1),
                    message: format!(
                        "局部动态bbox第{round}轮（联排段数={group_size}）：{:?} → {:?}，首基线 {:.3} → {:.3}；字号/行距不变",
                        old.bbox, frame.bbox, old.first_baseline, frame.first_baseline
                    ),
                });
                sink.emit(Event::Paragraph {
                    paragraph_id: changed_id.clone(),
                    page: page + 1,
                    status: ParagraphStatus::Typeset,
                    boxes: Some(boxes),
                    coord_system: syncpdf_core::CoordSystem::PdfUser,
                    translated_html: Some(state.targets[&changed_id].html.clone()),
                });
                state.frames.insert(changed_id, frame);
            }
            state.fallbacks -= 1;
            state.tgt_chars = state.tgt_chars - para.text.chars().count() as u64
                + target.parsed.text().chars().count() as u64;
            improved = true;
        }
        if !improved {
            break;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use syncpdf_core::ir::DisplayItem;

    fn pair_state(dir: &Path, block_below: bool) -> RunState {
        let (mut state, _) = super::super::transaction_tests::state(dir);
        let a: ParagraphId = "P01-001".parse().unwrap();
        let b: ParagraphId = "P01-002".parse().unwrap();
        let crop = Rect::new(0., 0., 300., 300.);
        state.bound.get_mut(&0).unwrap().ir.crop_box = crop;
        let mut first = state.pars[&a].clone();
        first.bbox = Rect::new(20., 180., 120., 215.);
        let mut next = first.clone();
        next.id = b.clone();
        next.glyphs.clear();
        next.bbox = Rect::new(20., 100., 120., 176.);
        let mut frame = LayoutFrame {
            bbox: Rect::new(20., 0., 120., 300.),
            first_baseline: 200.,
            obstacles: vec![],
        };
        let prepare = |p: &Paragraph, text: &str| {
            let html = format!("<p id=\"{}\">{text}</p>", p.id);
            let parsed = syncpdf_translate::parse_unit_html(&html).unwrap();
            stages::link_text::prepare(p, &state.bound[&0].ir, &state.doc, &parsed).unwrap()
        };
        let first_target = prepare(&first, "标题第一行<br>标题第二行");
        let next_target = prepare(&next, "下方正文甲<br>下方正文乙");
        let shaper = StoreShaper::new(&state.font_store, &state.font_profile);
        let first_laid = probe(&first_target, &frame, crop, &shaper, state.typography).unwrap();
        let next_probe = probe(&next_target, &frame, crop, &shaper, state.typography).unwrap();
        let next_baseline =
            frame.first_baseline + first_laid.used_bbox.y0 + 4. - next_probe.used_bbox.y1;
        frame.first_baseline = next_baseline;
        let next_laid = probe(&next_target, &frame, crop, &shaper, state.typography).unwrap();
        state.frames.insert(b.clone(), frame.clone());
        frame.first_baseline = 200.;
        state.frames.insert(a.clone(), frame);
        state.pars.insert(a.clone(), first);
        state.pars.insert(b.clone(), next);
        state.targets.insert(a, first_target);
        state.targets.insert(b, next_target);
        // Cap upward movement. A's current two lines overlap B by 4pt; only
        // moving both in order can succeed. A solid lower obstacle removes that option.
        let ir = &mut state.bound.get_mut(&0).unwrap().ir;
        ir.items.push(DisplayItem::Image {
            bbox: Rect::new(0., first_laid.used_bbox.y1 + 0.25, 300., 300.),
        });
        if block_below {
            ir.items.push(DisplayItem::Image {
                bbox: Rect::new(0., 0., 300., next_laid.used_bbox.y0 - 0.25),
            });
        }
        state.typeset_by_page.insert(0, vec![next_laid]);
        state.fallbacks = 1;
        state.tgt_chars = 100;
        state
    }

    #[test]
    fn pair_refinement_commits_both_placements_and_counts_only_the_recovered_block() {
        let dir = tempfile::tempdir().unwrap();
        let mut state = pair_state(dir.path(), false);
        let before = state.typeset_by_page[&0][0].clone();
        let sink = SharedSink::new(super::super::tests::RunRecorder::new(Arc::new(Mutex::new(
            Vec::new(),
        ))));
        refine_page(&mut state, 0, &sink);
        assert_eq!(state.fallbacks, 0);
        let laid = &state.typeset_by_page[&0];
        assert_eq!(laid.len(), 2);
        let next = laid.iter().find(|p| p.id == before.id).unwrap();
        let first = laid.iter().find(|p| p.id != before.id).unwrap();
        assert!(next.used_bbox.y1 < before.used_bbox.y1);
        assert!(first.used_bbox.y0 > next.used_bbox.y1);
        assert_eq!(next.line_height, before.line_height);
        assert_eq!(next.font_scale, before.font_scale);
        assert_eq!(next.lines[0].glyphs[0].size, before.lines[0].glyphs[0].size);
    }

    #[test]
    fn rejected_pair_keeps_accepted_neighbor_and_counters_unchanged() {
        let dir = tempfile::tempdir().unwrap();
        let mut state = pair_state(dir.path(), true);
        let before = state.typeset_by_page[&0].clone();
        let baseline = state.frames[&before[0].id].first_baseline;
        let sink = SharedSink::new(super::super::tests::RunRecorder::new(Arc::new(Mutex::new(
            Vec::new(),
        ))));
        refine_page(&mut state, 0, &sink);
        assert_eq!(state.typeset_by_page[&0], before);
        assert_eq!(state.frames[&before[0].id].first_baseline, baseline);
        assert_eq!(state.fallbacks, 1);
        assert_eq!(state.tgt_chars, 100);
    }

    #[test]
    fn measuring_near_page_bottom_keeps_vertical_overflow_available_for_relocation() {
        let dir = tempfile::tempdir().unwrap();
        let state = pair_state(dir.path(), false);
        let id: ParagraphId = "P01-001".parse().unwrap();
        let mut frame = state.frames[&id].clone();
        frame.first_baseline = 1.;
        let crop = state.bound[&0].ir.crop_box;
        let shaper = StoreShaper::new(&state.font_store, &state.font_profile);
        let measured = probe(&state.targets[&id], &frame, crop, &shaper, state.typography).unwrap();
        assert!(measured.overflow);
        assert!(measured.used_bbox.y0 < crop.y0);
        let spaces =
            stages::refine::free_frames(&state.pars[&id], &frame, measured.used_bbox, crop, &[]);
        assert_eq!(spaces.len(), 1);
        assert!(fit(&state.targets[&id], &spaces[0], &shaper, state.typography).is_some());
    }
}
