//! Page-local copy-on-write, including indirect and inherited resource containers.
use lopdf::{dictionary, Dictionary, Document, Object, Stream};
use syncpdf_pdf::{
    bind::bind_page,
    patch::{PatchError, PatchSet},
    pdfium::PdfiumWorker,
};

fn shared_document(form: bool, array: bool, inherited: bool) -> Document {
    let mut doc = Document::with_version("1.7");
    let pages = doc.new_object_id();
    let font = doc.add_object(dictionary! {"Type" => "Font", "Subtype" => "Type1", "BaseFont" => "Courier", "Encoding" => "WinAnsiEncoding", "FirstChar" => 32, "LastChar" => 126, "Widths" => vec![Object::Integer(600); 95]});
    let fonts = dictionary! {"F1" => font};
    let text = b"BT /F1 12 Tf 1 0 0 1 30 100 Tm (ABC) Tj ET";
    let mut resources = dictionary! {"Font" => fonts.clone()};
    let content = if form {
        let form_id = doc.add_object(Stream::new(dictionary! {
            "Type" => "XObject", "Subtype" => "Form", "BBox" => vec![0.into(),0.into(),200.into(),200.into()],
            "Resources" => dictionary! {"Font" => fonts}
        }, text.to_vec()));
        let xobjects = doc.add_object(dictionary! {"Shared" => form_id, "SPfX0" => form_id});
        resources.set("XObject", xobjects);
        b"q /Shared Do Q".to_vec()
    } else {
        text.to_vec()
    };
    let resources = doc.add_object(resources);
    let stream = doc.add_object(Stream::new(Dictionary::new(), content));
    let contents = if array {
        Object::Array(vec![stream.into()])
    } else {
        stream.into()
    };
    let mut kids = Vec::new();
    for _ in 0..2 {
        let mut page = dictionary! {"Type" => "Page", "Parent" => pages, "MediaBox" => vec![0.into(),0.into(),200.into(),200.into()], "Contents" => contents.clone()};
        if !inherited {
            page.set("Resources", resources);
        }
        kids.push(Object::Reference(doc.add_object(page)));
    }
    doc.objects.insert(
        pages,
        Object::Dictionary(
            dictionary! {"Type" => "Pages", "Kids" => kids, "Count" => 2, "Resources" => resources},
        ),
    );
    let root = doc.add_object(dictionary! {"Type" => "Catalog", "Pages" => pages});
    doc.trailer.set("Root", root);
    doc
}

#[test]
fn shared_contents_and_resources_are_page_local() {
    let worker = PdfiumWorker::spawn().expect("PDFium required for shared-stream regression");
    for form in [false, true] {
        for array in [false, true] {
            for inherited in [false, true] {
                let dir = tempfile::tempdir().unwrap();
                let input = dir.path().join("source.pdf");
                let output = dir.path().join("patched.pdf");
                let mut doc = shared_document(form, array, inherited);
                doc.save(&input).unwrap();
                let pdf = worker.open(&input).unwrap();
                let bound = bind_page(&worker, pdf, &doc, 1).unwrap();
                bound.check_replacement().unwrap();
                assert_eq!(bound.ir.glyphs().count(), 3);
                let other_before = format!("{:?}", worker.page_text_objects(pdf, 1).unwrap());
                let original = doc.objects.clone();
                let page1 = doc.get_pages()[&1];
                let page2 = doc.get_pages()[&2];
                let bytes = doc.get_page_content(page2);
                let mut patch = PatchSet::new();
                patch
                    .delete_glyphs(&bound, &bound.ir.glyphs().map(|g| g.id).collect::<Vec<_>>())
                    .unwrap();
                let stats = patch.apply(&mut doc, 1).unwrap();
                assert_eq!(stats.ops_deleted, 1);
                assert_eq!(doc.get_page_content(page2), bytes);
                // Every original object except the target page is byte/resource-identical.
                for (id, object) in &original {
                    if *id != page1 {
                        assert_eq!(format!("{:?}", doc.objects[id]), format!("{object:?}"));
                    }
                }
                let before_retry = format!("{:?}", doc.objects);
                assert!(matches!(
                    patch.apply(&mut doc, 1),
                    Err(PatchError::UnsupportedPath(_))
                ));
                assert_eq!(format!("{:?}", doc.objects), before_retry);
                // A separately queued paragraph from the original binding is stale too.
                let mut later = PatchSet::new();
                later
                    .delete_glyphs(&bound, &[bound.ir.glyphs().next().unwrap().id])
                    .unwrap();
                assert!(matches!(
                    later.apply(&mut doc, 1),
                    Err(PatchError::UnsupportedPath(_))
                ));
                assert_eq!(format!("{:?}", doc.objects), before_retry);
                doc.save(&output).unwrap();
                let reopened = worker.open(&output).unwrap();
                let target = bind_page(&worker, reopened, &doc, 1).unwrap();
                assert_eq!(
                    target.ir.glyphs().count(),
                    0,
                    "target text must actually disappear"
                );
                assert_eq!(
                    format!("{:?}", worker.page_text_objects(reopened, 1).unwrap()),
                    other_before,
                    "non-target text, matrices and character geometry must be identical"
                );
                eprintln!("form={form} array={array} inherited={inherited}: original objects unchanged except page1; page2 {} content bytes and PDFium geometry identical; target 3→0 glyphs", bytes.len());
                worker.close(reopened);
                worker.close(pdf);
            }
        }
    }
}

#[test]
fn stale_source_streams_and_contents_reject_atomically() {
    let worker = PdfiumWorker::spawn().expect("PDFium required for stale-stream regression");
    for form in [false, true] {
        for change in [
            "source",
            "form_target",
            "parent",
            "contents_add",
            "contents_remove",
            "contents_reorder",
        ] {
            if !form && matches!(change, "form_target" | "parent") || form && change == "source" {
                continue;
            }
            let dir = tempfile::tempdir().unwrap();
            let input = dir.path().join("source.pdf");
            let mut doc = shared_document(form, true, false);
            let page = doc.get_pages()[&1];
            let first = doc.get_page_contents(page)[0];
            // Two page streams make removal and reordering observable without
            // changing the original text-show operation's object identity.
            let extra = doc.add_object(Stream::new(Dictionary::new(), b"q Q".to_vec()));
            doc.get_object_mut(page)
                .unwrap()
                .as_dict_mut()
                .unwrap()
                .set(
                    "Contents",
                    vec![Object::Reference(first), Object::Reference(extra)],
                );
            doc.save(&input).unwrap();
            let pdf = worker.open(&input).unwrap();
            let bound = bind_page(&worker, pdf, &doc, 1).unwrap();
            bound.check_replacement().unwrap();
            let mut patch = PatchSet::new();
            patch
                .delete_glyphs(&bound, &[bound.ir.glyphs().next().unwrap().id])
                .unwrap();
            match change {
                "source" => doc
                    .get_object_mut(first)
                    .unwrap()
                    .as_stream_mut()
                    .unwrap()
                    .set_plain_content(b"BT /F1 12 Tf 1 0 0 1 30 100 Tm (XBC) Tj ET".to_vec()),
                "form_target" => {
                    let target = *doc
                        .objects
                        .iter()
                        .find(|(_, object)| {
                            object.as_stream().ok().is_some_and(|stream| {
                                stream
                                    .dict
                                    .get(b"Subtype")
                                    .ok()
                                    .and_then(|value| value.as_name().ok())
                                    == Some(b"Form")
                            })
                        })
                        .unwrap()
                        .0;
                    doc.get_object_mut(target)
                        .unwrap()
                        .as_stream_mut()
                        .unwrap()
                        .set_plain_content(b"BT /F1 12 Tf 1 0 0 1 30 100 Tm (XBC) Tj ET".to_vec());
                }
                "parent" => doc
                    .get_object_mut(first)
                    .unwrap()
                    .as_stream_mut()
                    .unwrap()
                    .set_plain_content(b"q /Shared Do Q q Q".to_vec()),
                "contents_add" => doc
                    .get_object_mut(page)
                    .unwrap()
                    .as_dict_mut()
                    .unwrap()
                    .set(
                        "Contents",
                        vec![
                            Object::Reference(first),
                            Object::Reference(extra),
                            Object::Reference(extra),
                        ],
                    ),
                "contents_remove" => doc
                    .get_object_mut(page)
                    .unwrap()
                    .as_dict_mut()
                    .unwrap()
                    .set("Contents", vec![Object::Reference(first)]),
                "contents_reorder" => doc
                    .get_object_mut(page)
                    .unwrap()
                    .as_dict_mut()
                    .unwrap()
                    .set(
                        "Contents",
                        vec![Object::Reference(extra), Object::Reference(first)],
                    ),
                _ => unreachable!(),
            }
            let before = format!("{doc:?}");
            assert!(
                matches!(
                    patch.apply(&mut doc, 1),
                    Err(PatchError::UnsupportedPath(_))
                ),
                "form={form} change={change}"
            );
            assert_eq!(format!("{doc:?}"), before, "form={form} change={change}");
            worker.close(pdf);
        }
    }
}

#[test]
fn source_changed_before_enqueue_still_rejects() {
    let worker = PdfiumWorker::spawn().expect("PDFium required for bind-time snapshot regression");
    let dir = tempfile::tempdir().unwrap();
    let input = dir.path().join("source.pdf");
    let mut doc = shared_document(false, false, false);
    doc.save(&input).unwrap();
    let pdf = worker.open(&input).unwrap();
    let bound = bind_page(&worker, pdf, &doc, 1).unwrap();
    bound.check_replacement().unwrap();
    let stream = doc.get_page_contents(doc.get_pages()[&1])[0];
    doc.get_object_mut(stream)
        .unwrap()
        .as_stream_mut()
        .unwrap()
        .set_plain_content(b"BT /F1 12 Tf 1 0 0 1 30 100 Tm (XBC) Tj ET".to_vec());
    let mut patch = PatchSet::new();
    patch
        .delete_glyphs(&bound, &[bound.ir.glyphs().next().unwrap().id])
        .unwrap();
    let before = format!("{doc:?}");
    assert!(matches!(
        patch.apply(&mut doc, 1),
        Err(PatchError::UnsupportedPath("stale source stream binding"))
    ));
    assert_eq!(format!("{doc:?}"), before);
    worker.close(pdf);
}

#[test]
fn same_page_requests_cannot_mix_source_snapshots() {
    let worker = PdfiumWorker::spawn().expect("PDFium required for source-snapshot regression");
    let dir = tempfile::tempdir().unwrap();
    let original_path = dir.path().join("original.pdf");
    let changed_path = dir.path().join("changed.pdf");
    let mut doc = shared_document(false, false, false);
    doc.save(&original_path).unwrap();
    let original = doc.clone();
    let pdf = worker.open(&original_path).unwrap();
    let first = bind_page(&worker, pdf, &doc, 1).unwrap();
    first.check_replacement().unwrap();
    let stream = doc.get_page_contents(doc.get_pages()[&1])[0];
    doc.get_object_mut(stream)
        .unwrap()
        .as_stream_mut()
        .unwrap()
        .set_plain_content(b"BT /F1 12 Tf 1 0 0 1 30 100 Tm (XBC) Tj ET".to_vec());
    doc.save(&changed_path).unwrap();
    let changed_pdf = worker.open(&changed_path).unwrap();
    let second = bind_page(&worker, changed_pdf, &doc, 1).unwrap();
    second.check_replacement().unwrap();
    let mut patch = PatchSet::new();
    patch
        .delete_glyphs(&first, &[first.ir.glyphs().next().unwrap().id])
        .unwrap();
    assert!(matches!(
        patch.delete_glyphs(&second, &[second.ir.glyphs().nth(1).unwrap().id]),
        Err(PatchError::UnsupportedPath("mixed source snapshots"))
    ));
    let mut original = original;
    assert_eq!(patch.apply(&mut original, 1).unwrap().ops_rewritten, 1);
    let content = original.get_page_content(original.get_pages()[&1]);
    let output = String::from_utf8_lossy(&content);
    assert!(
        output.contains("(BC)"),
        "rejected request must not delete B: {output}"
    );
    worker.close(changed_pdf);
    worker.close(pdf);
}
