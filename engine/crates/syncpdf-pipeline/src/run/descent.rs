//! Document-wide line-height descent. Leading is one typographic decision for
//! the whole document: when the requested leading leaves paragraphs that no
//! page-local placement can hold, every page is typeset again from the same
//! retained translations at the next lower leading, down to a readable floor.
//! No model is called; a lower leading is kept only if it recovers paragraphs.
use super::*;
use crate::events::EventSink;

/// Everything one typesetting pass produces from the retained translations.
struct Pass {
    doc: lopdf::Document,
    frames: BTreeMap<ParagraphId, stages::frame::LayoutFrame>,
    targets: BTreeMap<ParagraphId, stages::link_text::Target>,
    typography: stages::typeset::Typography,
    typeset_by_page: BTreeMap<u32, Vec<TypesetParagraph>>,
    schedule: PageSchedule,
    output: PathBuf,
    settled_ids: BTreeSet<ParagraphId>,
    echoed: BTreeSet<ParagraphId>,
    ready: Vec<u32>,
    font_stats: Option<FontStats>,
}

impl Pass {
    fn swap(&mut self, s: &mut RunState) {
        std::mem::swap(&mut self.doc, &mut s.doc);
        std::mem::swap(&mut self.frames, &mut s.frames);
        std::mem::swap(&mut self.targets, &mut s.targets);
        std::mem::swap(&mut self.typography, &mut s.typography);
        std::mem::swap(&mut self.typeset_by_page, &mut s.typeset_by_page);
        std::mem::swap(&mut self.schedule, &mut s.schedule);
        std::mem::swap(&mut self.output, &mut s.output);
        std::mem::swap(&mut self.settled_ids, &mut s.settled_ids);
        std::mem::swap(&mut self.echoed, &mut s.echoed);
        std::mem::swap(&mut self.ready, &mut s.ready);
        std::mem::swap(&mut self.font_stats, &mut s.font_stats);
    }
}

/// Source state a pass starts from.
pub(super) struct Origin {
    pub doc: lopdf::Document,
    pub frames: BTreeMap<ParagraphId, stages::frame::LayoutFrame>,
    pub schedule: PageSchedule,
}

struct Buffer(Arc<Mutex<Vec<Event>>>);

impl EventSink for Buffer {
    fn emit(&mut self, event: Event) {
        if let Ok(mut events) = self.0.lock() {
            events.push(event);
        }
    }
}

/// Forwards each distinct issue once, so a replayed pass does not repeat the
/// warnings of paragraphs whose outcome did not change.
pub(super) struct Distinct {
    inner: SharedSink,
    seen: BTreeSet<(String, Option<ParagraphId>, String)>,
}

impl Distinct {
    pub(super) fn new(inner: SharedSink) -> Self {
        Self {
            inner,
            seen: BTreeSet::new(),
        }
    }
}

impl EventSink for Distinct {
    fn emit(&mut self, event: Event) {
        if let Event::Issue {
            code,
            paragraph_id,
            message,
            ..
        } = &event
        {
            if !self
                .seen
                .insert((code.clone(), paragraph_id.clone(), message.clone()))
            {
                return;
            }
        }
        self.inner.emit(event);
    }
}

/// Lower the document leading while it recovers fallback paragraphs. `settle`
/// is the same end-of-translation settlement the first pass received.
pub(super) fn descend(
    state: &mut RunState,
    origin: &Origin,
    total: u32,
    sink: &SharedSink,
    settle: impl Fn(&mut RunState, &SharedSink) -> Result<(), PipelineError>,
) -> Result<(), PipelineError> {
    loop {
        let fallbacks = state.fallbacks();
        if fallbacks == 0 {
            break;
        }
        let Some(lower) = state.typography.lowered() else {
            break;
        };
        let events = Arc::new(Mutex::new(Vec::new()));
        let buffer = SharedSink::new(Buffer(events.clone()));
        let trial_output = state.output.with_extension("leading.pdf");
        let mut previous = Pass {
            doc: origin.doc.clone(),
            frames: origin.frames.clone(),
            targets: BTreeMap::new(),
            typography: lower,
            typeset_by_page: BTreeMap::new(),
            schedule: origin.schedule.clone(),
            output: trial_output.clone(),
            settled_ids: BTreeSet::new(),
            echoed: BTreeSet::new(),
            ready: Vec::new(),
            font_stats: None,
        };
        previous.swap(state);
        let blocks = std::mem::take(&mut state.blocks);
        let replay = blocks
            .iter()
            .try_for_each(|block| handle_block(state, &buffer, block.clone(), total))
            .and_then(|()| settle(state, &buffer));
        state.blocks = blocks;
        if let Err(error) = replay {
            previous.swap(state);
            let _ = std::fs::remove_file(&trial_output);
            return Err(error);
        }
        if state.fallbacks() >= fallbacks {
            previous.swap(state);
            let _ = std::fs::remove_file(&trial_output);
            break;
        }
        std::fs::rename(&trial_output, &previous.output)
            .map_err(|e| PipelineError::Protocol(format!("行距重排结果保存失败：{e}")))?;
        state.output = previous.output;
        sink.emit(Event::Issue {
            severity: Severity::Info,
            code: "line_height_lowered".into(),
            paragraph_id: None,
            page: None,
            message: format!(
                "全文行距 {} → {}：回退段 {} → {}；以下段落状态以本次重排为准",
                previous.typography.describe(),
                state.typography.describe(),
                fallbacks,
                state.fallbacks()
            ),
        });
        let events = std::mem::take(&mut *events.lock().expect("buffer lock"));
        for event in events {
            if !matches!(event, Event::Progress { .. }) {
                sink.emit(event);
            }
        }
    }
    Ok(())
}
