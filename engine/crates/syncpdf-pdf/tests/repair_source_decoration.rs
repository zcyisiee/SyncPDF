//! A claimed source underline is erased only inside the page transaction that
//! replaces its words; every other stroked path in the same stream survives.
//!
//! Fixture: one line of text and two plain `S` rules. Only the rule bound as a
//! text decoration may be cleared, and clearing it must not disturb the path
//! construction, the graphics state or the second rule.

use lopdf::{dictionary, Dictionary, Document, Object, Stream};
use syncpdf_pdf::{bind::bind_page, patch::PatchSet, pdfium::PdfiumWorker};

/// `BT ... (hello) Tj ET` plus an underline under the text and an unrelated rule.
const CONTENT: &[u8] = b"BT /F1 10 Tf 1 0 0 1 50 100 Tm (hello) Tj ET\n\
0 G 0.4 w 50 96 m 90 96 l S\n\
0.5 G 1.5 w 300 300 m 380 300 l S\n";

fn source_pdf(content: &[u8]) -> Document {
    let mut doc = Document::with_version("1.7");
    let pages = doc.new_object_id();
    let widths = doc.add_object(Object::Array(
        (32..=126).map(|_| Object::from(556)).collect(),
    ));
    let font = doc.add_object(dictionary! {
        "Type" => "Font", "Subtype" => "Type1", "BaseFont" => "Helvetica",
        "Encoding" => "WinAnsiEncoding", "FirstChar" => 32, "LastChar" => 126,
        "Widths" => widths,
    });
    let stream = doc.add_object(Stream::new(Dictionary::new(), content.to_vec()));
    let page = doc.add_object(dictionary! {
        "Type" => "Page", "Parent" => pages,
        "MediaBox" => vec![0.into(), 0.into(), 400.into(), 300.into()],
        "Resources" => dictionary! { "Font" => dictionary! { "F1" => font }, "ExtGState" => dictionary! { "Fade" => dictionary! { "CA" => 0.5 } } },
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

fn fixture_path(name: &str) -> std::path::PathBuf {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../tmp/paper-iteration/decoration-pdf-tests");
    std::fs::create_dir_all(&root).unwrap();
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    root.join(format!("{name}-{stamp}.pdf"))
}

#[test]
fn clipped_or_unsupported_pen_is_not_a_plain_decoration() {
    let worker = PdfiumWorker::spawn().expect("PDFium");
    for (name, path) in [
        (
            "clip",
            "0 0 70 200 re W n 50 80 m 90 80 l S 50 70 m 90 70 l S",
        ),
        ("moves", "50 80 m 90 80 m S"),
        ("dash", "[2 2] 0 d 50 80 m 90 80 l S"),
        ("caps", "1 J 50 80 m 90 80 l S"),
        ("alpha", "/Fade gs 50 80 m 90 80 l S"),
        ("shear", "1 0 0.5 1 0 0 cm 50 80 m 90 80 l S"),
    ] {
        let mut bytes = CONTENT.to_vec();
        bytes.extend(format!("q {path} Q\n").as_bytes());
        let mut doc = source_pdf(&bytes);
        let file = fixture_path(name);
        doc.save(&file).unwrap();
        let docid = worker.open(&file).unwrap();
        let bound = bind_page(&worker, docid, &doc, 1).unwrap();
        let strokes = bound
            .ir
            .items
            .iter()
            .filter(|i| {
                matches!(
                    i,
                    syncpdf_core::ir::DisplayItem::Path {
                        stroke: Some(_),
                        ..
                    }
                )
            })
            .count();
        assert_eq!(strokes, 2, "{name} must remain opaque source paint");
    }
}

fn content_text(doc: &Document) -> String {
    let page = doc.get_pages()[&1];
    let bytes = doc
        .get_page_contents(page)
        .into_iter()
        .flat_map(|id| {
            doc.get_object(id)
                .and_then(Object::as_stream)
                .and_then(|s| s.decompressed_content())
                .unwrap_or_default()
        })
        .collect::<Vec<u8>>();
    String::from_utf8_lossy(&bytes).into_owned()
}

#[test]
fn clearing_a_claimed_underline_keeps_every_other_paint_operation() {
    let mut doc = source_pdf(CONTENT);
    let worker = PdfiumWorker::spawn().expect("PDFium");
    let path = fixture_path("delete");
    doc.save(&path).expect("save fixture");
    let docid = worker.open(&path).expect("open fixture");
    let bound = bind_page(&worker, docid, &doc, 1).expect("bind");

    // Exactly the two plain single-segment rules are decoration candidates.
    let strokes: Vec<_> = bound
        .ir
        .items
        .iter()
        .filter_map(|item| match item {
            syncpdf_core::ir::DisplayItem::Path {
                stroke: Some(s),
                is_fill: false,
                ..
            } => Some(s.op),
            _ => None,
        })
        .collect();
    assert_eq!(
        strokes.len(),
        2,
        "both plain rules are bindable: {strokes:?}"
    );

    let before = content_text(&doc);
    assert_eq!(before.matches(" S\n").count(), 2, "{before}");

    let mut ps = PatchSet::new();
    ps.delete_paths(&bound, &strokes[..1]).expect("delete one");
    let stats = ps.apply(&mut doc, 1).expect("apply");
    assert_eq!(stats.ops_deleted, 1);

    let after = content_text(&doc);
    assert_eq!(
        after.matches(" S\n").count(),
        1,
        "the unowned rule survives: {after}"
    );
    assert_eq!(
        after.matches(" n\n").count(),
        1,
        "only the owned paint is cleared: {after}"
    );
    // Path construction and the graphics state of the cleared rule are intact.
    assert!(after.contains("50 96 m 90 96 l"), "{after}");
    assert!(after.contains("0.4 w"), "{after}");
    assert!(
        after.contains("0.5 G 1.5 w 300 300 m 380 300 l S"),
        "{after}"
    );
}

#[test]
fn an_unbound_or_foreign_op_is_rejected_before_mutation() {
    let mut doc = source_pdf(CONTENT);
    let worker = PdfiumWorker::spawn().expect("PDFium");
    let path = fixture_path("reject");
    doc.save(&path).expect("save fixture");
    let docid = worker.open(&path).expect("open fixture");
    let bound = bind_page(&worker, docid, &doc, 1).expect("bind");

    let mut ps = PatchSet::new();
    // The text show op is not a plain page-level stroke: guessing here could
    // clear real artwork, so the gate fails closed.
    let text_op = bound.ir.glyphs().next().expect("glyph").id.op;
    assert!(ps.delete_paths(&bound, &[text_op]).is_err());
    assert!(ps.is_empty(), "a rejected request enqueues nothing");
}
