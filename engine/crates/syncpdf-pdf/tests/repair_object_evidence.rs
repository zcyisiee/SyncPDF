//! Single-code object evidence; all fixtures are constructed here, not copied from papers.
use lopdf::{dictionary, Document, Object, Stream};
use syncpdf_pdf::bind::{bind_page, BoundPage, ObjectUnicodeSource};
use syncpdf_pdf::pdfium::{PdfiumWorker, TextObject};

fn fixture_with_subtype(
    name: &str,
    text: &[u8],
    subtype: &str,
    encoding: Object,
    cmap: Option<&[u8]>,
    form: bool,
) -> (BoundPage, Vec<TextObject>) {
    let mut d = Document::with_version("1.7");
    let pages = d.new_object_id();
    let mut font = dictionary! {"Type" => "Font", "Subtype" => subtype, "BaseFont" => "Helvetica", "Encoding" => encoding};
    if matches!(font.get(b"Encoding"), Ok(Object::Null)) {
        font.remove(b"Encoding");
    }
    if subtype == "Type3" {
        let four = d.add_object(Stream::new(
            dictionary! {},
            b"600 0 0 0 500 700 d1 0 0 500 700 re f".to_vec(),
        ));
        let five = d.add_object(Stream::new(
            dictionary! {},
            b"600 0 0 0 450 650 d1 0 0 450 650 re f".to_vec(),
        ));
        font.set("Subtype", "Type3");
        font.set("FontBBox", vec![0.into(), 0.into(), 500.into(), 700.into()]);
        font.set(
            "FontMatrix",
            vec![
                0.001.into(),
                0.into(),
                0.into(),
                0.001.into(),
                0.into(),
                0.into(),
            ],
        );
        font.set("FirstChar", 7);
        font.set("LastChar", 8);
        font.set("Widths", vec![600.into(), 600.into()]);
        font.set("CharProcs", dictionary! {"four" => four, "five" => five});
        font.set("Resources", dictionary! {});
    }
    if subtype == "Type0" {
        let cid=d.add_object(dictionary! {"Type" => "Font", "Subtype" => "CIDFontType2", "BaseFont" => "FakeCJK", "CIDSystemInfo" => dictionary! {"Registry" => Object::string_literal("Adobe"), "Ordering" => Object::string_literal("Identity"), "Supplement" => 0}, "CIDToGIDMap" => "Identity", "DW" => 1000});
        font.set("BaseFont", "FakeCJK");
        font.set("DescendantFonts", vec![Object::Reference(cid)]);
    }
    if let Some(cmap) = cmap {
        let id = d.add_object(Stream::new(dictionary! {}, cmap.to_vec()));
        font.set("ToUnicode", id);
    }
    let f = d.add_object(font);
    let resources = dictionary! {"Font" => dictionary! {"F" => f}};
    let (resources, text) = if form {
        let inner = d.add_object(Stream::new(dictionary! {"Type" => "XObject", "Subtype" => "Form", "BBox" => vec![0.into(),0.into(),200.into(),200.into()], "Matrix" => vec![0.into(),2.into(),(-3).into(),0.into(),150.into(),20.into()], "Resources" => resources}, text.to_vec()));
        let outer = d.add_object(Stream::new(dictionary! {"Type" => "XObject", "Subtype" => "Form", "BBox" => vec![(-500).into(),0.into(),500.into(),500.into()], "Matrix" => vec![0.4.into(),0.3.into(),(-0.3).into(),0.4.into(),10.into(),30.into()], "Resources" => dictionary! {"XObject" => dictionary! {"I" => inner}}}, b"/I Do".to_vec()));
        (
            dictionary! {"XObject" => dictionary! {"O" => outer}},
            b"/O Do".to_vec(),
        )
    } else {
        (resources, text.to_vec())
    };
    let content = d.add_object(Stream::new(dictionary! {}, text));
    let page = d.add_object(dictionary! {"Type" => "Page", "Parent" => pages, "MediaBox" => vec![0.into(),0.into(),400.into(),400.into()], "Resources" => resources, "Contents" => content});
    d.objects.insert(
        pages,
        Object::Dictionary(
            dictionary! {"Type" => "Pages", "Kids" => vec![page.into()], "Count" => 1},
        ),
    );
    let catalog = d.add_object(dictionary! {"Type" => "Catalog", "Pages" => pages});
    d.trailer.set("Root", catalog);
    let dir = std::env::temp_dir().join("repair-object-evidence");
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join(format!("{name}.pdf"));
    d.save(&path).unwrap();
    let worker = PdfiumWorker::spawn().expect("PDFium required for this regression");
    let doc = worker.open(&path).unwrap();
    let objects = worker.page_text_objects(doc, 0).unwrap();
    let bound = bind_page(&worker, doc, &d, 1).unwrap();
    worker.close(doc);
    (bound, objects)
}

fn fixture(
    name: &str,
    text: &[u8],
    type3: bool,
    encoding: Object,
    cmap: Option<&[u8]>,
    form: bool,
) -> (BoundPage, Vec<TextObject>) {
    fixture_with_subtype(
        name,
        text,
        if type3 { "Type3" } else { "Type1" },
        encoding,
        cmap,
        form,
    )
}

fn duplicate(code: &str, dx: f32) -> Vec<u8> {
    format!(
        "BT /F 16 Tf 1 0 0 1 20 40 Tm <{code}> Tj ET BT /F 16 Tf 1 0 0 1 {} 40 Tm <{code}> Tj ET",
        20.0 + dx
    )
    .into_bytes()
}
fn differences(name: &str) -> Object {
    Object::Dictionary(
        dictionary! {"Type" => "Encoding", "Differences" => vec![7.into(), Object::Name(name.as_bytes().to_vec()), Object::Name(b"five".to_vec())]},
    )
}
fn cmap(entries: &str) -> Vec<u8> {
    format!("/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def /CMapName /Test def /CMapType 2 def 1 begincodespacerange <00> <FF> endcodespacerange {entries} endcmap CMapName currentdict /CMap defineresource pop end end").into_bytes()
}
fn near(a: f32, b: f32) {
    assert!((a - b).abs() < 0.001, "{a} != {b}");
}

#[test]
fn duplicate_and_nearby_use_their_own_object_bounds() {
    for (name, dx, form) in [
        ("same", 0.0, false),
        ("near", 0.1, false),
        ("nested-rotated", 0.1, true),
    ] {
        let (mut b, objects) = fixture(
            name,
            &duplicate("41", dx),
            false,
            "WinAnsiEncoding".into(),
            None,
            form,
        );
        assert_eq!(objects.len(), 2);
        assert!(objects[1].chars.iter().all(|c| c.is_generated));
        assert_eq!(b.stats.object_geometry_bound_ops, 1, "{:?}", b.issues);
        assert_eq!(b.stats.matched, 1);
        b.check_replacement().unwrap();
        let e = &b.object_geometry_evidence()[0];
        assert_eq!(e.object_index, 1);
        assert_eq!(e.object_bounds, objects[1].object_bounds.unwrap());
        assert_eq!(e.unicode, vec!['A']);
        assert_eq!(
            e.unicode_source,
            ObjectUnicodeSource::Encoding("WinAnsiEncoding".into())
        );
        let o = e.object_bounds.origin;
        if form {
            near(o.x, 3.94);
            near(o.y, 63.08);
            assert_eq!(e.form_path, vec![0, 0]);
        } else {
            near(o.x, 20.0 + dx);
            near(o.y, 40.0);
            near(
                e.object_bounds.bbox.x0 - objects[0].object_bounds.unwrap().bbox.x0,
                dx,
            );
        }
        let glyphs: Vec<_> = b.ir.glyphs().collect();
        assert_ne!(glyphs[0].id.op, glyphs[1].id.op);
        assert_eq!(glyphs[1].source.string_operand_range, (0, 1));
        b.stats.object_geometry_bound_ops = 0;
        b.stats.matched += 1;
        assert!(b.check_replacement().is_err());
    }
}

#[test]
fn type3_differences_and_tounicode_are_explicit_sources() {
    for (code, name) in [("07", "four"), ("08", "five")] {
        let (b, objects) = fixture(
            name,
            &duplicate(code, 0.1),
            true,
            differences("four"),
            None,
            false,
        );
        assert!(objects[1].chars.is_empty());
        b.check_replacement().unwrap();
        let e = &b.object_geometry_evidence()[0];
        assert_eq!(
            e.unicode_source,
            ObjectUnicodeSource::Differences {
                glyph_name: name.into()
            }
        );
        assert_eq!(e.unicode, vec![if code == "07" { '4' } else { '5' }]);
        assert!(b.ir.glyphs().last().unwrap().flags.is_type3);
    }
    let cm = cmap("1 beginbfchar <41> <0041> endbfchar");
    let (mut b, _) = fixture(
        "unicode",
        &duplicate("41", 0.0),
        false,
        "WinAnsiEncoding".into(),
        Some(&cm),
        false,
    );
    b.check_replacement().unwrap();
    assert_eq!(
        b.object_geometry_evidence()[0].unicode_source,
        ObjectUnicodeSource::ToUnicode
    );
    for item in &mut b.ir.items {
        if let syncpdf_core::ir::DisplayItem::Text { glyphs } = item {
            for g in glyphs {
                g.bbox.x0 += 1.0;
            }
        }
    }
    assert!(b.check_replacement().is_err());
}

/// 一个门禁用例：名称、是否 Type3、字体 Encoding、可选 CMap 流、内容流字节。
type GateCase = (&'static str, bool, Object, Option<Vec<u8>>, Vec<u8>);

/// O4：不可证明操作按「几何可取与否」分流——取得到几何的成为操作级不可删除
/// 墨迹（门禁放行该页其余内容），取不到的仍按页级 fail-closed 拒绝。
#[test]
fn unprovable_ops_split_into_page_closed_and_undeletable_ink() {
    // 页级 fail-closed：pdfium 侧取不到可用的对象几何，无从记录墨迹。
    let closed: [GateCase; 3] = [
        // 差异表指向不存在的字形名 → 对象本身不可解码。
        (
            "unknown-name",
            true,
            differences("notARealGlyph"),
            None,
            duplicate("07", 0.0),
        ),
        // Type3 CharProc 缺失 → 同上。
        (
            "missing-charproc",
            true,
            differences("six"),
            None,
            duplicate("07", 0.0),
        ),
        // 对象边界退化（零宽/零高）→ 墨迹矩形无意义。
        (
            "degenerate",
            false,
            "WinAnsiEncoding".into(),
            None,
            b"BT /F 16 Tf 0 0 0 0 20 40 Tm (A) Tj ET".to_vec(),
        ),
    ];
    for (name, t3, enc, cm, text) in closed {
        let (b, _) = fixture(name, &text, t3, enc, cm.as_deref(), false);
        assert!(b.check_replacement().is_err(), "{name}: {:?}", b.stats);
        assert!(b.object_geometry_evidence().is_empty(), "{name}");
        assert!(b.unproven_source_ops().is_empty(), "{name}");
    }

    // 操作级：pdfium 对象边界可取 → 记为不可删除源墨迹，门禁不再拒绝整页。
    let op_level = [
        (
            "unknown-encoding",
            false,
            Object::Name(b"UnknownEncoding".to_vec()),
            None,
            duplicate("41", 0.0),
        ),
        (
            "empty-unicode",
            false,
            "WinAnsiEncoding".into(),
            Some(cmap("1 beginbfchar <41> <> endbfchar")),
            duplicate("41", 0.0),
        ),
        (
            "missing-unicode",
            false,
            "WinAnsiEncoding".into(),
            Some(cmap("1 beginbfchar <42> <0042> endbfchar")),
            duplicate("41", 0.0),
        ),
        (
            "conflict-unicode",
            false,
            "WinAnsiEncoding".into(),
            Some(cmap("2 beginbfchar <41> <0041> <41> <0042> endbfchar")),
            duplicate("41", 0.0),
        ),
        (
            "tj-array",
            false,
            "WinAnsiEncoding".into(),
            None,
            String::from_utf8(duplicate("41", 0.0))
                .unwrap()
                .replace("<41> Tj", "[<41>] TJ")
                .into_bytes(),
        ),
        (
            "no-encoding",
            false,
            Object::Null,
            None,
            duplicate("41", 0.0),
        ),
        (
            "conflicting-differences",
            true,
            Object::Dictionary(
                dictionary! {"Differences" => vec![7.into(),Object::Name(b"four".to_vec()),7.into(),Object::Name(b"five".to_vec())]},
            ),
            None,
            duplicate("07", 0.0),
        ),
        (
            "multi-code",
            false,
            "WinAnsiEncoding".into(),
            None,
            duplicate("4142", 0.0),
        ),
    ];
    for (name, t3, enc, cm, text) in op_level {
        let (b, _) = fixture(name, &text, t3, enc, cm.as_deref(), false);
        b.check_replacement()
            .unwrap_or_else(|e| panic!("{name}: 操作级降级不应拒绝整页：{e}"));
        assert!(b.object_geometry_evidence().is_empty(), "{name}");
        let ink = b.unproven_source_ops();
        assert_eq!(
            ink.len() as u32,
            b.stats.degraded,
            "{name}: 每个降级操作都应有墨迹记录：{:?}",
            b.stats
        );
        assert!(
            ink.iter()
                .all(|u| u.ink.width() > 0.0 && u.ink.height() > 0.0),
            "{name}: 墨迹几何非退化"
        );
    }
}

#[test]
#[ignore = "manual original-paper probe; set OBJECT_EVIDENCE_PDF"]
fn original_paper_object_evidence_probe() {
    let path = std::env::var("OBJECT_EVIDENCE_PDF").expect("OBJECT_EVIDENCE_PDF");
    let worker = PdfiumWorker::spawn().unwrap();
    let doc = worker.open(std::path::Path::new(&path)).unwrap();
    let lo = Document::load(&path).unwrap();
    for page in 1..=lo.get_pages().len() as u32 {
        let b = bind_page(&worker, doc, &lo, page).unwrap();
        println!(
            "page={page} stats={:?} gate={:?}",
            b.stats,
            b.check_replacement()
        );
        for e in b.object_geometry_evidence() {
            println!("page={page} evidence={e:?}");
        }
        b.check_replacement().unwrap();
        if page == 19 {
            let e = b
                .object_geometry_evidence()
                .iter()
                .find(|e| e.glyph_id.op.stream.obj == 264 && e.glyph_id.op.op_index == 165)
                .expect("p19 source object");
            assert_eq!(e.object_index, 108);
            assert_eq!(e.form_path, vec![0]);
            assert_eq!(e.code, 7);
            assert_eq!(e.font_id, syncpdf_core::ObjRef::new(271, 0));
            assert_eq!(e.unicode, vec!['4']);
            assert_eq!(
                e.unicode_source,
                ObjectUnicodeSource::Differences {
                    glyph_name: "four".into()
                }
            );
            near(e.object_bounds.origin.x, 129.193_22);
            near(e.object_bounds.origin.y, 123.056_9);
            near(e.object_bounds.bbox.x0, 129.427_98);
            near(e.object_bounds.bbox.y0, 123.056_9);
            near(e.object_bounds.bbox.x1, 131.972_03);
            near(e.object_bounds.bbox.y1, 126.549_57);
        }
    }
    worker.close(doc);
}

#[test]
fn rotated_corners_are_transformed_before_the_page_aabb() {
    let text = String::from_utf8(duplicate("07", 0.1))
        .unwrap()
        .replace("1 0 0 1", "0.8 0.6 -0.6 0.8");
    let (b, objects) = fixture(
        "rotated-corners",
        text.as_bytes(),
        true,
        differences("four"),
        None,
        true,
    );
    b.check_replacement().unwrap();
    assert!(objects[1].chars.is_empty());
    let e = &b.object_geometry_evidence()[0];
    let mut xs = Vec::new();
    let mut ys = Vec::new();
    for (x, y) in [(0.0, 0.0), (8.0, 0.0), (8.0, 11.2), (0.0, 11.2)] {
        let local_x = 20.1 + 0.8 * x - 0.6 * y;
        let local_y = 40.0 + 0.6 * x + 0.8 * y;
        let inner_x = 150.0 - 3.0 * local_y;
        let inner_y = 20.0 + 2.0 * local_x;
        xs.push(10.0 + 0.4 * inner_x - 0.3 * inner_y);
        ys.push(30.0 + 0.3 * inner_x + 0.4 * inner_y);
    }
    let r = e.object_bounds.bbox;
    near(r.x0, xs.iter().copied().fold(f32::INFINITY, f32::min));
    near(r.x1, xs.iter().copied().fold(f32::NEG_INFINITY, f32::max));
    near(r.y0, ys.iter().copied().fold(f32::INFINITY, f32::min));
    near(r.y1, ys.iter().copied().fold(f32::NEG_INFINITY, f32::max));
}

#[test]
fn type0_requires_complete_identity_h_source_code() {
    let cm = String::from_utf8(cmap("1 beginbfchar <0041> <0041> endbfchar"))
        .unwrap()
        .replace("<00> <FF>", "<0000> <FFFF>");
    // accepted = 走对象几何绑定；otherwise 操作级降级（O4：记墨迹放行）或
    // 页级拒绝（码不完整时 pdfium 解不出对象几何）。
    for (name, encoding, code, accepted) in [
        ("identity-h", "Identity-H", "0041", true),
        ("identity-v", "Identity-V", "0041", false),
        ("unknown-type0", "UnknownCMap", "0041", false),
        ("partial-identity-h", "Identity-H", "41", false),
    ] {
        let (b, objects) = fixture_with_subtype(
            name,
            &duplicate(code, 0.0),
            "Type0",
            encoding.into(),
            Some(cm.as_bytes()),
            false,
        );
        if accepted {
            assert!(objects[1].chars.is_empty());
            assert_eq!(b.stats.object_geometry_bound_ops, 1);
            b.check_replacement().unwrap();
        } else if name == "partial-identity-h" {
            // 码不完整 → 无对象几何可记 → 页级 fail-closed。
            assert!(b.check_replacement().is_err(), "{name}: {:?}", b.stats);
            assert!(b.unproven_source_ops().is_empty(), "{name}");
        } else {
            // Identity-V / 未知 CMap：不按对象几何绑定，但对象边界可取 →
            // 操作级不可删除墨迹，门禁放行。
            assert!(b.object_geometry_evidence().is_empty(), "{name}");
            assert_eq!(b.unproven_source_ops().len(), 1, "{name}: {:?}", b.stats);
            b.check_replacement()
                .unwrap_or_else(|e| panic!("{name}: 操作级降级不应拒绝整页：{e}"));
        }
    }
}
