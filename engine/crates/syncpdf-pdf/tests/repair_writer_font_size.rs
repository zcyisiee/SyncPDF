//! R1-writer 修复回归：字号随字号线性增长，`scale_x` 只影响横向。
//!
//! 背景：`writer.rs` 曾在 `scale_x == 1` 分支把字号同时乘进 `Tf` 与 `Tm`
//! （`Tf <size> … <size> 0 0 <size> … Tm`），而 PDF 视觉字号 = Tf × Tm 缩放，
//! 结果字号被平方（10pt 渲染成 100pt）。`scale_x != 1` 分支则把字号全部
//! 折进 Tm（`Tf 1`），两条分支语义不一致。
//!
//! 本文件用真实内置字体（Inter）+ pdfium 双重验证：
//!
//! - pdfium 文本对象几何：视觉 `font_size` ≈ 请求字号；字符框高度随
//!   10pt→20pt 线性翻倍（平方 bug 会 ×4）；`scale_x` 改变字符框宽度但不
//!   改变高度；基线原点不漂移；ToUnicode 正确（pdfium 读回 "H"）。
//! - 渲染像素（150dpi 位图墨迹外接框）：同样的线性关系。
//!
//! 证据（PDF、PPM 位图、measurements.txt）写入本 worktree 的
//! `tmp/backend-repair/evidence/`。缺字体包或缺 pdfium 动态库时打印原因
//! 并 skip（沿用仓库其它测试的约定）。

use std::path::{Path, PathBuf};

use lopdf::{Dictionary, Document, Object, ObjectId};
use syncpdf_core::ir::{LineBox, PlacedGlyph, TypesetParagraph};
use syncpdf_core::{Color, ParagraphId, Rect, StyleId};
use syncpdf_font::FontStore;
use syncpdf_pdf::pdfium::{PdfiumWorker, RgbaBitmap};
use syncpdf_pdf::writer::{save, Writer};

/// 证据输出目录：`<worktree>/tmp/backend-repair/evidence`（已被 .gitignore 忽略）。
fn evidence_dir() -> PathBuf {
    let d = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../tmp/backend-repair/evidence");
    let _ = std::fs::create_dir_all(&d);
    d
}

/// 一页空白 A4 文档（同 writer.rs 单元测试的构造方式）。
fn doc_with_page() -> (Document, ObjectId) {
    let mut doc = Document::with_version("1.7");
    let pages_id = doc.new_object_id();
    let mut page = Dictionary::new();
    page.set("Type", Object::Name(b"Page".to_vec()));
    page.set("Parent", Object::Reference(pages_id));
    page.set("MediaBox", vec![0.into(), 0.into(), 595.into(), 842.into()]);
    page.set("Resources", Dictionary::new());
    let page_id = doc.add_object(page);
    let mut pages = Dictionary::new();
    pages.set("Type", Object::Name(b"Pages".to_vec()));
    pages.set("Kids", vec![Object::Reference(page_id)]);
    pages.set("Count", 1);
    doc.objects.insert(pages_id, Object::Dictionary(pages));
    let cat = doc.add_object(Dictionary::new());
    let cd = doc.get_object_mut(cat).unwrap().as_dict_mut().unwrap();
    cd.set("Type", Object::Name(b"Catalog".to_vec()));
    cd.set("Pages", Object::Reference(pages_id));
    doc.trailer.set("Root", Object::Reference(cat));
    (doc, page_id)
}

/// 载入内置 Inter（真实字体，含真实度量与字形）。
fn inter_font() -> Option<(FontStore, u32)> {
    let dir = syncpdf_core::fixtures::fonts_dir()?;
    let store = FontStore::load_builtin(&dir).ok()?;
    let fid = store.find(&syncpdf_font::FontQuery {
        family: Some("Inter".into()),
        ..Default::default()
    })?;
    Some((store, fid.0))
}

/// 单字形段落：基线原点固定 (100, 700)。
fn glyph_para(font: u32, gid: u16, size: f32, scale_x: f32) -> TypesetParagraph {
    TypesetParagraph {
        id: "P01-001".parse::<ParagraphId>().unwrap(),
        lines: vec![LineBox {
            bbox: Rect::new(90.0, 680.0, 110.0, 710.0),
            baseline_y: 700.0,
            glyphs: vec![PlacedGlyph {
                font,
                gid,
                text: "H".to_string(),
                x: 100.0,
                y: 700.0,
                size,
                scale_x,
                style: StyleId(1),
                color: None,
            }],
            kept_atoms: Vec::new(),
            placed_atoms: Vec::new(),
        }],
        font_scale: 1.0,
        line_height: size * 1.2,
        color: Color::BLACK,
        used_bbox: Rect::new(90.0, 680.0, 110.0, 710.0),
        overflow: false,
    }
}

/// 生成一个单字形 PDF 到 `dir/<tag>.pdf`。
fn write_case(
    dir: &Path,
    tag: &str,
    store: &FontStore,
    font: u32,
    gid: u16,
    size: f32,
    scale_x: f32,
) -> PathBuf {
    let (mut doc, _) = doc_with_page();
    let mut w = Writer::new(store);
    w.write_paragraphs(&mut doc, 1, &[glyph_para(font, gid, size, scale_x)], 842.0)
        .expect("write_paragraphs");
    w.finalize(&mut doc).expect("finalize");
    let out = dir.join(format!("{tag}.pdf"));
    save(&mut doc, &out).expect("save");
    out
}

/// pdfium 侧的几何读数。
struct CaseGeom {
    /// 视觉字号（Tf × Tm 纵向缩放）。
    font_size: f32,
    /// Tf 原始字号。
    unscaled: f32,
    /// 首字符 loose bbox 宽（用户空间 pt）。
    w: f32,
    /// 首字符 loose bbox 高（用户空间 pt）。
    h: f32,
    /// 字符基线原点。
    origin: (f32, f32),
    /// ToUnicode 读回的文本。
    unicode: String,
    /// 填充色 RGB8。
    fill: [u8; 3],
}

/// 打开 PDF 并测量首个含字符的文本对象（页面空白，唯一的文本就是我们写的）。
fn measure(worker: &PdfiumWorker, path: &Path) -> CaseGeom {
    let doc = worker.open(path).expect("pdfium open");
    let objs = worker.page_text_objects(doc, 0).expect("page_text_objects");
    worker.close(doc);
    let obj = objs
        .iter()
        .find(|o| !o.chars.is_empty())
        .expect("应至少有一个含字符的文本对象");
    let ch = &obj.chars[0];
    CaseGeom {
        font_size: obj.font_size,
        unscaled: obj.unscaled_font_size,
        w: ch.bbox.width(),
        h: ch.bbox.height(),
        origin: (ch.origin.x, ch.origin.y),
        unicode: ch.unicode.clone().unwrap_or_default(),
        fill: obj.fill.to_rgb8(),
    }
}

/// 位图墨迹外接框（含端点）：只统计不透明且较暗的像素。
fn ink_bbox(b: &RgbaBitmap) -> Option<(u32, u32, u32, u32)> {
    let mut bb: Option<(u32, u32, u32, u32)> = None;
    for y in 0..b.height {
        for x in 0..b.width {
            let i = ((y * b.width + x) * 4) as usize;
            let (r, g, bl, a) = (b.data[i], b.data[i + 1], b.data[i + 2], b.data[i + 3]);
            if a <= 16 || (u32::from(r) + u32::from(g) + u32::from(bl)) / 3 >= 160 {
                continue;
            }
            bb = Some(match bb {
                Some((x0, y0, x1, y1)) => (x0.min(x), y0.min(y), x1.max(x), y1.max(y)),
                None => (x, y, x, y),
            });
        }
    }
    bb
}

/// 写 P6 PPM（便于人工查看渲染证据）。
fn write_ppm(path: &Path, b: &RgbaBitmap) {
    let mut out = format!("P6\n{} {}\n255\n", b.width, b.height).into_bytes();
    out.reserve((b.width as usize) * (b.height as usize) * 3);
    for px in b.data.chunks_exact(4) {
        out.extend_from_slice(&px[..3]);
    }
    let _ = std::fs::write(path, out);
}

#[test]
fn font_size_linear_and_scale_x_horizontal_only() {
    let Some((store, font)) = inter_font() else {
        eprintln!("SKIP: font package missing");
        return;
    };
    if syncpdf_core::fixtures::pdfium_lib_dir().is_none() {
        eprintln!("SKIP: pdfium 动态库缺失；设置 PDFIUM_DYNAMIC_LIB_PATH");
        return;
    }
    let worker = match PdfiumWorker::spawn() {
        Ok(w) => w,
        Err(e) => {
            eprintln!("SKIP: pdfium worker 启动失败：{e}");
            return;
        }
    };
    let Some(f) = store.get(syncpdf_font::FontId(font)) else {
        eprintln!("SKIP: Inter 不在 store");
        return;
    };
    let shaped = syncpdf_font::shape(f, "H", 12.0, false, &[]);
    let Some(gid) = shaped.first().map(|g| g.gid) else {
        eprintln!("SKIP: Inter 塑形为空");
        return;
    };

    // (tag, size, scale_x)：10pt/20pt × scale_x 1/0.8/1.2。
    let cases: &[(&str, f32, f32)] = &[
        ("s10", 10.0, 1.0),
        ("s20", 20.0, 1.0),
        ("x08", 10.0, 0.8),
        ("x12", 10.0, 1.2),
    ];
    let ev = evidence_dir();
    let mut report =
        String::from("case size scale_x font_size unscaled bbox_w bbox_h origin fill unicode\n");
    let mut geoms: Vec<CaseGeom> = Vec::new();
    for &(tag, size, sx) in cases {
        let p = write_case(&ev, tag, &store, font, gid, size, sx);
        let g = measure(&worker, &p);
        // 视觉字号 = 请求字号（旧 bug：scale_x=1 时为 size²）。
        assert!(
            (g.font_size - size).abs() < 0.05,
            "{tag}: 视觉字号 {} ≠ {size}（平方缩放未修复？）",
            g.font_size
        );
        assert!(
            (g.unscaled - size).abs() < 0.05,
            "{tag}: Tf 字号 {}",
            g.unscaled
        );
        // 基线原点不漂移。
        assert!(
            (g.origin.0 - 100.0).abs() < 0.05 && (g.origin.1 - 700.0).abs() < 0.05,
            "{tag}: 原点 {:?}",
            g.origin
        );
        // ToUnicode / 颜色不受影响。
        assert_eq!(g.unicode, "H", "{tag}: ToUnicode");
        assert_eq!(g.fill, [0, 0, 0], "{tag}: 填充色 {:?}", g.fill);
        report.push_str(&format!(
            "{tag} {size} {sx} {} {} {} {} {:?} {:?} {}\n",
            g.font_size, g.unscaled, g.w, g.h, g.origin, g.fill, g.unicode
        ));
        geoms.push(g);
    }
    let [s10, s20, x08, x12] = [&geoms[0], &geoms[1], &geoms[2], &geoms[3]];

    // 字号线性：10→20 高度/宽度翻倍（平方 bug 会 ×4）。
    let hr = s20.h / s10.h;
    let wr = s20.w / s10.w;
    assert!((hr - 2.0).abs() < 0.02, "字符框高度比 20/10 = {hr}，应≈2");
    assert!((wr - 2.0).abs() < 0.02, "字符框宽度比 20/10 = {wr}，应≈2");
    // scale_x 只影响横向。
    assert!(
        (x08.h - s10.h).abs() < 0.1,
        "scale_x=0.8 高度 {} vs {}",
        x08.h,
        s10.h
    );
    assert!(
        (x12.h - s10.h).abs() < 0.1,
        "scale_x=1.2 高度 {} vs {}",
        x12.h,
        s10.h
    );
    assert!(
        (x08.w / s10.w - 0.8).abs() < 0.02,
        "scale_x=0.8 宽度比 {}",
        x08.w / s10.w
    );
    assert!(
        (x12.w / s10.w - 1.2).abs() < 0.02,
        "scale_x=1.2 宽度比 {}",
        x12.w / s10.w
    );

    // 渲染像素验证（150dpi，页面除我们的字形外空白）。
    let dpi = 150.0;
    let mut inks: Vec<(u32, u32, u32, u32)> = Vec::new();
    for &(tag, _, _) in cases {
        let p = ev.join(format!("{tag}.pdf"));
        let doc = worker.open(&p).expect("pdfium open");
        let bmp = worker.render_page(doc, 0, dpi).expect("render_page");
        worker.close(doc);
        let bb = ink_bbox(&bmp).unwrap_or_else(|| panic!("{tag}: 渲染位图无墨迹"));
        write_ppm(&ev.join(format!("{tag}.ppm")), &bmp);
        inks.push(bb);
        report.push_str(&format!(
            "{tag} ink_px x0={} y0={} x1={} y1={} w={} h={}\n",
            bb.0,
            bb.1,
            bb.2,
            bb.3,
            bb.2 - bb.0 + 1,
            bb.3 - bb.1 + 1
        ));
    }
    let ih = |bb: (u32, u32, u32, u32)| (bb.3 - bb.1 + 1) as f32;
    let iw = |bb: (u32, u32, u32, u32)| (bb.2 - bb.0 + 1) as f32;
    // 墨迹高度：20pt ≈ 2×10pt（平方 bug 会 ≈4×）。
    assert!(
        (ih(inks[1]) / ih(inks[0]) - 2.0).abs() < 0.08,
        "墨迹高度比 {}",
        ih(inks[1]) / ih(inks[0])
    );
    // scale_x 不改变墨迹高度（横向缩放只影响横向）。
    assert!(
        (ih(inks[2]) - ih(inks[0])).abs() <= 2.0,
        "墨迹高度 scale_x=0.8 {} vs 1.0 {}",
        ih(inks[2]),
        ih(inks[0])
    );
    assert!(
        (ih(inks[3]) - ih(inks[0])).abs() <= 2.0,
        "墨迹高度 scale_x=1.2 {} vs 1.0 {}",
        ih(inks[3]),
        ih(inks[0])
    );
    // 墨迹宽度随 scale_x 线性（栅格化允许 ±1px 级误差）。
    assert!(
        (iw(inks[2]) / iw(inks[0]) - 0.8).abs() < 0.1,
        "墨迹宽度比 scale_x=0.8 {}",
        iw(inks[2]) / iw(inks[0])
    );
    assert!(
        (iw(inks[3]) / iw(inks[0]) - 1.2).abs() < 0.1,
        "墨迹宽度比 scale_x=1.2 {}",
        iw(inks[3]) / iw(inks[0])
    );

    std::fs::write(ev.join("measurements.txt"), &report).expect("写 measurements.txt");
    eprintln!("\n{report}");
}

#[test]
fn mixed_run_exact_sizes_and_colors_are_visible_in_pdfium() {
    let (store, font) = inter_font().expect("real builtin font required");
    let worker = PdfiumWorker::spawn().expect("real PDFium required");
    let loaded = store.get(syncpdf_font::FontId(font)).unwrap();
    let gid = syncpdf_font::shape(loaded, "H", 12.0, false, &[])[0].gid;
    let mut p = glyph_para(font, gid, 9.963, 1.0);
    let first = &mut p.lines[0].glyphs[0];
    first.color = Some(Color::rgb(0.2, 0.3, 0.4));
    let mut second = first.clone();
    second.x += 30.0;
    second.size = 13.125;
    second.color = Some(Color::rgb(0.7, 0.1, 0.2));
    p.lines[0].glyphs.push(second);
    let expected = p.lines[0].glyphs.clone();
    let (mut doc, _) = doc_with_page();
    let mut writer = Writer::new(&store);
    writer.write_paragraphs(&mut doc, 1, &[p], 842.0).unwrap();
    writer.finalize(&mut doc).unwrap();
    let path = evidence_dir().join("mixed-run-style.pdf");
    save(&mut doc, &path).unwrap();
    let handle = worker.open(&path).unwrap();
    let objects = worker.page_text_objects(handle, 0).unwrap();
    worker.close(handle);
    assert_eq!(objects.len(), 2);
    for (object, glyph) in objects.iter().zip(&expected) {
        assert!((object.font_size - glyph.size).abs() < 1e-3);
        assert_eq!(object.fill.to_rgb8(), glyph.color.unwrap().to_rgb8());
        // ActualText spans can attach all logical chars to the first object;
        // object matrices still independently identify both paint origins.
        let origin = object.object_bounds.as_ref().unwrap().origin;
        assert!((origin.x - glyph.x).abs() < 1e-3);
        assert!((origin.y - glyph.y).abs() < 1e-3);
    }
}
