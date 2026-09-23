//! Writer's content codes are registration-order CIDs, not subset GIDs.
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use lopdf::{Dictionary, Document, Object};
use syncpdf_core::ir::{LineBox, PlacedGlyph, TypesetParagraph};
use syncpdf_core::{Color, ParagraphId, Rect, StyleId};
use syncpdf_font::loader::Script;
use syncpdf_font::{shape, subset, FontQuery, FontStore, LoadedFont};
use syncpdf_pdf::writer::{save, Writer};

fn blank_doc() -> Document {
    let mut doc = Document::with_version("1.7");
    let pages_id = doc.new_object_id();
    let mut page = Dictionary::new();
    page.set("Type", Object::Name(b"Page".to_vec()));
    page.set("Parent", Object::Reference(pages_id));
    page.set("MediaBox", vec![0.into(), 0.into(), 400.into(), 400.into()]);
    page.set("Resources", Dictionary::new());
    let page_id = doc.add_object(page);
    let mut pages = Dictionary::new();
    pages.set("Type", Object::Name(b"Pages".to_vec()));
    pages.set("Kids", vec![Object::Reference(page_id)]);
    pages.set("Count", 1);
    doc.objects.insert(pages_id, Object::Dictionary(pages));
    let mut catalog = Dictionary::new();
    catalog.set("Type", Object::Name(b"Catalog".to_vec()));
    catalog.set("Pages", Object::Reference(pages_id));
    let catalog_id = doc.add_object(catalog);
    doc.trailer.set("Root", Object::Reference(catalog_id));
    doc
}

fn para(font: u32, gid: u16, text: &str, x: f32) -> TypesetParagraph {
    TypesetParagraph {
        id: "P01-001".parse::<ParagraphId>().unwrap(),
        lines: vec![LineBox {
            bbox: Rect::new(x, 170.0, x + 60.0, 230.0),
            baseline_y: 200.0,
            glyphs: vec![PlacedGlyph {
                font,
                gid,
                text: text.into(),
                x,
                y: 200.0,
                size: 48.0,
                scale_x: 1.0,
                style: StyleId(1),
                color: None,
            }],
            kept_atoms: vec![],
            placed_atoms: Vec::new(),
        }],
        font_scale: 1.0,
        line_height: 58.0,
        color: Color::BLACK,
        used_bbox: Rect::new(x, 170.0, x + 60.0, 230.0),
        overflow: false,
    }
}

fn font<'a>(store: &'a FontStore, script: Script, family: &str) -> (u32, &'a LoadedFont) {
    let id = store
        .find(&FontQuery {
            script,
            family: Some(family.into()),
            ..Default::default()
        })
        .expect("required builtin font");
    (id.0, store.get(id).unwrap())
}

fn descendants(doc: &Document, fid: u32) -> (&Dictionary, &Dictionary) {
    let page = doc
        .get_object(*doc.get_pages().get(&1).unwrap())
        .unwrap()
        .as_dict()
        .unwrap();
    let fonts = page
        .get(b"Resources")
        .unwrap()
        .as_dict()
        .unwrap()
        .get(b"Font")
        .unwrap()
        .as_dict()
        .unwrap();
    let type0_id = fonts
        .get(format!("SPF{fid}").as_bytes())
        .unwrap()
        .as_reference()
        .unwrap();
    let type0 = doc.get_object(type0_id).unwrap().as_dict().unwrap();
    let cid_id = type0.get(b"DescendantFonts").unwrap().as_array().unwrap()[0]
        .as_reference()
        .unwrap();
    (type0, doc.get_object(cid_id).unwrap().as_dict().unwrap())
}

fn widths(w: &Object) -> BTreeMap<u16, f32> {
    let a = w.as_array().unwrap();
    let mut out = BTreeMap::new();
    let mut i = 0;
    while i < a.len() {
        let start = a[i].as_i64().unwrap() as u16;
        match &a[i + 1] {
            Object::Array(ws) => {
                for (n, value) in ws.iter().enumerate() {
                    out.insert(start + n as u16, value.as_float().unwrap());
                }
                i += 2;
            }
            Object::Integer(end) => {
                let value = a[i + 2].as_float().unwrap();
                for cid in start..=*end as u16 {
                    out.insert(cid, value);
                }
                i += 3;
            }
            _ => panic!("invalid W"),
        }
    }
    out
}

fn run_case(store: &FontStore, fid: u32, font: &LoadedFont, chars: &[&str], name: &str) -> PathBuf {
    let mut doc = blank_doc();
    let mut writer = Writer::new(store);
    let gids: Vec<u16> = chars
        .iter()
        .map(|ch| shape(font, ch, 48.0, false, &[])[0].gid)
        .collect();
    assert!(
        gids.windows(2).any(|w| w[0] > w[1]),
        "test must differ from GID order"
    );
    for (i, (&gid, &ch)) in gids.iter().zip(chars).enumerate() {
        writer
            .write_paragraphs(
                &mut doc,
                1,
                &[para(fid, gid, ch, 35.0 + i as f32 * 65.0)],
                400.0,
            )
            .unwrap();
    }
    writer.finalize(&mut doc).unwrap();
    let (type0, cidfont) = descendants(&doc, fid);
    let page_id = *doc.get_pages().get(&1).unwrap();
    let content = String::from_utf8_lossy(&doc.get_page_content(page_id)).to_string();
    for cid in 1..=gids.len() {
        assert!(content.contains(&format!("<{cid:04X}> Tj")));
    }
    let map_id = cidfont
        .get(b"CIDToGIDMap")
        .unwrap()
        .as_reference()
        .expect("explicit CID map");
    let map = &doc.get_object(map_id).unwrap().as_stream().unwrap().content;
    let sub = subset(font, &gids).unwrap();
    for (i, &gid) in gids.iter().enumerate() {
        let cid = i + 1;
        let actual = u16::from_be_bytes([map[2 * cid], map[2 * cid + 1]]);
        assert_eq!(
            actual, sub.gid_map[&gid],
            "CID {cid} must point to registered original GID {gid}"
        );
    }
    let cmap_id = type0.get(b"Encoding").unwrap().as_reference().unwrap();
    let cmap = String::from_utf8_lossy(
        &doc.get_object(cmap_id)
            .unwrap()
            .as_stream()
            .unwrap()
            .content,
    )
    .to_string();
    for cid in 1..=gids.len() {
        assert!(
            cmap.contains(&format!("<{cid:04X}> {cid}\n")),
            "missing content CID {cid} in Encoding"
        );
    }
    let tu_id = type0.get(b"ToUnicode").unwrap().as_reference().unwrap();
    let tu = String::from_utf8_lossy(&doc.get_object(tu_id).unwrap().as_stream().unwrap().content)
        .to_string();
    for (i, ch) in chars.iter().enumerate() {
        let unicode: String = ch.encode_utf16().map(|u| format!("{u:04X}")).collect();
        assert!(tu.contains(&format!("<{:04X}> <{unicode}>", i + 1)));
    }
    let ws = widths(cidfont.get(b"W").unwrap());
    use skrifa::MetadataProvider;
    let face = skrifa::FontRef::from_index(&font.data, font.face_index).unwrap();
    let metrics = face.glyph_metrics(
        skrifa::instance::Size::new(1000.0),
        skrifa::instance::LocationRef::default(),
    );
    for (i, &gid) in gids.iter().enumerate() {
        let expected = metrics
            .advance_width(skrifa::GlyphId::from(gid as u32))
            .unwrap();
        let actual = ws[&((i + 1) as u16)];
        assert!(
            (actual - expected).abs() <= 0.11,
            "CID {} width {actual} != {expected}",
            i + 1
        );
    }
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../tmp/backend-repair/fontmap");
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join(format!("{name}.pdf"));
    save(&mut doc, &path).unwrap();
    path
}

#[test]
fn registration_cids_drive_cjk_and_latin_fonts_across_batches() {
    let root = syncpdf_core::fixtures::fonts_dir().expect("builtin font fixtures required");
    let store = FontStore::load_builtin(&root).unwrap();
    let (cjk_id, cjk) = font(&store, Script::HanSC, "Noto Sans CJK SC");
    let (latin_id, latin) = font(&store, Script::Latin, "PT Sans");
    let cjk_chars = ["文", "A", "中"];
    let latin_chars = ["z", "é", "A", "m"];
    run_case(&store, cjk_id, cjk, &cjk_chars, "cjk_order_a");
    run_case(&store, cjk_id, cjk, &["中", "文", "A"], "cjk_order_b");
    run_case(&store, cjk_id, cjk, &["文", "A"], "cjk_selected_subset");
    run_case(&store, latin_id, latin, &latin_chars, "latin_order_a");
}
