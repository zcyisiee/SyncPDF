//! Glyph matrices carry the writing direction, not just the origin.
use lopdf::{dictionary, Document, Object, Stream};
use syncpdf_pdf::bind::bind_page;
use syncpdf_pdf::pdfium::PdfiumWorker;

/// The linear part of `Glyph.matrix` points along the baseline in page space,
/// whether the rotation comes from `Tm`, the CTM or an enclosing form.
#[test]
fn glyph_matrix_points_along_the_page_space_baseline() {
    let mut d = Document::with_version("1.7");
    let pages = d.new_object_id();
    let font = d.add_object(dictionary! {"Type" => "Font", "Subtype" => "Type1", "BaseFont" => "Helvetica", "Encoding" => "WinAnsiEncoding"});
    let fonts = dictionary! {"Font" => dictionary! {"F" => font}};
    // Scaled but upright form: stays horizontal.
    let scaled = d.add_object(Stream::new(
        dictionary! {"Type" => "XObject", "Subtype" => "Form", "BBox" => vec![0.into(),0.into(),800.into(),800.into()], "Matrix" => vec![0.5.into(),0.into(),0.into(),0.5.into(),0.into(),0.into()], "Resources" => fonts.clone()},
        b"BT /F 20 Tf 40 700 Td (Ddd) Tj ET".to_vec(),
    ));
    // Upright form content, rotated clockwise by the CTM at `Do`.
    let plain = d.add_object(Stream::new(
        dictionary! {"Type" => "XObject", "Subtype" => "Form", "BBox" => vec![0.into(),0.into(),400.into(),400.into()], "Resources" => fonts.clone()},
        b"BT /F 10 Tf 0 0 Td (Ccc) Tj ET".to_vec(),
    ));
    let mut resources = fonts;
    resources.set("XObject", dictionary! {"S" => scaled, "P" => plain});
    let content = d.add_object(Stream::new(
        dictionary! {},
        b"BT /F 10 Tf 20 20 Td (Aaa) Tj ET \
          BT /F 10 Tf 0 1 -1 0 200 50 Tm (Bbb) Tj ET \
          q 0 -1 1 0 300 350 cm /P Do Q /S Do"
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
    let path = dir.path().join("glyph-direction.pdf");
    d.save(&path).unwrap();
    let worker = PdfiumWorker::spawn().expect("PDFium required for this regression");
    let doc = worker.open(&path).unwrap();
    let bound = bind_page(&worker, doc, &d, 1).unwrap();
    worker.close(doc);
    let mut seen = std::collections::BTreeMap::new();
    for g in bound.ir.glyphs() {
        if let Some(c) = g.unicode.first() {
            let len = g.matrix.a.hypot(g.matrix.b);
            assert!(len > 0.0, "{c}: degenerate matrix {:?}", g.matrix);
            seen.entry(*c)
                .or_insert_with(Vec::new)
                .push((g.matrix.a / len, g.matrix.b / len));
        }
    }
    for (c, want) in [
        ('A', (1.0, 0.0)),
        ('B', (0.0, 1.0)),
        ('C', (0.0, -1.0)),
        ('D', (1.0, 0.0)),
    ] {
        let dirs = seen
            .get(&c)
            .unwrap_or_else(|| panic!("{c} not bound: {seen:?}"));
        assert!(
            dirs.iter()
                .all(|(x, y)| (x - want.0).abs() < 0.01 && (y - want.1).abs() < 0.01),
            "{c}: {dirs:?}"
        );
    }
}
