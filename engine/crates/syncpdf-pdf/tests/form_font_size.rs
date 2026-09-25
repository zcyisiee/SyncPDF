//! Visual font size of text drawn inside a scaled Form XObject.
use lopdf::{dictionary, Document, Object, Stream};
use syncpdf_pdf::bind::bind_page;
use syncpdf_pdf::pdfium::PdfiumWorker;

/// Every glyph's `size` is the visual size: `Tf` scaled by the text matrix,
/// the CTM and every enclosing form's matrix.
#[test]
fn text_inside_a_scaled_form_reports_its_visual_size() {
    let mut d = Document::with_version("1.7");
    let pages = d.new_object_id();
    let font = d.add_object(dictionary! {"Type" => "Font", "Subtype" => "Type1", "BaseFont" => "Helvetica", "Encoding" => "WinAnsiEncoding"});
    let fonts = dictionary! {"Font" => dictionary! {"F" => font}};
    // Scaled by the form's own /Matrix.
    let scaled = d.add_object(Stream::new(
        dictionary! {"Type" => "XObject", "Subtype" => "Form", "BBox" => vec![0.into(),0.into(),800.into(),800.into()], "Matrix" => vec![0.5.into(),0.into(),0.into(),0.5.into(),0.into(),0.into()], "Resources" => fonts.clone()},
        b"BT /F 20 Tf 40 400 Td (Aaa) Tj ET".to_vec(),
    ));
    // Identity /Matrix, scaled by the CTM in effect at `Do`.
    let plain = d.add_object(Stream::new(
        dictionary! {"Type" => "XObject", "Subtype" => "Form", "BBox" => vec![0.into(),0.into(),1600.into(),1600.into()], "Resources" => fonts.clone()},
        b"BT /F 40 Tf 80 400 Td (Bbb) Tj ET".to_vec(),
    ));
    let mut resources = fonts;
    resources.set("XObject", dictionary! {"S" => scaled, "P" => plain});
    let content = d.add_object(Stream::new(
        dictionary! {},
        b"/S Do q 0.25 0 0 0.25 0 200 cm /P Do Q \
          BT /F 10 Tf 20 20 Td (Ccc) Tj ET \
          q 0.5 0 0 0.5 0 0 cm BT /F 20 Tf 40 120 Td (Ddd) Tj ET Q"
            .to_vec(),
    ));
    let page = d.add_object(dictionary! {"Type" => "Page", "Parent" => pages, "MediaBox" => vec![0.into(),0.into(),400.into(),400.into()], "Resources" => resources, "Contents" => content});
    d.objects.insert(
        pages,
        Object::Dictionary(
            dictionary! {"Type" => "Pages", "Kids" => vec![page.into()], "Count" => 1},
        ),
    );
    let catalog = d.add_object(dictionary! {"Type" => "Catalog", "Pages" => pages});
    d.trailer.set("Root", catalog);
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("form-font-size.pdf");
    d.save(&path).unwrap();
    let worker = PdfiumWorker::spawn().expect("PDFium required for this regression");
    let doc = worker.open(&path).unwrap();
    let bound = bind_page(&worker, doc, &d, 1).unwrap();
    worker.close(doc);
    let mut seen = std::collections::BTreeMap::new();
    for g in bound.ir.glyphs() {
        if let Some(c) = g.unicode.first() {
            seen.entry(*c).or_insert_with(Vec::new).push(g.size);
        }
    }
    for c in ['A', 'a', 'B', 'b', 'C', 'c', 'D', 'd'] {
        let sizes = seen
            .get(&c)
            .unwrap_or_else(|| panic!("{c} not bound: {seen:?}"));
        assert!(
            sizes.iter().all(|s| (s - 10.0).abs() < 0.01),
            "{c}: {sizes:?}"
        );
    }
}
