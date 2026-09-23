use super::*;

/// Refine only unsaved pages. All candidates are laid out from immutable source
/// and cached parsed translations; publication still uses one cloned transaction.
pub(super) fn refine_page(state: &mut RunState, page: u32, sink: &SharedSink) {
    let Some(bound) = state.bound.get(&page) else {
        return;
    };
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
            if state
                .typeset_by_page
                .get(&page)
                .is_some_and(|p| p.iter().any(|p| p.id == *id))
            {
                continue;
            }
            let target = &state.targets[id];
            let Some(initial) = state.frames.get(id) else {
                continue;
            };
            let probe = stages::typeset::typeset_with_typography(
                &shaper,
                &target.para,
                &target.parsed,
                &Obstacles::default(),
                Some(initial),
                state.typography,
            );
            if probe.paragraph.lines.is_empty()
                || probe
                    .paragraph
                    .lines
                    .iter()
                    .flat_map(|l| &l.glyphs)
                    .any(|g| g.gid == 0)
            {
                continue;
            }
            let frames = stages::refine::frames(
                &bound.ir,
                &state.pars[id],
                initial,
                probe.paragraph.used_bbox,
                &state.pars,
                state.typeset_by_page.get(&page).map_or(&[], Vec::as_slice),
                &shaper,
            );
            for frame in frames {
                let result = stages::typeset::typeset_with_typography(
                    &shaper,
                    &target.para,
                    &target.parsed,
                    &Obstacles::default(),
                    Some(&frame),
                    state.typography,
                );
                if result.paragraph.overflow
                    || stages::link_text::geometry(target, &result.paragraph, &shaper).is_none()
                {
                    continue;
                }
                let boxes = result.paragraph.lines.iter().map(|l| l.bbox).collect();
                state
                    .typeset_by_page
                    .entry(page)
                    .or_default()
                    .push(result.paragraph);
                state.fallbacks -= 1;
                state.tgt_chars = state.tgt_chars - state.pars[id].text.chars().count() as u64
                    + target.parsed.text().chars().count() as u64;
                sink.emit(Event::Issue {
                    severity: Severity::Info,
                    code: "layout_refined".into(),
                    paragraph_id: Some(id.clone()),
                    page: Some(page + 1),
                    message: format!(
                        "同栏动态bbox第{round}轮：{:?} → {:?}，首基线 {:.3} → {:.3}；字号不变",
                        initial.bbox, frame.bbox, initial.first_baseline, frame.first_baseline
                    ),
                });
                sink.emit(Event::Paragraph {
                    paragraph_id: id.clone(),
                    page: page + 1,
                    status: ParagraphStatus::Typeset,
                    boxes: Some(boxes),
                    coord_system: syncpdf_core::CoordSystem::PdfUser,
                    translated_html: Some(target.html.clone()),
                });
                state.frames.insert(id.clone(), frame);
                improved = true;
                break;
            }
        }
        if !improved {
            break;
        }
    }
}
