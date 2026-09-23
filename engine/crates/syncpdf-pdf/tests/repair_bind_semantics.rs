//! R1-bind 回归样本：合成 PDF 上逐类验证 code ↔ pdfium 字符的对齐语义。
//!
//! 覆盖实测到的 pdfium 文本页行为（pdfium 156/8066，见 `bind.rs` 模块注释）：
//! - TJ 位移产生的合成空格（`is_generated`）：无源字节，不得成为可删字形；
//! - 真实空格 code（0x20）：有源字节，必须绑定为可删字形；
//! - 连续空格 code 被 pdfium 折叠成一个字符：字形保留、共享几何证据；
//! - 连字/一对多 Unicode（Type0 ToUnicode 多码点）：一个 code 一个字形，
//!   且不能让后续整段移位；
//! - 无映射 code：字形安全保留（可删身份），可观察；
//! - 重复文本操作：各自独立配对；
//! - Form XObject 混在路径等其它对象之间：form_path 两侧编号一致才能配对。
//!
//! 缺 pdfium 动态库时打印原因并 skip（与 `pdfium_smoke.rs` 同策略）；
//! 真实 PDF 的全篇统计见 `repair_bind_report.rs`。
//!
//! 合成 PDF 用 lopdf 从零构造（与 `patch.rs` 测试同款手法）。

use std::path::PathBuf;

use lopdf::content::Content as LContent;
use lopdf::{Dictionary as LDict, Document as LDoc, Object as LObj, ObjectId, Stream};
use syncpdf_core::ir::{DisplayItem, Glyph};
use syncpdf_pdf::bind::{bind_page, BoundPage};
use syncpdf_pdf::pdfium::{DocId, PdfiumWorker};

fn worker() -> Option<PdfiumWorker> {
    if syncpdf_core::fixtures::pdfium_lib_dir().is_none() {
        eprintln!("SKIP: pdfium 动态库缺失；设置 PDFIUM_DYNAMIC_LIB_PATH 或运行 vendor 同步脚本");
        return None;
    }
    match PdfiumWorker::spawn() {
        Ok(w) => Some(w),
        Err(e) => {
            eprintln!("SKIP: pdfium worker 启动失败：{e}");
            None
        }
    }
}

/// 写到 TMPDIR 下的临时文件（TMPDIR 由测试环境指到本 worktree 的 tmp/）。
fn write_pdf(mut doc: LDoc, name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join("repair-bind-semantics");
    std::fs::create_dir_all(&dir).expect("create temp dir");
    let path = dir.join(name);
    let mut out = Vec::new();
    doc.save_to(&mut out).expect("save pdf");
    std::fs::write(&path, out).expect("write pdf");
    path
}

/// 绑定单页合成 PDF。
fn bind(path: &std::path::Path) -> (PdfiumWorker, DocId, LDoc, BoundPage) {
    let worker = PdfiumWorker::spawn().expect("pdfium worker");
    let doc = worker.open(path).expect("open");
    let lo = LDoc::load(path).expect("lopdf load");
    let bound = bind_page(&worker, doc, &lo, 1).expect("bind_page");
    (worker, doc, lo, bound)
}

/// 一次操作的文本项与字形。
fn text_items(bound: &BoundPage) -> Vec<&Vec<Glyph>> {
    bound
        .ir
        .items
        .iter()
        .filter_map(|it| match it {
            DisplayItem::Text { glyphs } => Some(glyphs),
            _ => None,
        })
        .collect()
}

fn glyph_text(g: &Glyph) -> String {
    g.unicode.iter().collect()
}

// ---------------------------------------------------------------------------
// lopdf 构造器
// ---------------------------------------------------------------------------

/// 组装单页文档骨架；返回页对象 id，资源由调用方补。
fn one_page(doc: &mut LDoc, content: &[u8]) -> (ObjectId, ObjectId) {
    let pages_id = doc.new_object_id();
    let content_id = doc.add_object(Stream::new(LDict::new(), content.to_vec()));
    let mut page = LDict::new();
    page.set("Type", LObj::Name(b"Page".to_vec()));
    page.set("Parent", LObj::Reference(pages_id));
    page.set("MediaBox", vec![0.into(), 0.into(), 612.into(), 792.into()]);
    page.set("Contents", LObj::Reference(content_id));
    let page_id = doc.add_object(page);
    let mut pages = LDict::new();
    pages.set("Type", LObj::Name(b"Pages".to_vec()));
    pages.set("Kids", vec![LObj::Reference(page_id)]);
    pages.set("Count", 1);
    doc.objects.insert(pages_id, LObj::Dictionary(pages));
    let catalog = doc.add_object(LDict::new());
    if let Ok(LObj::Dictionary(d)) = doc.get_object_mut(catalog) {
        d.set("Type", LObj::Name(b"Catalog".to_vec()));
        d.set("Pages", LObj::Reference(pages_id));
    }
    doc.trailer.set("Root", LObj::Reference(catalog));
    (page_id, content_id)
}

/// Type1 Helvetica（基 14，WinAnsi，0..255 宽度 600）字体字典。
fn helvetica_dict() -> LDict {
    let mut d = LDict::new();
    d.set("Type", LObj::Name(b"Font".to_vec()));
    d.set("Subtype", LObj::Name(b"Type1".to_vec()));
    d.set("BaseFont", LObj::Name(b"Helvetica".to_vec()));
    d.set("Encoding", LObj::Name(b"WinAnsiEncoding".to_vec()));
    d.set("FirstChar", LObj::Integer(0));
    d.set("LastChar", LObj::Integer(255));
    d.set("Widths", LObj::Array(vec![LObj::Real(600.0); 256]));
    d
}

/// 单页 + F1 = Helvetica 的文档，内容流为 `content`。
fn simple_font_doc(content: &[u8]) -> LDoc {
    let mut doc = LDoc::with_version("1.7");
    let (page_id, _) = one_page(&mut doc, content);
    let font_id = doc.add_object(helvetica_dict());
    let mut fonts = LDict::new();
    fonts.set("F1", LObj::Reference(font_id));
    let mut res = LDict::new();
    res.set("Font", fonts);
    set_page_resources(&mut doc, page_id, res);
    doc
}

/// Type0（Identity-H，CIDFontType2，无字体文件 → pdfium 用后备字体）+
/// ToUnicode bfchar。返回 Type0 字体字典（含已挂好的间接对象）。
fn type0_font(doc: &mut LDoc, to_uni: &[(u32, &str)]) -> LObj {
    // Descendant CIDFontType2。
    let mut csi = LDict::new();
    csi.set(
        "Registry",
        LObj::String(b"Adobe".to_vec(), lopdf::StringFormat::Literal),
    );
    csi.set(
        "Ordering",
        LObj::String(b"Identity".to_vec(), lopdf::StringFormat::Literal),
    );
    csi.set("Supplement", LObj::Integer(0));
    let mut cid = LDict::new();
    cid.set("Type", LObj::Name(b"Font".to_vec()));
    cid.set("Subtype", LObj::Name(b"CIDFontType2".to_vec()));
    cid.set("BaseFont", LObj::Name(b"FakeCJK".to_vec()));
    cid.set("CIDSystemInfo", csi);
    cid.set("CIDToGIDMap", LObj::Name(b"Identity".to_vec()));
    cid.set("DW", LObj::Integer(1000));
    let cid_id = doc.add_object(LObj::Dictionary(cid));

    // ToUnicode CMap（bfchar）。空文本 → 空目的地（明确无映射）。
    let mut cmap = String::from(
        "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n\
         /CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n\
         /CMapName /Adobe-Identity-UCS def /CMapType 2 def\n\
         1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n",
    );
    cmap.push_str(&format!("{} beginbfchar\n", to_uni.len()));
    for &(code, text) in to_uni {
        let dst: String = text.encode_utf16().map(|u| format!("{u:04X}")).collect();
        if dst.is_empty() {
            cmap.push_str(&format!("<{code:04X}> <>\n"));
        } else {
            cmap.push_str(&format!("<{code:04X}> <{dst}>\n"));
        }
    }
    cmap.push_str("endbfchar\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n");
    let mut sd = LDict::new();
    sd.set("Length", cmap.len() as i64);
    let to_uni_id = doc.add_object(LObj::Stream(Stream::new(sd, cmap.into_bytes())));

    let mut t0 = LDict::new();
    t0.set("Type", LObj::Name(b"Font".to_vec()));
    t0.set("Subtype", LObj::Name(b"Type0".to_vec()));
    t0.set("BaseFont", LObj::Name(b"FakeCJK".to_vec()));
    t0.set("Encoding", LObj::Name(b"Identity-H".to_vec()));
    t0.set("DescendantFonts", vec![LObj::Reference(cid_id)]);
    t0.set("ToUnicode", LObj::Reference(to_uni_id));
    LObj::Dictionary(t0)
}

/// 单页 + T1 = Type0 的文档，内容流为 `content`（2 字节 code，hex 串写法）。
fn type0_font_doc(to_uni: &[(u32, &str)], content: &[u8]) -> LDoc {
    let mut doc = LDoc::with_version("1.7");
    let (page_id, _) = one_page(&mut doc, content);
    let font = type0_font(&mut doc, to_uni);
    let font_id = doc.add_object(font);
    let mut fonts = LDict::new();
    fonts.set("T1", LObj::Reference(font_id));
    let mut res = LDict::new();
    res.set("Font", fonts);
    set_page_resources(&mut doc, page_id, res);
    doc
}

/// Form XObject：带 /Resources（F1 = Helvetica）与内容流。
fn form_object(doc: &mut LDoc, body: &str) -> ObjectId {
    let helv_id = doc.add_object(helvetica_dict());
    let mut fdict = LDict::new();
    fdict.set("F1", LObj::Reference(helv_id));
    let mut res = LDict::new();
    res.set("Font", fdict);
    let mut d = LDict::new();
    d.set("Type", LObj::Name(b"XObject".to_vec()));
    d.set("Subtype", LObj::Name(b"Form".to_vec()));
    d.set("BBox", vec![0.into(), 0.into(), 400.into(), 20.into()]);
    d.set("Resources", res);
    let content = LContent::decode(body.as_bytes()).expect("decode form body");
    doc.add_object(LObj::Stream(Stream::new(
        d,
        content.encode().expect("encode"),
    )))
}

fn set_page_resources(doc: &mut LDoc, page_id: ObjectId, res: LDict) {
    if let Ok(LObj::Dictionary(d)) = doc.get_object_mut(page_id) {
        d.set("Resources", LObj::Dictionary(res));
    }
}

/// 页内容流字节里构造一段 BT…ET 文本。
fn bt_text(font: &str, size: f32, x: f32, y: f32, show: &str) -> String {
    format!("BT /{font} {size} Tf 1 0 0 1 {x} {y} Tm {show} ET\n")
}

// ---------------------------------------------------------------------------
// 用例
// ---------------------------------------------------------------------------

#[test]
fn plain_tj_one_glyph_per_code() {
    let Some(_w) = worker() else { return };
    let doc = simple_font_doc(bt_text("F1", 12.0, 72.0, 700.0, "(Hello World) Tj").as_bytes());
    let path = write_pdf(doc, "plain-tj.pdf");
    let (_, _docid, _, bound) = bind(&path);
    assert_eq!(bound.stats.text_ops, 1, "{:?}", bound.stats);
    assert_eq!(
        bound.stats.matched, 1,
        "{:?} issues={:?}",
        bound.stats, bound.issues
    );
    assert_eq!(bound.stats.degraded, 0, "issues: {:?}", bound.issues);
    let items = text_items(&bound);
    assert_eq!(items.len(), 1);
    let glyphs = items[0];
    assert_eq!(glyphs.len(), 11, "Hello World = 11 code");
    let text: String = glyphs.iter().map(glyph_text).collect();
    assert_eq!(text, "Hello World");
    // 逐字形：序号连续、几何非空（空格除外）。
    for (i, g) in glyphs.iter().enumerate() {
        assert_eq!(g.id.ordinal, i as u16);
        assert!(
            !g.bbox.is_empty() || g.flags.is_space,
            "字形 {i} 无几何：{g:?}"
        );
    }
    // 字节范围覆盖整个操作数。
    let (b0, _) = glyphs[0].source.string_operand_range;
    let (_, b1) = glyphs[10].source.string_operand_range;
    assert_eq!((b0, b1), (0, 11));
}

#[test]
fn tj_displacement_gap_is_generated_space_not_glyph() {
    let Some(_w) = worker() else { return };
    // -2500/1000*12 = 30pt 位移，pdfium 会补一个合成空格。
    let content = "BT /F1 12 Tf 1 0 0 1 72 700 Tm [(Handcrafted)-2500(Backdoors)] TJ ET\n";
    let doc = simple_font_doc(content.as_bytes());
    let path = write_pdf(doc, "tj-gap.pdf");
    let (_, _, _, bound) = bind(&path);
    assert!(
        bound.stats.generated_chars >= 1,
        "pdfium 应补合成空格：{:?}",
        bound.stats
    );
    assert_eq!(
        bound.stats.matched, 1,
        "{:?} issues={:?}",
        bound.stats, bound.issues
    );
    let items = text_items(&bound);
    let glyphs = items[0];
    assert_eq!(glyphs.len(), 20, "Handcrafted+Backdoors = 11+9 code");
    // 合成空格不占字形：11 + 9 = 20，且中间没有多余空格字形。
    let text: String = glyphs.iter().map(glyph_text).collect();
    assert_eq!(text, "HandcraftedBackdoors", "合成空格不得混入字形");
}

#[test]
fn real_spaces_have_source_bytes_and_bind() {
    let Some(_w) = worker() else { return };
    let doc = simple_font_doc(bt_text("F1", 12.0, 72.0, 700.0, "(A  B) Tj").as_bytes());
    let path = write_pdf(doc, "real-spaces.pdf");
    let (_, _, _, bound) = bind(&path);
    let items = text_items(&bound);
    assert_eq!(items.len(), 1);
    let glyphs = items[0];
    assert_eq!(glyphs.len(), 4, "A、空格、空格、B 四个 code");
    let text: String = glyphs.iter().map(glyph_text).collect();
    assert_eq!(text, "A  B", "两个空格 code 都保留");
    let spaces: Vec<_> = glyphs.iter().filter(|g| g.flags.is_space).collect();
    assert_eq!(spaces.len(), 2, "应有 2 个 is_space 字形");
    for g in &spaces {
        assert_eq!(g.code, 0x20);
        let (b0, b1) = g.source.string_operand_range;
        assert_eq!(b1 - b0, 1, "空格占 1 字节：{g:?}");
    }
    // pdfium 折叠连续空格 → 计入 space_collapsed 而非 degraded。
    assert_eq!(bound.stats.space_collapsed, 1, "{:?}", bound.stats);
    assert_eq!(bound.stats.degraded, 0, "issues: {:?}", bound.issues);
}

#[test]
fn triple_spaces_all_retained() {
    let Some(_w) = worker() else { return };
    let doc = simple_font_doc(bt_text("F1", 12.0, 72.0, 700.0, "(A   B) Tj").as_bytes());
    let path = write_pdf(doc, "triple-spaces.pdf");
    let (_, _, _, bound) = bind(&path);
    let items = text_items(&bound);
    let glyphs = items[0];
    assert_eq!(glyphs.len(), 5);
    let text: String = glyphs.iter().map(glyph_text).collect();
    assert_eq!(text, "A   B", "三个空格 code 全部保留");
    assert_eq!(bound.stats.degraded, 0, "issues: {:?}", bound.issues);
}

#[test]
fn type0_ligature_single_glyph_no_shift() {
    let Some(_w) = worker() else { return };
    // CID 1 → "fi"（一对多），CID 2 → "x"。hex 串 <00010002> = 两个双字节 code。
    let content = "BT /T1 12 Tf 1 0 0 1 72 700 Tm <00010002> Tj ET\n";
    let doc = type0_font_doc(&[(1, "fi"), (2, "x")], content.as_bytes());
    let path = write_pdf(doc, "type0-ligature.pdf");
    let (_, _, _, bound) = bind(&path);
    let items = text_items(&bound);
    assert_eq!(items.len(), 1);
    let glyphs = items[0];
    assert_eq!(glyphs.len(), 2, "两个 CID 两个字形");
    assert_eq!(glyph_text(&glyphs[0]), "fi", "连字 CID → 一个字形带 \"fi\"");
    assert_eq!(glyph_text(&glyphs[1]), "x", "后续字形不得移位");
    assert_eq!(bound.stats.multi_char_glyphs, 1, "{:?}", bound.stats);
    assert_eq!(
        bound.stats.matched, 1,
        "{:?} issues={:?}",
        bound.stats, bound.issues
    );
    assert_eq!(bound.stats.degraded, 0, "issues: {:?}", bound.issues);
    // 连字字形的字节范围覆盖整个 CID（2 字节）。
    let (b0, b1) = glyphs[0].source.string_operand_range;
    assert_eq!((b0, b1), (0, 2));
    let (b0, b1) = glyphs[1].source.string_operand_range;
    assert_eq!((b0, b1), (2, 4));
}

#[test]
fn type0_scalar_bfrange_supplementary_geometry() {
    let mut doc = type0_font_doc(&[], b"BT /T1 12 Tf 1 0 0 1 72 700 Tm <00010002> Tj ET\n");
    let cmap = b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n\
        /CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n\
        /CMapName /Adobe-Identity-UCS def /CMapType 2 def\n\
        1 begincodespacerange <0000> <FFFF> endcodespacerange\n\
        1 beginbfrange <0001> <0002> <D83DDE00> endbfrange\n\
        endcmap CMapName currentdict /CMap defineresource pop end end\n";
    let cmap_id = doc
        .objects
        .values()
        .find_map(|obj| {
            obj.as_dict()
                .ok()?
                .get(b"ToUnicode")
                .ok()?
                .as_reference()
                .ok()
        })
        .expect("Type0 ToUnicode");
    doc.objects.insert(
        cmap_id,
        LObj::Stream(Stream::new(LDict::new(), cmap.to_vec())),
    );
    let path = write_pdf(doc, "type0-scalar-bfrange-supplementary.pdf");
    let (worker, docid, _, bound) = bind(&path);
    let objects = worker
        .page_text_objects(docid, 0)
        .expect("independent extraction");
    let chars: Vec<_> = objects[0]
        .chars
        .iter()
        .filter(|c| !c.is_generated)
        .collect();
    eprintln!("scalar supplementary PDFium chars={chars:?}; bound={bound:?}");
    let items = text_items(&bound);
    assert_eq!(items.len(), 1);
    let glyphs = items[0];
    assert_eq!(glyphs.len(), 2);
    assert_eq!(bound.stats.degraded, 0, "{:?}", bound.issues);
    for (i, expected) in ["😀", "😁"].iter().enumerate() {
        let g = &glyphs[i];
        assert_eq!(glyph_text(g), *expected);
        assert_eq!(g.code, (i + 1) as u32);
        assert_eq!(
            g.source.string_operand_range,
            (i as u32 * 2, i as u32 * 2 + 2)
        );
        assert_eq!(g.source.decoded_code_range, (i as u32, i as u32 + 1));
        let observed = chars
            .iter()
            .find(|c| (c.origin.x - (72.0 + i as f32 * 12.0)).abs() < 0.01)
            .expect("each CID has its own PDFium geometry");
        let origin = observed.origin;
        assert_eq!(
            g.matrix,
            syncpdf_core::Matrix::translate(origin.x, origin.y)
        );
        assert_eq!(g.bbox, observed.bbox);
        assert_eq!(g.advance, observed.width);
    }
    assert_ne!(glyphs[0].matrix, glyphs[1].matrix);
}

#[test]
fn type0_empty_mapping_binds_empty_unicode() {
    let Some(_w) = worker() else { return };
    // CID 1 无 Unicode 映射（空目的地），CID 2 → "y"。
    let content = "BT /T1 12 Tf 1 0 0 1 72 700 Tm <00010002> Tj ET\n";
    let doc = type0_font_doc(&[(1, ""), (2, "y")], content.as_bytes());
    let path = write_pdf(doc, "type0-empty-mapping.pdf");
    let (_, _, _, bound) = bind(&path);
    let items = text_items(&bound);
    let glyphs = items[0];
    assert_eq!(glyphs.len(), 2);
    assert_eq!(glyph_text(&glyphs[0]), "", "无映射 CID 的 unicode 为空");
    assert_eq!(glyph_text(&glyphs[1]), "y", "后续字形不得移位");
    // 无字体文件时 pdfium 后备字体给两个 CID 都出控制字符：CID1 空映射
    // 消费占位但仍 unbound；CID2 必须使用属于 CID2 的控制字符几何。
    assert_eq!(bound.stats.degraded, 1, "{:?}", bound.stats);
    assert_eq!(bound.stats.unbound_glyphs, 1, "{:?}", bound.stats);
}

/// Blocking regression: text equality alone conceals CID2 using CID1's origin.
/// Requires the independently extracted CID2 geometry, not merely equal text.
#[test]
fn empty_mapping_must_not_steal_next_codes_geometry() {
    let doc = type0_font_doc(
        &[(1, ""), (2, "y")],
        b"BT /T1 12 Tf 1 0 0 1 72 700 Tm <00010002> Tj ET\n",
    );
    let path = write_pdf(doc, "empty-mapping-geometry.pdf");
    // Unlike optional corpus probes, this safety regression must not silently skip.
    let (worker, docid, _, bound) = bind(&path);
    let objects = worker
        .page_text_objects(docid, 0)
        .expect("independent extraction");
    let chars: Vec<_> = objects[0]
        .chars
        .iter()
        .filter(|c| !c.is_generated)
        .collect();
    assert_eq!(chars.len(), 2, "fixture must expose both source glyphs");
    assert_ne!(chars[0].origin, chars[1].origin);
    let items = text_items(&bound);
    let second = &items[0][1];
    assert_eq!(second.code, 2);
    assert_eq!(second.source.string_operand_range, (2, 4));
    assert_eq!(glyph_text(second), "y");
    eprintln!(
        "CID1 origin={:?}; CID2 origin={:?}; bound CID2 matrix={:?}; stats={:?}; issues={:?}",
        chars[0].origin, chars[1].origin, second.matrix, bound.stats, bound.issues
    );
    assert_eq!(
        second.matrix,
        syncpdf_core::Matrix::translate(chars[1].origin.x, chars[1].origin.y),
        "CID2 must use its own PDFium origin, not CID1's control-character cluster"
    );
}

#[test]
fn repeated_ops_bind_independently() {
    let Some(_w) = worker() else { return };
    let content = format!(
        "{}{}",
        bt_text("F1", 12.0, 72.0, 700.0, "(Dup) Tj"),
        bt_text("F1", 12.0, 72.0, 650.0, "(Dup) Tj"),
    );
    let doc = simple_font_doc(content.as_bytes());
    let path = write_pdf(doc, "repeated-ops.pdf");
    let (_, _, _, bound) = bind(&path);
    assert_eq!(bound.stats.text_ops, 2);
    assert_eq!(bound.stats.matched, 2, "{:?}", bound.stats);
    let items = text_items(&bound);
    assert_eq!(items.len(), 2);
    for (i, glyphs) in items.iter().enumerate() {
        assert_eq!(glyphs.len(), 3, "第 {i} 个操作 3 code");
        let text: String = glyphs.iter().map(glyph_text).collect();
        assert_eq!(text, "Dup");
        for (j, g) in glyphs.iter().enumerate() {
            assert_eq!(g.id.ordinal, j as u16);
        }
    }
    // 两个操作的 op key 不同，字节范围都指向各自字符串内的 0..1。
    assert_ne!(items[0][0].id.op, items[1][0].id.op);
    assert_eq!(items[0][0].source.string_operand_range, (0, 1));
    assert_eq!(items[1][0].source.string_operand_range, (0, 1));
}

#[test]
fn unmapped_code_retains_glyph_safely() {
    let Some(_w) = worker() else { return };
    // 0x02 在 WinAnsi Helvetica 没有字形。无论 pdfium 出不出字符，
    // 字形必须保留（可删身份），且不能吞掉后续字形的证据。
    let body = "(A\\002B) Tj";
    let doc = simple_font_doc(bt_text("F1", 12.0, 72.0, 700.0, body).as_bytes());
    let path = write_pdf(doc, "unmapped-code.pdf");
    let (_, _, _, bound) = bind(&path);
    let items = text_items(&bound);
    assert_eq!(items.len(), 1);
    let glyphs = items[0];
    assert_eq!(glyphs.len(), 3, "三个 code 三个字形（含无映射 code）");
    assert_eq!(glyph_text(&glyphs[0]), "A");
    // 实测：pdfium 对 WinAnsi 未定义的 code 出一个同码点的控制字符，
    // 字形照常绑定（matched），不吞后续证据。
    assert_eq!(
        glyph_text(&glyphs[1]),
        "\u{2}",
        "无映射 code 的 unicode = 原码点"
    );
    assert_eq!(
        glyph_text(&glyphs[2]),
        "B",
        "后续字形不得被无映射 code 吞掉"
    );
    assert_eq!(bound.stats.matched, 1, "{:?}", bound.stats);
    assert_eq!(bound.stats.degraded, 0, "issues: {:?}", bound.issues);
}

#[test]
fn form_after_path_object_binds() {
    let Some(_w) = worker() else { return };
    // 页对象序列：路径、FormA(文本)、路径、FormB(文本)。
    // 修复前 pdfium 的 form_path 用「层内全对象下标」（1、3），bind 用 Form
    // 进入序号（0、1），两侧分组错位 → no_pdfium_object（真实论文曾 140 例）。
    let mut doc = LDoc::with_version("1.7");
    let page_content =
        b"0 0 612 20 re f\nq 1 0 0 1 72 700 cm /FmA Do Q\n0 772 612 20 re f\nq 1 0 0 1 72 600 cm /FmB Do Q\n";
    let (page_id, _) = one_page(&mut doc, page_content);
    let form_a = form_object(&mut doc, "BT /F1 12 Tf 0 5 Td (Alpha) Tj ET\n");
    let form_b = form_object(&mut doc, "BT /F1 12 Tf 0 5 Td (Beta) Tj ET\n");
    let mut xobjects = LDict::new();
    xobjects.set("FmA", LObj::Reference(form_a));
    xobjects.set("FmB", LObj::Reference(form_b));
    let mut res = LDict::new();
    res.set("XObject", xobjects);
    set_page_resources(&mut doc, page_id, res);
    let path = write_pdf(doc, "form-after-path.pdf");
    let (_, _, _, bound) = bind(&path);
    assert_eq!(bound.stats.text_ops, 2, "{:?}", bound.stats);
    assert!(
        bound.stats.matched == 2,
        "Form 内文本应全部配对：{:?} issues={:?}",
        bound.stats,
        bound.issues
    );
    let items = text_items(&bound);
    let text: String = items
        .iter()
        .flat_map(|gs| gs.iter().map(glyph_text))
        .collect();
    assert_eq!(text, "AlphaBeta");
}

#[test]
fn replacement_gate_rejects_diagnostics_and_missing_source_identity() {
    use syncpdf_pdf::bind::ReplacementError;
    use syncpdf_pdf::patch::{PatchError, PatchSet};
    let path = write_pdf(
        simple_font_doc(b"BT /F1 12 Tf 72 700 Td (ABC) Tj ET"),
        "gate-trusted.pdf",
    );
    let (_, _, mut lo, bound) = bind(&path);
    bound.check_replacement().unwrap();
    let ids: Vec<_> = bound.ir.glyphs().map(|g| g.id).collect();
    let before = format!("{:?}", lo.objects);
    let mut bad_pages = Vec::new();
    let mut bad = bound.clone();
    bad.stats.degraded = 1;
    bad_pages.push(bad);
    let mut bad = bound.clone();
    bad.stats.unbound_glyphs = 1;
    bad_pages.push(bad);
    let mut bad = bound.clone();
    bad.stats.text_objects += 1;
    bad_pages.push(bad);
    let mut bad = bound.clone();
    bad.issues.push("parse/alignment anomaly".into());
    bad_pages.push(bad);
    let mut bad = bound.clone();
    if let DisplayItem::Text { glyphs } = &mut bad.ir.items[0] {
        glyphs[0].source.string_operand_range = (0, 0);
    }
    bad_pages.push(bad);
    let mut bad = bound.clone();
    if let DisplayItem::Text { glyphs } = &mut bad.ir.items[0] {
        glyphs.push(glyphs[0].clone());
    }
    assert!(matches!(
        bad.check_replacement(),
        Err(ReplacementError::DuplicateIdentity(_))
    ));
    bad_pages.push(bad);
    let mut bad = bound.clone();
    if let DisplayItem::Text { glyphs } = &mut bad.ir.items[0] {
        glyphs.pop();
    }
    assert!(matches!(
        bad.check_replacement(),
        Err(ReplacementError::SourceCoverage(_))
    ));
    bad_pages.push(bad);
    for bad in bad_pages {
        let mut ps = PatchSet::new();
        assert!(matches!(
            ps.delete_glyphs(&bad, &ids),
            Err(PatchError::UnsafeBinding(_))
        ));
        assert!(ps.is_empty());
        assert_eq!(ps.apply(&mut lo, 1).unwrap().streams_touched, 0);
        assert_eq!(format!("{:?}", lo.objects), before);
    }
    for invalid in [
        syncpdf_core::GlyphId {
            ordinal: 999,
            ..ids[0]
        },
        syncpdf_core::GlyphId {
            page: syncpdf_core::PageId(1),
            ..ids[0]
        },
    ] {
        let mut ps = PatchSet::new();
        let pristine = format!("{ps:?}");
        assert!(matches!(
            ps.delete_glyphs(&bound, &[ids[0], invalid]),
            Err(PatchError::UnknownGlyph(_))
        ));
        assert_eq!(format!("{ps:?}"), pristine, "no partial enqueue");
        ps.delete_glyphs(&bound, &ids[..1]).unwrap();
        let queued = format!("{ps:?}");
        assert!(ps.delete_glyphs(&bound, &[ids[1], invalid]).is_err());
        assert_eq!(format!("{ps:?}"), queued, "existing queue unchanged");
    }
}

// Identity regressions require PDFium; never skip missing runtime dependencies.
fn identity_forms(contents: &[&str], repeated: bool) -> (LDoc, Vec<ObjectId>, Vec<ObjectId>) {
    let mut doc = LDoc::with_version("1.7");
    let (page, first) = one_page(&mut doc, contents[0].as_bytes());
    let mut streams = vec![first];
    for body in &contents[1..] {
        streams.push(doc.add_object(Stream::new(LDict::new(), body.as_bytes().to_vec())));
    }
    doc.get_object_mut(page)
        .unwrap()
        .as_dict_mut()
        .unwrap()
        .set(
            "Contents",
            streams
                .iter()
                .copied()
                .map(LObj::Reference)
                .collect::<Vec<_>>(),
        );
    let a = form_object(&mut doc, "BT /F1 12 Tf 0 5 Td (F) Tj ET");
    let b = if repeated {
        a
    } else {
        form_object(&mut doc, "BT /F1 12 Tf 0 5 Td (G) Tj ET")
    };
    let font = doc.add_object(helvetica_dict());
    let mut fonts = LDict::new();
    fonts.set("F1", LObj::Reference(font));
    let mut x = LDict::new();
    x.set("Fa", LObj::Reference(a));
    x.set("Fb", LObj::Reference(b));
    let mut res = LDict::new();
    res.set("Font", fonts);
    res.set("XObject", x);
    set_page_resources(&mut doc, page, res);
    (doc, streams, vec![a, b])
}

fn assert_identity(
    bound: &BoundPage,
    objects: &[syncpdf_pdf::pdfium::TextObject],
    expected: &[(&str, ObjectId, u32)],
) {
    let items = text_items(bound);
    assert_eq!(items.len(), expected.len());
    for ((glyphs, object), &(text, stream, op)) in items.iter().zip(objects).zip(expected) {
        assert_eq!(glyphs.len(), 1);
        let g = &glyphs[0];
        assert_eq!(glyph_text(g), text);
        assert_eq!(
            g.id.op,
            syncpdf_core::OpKey::new(syncpdf_core::ObjRef::new(stream.0, stream.1), op)
        );
        assert_eq!(g.source.string_operand_range, (0, 1));
        assert_eq!(g.source.decoded_code_range, (0, 1));
        let c = object.chars.iter().find(|c| !c.is_generated).unwrap();
        assert_eq!(
            g.matrix,
            syncpdf_core::Matrix::translate(c.origin.x, c.origin.y)
        );
        assert_eq!(g.bbox, c.bbox);
        assert!(!g.bbox.is_empty());
    }
    assert_eq!(objects.len(), expected.len());
    bound.check_replacement().unwrap();
}

#[test]
fn identity_repeated_form_rejected_at_source_gate() {
    let (doc, _, _) = identity_forms(
        &["q 1 0 0 1 72 700 cm /Fa Do Q q 1 0 0 1 72 600 cm /Fa Do Q"],
        true,
    );
    let (_, _, _, bound) = bind(&write_pdf(doc, "identity-repeated.pdf"));
    assert_eq!(bound.stats.matched, 2);
    eprintln!(
        "repeated form: {:?}, {:?}; glyphs={}",
        bound.stats,
        bound.check_replacement(),
        bound.ir.glyphs().count()
    );
    assert!(matches!(
        bound.check_replacement(),
        Err(syncpdf_pdf::bind::ReplacementError::DuplicateSourceOperation(_))
    ));
    assert!(bound
        .issues
        .iter()
        .any(|issue| issue.starts_with("duplicate source operation:")));
    // The independent source-span guard still rejects even if diagnostics are cleared.
    let mut cleared = bound.clone();
    cleared.issues.clear();
    assert!(matches!(
        cleared.check_replacement(),
        Err(syncpdf_pdf::bind::ReplacementError::DuplicateSourceOperation(_))
    ));
}

#[test]
fn identity_interleaved_form_text_slots() {
    let (doc, streams, forms) = identity_forms(&["BT /F1 12 Tf 72 700 Td (P) Tj ET q 1 0 0 1 72 600 cm /Fa Do Q BT /F1 12 Tf 72 500 Td (Q) Tj ET"], false);
    let (w, id, _, bound) = bind(&write_pdf(doc, "identity-interleaved.pdf"));
    let shape: Vec<_> = bound
        .ir
        .items
        .iter()
        .filter_map(|item| match item {
            DisplayItem::Text { glyphs } => Some(glyphs.iter().map(glyph_text).collect::<String>()),
            DisplayItem::FormBegin { .. } => Some("begin".into()),
            DisplayItem::FormEnd => Some("end".into()),
            _ => None,
        })
        .collect();
    assert_eq!(shape, ["P", "begin", "F", "end", "Q"]);
    assert_identity(
        &bound,
        &w.page_text_objects(id, 0).unwrap(),
        &[
            ("P", streams[0], 3),
            ("F", forms[0], 3),
            ("Q", streams[0], 12),
        ],
    );
}

#[test]
fn identity_reverse_contents_order() {
    let (mut doc, streams, _) = identity_forms(
        &[
            "BT /F1 12 Tf 72 500 Td (Q) Tj ET",
            "BT /F1 12 Tf 72 700 Td (P) Tj ET",
        ],
        false,
    );
    let page = doc.get_pages()[&1];
    doc.get_object_mut(page)
        .unwrap()
        .as_dict_mut()
        .unwrap()
        .set(
            "Contents",
            vec![LObj::Reference(streams[1]), LObj::Reference(streams[0])],
        );
    let (w, id, _, bound) = bind(&write_pdf(doc, "identity-reverse.pdf"));
    assert_identity(
        &bound,
        &w.page_text_objects(id, 0).unwrap(),
        &[("P", streams[1], 3), ("Q", streams[0], 3)],
    );
}

#[test]
fn identity_form_numbering_across_contents() {
    let (doc, _, forms) = identity_forms(
        &[
            "0 0 10 10 re f q 1 0 0 1 72 700 cm /Fa Do Q",
            "q Q 0 0 10 10 re f q 1 0 0 1 72 600 cm /Fb Do Q",
        ],
        false,
    );
    let (w, id, _, bound) = bind(&write_pdf(doc, "identity-multi-contents.pdf"));
    let objects = w.page_text_objects(id, 0).unwrap();
    assert_eq!(
        objects
            .iter()
            .map(|o| o.form_path.clone())
            .collect::<Vec<_>>(),
        [vec![0], vec![1]]
    );
    assert_eq!(
        bound.stats.matched, 2,
        "{:?} {:?}",
        bound.stats, bound.issues
    );
    assert_identity(&bound, &objects, &[("F", forms[0], 3), ("G", forms[1], 3)]);
}
