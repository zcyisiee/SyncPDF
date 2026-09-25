//! 标准 14 字体可以省略 `/Widths`（如 arXiv 侧边水印的 Times-Roman）：字宽取
//! 阅读器内置度量，不能是未知值，否则删除这些字形的补丁无法保持后续文字位置。
use lopdf::{dictionary, Document, Object, Stream};
use syncpdf_pdf::bind::bind_page;
use syncpdf_pdf::pdfium::PdfiumWorker;
use syncpdf_pdf::PatchSet;

#[test]
fn fonts_without_widths_use_builtin_metrics_and_explicit_widths_still_win() {
    let mut d = Document::with_version("1.7");
    let pages = d.new_object_id();
    // Times-Roman AFM：W = 944，i = 278。
    let builtin = d.add_object(dictionary! {"Type" => "Font", "Subtype" => "Type1", "BaseFont" => "Times-Roman", "Encoding" => "WinAnsiEncoding"});
    // 编码重映射：字符码 A/B 画 W/i，字宽跟字形走，不跟字符码走。
    let remapped = d.add_object(dictionary! {"Type" => "Font", "Subtype" => "Type1", "BaseFont" => "Times-Roman",
        "Encoding" => dictionary! {"Type" => "Encoding", "BaseEncoding" => "WinAnsiEncoding",
            "Differences" => vec![65.into(), Object::Name(b"W".to_vec()), Object::Name(b"i".to_vec())]}});
    // 反例：显式 `/Widths` 优先于内置度量。
    let explicit = d.add_object(dictionary! {"Type" => "Font", "Subtype" => "Type1", "BaseFont" => "Times-Roman", "Encoding" => "WinAnsiEncoding",
        "FirstChar" => 87, "LastChar" => 87, "Widths" => vec![500.into()]});
    let resources =
        dictionary! {"Font" => dictionary! {"B" => builtin, "R" => remapped, "E" => explicit}};
    let content = d.add_object(Stream::new(
        dictionary! {},
        // 水平一行 + arXiv 式竖排一行（`Tm` 旋转）+ 重映射 + 显式字宽。
        b"BT /B 10 Tf 20 300 Td (Wi) Tj ET \
          BT /B 10 Tf 0 1 -1 0 30 20 Tm (Wi) Tj ET \
          BT /R 10 Tf 100 200 Td (AB) Tj ET \
          BT /E 10 Tf 100 100 Td (W) Tj ET"
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
    let path = dir.path().join("builtin-widths.pdf");
    d.save(&path).unwrap();

    let worker = PdfiumWorker::spawn().expect("PDFium required for this regression");
    let doc = worker.open(&path).unwrap();
    let bound = bind_page(&worker, doc, &d, 1).unwrap();
    worker.close(doc);

    let advances: Vec<(char, f32)> = bound
        .ir
        .glyphs()
        .map(|g| (g.unicode.first().copied().unwrap_or('?'), g.advance))
        .collect();
    let want = [
        ('W', 9.44),
        ('i', 2.78),
        ('W', 9.44),
        ('i', 2.78),
        ('W', 9.44),
        ('i', 2.78),
        ('W', 5.0),
    ];
    assert_eq!(advances.len(), want.len(), "{advances:?}");
    for ((c, got), (wc, w)) in advances.iter().zip(want) {
        assert_eq!(*c, wc, "{advances:?}");
        assert!((got - w).abs() < 0.01, "{c}: {got} != {w} in {advances:?}");
    }

    // 删掉整页字形（公式源绘制隔离副本的做法）必须成功。
    let ids: Vec<_> = bound.ir.glyphs().map(|g| g.id).collect();
    let mut patch = PatchSet::new();
    patch.delete_glyphs(&bound, &ids).unwrap();
    let mut out = d.clone();
    patch.apply(&mut out, 1).unwrap();
}
