//! Opt-in real-paper check: actual ink, source baseline, fixed size and retained content.
use std::collections::BTreeMap;
use std::path::Path;
use syncpdf_core::ir::{RegionKind, Translatable};
use syncpdf_pdf::pdfium::PdfiumWorker;
use syncpdf_pipeline::stages;
use syncpdf_typeset::Obstacles;

#[test]
#[ignore = "manual specified-paper layout/writeback acceptance; requires R3_FRAME_PDF and R3_FRAME_OUTPUT"]
fn real_page_one_short_title_and_abstract_keep_size_and_baseline() {
    let input = std::env::var("R3_FRAME_PDF").expect("R3_FRAME_PDF");
    let output = std::env::var("R3_FRAME_OUTPUT").expect("R3_FRAME_OUTPUT");
    let output = Path::new(&output);
    std::fs::create_dir_all(output).unwrap();
    let worker = PdfiumWorker::spawn().unwrap();
    let pf = stages::preflight(&worker, Path::new(&input)).unwrap();
    let mut doc = lopdf::Document::load(&input).unwrap();
    let bound = syncpdf_pdf::bind::bind_page(&worker, pf.doc, &doc, 1).unwrap();
    bound.check_replacement().unwrap();
    let source_objects = worker.page_text_objects(pf.doc, 0).unwrap();
    let model_path = syncpdf_core::fixtures::models_dir()
        .unwrap()
        .join("pp_doc_layoutv3.onnx");
    let mut model = syncpdf_layout::LayoutModel::load(&model_path, 2).unwrap();
    let opts = stages::LayoutOpts::default();
    let mut regions =
        stages::detect_regions(&mut model, &worker, pf.doc, 0, &pf.page_infos[0], &opts).unwrap();
    let coverage = stages::apply_coverage_fallback(&mut regions, &bound.ir, 0, opts.coverage_limit);
    assert_eq!(coverage.ratio, 0.0);
    let mut paras = stages::analyze_page(&bound.ir, &regions);
    assert_eq!(
        stages::source_policy::protect_front_matter(&bound.ir, &regions, &mut paras).len(),
        4
    );
    let frames = stages::frame::page_frames(&bound.ir, &regions, &paras);
    let (fonts, profile) =
        stages::load_fonts(&syncpdf_core::fixtures::fonts_dir().unwrap(), "zh-CN").unwrap();
    let shaper = stages::StoreShaper::new(&fonts, &profile);
    let cases = [
        (
            paras
                .iter()
                .find(|p| p.kind == RegionKind::Title && p.text.contains("Backdoor"))
                .unwrap(),
            "深度神经网络中的手工后门",
        ),
        (
            paras.iter().find(|p| p.text.trim() == "Abstract").unwrap(),
            "摘要",
        ),
    ];
    let mut translated = Vec::new();
    let mut records = Vec::new();
    for (para, target) in cases {
        assert!(matches!(para.translatable, Translatable::Yes));
        assert!(para.atoms.is_empty());
        assert_eq!(para.align, syncpdf_core::ir::Align::Center);
        let style = para.style_runs[0].id.0;
        let parsed = syncpdf_translate::parse_unit_html(&format!(
            "<p id=\"{}\"><span data-style=\"{style}\">{target}</span></p>",
            para.id
        ))
        .unwrap();
        let frame = &frames[&para.id];
        let result = stages::typeset::typeset_with_frame(
            &shaper,
            para,
            &parsed,
            &Obstacles::default(),
            Some(frame),
        );
        assert!(
            !result.paragraph.overflow,
            "{}: {:?}, frame {:?}, ink {:?}",
            para.id, result.issues, frame, result.paragraph.used_bbox
        );
        assert_eq!(result.scale, 1.0);
        assert_eq!(result.paragraph.lines.len(), 1);
        let line = &result.paragraph.lines[0];
        assert_eq!(line.baseline_y, frame.first_baseline);
        assert!((line.bbox.center().x - para.bbox.center().x).abs() < 1.0);
        assert_eq!(line.baseline_y, para.lines[0].baseline_y);
        assert!(line
            .glyphs
            .iter()
            .all(|g| g.size == para.style_runs[0].size));
        assert!(!stages::frame::collides(frame, &result.paragraph.lines));
        records.push(serde_json::json!({"id":para.id,"size":para.style_runs[0].size,"baseline":line.baseline_y,"frame":frame.bbox,"ink":line.bbox}));
        translated.push(result.paragraph);
    }
    let selected: Vec<_> = cases.iter().map(|(p, _)| *p).collect();
    stages::delete_translated(&mut doc, &bound, &selected).unwrap();
    let pdf = output.join("short-translations.pdf");
    stages::render_snapshot(
        &doc,
        &fonts,
        &BTreeMap::from([(0, translated)]),
        &BTreeMap::from([(0, bound.ir.media_box.height())]),
        &pdf,
    )
    .unwrap();
    let written = worker.open(&pdf).unwrap();
    let objects = worker.page_text_objects(written, 0).unwrap();
    let chars: Vec<_> = objects.iter().flat_map(|o| &o.chars).collect();
    let text: String = chars.iter().filter_map(|c| c.unicode.as_deref()).collect();
    for (para, target) in cases {
        assert!(text.contains(target), "written target missing: {target}");
        let measured = objects
            .iter()
            .find(|o| {
                o.chars.iter().any(|c| {
                    c.unicode
                        .as_deref()
                        .is_some_and(|s| s.starts_with(target.chars().next().unwrap()))
                })
            })
            .unwrap();
        assert!((measured.font_size - para.style_runs[0].size).abs() < 0.001);
        assert!(
            (measured.object_bounds.as_ref().unwrap().origin.y - frames[&para.id].first_baseline)
                .abs()
                < 0.001
        );
    }
    let mut retained = 0;
    for old in source_objects.iter().flat_map(|o| &o.chars) {
        if old.unicode.is_none()
            || cases
                .iter()
                .any(|(p, _)| p.bbox.contains(old.bbox.center()))
        {
            continue;
        }
        assert!(
            chars.iter().any(|new| new.unicode == old.unicode
                && (new.origin.x - old.origin.x).abs() < 0.01
                && (new.origin.y - old.origin.y).abs() < 0.01),
            "retained char moved/missing: {:?}",
            old.unicode
        );
        retained += 1;
    }
    assert!(retained > 1000);
    std::fs::write(
        output.join("measurements.json"),
        serde_json::to_vec_pretty(
            &serde_json::json!({"paragraphs":records,"retained_chars":retained}),
        )
        .unwrap(),
    )
    .unwrap();
    worker.close(written);
    worker.close(pf.doc);
}
