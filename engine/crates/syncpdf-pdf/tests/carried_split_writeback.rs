//! 跨流 TJ 拆分的端到端回归（PDF §7.7.3.3 拼接语义）。
//!
//! InDesign 等生成器会把一次 TJ 的数组留在上一条流尾部、裸 `TJ` 关键字
//! 放在下一条流开头。验证：
//! 1. bind 能把两条流的证据配对到同一操作（不再降级为 unproven）；
//! 2. 删除该操作的字形后写回：下一流序列化完整操作、上一流截除被借走的
//!    尾字节，pdfium 重开只渲染一次译文/删除后的文本，无重复渲染。

use lopdf::{dictionary, Dictionary, Document, Object, Stream};
use syncpdf_pdf::{
    bind::bind_page,
    content::parse_page_streams,
    patch::PatchSet,
    pdfium::PdfiumWorker,
};

/// 两页文档：页 1 内容流拆成两条，TJ 的数组在前一流、裸 `TJ` 在后一流。
fn split_tj_document(text: &str) -> Document {
    let mut doc = Document::with_version("1.7");
    let pages = doc.new_object_id();
    let font = doc.add_object(dictionary! {
        "Type" => "Font", "Subtype" => "Type1", "BaseFont" => "Courier",
        "Encoding" => "WinAnsiEncoding",
    });
    let resources = doc.add_object(dictionary! {"Font" => dictionary! {"F1" => font}});
    let prev = format!("BT /F1 12 Tf 1 0 0 1 30 100 Tm [({text})] ");
    let next = b"TJ ET".to_vec();
    let s1 = doc.add_object(Stream::new(Dictionary::new(), prev.into_bytes()));
    let s2 = doc.add_object(Stream::new(Dictionary::new(), next));
    let contents = Object::Array(vec![Object::Reference(s1), Object::Reference(s2)]);
    let page = dictionary! {
        "Type" => "Page", "Parent" => pages,
        "MediaBox" => vec![0.into(), 0.into(), 200.into(), 200.into()],
        "Contents" => contents, "Resources" => resources,
    };
    let kids = vec![Object::Reference(doc.add_object(page))];
    doc.objects.insert(
        pages,
        Object::Dictionary(dictionary! {
            "Type" => "Pages", "Kids" => kids, "Count" => 1,
        }),
    );
    let root = doc.add_object(dictionary! {"Type" => "Catalog", "Pages" => pages});
    doc.trailer.set("Root", root);
    doc
}

fn page_text(worker: &PdfiumWorker, doc: syncpdf_pdf::pdfium::DocId, page: u32) -> String {
    worker
        .page_text_objects(doc, page - 1)
        .map(|objs| {
            objs.iter()
                .flat_map(|o| o.chars.iter())
                .filter_map(|c| c.unicode.as_deref())
                .collect::<String>()
        })
        .unwrap_or_default()
}

#[test]
fn bind_and_delete_split_tj_glyphs() {
    let worker = match PdfiumWorker::spawn() {
        Ok(w) => w,
        Err(_) => return, // 无 pdfium 夹具时跳过
    };
    let dir = tempfile::tempdir().unwrap();
    let input = dir.path().join("split.pdf");
    let output = dir.path().join("patched.pdf");
    let mut doc = split_tj_document("AB");
    doc.save(&input).unwrap();

    let pdf = worker.open(&input).unwrap();
    let bound = bind_page(&worker, pdf, &doc, 1).unwrap();
    bound.check_replacement().unwrap();
    // 拆开的 TJ 重组成功：两个字符都绑上，无 unproven。
    assert_eq!(bound.ir.glyphs().count(), 2);
    assert!(bound.unproven_source_ops().is_empty());
    worker.close(pdf);

    let ids: Vec<_> = bound.ir.glyphs().map(|g| g.id).collect();
    let mut patch = PatchSet::new();
    patch.delete_glyphs(&bound, &ids).unwrap();
    let stats = patch.apply(&mut doc, 1).unwrap();
    assert_eq!(stats.ops_deleted, 1);
    doc.save(&output).unwrap();

    // pdfium 重开：源文本消失，且没有重复渲染（空串除外不应有任何字符）。
    let out = worker.open(&output).unwrap();
    assert_eq!(page_text(&worker, out, 1), "");
    worker.close(out);
}

#[test]
fn carried_rewrite_serializes_full_operation_and_truncates_prev() {
    let worker = match PdfiumWorker::spawn() {
        Ok(w) => w,
        Err(_) => return,
    };
    let dir = tempfile::tempdir().unwrap();
    let input = dir.path().join("split.pdf");
    let mut doc = split_tj_document("AB");
    doc.save(&input).unwrap();

    let pdf = worker.open(&input).unwrap();
    let bound = bind_page(&worker, pdf, &doc, 1).unwrap();
    worker.close(pdf);

    // 只删第二个字形：携带操作被部分改写。
    let a = bound
        .ir
        .glyphs()
        .find(|g| g.unicode.iter().collect::<String>() == "B")
        .map(|g| g.id)
        .expect("glyph B");
    let mut patch = PatchSet::new();
    patch.delete_glyphs(&bound, &[a]).unwrap();
    patch.apply(&mut doc, 1).unwrap();
    doc.save(&input).unwrap();

    let out = worker.open(&input).unwrap();
    let text = page_text(&worker, out, 1);
    assert_eq!(text, "A", "部分删除后只保留 A，且不得重复渲染：{text:?}");
    worker.close(out);

    // 上一流必须已截除被借走的数组尾字节。
    let reloaded = Document::load(&input).unwrap();
    let page_id = reloaded.get_pages()[&1];
    let contents = reloaded.get_page_contents(page_id);
    let prev = reloaded
        .get_object(contents[0])
        .unwrap()
        .as_stream()
        .unwrap()
        .decompressed_content()
        .unwrap();
    assert!(
        !prev.contains(&b'['),
        "上一流不应残留被借走的数组字节：{:?}",
        String::from_utf8_lossy(&prev)
    );
    assert!(prev.trim_ascii_end().ends_with(b"Tm"));
}

#[test]
fn parse_page_streams_reports_leftover_start() {
    // 纯解析层反例：无拆分时各流无截除点，操作归属不变。
    let s1 = b"BT /F1 12 Tf 30 100 Td (A) Tj ET".to_vec();
    let s2 = b"BT /F1 12 Tf 30 80 Td (B) Tj ET".to_vec();
    let parsed = parse_page_streams(&[&s1, &s2]).unwrap();
    assert_eq!(parsed[0].ops.len(), 5);
    assert_eq!(parsed[1].ops.len(), 5);
    assert!(parsed.iter().all(|p| p.leftover_start.is_none()));
    assert!(parsed.iter().all(|p| p.ops.iter().all(|o| !o.carried)));
}
