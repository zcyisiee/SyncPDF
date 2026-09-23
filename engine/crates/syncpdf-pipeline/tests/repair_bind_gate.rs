//! Known-unsafe source rejection and checked deletion; not full binding certification.
use lopdf::{dictionary, Document, Object, Stream};
use syncpdf_core::ir::{Align, Paragraph, RegionKind, Translatable};
use syncpdf_core::{GlyphId, ParagraphId};
use syncpdf_pdf::bind::{bind_page, BoundPage, ReplacementError};
use syncpdf_pdf::patch::{PatchError, PatchSet};
use syncpdf_pdf::pdfium::PdfiumWorker;
use syncpdf_pipeline::cancel::CancellationToken;
use syncpdf_pipeline::stages::{delete_translated, source_analysis, PipelineError};

fn document(empty_mapping: bool) -> Document {
    let mut doc = Document::with_version("1.7");
    let pages = doc.new_object_id();
    let font = if empty_mapping {
        let cid = doc.add_object(dictionary! {
            "Type" => "Font", "Subtype" => "CIDFontType2", "BaseFont" => "FakeCJK",
            "CIDSystemInfo" => dictionary! {"Registry" => Object::string_literal("Adobe"), "Ordering" => Object::string_literal("Identity"), "Supplement" => 0},
            "CIDToGIDMap" => "Identity", "DW" => 1000
        });
        let cmap = b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def /CMapName /Adobe-Identity-UCS def /CMapType 2 def 1 begincodespacerange <0000> <FFFF> endcodespacerange 2 beginbfchar <0001> <> <0002> <0079> endbfchar endcmap CMapName currentdict /CMap defineresource pop end end";
        let unicode = doc.add_object(Stream::new(dictionary! {}, cmap.to_vec()));
        doc.add_object(dictionary! {"Type" => "Font", "Subtype" => "Type0", "BaseFont" => "FakeCJK", "Encoding" => "Identity-H", "DescendantFonts" => vec![Object::Reference(cid)], "ToUnicode" => unicode})
    } else {
        doc.add_object(dictionary! {"Type" => "Font", "Subtype" => "Type1", "BaseFont" => "Courier", "Encoding" => "WinAnsiEncoding", "FirstChar" => 32, "LastChar" => 126, "Widths" => vec![Object::Integer(600); 95]})
    };
    let mut kids = Vec::new();
    for text in ["ABC", "XYZ"] {
        let content = if empty_mapping {
            "BT /F1 12 Tf 72 700 Td <00010002> Tj ET".into()
        } else {
            format!("BT /F1 12 Tf 72 700 Td ({text}) Tj ET")
        };
        let stream = doc.add_object(Stream::new(dictionary! {}, content.into_bytes()));
        let page = doc.add_object(dictionary! {"Type" => "Page", "Parent" => pages, "MediaBox" => vec![0.into(), 0.into(), 612.into(), 792.into()], "Resources" => dictionary! {"Font" => dictionary! {"F1" => font}}, "Contents" => stream});
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

fn paragraph(bound: &BoundPage, ids: Vec<GlyphId>) -> Paragraph {
    Paragraph {
        id: ParagraphId::new(bound.ir.page, 1),
        page: bound.ir.page,
        region: 0,
        kind: RegionKind::Text,
        bbox: bound.ir.media_box,
        lines: vec![],
        glyphs: ids,
        text_spans: Vec::new(),
        style_runs: vec![],
        atoms: vec![],
        text: "source".into(),
        align: Align::Left,
        first_indent: 0.0,
        line_height: 12.0,
        is_rtl: false,
        translatable: Translatable::Yes,
    }
}

#[test]
fn empty_mapping_source_rejects_before_returning_translation_input() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("unsafe.pdf");
    document(true).save(&path).unwrap();
    let lo = Document::load(&path).unwrap();
    let before = std::fs::read(&path).unwrap();
    let worker = PdfiumWorker::spawn().unwrap();
    let doc = worker.open(&path).unwrap();
    let bound = bind_page(&worker, doc, &lo, 1).unwrap();
    assert_eq!(bound.stats.degraded, 1);
    assert_eq!(bound.stats.unbound_glyphs, 1);
    assert!(matches!(
        bound.check_replacement(),
        Err(ReplacementError::Statistics(_))
    ));
    let mut progress = 0;
    let result = source_analysis(
        &worker,
        doc,
        &lo,
        &[0, 1],
        &CancellationToken::new(),
        |_, _| progress += 1,
    );
    assert!(
        matches!(result, Err(PipelineError::Protocol(ref msg)) if msg.contains("page 1 replacement rejected") && msg.contains("degraded: 1"))
    );
    assert_eq!(progress, 0);
    assert_eq!(std::fs::read(&path).unwrap(), before);
    let mut output = lo.clone();
    let before_objects = format!("{:?}", output.objects);
    let para = paragraph(&bound, bound.ir.glyphs().map(|g| g.id).collect());
    assert!(matches!(
        delete_translated(&mut output, &bound, &[&para]),
        Err(PipelineError::Protocol(_))
    ));
    assert_eq!(format!("{:?}", output.objects), before_objects);
    worker.close(doc);
}

#[test]
fn checked_deletion_uses_real_page_identity_and_preserves_other_page() {
    let temp = tempfile::tempdir().unwrap();
    let input = temp.path().join("two.pdf");
    document(false).save(&input).unwrap();
    let mut lo = Document::load(&input).unwrap();
    let worker = PdfiumWorker::spawn().unwrap();
    let doc = worker.open(&input).unwrap();
    let bounds = source_analysis(
        &worker,
        doc,
        &lo,
        &[0, 1],
        &CancellationToken::new(),
        |_, _| {},
    )
    .unwrap();
    for (index, bound) in bounds.iter().enumerate() {
        assert!(bound.ir.glyphs().all(|g| g.id.page.0 == index as u32));
    }
    let second_bytes = lo.get_page_content(bounds[1].page_id);
    let second_geometry = worker.page_text_objects(doc, 1).unwrap();
    let first_id = bounds[0].ir.glyphs().next().unwrap().id;
    let first = paragraph(&bounds[0], vec![first_id]);
    let invalid = paragraph(&bounds[0], vec![bounds[1].ir.glyphs().next().unwrap().id]);
    let before = format!("{:?}", lo.objects);
    assert!(delete_translated(&mut lo, &bounds[0], &[&first, &invalid]).is_err());
    assert_eq!(
        format!("{:?}", lo.objects),
        before,
        "whole paragraph batch preflight"
    );
    let mut ps = PatchSet::new();
    ps.delete_glyphs(&bounds[0], &[first_id]).unwrap();
    assert!(matches!(
        ps.apply(&mut lo, 2),
        Err(PatchError::PageIdentity)
    ));
    assert_eq!(format!("{:?}", lo.objects), before);
    delete_translated(&mut lo, &bounds[0], &[&first]).unwrap();
    assert_eq!(lo.get_page_content(bounds[1].page_id), second_bytes);
    let output = temp.path().join("first-deleted.pdf");
    lo.save(&output).unwrap();
    let outdoc = worker.open(&output).unwrap();
    let first_text: String = worker
        .page_text_objects(outdoc, 0)
        .unwrap()
        .iter()
        .flat_map(|o| &o.chars)
        .filter_map(|c| c.unicode.as_deref())
        .collect();
    assert_eq!(first_text, "BC");
    assert_eq!(
        format!("{:?}", worker.page_text_objects(outdoc, 1).unwrap()),
        format!("{second_geometry:?}"),
        "second page content and geometry unchanged"
    );
    let first_bytes = lo.get_page_content(bounds[0].page_id);
    let last = paragraph(&bounds[1], bounds[1].ir.glyphs().map(|g| g.id).collect());
    delete_translated(&mut lo, &bounds[1], &[&last]).unwrap();
    assert_eq!(lo.get_page_content(bounds[0].page_id), first_bytes);
    assert_ne!(lo.get_page_content(bounds[1].page_id), second_bytes);
    worker.close(outdoc);
    worker.close(doc);
}
