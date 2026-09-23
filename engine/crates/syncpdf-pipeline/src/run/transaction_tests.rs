use super::*;
use lopdf::{dictionary, Document, Object, Stream};
use syncpdf_core::ir::{Align, Atom, AtomKind, RegionKind, StyleRun};
use syncpdf_core::{AtomId, Color, Rect, StyleId};
use syncpdf_pdf::bind::bind_page;
use syncpdf_pdf::pdfium::PdfiumWorker;
use syncpdf_translate::{BlockStatus, TranslatedBlock};

fn source() -> Document {
    let mut doc = Document::with_version("1.7");
    let pages = doc.new_object_id();
    let font = doc.add_object(dictionary! {"Type" => "Font", "Subtype" => "Type1", "BaseFont" => "Courier", "Encoding" => "WinAnsiEncoding", "FirstChar" => 32, "LastChar" => 126, "Widths" => vec![Object::Integer(600); 95]});
    let mut kids = Vec::new();
    for text in ["SourceAlpha", "SourceBeta"] {
        let content = doc.add_object(Stream::new(
            dictionary! {},
            format!("BT /F1 12 Tf 72 700 Td ({text}) Tj ET").into_bytes(),
        ));
        let page = doc.add_object(dictionary! {"Type" => "Page", "Parent" => pages, "MediaBox" => vec![0.into(), 0.into(), 612.into(), 792.into()], "Resources" => dictionary! {"Font" => dictionary! {"F1" => font}}, "Contents" => content});
        kids.push(Object::Reference(page));
    }
    doc.objects.insert(
        pages,
        Object::Dictionary(dictionary! {"Type" => "Pages", "Kids" => kids, "Count" => 2}),
    );
    let root = doc.add_object(dictionary! {"Type" => "Catalog", "Pages" => pages});
    doc.trailer.set("Root", root);
    doc
}

pub(super) fn state(dir: &Path) -> (RunState, PathBuf) {
    let input = dir.join("source.pdf");
    source().save(&input).unwrap();
    let doc = Document::load(&input).unwrap();
    let worker = PdfiumWorker::spawn().unwrap();
    let source = worker.open(&input).unwrap();
    let mut bound = BTreeMap::new();
    let mut pars = BTreeMap::new();
    for page in 0..2 {
        let b = bind_page(&worker, source, &doc, page + 1).unwrap();
        b.check_replacement().unwrap();
        let id = ParagraphId {
            page: page + 1,
            seq: 1,
        };
        let para = Paragraph {
            id: id.clone(),
            page: b.ir.page,
            region: 0,
            kind: RegionKind::Text,
            bbox: Rect::new(72.0, 650.0, 300.0, 730.0),
            lines: vec![],
            glyphs: b.ir.glyphs().map(|g| g.id).collect(),
            text_spans: vec![],
            style_runs: vec![StyleRun {
                id: StyleId(0),
                glyph_range: (0, b.ir.glyphs().count() as u32),
                font: 0,
                size: 12.0,
                color: Color::BLACK,
                bold: false,
                italic: false,
                serif: false,
                mono: false,
            }],
            atoms: vec![],
            text: "Source".into(),
            align: Align::Left,
            first_indent: 0.0,
            line_height: 15.0,
            is_rtl: false,
            translatable: Translatable::Yes,
        };
        pars.insert(id, para);
        bound.insert(page, b);
    }
    worker.close(source);
    let (font_store, font_profile) =
        load_fonts(&syncpdf_core::fixtures::fonts_dir().unwrap(), "zh-CN").unwrap();
    let schedule = PageSchedule::new(pars.keys().map(|id| (id.page, id.clone())));
    let frames = pars
        .iter()
        .map(|(id, para)| {
            (
                id.clone(),
                stages::frame::LayoutFrame {
                    bbox: para.bbox,
                    first_baseline: 700.0,
                    obstacles: vec![],
                },
            )
        })
        .collect();
    (
        RunState {
            doc,
            bound,
            frames,
            targets: BTreeMap::new(),
            typography: stages::typeset::Typography::default(),
            pars,
            font_store,
            font_profile,
            schedule,
            typeset_by_page: BTreeMap::new(),
            page_heights: [(0, 792.0), (1, 792.0)].into_iter().collect(),
            output: dir.join("output.pdf"),
            src_chars: 0,
            tgt_chars: 0,
            fallbacks: 0,
            settled: 0,
            settled_ids: BTreeSet::new(),
            ready: vec![],
            revision: 0,
            font_stats: None,
            callback_error: None,
        },
        input,
    )
}

fn block(page: u32) -> TranslatedBlock {
    let id = ParagraphId { page, seq: 1 };
    TranslatedBlock {
        html: format!("<p id=\"{id}\">译文测试</p>"),
        id,
        status: BlockStatus::Ok,
        from_cache: false,
    }
}

type RecordedEvents = Arc<Mutex<Vec<(u64, Event)>>>;

fn recorder() -> (SharedSink, RecordedEvents) {
    let log = Arc::new(Mutex::new(Vec::new()));
    (
        SharedSink::new(super::tests::RunRecorder::new(log.clone())),
        log,
    )
}

fn text(path: &Path, page: u32) -> String {
    let worker = PdfiumWorker::spawn().unwrap();
    let doc = worker.open(path).unwrap();
    let text = worker
        .page_text_objects(doc, page)
        .unwrap()
        .iter()
        .flat_map(|o| &o.chars)
        .filter_map(|c| c.unicode.as_deref())
        .collect();
    worker.close(doc);
    text
}

#[test]
fn snapshot_excludes_prepared_but_unready_page_and_duplicate_is_idempotent() {
    let dir = tempfile::tempdir().unwrap();
    let (mut s, input) = state(dir.path());
    // p1 第一段已到但另一段仍未到，p2 先提交。
    s.schedule = PageSchedule::new(
        [
            (1, ParagraphId { page: 1, seq: 1 }),
            (1, ParagraphId { page: 1, seq: 2 }),
            (2, ParagraphId { page: 2, seq: 1 }),
        ]
        .into_iter(),
    );
    let (sink, log) = recorder();
    handle_block(&mut s, &sink, block(1), 3).unwrap();
    assert_eq!(s.typeset_by_page[&0].len(), 1);
    assert!(!s.output.exists());
    handle_block(&mut s, &sink, block(2), 3).unwrap();
    assert_eq!(s.ready, vec![1]);
    assert_eq!(s.revision, 1);
    assert_eq!(
        text(&s.output, 0),
        text(&input, 0),
        "未就绪页必须保留源内容"
    );
    assert!(text(&s.output, 1).contains("译文测试"));
    assert!(!text(&s.output, 1).contains("SourceBeta"));
    assert_eq!(s.cjk_pages(), vec![2]);
    let output = std::fs::read(&s.output).unwrap();
    let events = log.lock().unwrap().len();
    handle_block(&mut s, &sink, block(2), 3).unwrap();
    assert_eq!(s.settled, 2);
    assert_eq!(s.revision, 1);
    assert_eq!(log.lock().unwrap().len(), events);
    assert_eq!(std::fs::read(&s.output).unwrap(), output);
    writeback_page(&mut s, 0, &sink).unwrap();
    assert_eq!(s.revision, 2);
    assert_eq!(s.cjk_pages(), vec![1, 2]);
    assert!(!text(&s.output, 0).contains("SourceAlpha"));
}

#[test]
fn save_failure_keeps_committed_doc_revision_and_previous_output() {
    let dir = tempfile::tempdir().unwrap();
    let (mut s, _) = state(dir.path());
    let (sink, log) = recorder();
    handle_block(&mut s, &sink, block(1), 2).unwrap();
    let output_path = s.output.clone();
    let output_bytes = std::fs::read(&output_path).unwrap();
    let objects = format!("{:?}", s.doc.objects);
    // 现有 PDF 是文件，无法用它作父目录；确定性模拟保存失败，不改权限。
    s.output = output_path.join("cannot-save.pdf");
    assert!(handle_block(&mut s, &sink, block(2), 2).is_err());
    assert_eq!(format!("{:?}", s.doc.objects), objects);
    assert_eq!(s.ready, vec![0]);
    assert_eq!(s.revision, 1);
    assert_eq!(std::fs::read(output_path).unwrap(), output_bytes);
    assert_eq!(
        log.lock()
            .unwrap()
            .iter()
            .filter(|(_, e)| matches!(e, Event::PageReady { .. }))
            .count(),
        1
    );
}

#[test]
fn missing_binding_and_unknown_block_are_errors_without_publication() {
    let dir = tempfile::tempdir().unwrap();
    let (mut s, _) = state(dir.path());
    let (sink, _) = recorder();
    assert!(handle_block(&mut s, &sink, block(99), 2).is_err());
    s.bound.remove(&0);
    assert!(handle_block(&mut s, &sink, block(1), 2).is_err());
    assert_eq!(s.revision, 0);
    assert!(s.ready.is_empty());
    assert!(!s.output.exists());
}

#[test]
fn atom_without_drawing_placement_keeps_source_and_is_explicit_fallback() {
    let dir = tempfile::tempdir().unwrap();
    let (mut s, input) = state(dir.path());
    let id = ParagraphId { page: 1, seq: 1 };
    s.pars.get_mut(&id).unwrap().atoms.push(Atom {
        source: None,
        id: AtomId(1),
        glyph_range: (0, 1),
        kind: AtomKind::Formula,
        text: "x".into(),
    });
    let (sink, log) = recorder();
    handle_block(&mut s, &sink, block(1), 2).unwrap();
    assert_eq!(s.fallbacks, 1);
    assert!(s.typeset_by_page.is_empty());
    assert!(s.cjk_pages().is_empty());
    assert_eq!(text(&s.output, 0), text(&input, 0));
    assert!(log
        .lock()
        .unwrap()
        .iter()
        .any(|(_, e)| matches!(e, Event::Issue { code, .. } if code == "atom_source_unplaced")));
}

#[test]
fn output_validation_checks_actual_target_expectation_and_returns_errors() {
    let dir = tempfile::tempdir().unwrap();
    let (_, input) = state(dir.path());
    let (mut sink, log) = recorder();
    validate_output(&mut sink, &input, &[], &input).unwrap();
    assert!(matches!(
        validate_output(&mut sink, &input, &[1], &input),
        Err(PipelineError::Validation(_))
    ));
    assert!(log.lock().unwrap().iter().any(|(_, e)| matches!(e, Event::Issue { severity: Severity::Error, code, .. } if code == "self_check")));
}

fn linked_state(dir: &Path) -> (RunState, lopdf::ObjectId, Object) {
    let (mut state, _) = state(dir);
    let id = ParagraphId { page: 1, seq: 1 };
    let para = state.pars.get_mut(&id).unwrap();
    let glyphs: Vec<_> = state.bound[&0].ir.glyphs().collect();
    para.text = "SourceAlpha".into();
    para.text_spans = glyphs
        .iter()
        .enumerate()
        .map(|(i, g)| syncpdf_core::ir::SourceTextSpan {
            text: g.unicode.iter().collect(),
            glyph_range: (i as u32, i as u32 + 1),
        })
        .collect();
    let bbox = glyphs[6..]
        .iter()
        .skip(1)
        .fold(glyphs[6].bbox, |a, g| a.union(&g.bbox));
    let rect = Object::Array(vec![
        bbox.x0.into(),
        bbox.y0.into(),
        bbox.x1.into(),
        bbox.y1.into(),
    ]);
    let annot = state.doc.add_object(dictionary! { "Type"=>"Annot", "Subtype"=>"Link", "Rect"=>rect.clone(), "A"=>dictionary!{"S"=>"URI","URI"=>Object::string_literal("https://example.org/anchor")} });
    let page = state.doc.get_pages()[&1];
    state
        .doc
        .get_dictionary_mut(page)
        .unwrap()
        .set("Annots", vec![Object::Reference(annot)]);
    (state, annot, rect)
}
fn linked_block() -> TranslatedBlock {
    TranslatedBlock {
        id: ParagraphId { page: 1, seq: 1 },
        html: "<p id=\"P01-001\">中文 Alpha</p>".into(),
        status: BlockStatus::Ok,
        from_cache: true,
    }
}
#[test]
fn plain_link_moves_with_glyphs_and_keeps_action() {
    let dir = tempfile::tempdir().unwrap();
    let (mut s, annot, old_rect) = linked_state(dir.path());
    let action = s
        .doc
        .get_dictionary(annot)
        .unwrap()
        .get(b"A")
        .unwrap()
        .clone();
    let (sink, _) = recorder();
    handle_block(&mut s, &sink, linked_block(), 2).unwrap();
    assert_eq!(s.fallbacks, 0);
    let saved = Document::load(&s.output).unwrap();
    let link = saved.get_dictionary(annot).unwrap();
    assert_ne!(link.get(b"Rect").unwrap(), &old_rect);
    assert_eq!(link.get(b"A").unwrap(), &action);
    assert_eq!(
        link.get(b"QuadPoints").unwrap().as_array().unwrap().len(),
        8
    );
}

#[test]
fn link_inside_source_formula_moves_with_the_original_glyphs() {
    let dir = tempfile::tempdir().unwrap();
    let (mut s, annot, old_rect) = linked_state(dir.path());
    let id = ParagraphId { page: 1, seq: 1 };
    let glyphs: Vec<_> = s.bound[&0].ir.glyphs().collect();
    let bbox = glyphs[6..]
        .iter()
        .fold(glyphs[6].bbox, |b, g| b.union(&g.bbox));
    s.pars.get_mut(&id).unwrap().atoms.push(Atom {
        id: AtomId(1),
        glyph_range: (6, 11),
        kind: AtomKind::Formula,
        text: "Alpha".into(),
        source: Some(syncpdf_core::ir::SourceAtom {
            bbox,
            baseline: glyphs[6].matrix.f,
        }),
    });
    let action = s
        .doc
        .get_dictionary(annot)
        .unwrap()
        .get(b"A")
        .unwrap()
        .clone();
    let mut translated = linked_block();
    translated.html = "<p id=\"P01-001\">中文 {{KEEP_1}}</p>".into();
    let (sink, _) = recorder();
    handle_block(&mut s, &sink, translated, 2).unwrap();
    assert_eq!(s.fallbacks, 0);
    let saved = Document::load(&s.output).unwrap();
    let link = saved.get_dictionary(annot).unwrap();
    assert_ne!(link.get(b"Rect").unwrap(), &old_rect);
    assert_eq!(link.get(b"A").unwrap(), &action);
    let atom = &s.typeset_by_page[&0][0].lines[0].placed_atoms[0];
    let r = link.get(b"Rect").unwrap().as_array().unwrap();
    assert!((r[0].as_float().unwrap() - atom.bbox.x0).abs() < 0.01);
    assert!((r[1].as_float().unwrap() - atom.bbox.y0).abs() < 0.01);
    assert_eq!(text(&s.output, 0).matches("Alpha").count(), 1);
}
#[test]
fn ambiguous_unmarked_link_preserves_source_instead_of_guessing() {
    let dir = tempfile::tempdir().unwrap();
    let (mut s, annot, old_rect) = linked_state(dir.path());
    let mut translated = linked_block();
    translated.html = "<p id=\"P01-001\">中文 Alpha 和 Alpha</p>".into();
    let (sink, _) = recorder();
    handle_block(&mut s, &sink, translated, 2).unwrap();
    assert_eq!(s.fallbacks, 1);
    assert!(s.typeset_by_page.is_empty());
    assert_eq!(
        s.doc.get_dictionary(annot).unwrap().get(b"Rect").unwrap(),
        &old_rect
    );
    assert!(text(&s.output, 0).contains("SourceAlpha"));
}

#[test]
fn failed_snapshot_does_not_commit_relocated_links() {
    let dir = tempfile::tempdir().unwrap();
    let (mut s, annot, old_rect) = linked_state(dir.path());
    s.output = dir.path().to_path_buf();
    let (sink, _) = recorder();
    assert!(handle_block(&mut s, &sink, linked_block(), 2).is_err());
    assert_eq!(
        s.doc.get_dictionary(annot).unwrap().get(b"Rect").unwrap(),
        &old_rect
    );
    assert_eq!(s.revision, 0);
    assert!(s.ready.is_empty());
}

#[test]
fn missing_safe_frame_preserves_source_and_reports_reason() {
    let dir = tempfile::tempdir().unwrap();
    let (mut s, input) = state(dir.path());
    s.frames.clear();
    let (sink, log) = recorder();
    handle_block(&mut s, &sink, block(1), 2).unwrap();
    assert!(s.typeset_by_page.is_empty());
    assert_eq!(text(&s.output, 0), text(&input, 0));
    assert!(log
        .lock()
        .unwrap()
        .iter()
        .any(|(_, e)| matches!(e, Event::Issue { code, .. } if code == "layout_frame_missing")));
    assert_eq!(s.fallbacks, 1);
}
