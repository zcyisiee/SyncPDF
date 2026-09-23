use super::*;

fn fixture(text: &str, rotations: &[i32]) -> Document {
    let mut doc = Document::with_version("1.7");
    let root = doc.new_object_id();
    let font =
        doc.add_object(dictionary! {"Type"=>"Font","Subtype"=>"Type1","BaseFont"=>"Helvetica"});
    let pages: Vec<_> = rotations.iter().map(|_| doc.new_object_id()).collect();
    let destination = doc.add_object(dictionary! {"D"=>vec![Object::Reference(pages[0]),Object::Name(b"XYZ".to_vec()),30.into(),250.into(),Object::Null]});
    for (&id, &rotation) in pages.iter().zip(rotations) {
        let stream = doc.add_object(Stream::new(
            Dictionary::new(),
            format!("BT /F1 12 Tf 30 250 Td ({text}) Tj ET\n0 0 1 rg 40 50 20 20 re f")
                .into_bytes(),
        ));
        let link = doc.add_object(dictionary! {
            "Type"=>"Annot","Subtype"=>"Link","Rect"=>vec![30.into(),245.into(),90.into(),260.into()],
            "QuadPoints"=>vec![30.into(),260.into(),90.into(),260.into(),30.into(),245.into(),90.into(),245.into()],
            "Dest"=>Object::string_literal("section"), "P"=>id,
        });
        let uri = doc.add_object(dictionary! {
            "Type"=>"Annot","Subtype"=>"Link","Rect"=>vec![40.into(),50.into(),60.into(),70.into()],
            "A"=>dictionary! {"S"=>"URI","URI"=>Object::string_literal("https://example.test/paper")},
        });
        doc.objects.insert(
            id,
            Object::Dictionary(dictionary! {
                "Type"=>"Page","Parent"=>root,"Rotate"=>rotation,
                "Contents"=>stream,"Annots"=>vec![Object::Reference(link),Object::Reference(uri)]
            }),
        );
    }
    doc.objects.insert(root,Object::Dictionary(dictionary! {
        "Type"=>"Pages","Kids"=>pages.iter().copied().map(Object::Reference).collect::<Vec<_>>(),
        "Count"=>pages.len() as i64,"MediaBox"=>vec![0.into(),0.into(),240.into(),340.into()],
        "CropBox"=>vec![10.into(),20.into(),210.into(),320.into()],
        "Resources"=>dictionary! {"Font"=>dictionary! {"F1"=>font}},
    }));
    let tree = doc.add_object(dictionary! {"Names"=>vec![Object::string_literal("section"),Object::Reference(destination)]});
    let catalog = doc.add_object(dictionary! {"Type"=>"Catalog","Pages"=>root,
    "Names"=>dictionary! {"Dests"=>dictionary! {"Kids"=>vec![Object::Reference(tree)]}}});
    doc.trailer.set("Root", catalog);
    doc
}

#[test]
fn vector_export_preserves_page_pairing_rotation_crop_and_links() {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("source.pdf");
    let mono = dir.path().join("mono.pdf");
    let dual = dir.path().join("dual.pdf");
    let original = fixture("ORIGINAL", &[0, 90, 180, 270]);
    original.clone().save(&source).unwrap();
    fixture("TRANSLATED", &[0, 90, 180, 270])
        .save(&mono)
        .unwrap();
    let source_bytes = std::fs::read(&source).unwrap();
    let mono_bytes = std::fs::read(&mono).unwrap();
    export(&source, &mono, &dual).unwrap();
    assert_eq!(std::fs::read(&source).unwrap(), source_bytes);
    assert_eq!(std::fs::read(&mono).unwrap(), mono_bytes);
    let out = Document::load(&dual).unwrap();
    assert_eq!(out.get_pages().len(), 4);
    for (index, id) in out.get_pages() {
        let page = out.get_dictionary(id).unwrap();
        assert_eq!(
            rectangle(page.get(b"MediaBox").unwrap()).unwrap(),
            [0., 0., A3_WIDTH, A3_HEIGHT]
        );
        let annotations = out.get_page_annotations(id).unwrap();
        assert_eq!(annotations.len(), 4);
        for side in 0..2 {
            let old_id = original.get_pages()[&index];
            let p = Placement::new(&original, old_id, id, side == 1).unwrap();
            let expected = p.rect([30., 245., 90., 260.]);
            let actual = rectangle(annotations[side * 2].get(b"Rect").unwrap()).unwrap();
            for (x, y) in actual.into_iter().zip(expected) {
                assert!((x - y).abs() < 0.001);
            }
            assert!(actual[0] >= side as f32 * A3_WIDTH * 0.5 - 0.001);
            assert!(actual[2] <= (side + 1) as f32 * A3_WIDTH * 0.5 + 0.001);
            let uri = annotations[side * 2 + 1]
                .get(b"A")
                .unwrap()
                .as_dict()
                .unwrap()
                .get(b"URI")
                .unwrap()
                .as_str()
                .unwrap();
            assert_eq!(uri, b"https://example.test/paper");
        }
        let target = annotations[2].get(b"Dest").unwrap().as_array().unwrap();
        assert_eq!(target[0].as_reference().unwrap(), out.get_pages()[&1]);
        assert!(target[2].as_float().unwrap() > A3_WIDTH * 0.5);
    }
    let worker = crate::pdfium::PdfiumWorker::spawn().unwrap();
    let handle = worker.open(&dual).unwrap();
    for page in 0..4 {
        let text: String = worker
            .page_text_objects(handle, page)
            .unwrap()
            .iter()
            .flat_map(|o| o.chars.iter())
            .filter_map(|c| c.unicode.as_deref())
            .collect();
        assert_eq!(text.matches("ORIGINAL").count(), 1);
        assert_eq!(text.matches("TRANSLATED").count(), 1);
    }
    worker.close(handle);
}

#[test]
fn mismatched_pages_and_alias_output_do_not_overwrite_files() {
    let dir = tempfile::tempdir().unwrap();
    let a = dir.path().join("a.pdf");
    let b = dir.path().join("b.pdf");
    let out = dir.path().join("dual.pdf");
    fixture("A", &[0]).save(&a).unwrap();
    fixture("B", &[0, 0]).save(&b).unwrap();
    std::fs::write(&out, b"previous artifact").unwrap();
    assert!(export(&a, &b, &out).is_err());
    assert_eq!(std::fs::read(&out).unwrap(), b"previous artifact");
    let before = std::fs::read(&a).unwrap();
    assert!(export(&a, &b, &a).is_err());
    assert_eq!(std::fs::read(&a).unwrap(), before);
}
