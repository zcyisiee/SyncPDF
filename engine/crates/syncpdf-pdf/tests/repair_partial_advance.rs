//! Real PDFium geometry guard for partial TJ deletion and its following text show.
//! The explicit Helvetica widths are PDF standard metrics, held in an indirect array.

use lopdf::{dictionary, Dictionary, Document, Object, Stream};
use syncpdf_pdf::{bind::bind_page, patch::PatchSet, pdfium::PdfiumWorker};

#[derive(Debug)]
struct CharGeometry {
    text: String,
    x: f32,
    y: f32,
    size: f32,
}

fn source_pdf(content: &[u8]) -> Document {
    let mut doc = Document::with_version("1.7");
    let pages = doc.new_object_id();
    // Standard Helvetica AFM widths in 1/1000 em, codes 32..=68.
    let mut all_widths = vec![
        278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556,
        556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015,
    ];
    all_widths.extend([667, 667, 722, 722]);
    let widths = doc.add_object(Object::Array(
        all_widths.into_iter().map(Into::into).collect(),
    ));
    let font = doc.add_object(dictionary! {
        "Type" => "Font", "Subtype" => "Type1", "BaseFont" => "Helvetica",
        "Encoding" => "WinAnsiEncoding", "FirstChar" => 32, "LastChar" => 68,
        "Widths" => widths,
    });
    let stream = doc.add_object(Stream::new(Dictionary::new(), content.to_vec()));
    let page = doc.add_object(dictionary! {
        "Type" => "Page", "Parent" => pages,
        "MediaBox" => vec![0.into(), 0.into(), 400.into(), 300.into()],
        "Resources" => dictionary! { "Font" => dictionary! { "F1" => font } },
        "Contents" => stream,
    });
    doc.objects.insert(
        pages,
        Object::Dictionary(dictionary! {
            "Type" => "Pages", "Kids" => vec![Object::Reference(page)], "Count" => 1,
        }),
    );
    let root = doc.add_object(dictionary! { "Type" => "Catalog", "Pages" => pages });
    doc.trailer.set("Root", root);
    doc
}

fn geometry(worker: &PdfiumWorker, pdf: syncpdf_pdf::pdfium::DocId) -> Vec<CharGeometry> {
    worker
        .page_text_objects(pdf, 0)
        .expect("PDFium text objects")
        .into_iter()
        .flat_map(|obj| {
            obj.chars
                .into_iter()
                .filter(|c| !c.is_generated)
                .map(move |c| CharGeometry {
                    text: c.unicode.expect("source character Unicode"),
                    x: c.origin.x,
                    y: c.origin.y,
                    size: obj.font_size,
                })
        })
        .collect()
}

fn assert_survivors(content: &[u8], deleted: &[char], expected_before: &str) {
    let dir = tempfile::tempdir().expect("temporary PDF directory");
    let source = dir.path().join("source.pdf");
    let result = dir.path().join("result.pdf");
    let mut doc = source_pdf(content);
    doc.save(&source).expect("save source");
    // Mandatory real PDFium integration; an unavailable worker fails this guard.
    let worker = PdfiumWorker::spawn().expect("PDFium worker required");
    let pdf = worker.open(&source).expect("open source");
    let before = geometry(&worker, pdf);
    assert_eq!(
        before.iter().map(|c| c.text.as_str()).collect::<String>(),
        expected_before,
        "fixture must render its intended source characters"
    );
    let bound = bind_page(&worker, pdf, &doc, 1).expect("bind source page");
    bound.check_replacement().expect("trusted binding");
    let ids: Vec<_> = bound
        .ir
        .glyphs()
        .filter(|g| g.unicode.len() == 1 && deleted.contains(&g.unicode[0]))
        .map(|g| g.id)
        .collect();
    assert_eq!(
        ids.len(),
        deleted.len(),
        "each requested character must bind exactly once"
    );
    let mut patch = PatchSet::new();
    patch.delete_glyphs(&bound, &ids).expect("queue deletion");
    patch.apply(&mut doc, 1).expect("apply deletion");
    doc.save(&result).expect("save result");
    let patched = worker.open(&result).expect("open saved result");
    let after = geometry(&worker, patched);
    let expected: Vec<_> = before
        .iter()
        .filter(|c| !deleted.iter().any(|d| c.text == d.to_string()))
        .collect();
    assert_eq!(
        after.len(),
        expected.len(),
        "deleted glyphs must disappear, with no new glyphs"
    );
    for (i, (actual, original)) in after.iter().zip(expected).enumerate() {
        assert_eq!(actual.text, original.text, "survivor {i} Unicode");
        assert!(
            (actual.x - original.x).abs() < 0.03,
            "survivor {i} ({}) origin.x shifted: before {}, after {}",
            original.text,
            original.x,
            actual.x
        );
        assert!(
            (actual.y - original.y).abs() < 0.03,
            "survivor {i} ({}) origin.y shifted: before {}, after {}",
            original.text,
            original.y,
            actual.y
        );
        assert!(
            (actual.size - original.size).abs() < 0.03,
            "survivor {i} ({}) font size changed: before {}, after {}",
            original.text,
            original.size,
            actual.size
        );
    }
    worker.close(patched);
    worker.close(pdf);
}

#[test]
fn indirect_widths_and_signed_tj_spacing_preserve_partial_survivors() {
    // Tc/Tw/Tz all affect advances; signed TJ displacements occur on both sides of B.
    assert_survivors(
        b"BT /F1 15 Tf 1.3 Tc 2.1 Tw 83 Tz 1 0 0 1 42 210 Tm [(A ) 120 (B) -80 (C)] TJ ET",
        &['B'],
        "A BC",
    );
}

#[test]
fn deleting_tj_tail_preserves_next_show_on_same_text_matrix() {
    assert_survivors(
        b"BT /F1 15 Tf 1.3 Tc 2.1 Tw 83 Tz 1 0 0 1 42 210 Tm [(A) 120 (B) -80 (C)] TJ (D) Tj ET",
        &['C'],
        "ABCD",
    );
}

#[test]
fn deleting_entire_tj_preserves_next_show_on_same_text_matrix() {
    assert_survivors(
        b"BT /F1 15 Tf 1.3 Tc 2.1 Tw 83 Tz 1 0 0 1 42 210 Tm [(A) 120 (B) -80 (C)] TJ (D) Tj ET",
        &['A', 'B', 'C'],
        "ABCD",
    );
}
