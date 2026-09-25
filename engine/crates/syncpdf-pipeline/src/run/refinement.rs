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

/// Whitespace between columns can be reused; the paper's outer text margin
/// remains a boundary even when the CropBox extends to the sheet edge.
fn text_area(ir: &syncpdf_core::ir::PageIR) -> Rect {
    let crop = ir.crop_box;
    let mut area = crop;
    area.x1 = ir
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
    area
}

/// Refine only unsaved pages. All candidates are laid out from immutable source
/// and cached parsed translations; publication still uses one cloned transaction.
pub(super) fn refine_page(state: &mut RunState, page: u32, sink: &SharedSink) {
    let Some(bound) = state.bound.get(&page) else {
        return;
    };
    let crop = bound.ir.crop_box;
    let text_area = text_area(&bound.ir);
    // 角色按各 target 的区域类别决定（只影响选字；obstacles/几何只用
    // glyph_bounds），candidates 循环内逐 target 重建。
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
            let bound = &state.bound[&page];
            let target = &state.targets[id];
            let shaper = state.shaper_for(&target.para);
            let para = &state.pars[id];
            let Some(initial) = state.frames.get(id) else {
                continue;
            };
            let obstacles =
                stages::refine::obstacles(&bound.ir, &[para], &state.pars, placed, &shaper, &[]);
            let wider = stages::refine::wider_measure(para, initial, text_area, &obstacles);
            let mut accepted = Vec::new();
            for measure in std::iter::once(initial).chain(wider.as_ref()) {
                let Some(measured) =
                    probe(target, measure, crop, &shaper, state.typography_for(id))
                else {
                    continue;
                };
                for frame in
                    stages::refine::free_frames(para, measure, measured.used_bbox, crop, &obstacles)
                {
                    if let Some(result) = fit(target, &frame, &shaper, state.typography_for(id)) {
                        accepted.push((frame, result));
                        break;
                    }
                }
                if !accepted.is_empty() {
                    break;
                }
            }
            if accepted.is_empty() {
                accepted = reflow_column(state, bound, id, placed, crop, text_area, &shaper);
            }
            if accepted.is_empty() {
                continue;
            }
            commit(state, page, round, accepted, sink);
            improved = true;
        }
        if !improved {
            break;
        }
    }
    restore_separation(state, page, sink);
}

/// A placement can spend half of a source gap, which at a looser target line
/// pitch leaves adjacent paragraphs no more apart than their own lines. Repack
/// any same-column stack whose members are closer than their paragraph
/// separation; a stack that cannot be repacked keeps its current layout.
fn restore_separation(state: &mut RunState, page: u32, sink: &SharedSink) {
    let Some(crop) = state.bound.get(&page).map(|b| b.ir.crop_box) else {
        return;
    };
    let mut done = BTreeSet::new();
    let ids: Vec<ParagraphId> = state
        .typeset_by_page
        .get(&page)
        .map(|v| v.iter().map(|p| p.id.clone()).collect())
        .unwrap_or_default();
    for id in ids {
        if done.contains(&id) {
            continue;
        }
        let placed: &[TypesetParagraph] =
            state.typeset_by_page.get(&page).map_or(&[], Vec::as_slice);
        let Some(laid) = placed.iter().find(|p| p.id == id) else {
            continue;
        };
        let para = &state.pars[&id];
        // The nearest placed paragraph below in the same column.
        let below = placed
            .iter()
            .filter(|o| {
                let other = &state.pars[&o.id];
                other.bbox.y1 <= para.bbox.y0 + 0.5
                    && (other.bbox.x0 - para.bbox.x0).abs() <= 4.0
                    && (other.bbox.x1.min(para.bbox.x1) - other.bbox.x0.max(para.bbox.x0))
                        > 0.8 * other.bbox.width().min(para.bbox.width())
            })
            .max_by(|a, b| {
                state.pars[&a.id]
                    .bbox
                    .y1
                    .total_cmp(&state.pars[&b.id].bbox.y1)
            });
        let Some(next) = below else {
            continue;
        };
        let other = &state.pars[&next.id];
        let required = stages::refine::separation(
            para.bbox.y0 - other.bbox.y1,
            laid.line_height - para.line_height,
            next.line_height - other.line_height,
        );
        if laid.used_bbox.y0 - next.used_bbox.y1 >= required - 0.01 {
            continue;
        }
        let shaper = state.shaper_for(para);
        let bound = &state.bound[&page];
        let accepted = reflow_column(
            state,
            bound,
            &id,
            placed,
            crop,
            text_area(&bound.ir),
            &shaper,
        );
        if accepted.is_empty() {
            continue;
        }
        done.extend(accepted.iter().map(|(_, p)| p.id.clone()));
        commit(state, page, 0, accepted, sink);
    }
}

/// Publish one accepted group: replace members' placements and frames, and
/// count every member that had no placement as a recovered fallback.
fn commit(
    state: &mut RunState,
    page: u32,
    round: u32,
    accepted: Vec<(LayoutFrame, TypesetParagraph)>,
    sink: &SharedSink,
) {
    let group_size = accepted.len();
    for (frame, paragraph) in accepted {
        let changed_id = paragraph.id.clone();
        // Every member that had no placement is a recovered fallback.
        let recovered = !state
            .typeset_by_page
            .get(&page)
            .is_some_and(|v| v.iter().any(|p| p.id == changed_id));
        if recovered {
            state.fallbacks -= 1;
            state.tgt_chars = state.tgt_chars - state.pars[&changed_id].text.chars().count() as u64
                + state.targets[&changed_id].parsed.text().chars().count() as u64;
        }
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
        sink.emit(super::paragraph_event(
            &state.pars[&changed_id],
            ParagraphStatus::Typeset,
            Some(boxes),
            Some(state.targets[&changed_id].html.clone()),
        ));
        state.frames.insert(changed_id, frame);
    }
}

/// Repack the whole same-column stack around `id` as one flow. The stack runs
/// through translated paragraphs (placed or not) until retained content or a
/// fixed obstacle; every internal gap is re-read against the target leading,
/// and the stack keeps its source clearance to content above and below. Only
/// an open side may grow, and only to the page's body extent: a stack never
/// takes the page margin, and a page that is simply too dense stays a fallback
/// for the document-wide line-height decision.
fn reflow_column(
    state: &RunState,
    bound: &BoundPage,
    id: &ParagraphId,
    placed: &[TypesetParagraph],
    crop: Rect,
    text_area: Rect,
    shaper: &StoreShaper<'_>,
) -> Vec<(LayoutFrame, TypesetParagraph)> {
    let para = &state.pars[id];
    let same_column = |other: &Paragraph| {
        (other.bbox.x0 - para.bbox.x0).abs() <= 4.0
            && (other.bbox.x1.min(para.bbox.x1) - other.bbox.x0.max(para.bbox.x0))
                > 0.8 * other.bbox.width().min(para.bbox.width())
    };
    // A member's frame and ink: its accepted placement, or a probe of its
    // translation in its initial (or widened) frame; plus how much looser its
    // target line pitch is than the source's.
    let member = |pid: &ParagraphId, wide: Option<&'_ LayoutFrame>| {
        let target = state.targets.get(pid)?;
        let source_pitch = state.pars[pid].line_height;
        if wide.is_none() {
            if let Some(laid) = placed.iter().find(|p| p.id == *pid) {
                let frame = state.frames.get(pid)?.clone();
                return Some((frame, laid.used_bbox, laid.line_height - source_pitch));
            }
        }
        let frame = wide.or(state.frames.get(pid))?.clone();
        let shaper = state.shaper_for(&target.para);
        probe(target, &frame, crop, &shaper, state.typography_for(pid))
            .map(|p| (frame, p.used_bbox, p.line_height - source_pitch))
    };
    let page_paras: Vec<&Paragraph> = state
        .pars
        .values()
        .filter(|p| p.page == para.page)
        .collect();
    let mut column: Vec<&Paragraph> = page_paras
        .iter()
        .filter(|p| same_column(p))
        .copied()
        .collect();
    column.sort_by(|a, b| b.bbox.y1.total_cmp(&a.bbox.y1));
    let Some(at) = column.iter().position(|p| p.id == *id) else {
        return Vec::new();
    };
    // Vertically chained neighbors only: an overlapping one is not a stack.
    let chained = |upper: &Paragraph, lower: &Paragraph| upper.bbox.y0 >= lower.bbox.y1 - 0.5;
    let mut first = at;
    while first > 0
        && chained(column[first - 1], column[first])
        && state.targets.contains_key(&column[first - 1].id)
    {
        first -= 1;
    }
    let mut last = at;
    while last + 1 < column.len()
        && chained(column[last], column[last + 1])
        && state.targets.contains_key(&column[last + 1].id)
    {
        last += 1;
    }
    let mut stack = column[first..=last].to_vec();
    let mut obstacles = {
        let displaced: Vec<&ParagraphId> = stack.iter().map(|p| &p.id).collect();
        stages::refine::obstacles(&bound.ir, &stack, &state.pars, placed, shaper, &displaced)
    };
    let x_range = |b: &Rect, members: &[&Paragraph]| {
        members.iter().any(|p| b.x1 > p.bbox.x0 && b.x0 < p.bbox.x1)
    };
    // Cut the stack at fixed content between two members.
    let cuts: Vec<usize> = (1..stack.len())
        .filter(|&i| {
            let (upper, lower) = (stack[i - 1], stack[i]);
            obstacles.iter().any(|b| {
                let y = (b.y0 + b.y1) / 2.0;
                x_range(b, &[upper, lower]) && y < upper.bbox.y0 && y > lower.bbox.y1
            })
        })
        .collect();
    if !cuts.is_empty() {
        let pos = stack.iter().position(|p| p.id == *id).unwrap_or(0);
        let lo = cuts
            .iter()
            .copied()
            .filter(|&c| c <= pos)
            .max()
            .unwrap_or(0);
        let hi = cuts
            .iter()
            .copied()
            .find(|&c| c > pos)
            .unwrap_or(stack.len());
        stack = stack[lo..hi].to_vec();
        let displaced: Vec<&ParagraphId> = stack.iter().map(|p| &p.id).collect();
        obstacles =
            stages::refine::obstacles(&bound.ir, &stack, &state.pars, placed, shaper, &displaced);
    }
    if stack.len() < 2 {
        return Vec::new();
    }
    // Vertical limits: the source edge where content lies beyond it, else the
    // body extent of the page (translatable paragraphs), never the margin.
    let top = stack[0].bbox.y1;
    let bottom = stack[stack.len() - 1].bbox.y0;
    let body_top = page_paras
        .iter()
        .filter(|p| p.translatable == syncpdf_core::ir::Translatable::Yes)
        .map(|p| p.bbox.y1)
        .fold(top, f32::max);
    let body_bottom = page_paras
        .iter()
        .filter(|p| p.translatable == syncpdf_core::ir::Translatable::Yes)
        .map(|p| p.bbox.y0)
        .fold(bottom, f32::min);
    let ceiling = if obstacles
        .iter()
        .any(|b| x_range(b, &stack) && b.y0 >= top - 0.5)
    {
        top
    } else {
        body_top
    };
    let floor = if obstacles
        .iter()
        .any(|b| x_range(b, &stack) && b.y1 <= bottom + 0.5)
    {
        bottom
    } else {
        body_bottom
    };
    let limits = Rect::new(crop.x0, floor.max(crop.y0), crop.x1, ceiling.min(crop.y1));
    // Each member keeps its own measure first. A stack still too tall may
    // widen every member to the right, as a single paragraph may: a short
    // source line's extent is not the column's measure.
    let widened: Vec<Option<LayoutFrame>> = stack
        .iter()
        .map(|p| {
            let frame = state.frames.get(&p.id)?;
            stages::refine::wider_measure(p, frame, text_area, &obstacles)
        })
        .collect();
    for widen in [false, true] {
        if widen && widened.iter().all(Option::is_none) {
            break;
        }
        let mut group = Vec::new();
        let mut leading = Vec::new();
        for (p, wide) in stack.iter().zip(&widened) {
            let wide = wide.as_ref().filter(|_| widen);
            let Some((frame, used, lead)) = member(&p.id, wide) else {
                return Vec::new();
            };
            group.push((*p, frame, used));
            leading.push(lead);
        }
        let group: Vec<_> = group.iter().map(|(p, f, u)| (*p, f, *u)).collect();
        for frames in stages::refine::group_frames(&group, &leading, limits, &obstacles) {
            let results: Option<Vec<_>> = group
                .iter()
                .zip(frames)
                .map(|((p, _, _), frame)| {
                    let target = &state.targets[&p.id];
                    let shaper = state.shaper_for(&target.para);
                    fit(target, &frame, &shaper, state.typography_for(&p.id))
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
            return results;
        }
    }
    Vec::new()
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

    /// Both paragraphs placed, the lower one's ink 0.5pt under the upper one's
    /// although their source boxes are 4pt apart.
    fn touching_state(dir: &Path, block_below: bool) -> RunState {
        let mut state = pair_state(dir, block_below);
        let a: ParagraphId = "P01-001".parse().unwrap();
        let b: ParagraphId = "P01-002".parse().unwrap();
        let crop = state.bound[&0].ir.crop_box;
        let shaper = StoreShaper::new(&state.font_store, &state.font_profile);
        let first = probe(
            &state.targets[&a],
            &state.frames[&a],
            crop,
            &shaper,
            state.typography,
        )
        .unwrap();
        let mut frame = state.frames[&b].clone();
        let next = probe(&state.targets[&b], &frame, crop, &shaper, state.typography).unwrap();
        frame.first_baseline += first.used_bbox.y0 - 0.5 - next.used_bbox.y1;
        let next = probe(&state.targets[&b], &frame, crop, &shaper, state.typography).unwrap();
        state.frames.insert(b, frame);
        if block_below {
            let ir = &mut state.bound.get_mut(&0).unwrap().ir;
            ir.items.pop();
            ir.items.push(DisplayItem::Image {
                bbox: Rect::new(0., 0., 300., next.used_bbox.y0 - 0.25),
            });
        }
        state.typeset_by_page.insert(0, vec![first, next]);
        state.fallbacks = 0;
        state
    }

    fn ink_gap(state: &RunState) -> f32 {
        let laid = &state.typeset_by_page[&0];
        let find = |id: &str| laid.iter().find(|p| p.id.to_string() == id).unwrap();
        find("P01-001").used_bbox.y0 - find("P01-002").used_bbox.y1
    }

    #[test]
    fn touching_stack_is_repacked_to_its_paragraph_separation() {
        let dir = tempfile::tempdir().unwrap();
        let mut state = touching_state(dir.path(), false);
        assert!(ink_gap(&state) < 1.);
        let log = Arc::new(Mutex::new(Vec::new()));
        let sink = SharedSink::new(super::super::tests::RunRecorder::new(log.clone()));
        restore_separation(&mut state, 0, &sink);
        assert!(ink_gap(&state) >= 4. - 0.01, "{}", ink_gap(&state));
        assert_eq!(state.fallbacks, 0);
        assert_eq!(state.typeset_by_page[&0].len(), 2);
        assert!(log.lock().unwrap().iter().any(|(_, e)| matches!(e,
            Event::Issue { code, .. } if code == "layout_refined")));
    }

    #[test]
    fn touching_stack_without_room_keeps_its_layout() {
        let dir = tempfile::tempdir().unwrap();
        let mut state = touching_state(dir.path(), true);
        let before = state.typeset_by_page[&0].clone();
        let sink = SharedSink::new(super::super::tests::RunRecorder::new(Arc::new(Mutex::new(
            Vec::new(),
        ))));
        restore_separation(&mut state, 0, &sink);
        assert_eq!(state.typeset_by_page[&0], before);
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

    /// A (unplaced) needs a third line; B below is a short source line whose
    /// translation wraps at its source extent but fits one line at the column
    /// measure. The stack has room for A's third line only if B widens.
    fn narrow_tail_state(dir: &Path, blocked_right: bool) -> RunState {
        let (mut state, _) = super::super::transaction_tests::state(dir);
        let a: ParagraphId = "P01-001".parse().unwrap();
        let b: ParagraphId = "P01-002".parse().unwrap();
        let crop = Rect::new(0., 0., 300., 300.);
        state.bound.get_mut(&0).unwrap().ir.crop_box = crop;
        let right = text_area(&state.bound[&0].ir).x1;
        assert!(right >= 90., "fixture text area too narrow: {right}");
        let mut first = state.pars[&a].clone();
        first.bbox = Rect::new(20., 180., right, 215.);
        let mut next = first.clone();
        next.id = b.clone();
        next.glyphs.clear();
        next.bbox = Rect::new(20., 150., 56., 176.);
        let prepare = |p: &Paragraph, text: &str| {
            let html = format!("<p id=\"{}\">{text}</p>", p.id);
            let parsed = syncpdf_translate::parse_unit_html(&html).unwrap();
            stages::link_text::prepare(p, &state.bound[&0].ir, &state.doc, &parsed).unwrap()
        };
        let first_target = prepare(&first, "甲乙<br>丙丁<br>戊己");
        let next_target = prepare(&next, "子丑寅卯辰");
        let shaper = StoreShaper::new(&state.font_store, &state.font_profile);
        let first_frame = LayoutFrame {
            bbox: Rect::new(20., 0., right, 300.),
            first_baseline: 200.,
            obstacles: vec![],
        };
        let first_laid =
            probe(&first_target, &first_frame, crop, &shaper, state.typography).unwrap();
        let mut narrow = LayoutFrame {
            bbox: Rect::new(20., 0., 56., 300.),
            first_baseline: 160.,
            obstacles: vec![],
        };
        let mut wide = narrow.clone();
        wide.bbox.x1 = right;
        let two = probe(&next_target, &narrow, crop, &shaper, state.typography).unwrap();
        let one = probe(&next_target, &wide, crop, &shaper, state.typography).unwrap();
        assert_eq!((two.lines.len(), one.lines.len()), (2, 1));
        let sep = stages::refine::separation(
            4.,
            first_laid.line_height - first.line_height,
            one.line_height - next.line_height,
        );
        // Room for A's three lines, the separation and B's single line only.
        let floor = first_laid.used_bbox.y0 - sep - one.used_bbox.height() - 1.;
        next.bbox.y0 = floor;
        narrow.first_baseline += floor + 2. - two.used_bbox.y0;
        let next_laid = probe(&next_target, &narrow, crop, &shaper, state.typography).unwrap();
        state.frames.insert(a.clone(), first_frame);
        state.frames.insert(b.clone(), narrow);
        state.pars.insert(a.clone(), first);
        state.pars.insert(b.clone(), next);
        state.targets.insert(a, first_target);
        state.targets.insert(b, next_target);
        let ir = &mut state.bound.get_mut(&0).unwrap().ir;
        ir.items.push(DisplayItem::Image {
            bbox: Rect::new(0., first_laid.used_bbox.y1 + 0.25, 300., 300.),
        });
        ir.items.push(DisplayItem::Image {
            bbox: Rect::new(0., 0., 300., floor),
        });
        if blocked_right {
            ir.items.push(DisplayItem::Image {
                bbox: Rect::new(58., floor + 1., 300., 175.),
            });
        }
        state.typeset_by_page.insert(0, vec![next_laid]);
        state.fallbacks = 1;
        state
    }

    #[test]
    fn stack_widens_a_short_source_line_to_the_column_to_make_room() {
        for blocked_right in [false, true] {
            let dir = tempfile::tempdir().unwrap();
            let mut state = narrow_tail_state(dir.path(), blocked_right);
            let sink = SharedSink::new(super::super::tests::RunRecorder::new(Arc::new(
                Mutex::new(Vec::new()),
            )));
            refine_page(&mut state, 0, &sink);
            let laid = &state.typeset_by_page[&0];
            let find = |id: &str| laid.iter().find(|p| p.id.to_string() == id);
            if blocked_right {
                // Nothing to borrow on the right: A stays a fallback, B unchanged.
                assert_eq!(state.fallbacks, 1);
                assert!(find("P01-001").is_none());
                assert_eq!(find("P01-002").unwrap().lines.len(), 2);
            } else {
                assert_eq!(state.fallbacks, 0);
                assert_eq!(find("P01-001").unwrap().lines.len(), 3);
                let next = find("P01-002").unwrap();
                assert_eq!(next.lines.len(), 1);
                assert!(next.used_bbox.x1 > 56.);
            }
        }
    }
}
