//! 合成斜体写回测试：剪切量进 `Tm` 的 c 分量，正体输出不带剪切；
//! pdfium 读回验证字形框向右倾斜。

use syncpdf_core::ir::{LineBox, PlacedGlyph, TypesetParagraph};
use syncpdf_core::{Color, ParagraphId, Rect, StyleId};
use syncpdf_font::loader::Script;
use syncpdf_font::{FontId, FontStore};
use syncpdf_pdf::pdfium::PdfiumWorker;
use syncpdf_pdf::writer::{save, Writer};

fn store() -> Option<FontStore> {
    let dir = syncpdf_core::fixtures::fonts_dir()?;
    FontStore::load_builtin(&dir).ok()
}

/// 正文衬线 CJK 面（zh-CN Text 区域的 italic 槽回落目标）。
fn cjk_serif_font(store: &FontStore) -> Option<FontId> {
    store.find(&syncpdf_font::FontQuery {
        serif: true,
        script: Script::HanSC,
        ..Default::default()
    })
}

fn doc_with_page() -> (lopdf::Document, lopdf::ObjectId) {
    let mut doc = lopdf::Document::with_version("1.7");
    let pages_id = doc.new_object_id();
    let mut page = lopdf::Dictionary::new();
    page.set("Type", lopdf::Object::Name(b"Page".to_vec()));
    page.set("Parent", lopdf::Object::Reference(pages_id));
    page.set("MediaBox", vec![0.into(), 0.into(), 595.into(), 842.into()]);
    page.set("Resources", lopdf::Dictionary::new());
    let page_id = doc.add_object(page);
    let mut pages = lopdf::Dictionary::new();
    pages.set("Type", lopdf::Object::Name(b"Pages".to_vec()));
    pages.set("Kids", vec![lopdf::Object::Reference(page_id)]);
    pages.set("Count", 1);
    doc.objects
        .insert(pages_id, lopdf::Object::Dictionary(pages));
    let cat = doc.add_object(lopdf::Dictionary::new());
    let cd = doc.get_object_mut(cat).unwrap().as_dict_mut().unwrap();
    cd.set("Type", lopdf::Object::Name(b"Catalog".to_vec()));
    cd.set("Pages", lopdf::Object::Reference(pages_id));
    doc.trailer.set("Root", lopdf::Object::Reference(cat));
    (doc, page_id)
}

fn para(glyphs: Vec<PlacedGlyph>) -> TypesetParagraph {
    TypesetParagraph {
        id: "P01-001".parse::<ParagraphId>().unwrap(),
        lines: vec![LineBox {
            bbox: Rect::new(0.0, 0.0, 400.0, 40.0),
            baseline_y: 700.0,
            glyphs,
            kept_atoms: Vec::new(),
            placed_atoms: Vec::new(),
            underlines: Vec::new(),
        }],
        font_scale: 1.0,
        line_height: 40.0,
        color: Color::BLACK,
        used_bbox: Rect::new(0.0, 0.0, 400.0, 40.0),
        overflow: false,
    }
}

/// 解压后的页内容流文本（译文流是最后追加的）。
fn content_text(doc: &lopdf::Document, page_id: lopdf::ObjectId) -> String {
    let page = doc.get_object(page_id).unwrap().as_dict().unwrap();
    let contents = page.get(b"Contents").unwrap();
    let ids: Vec<lopdf::ObjectId> = match contents {
        lopdf::Object::Array(a) => a.iter().filter_map(|o| o.as_reference().ok()).collect(),
        lopdf::Object::Reference(r) => vec![*r],
        _ => panic!("no contents"),
    };
    let mut out = String::new();
    for id in ids {
        let mut stream = doc.get_object(id).unwrap().as_stream().unwrap().clone();
        stream.decompress().unwrap();
        out.push_str(&String::from_utf8_lossy(&stream.content));
    }
    out
}

#[test]
fn writer_applies_shear_in_text_matrix_and_upright_stays_plain() {
    let Some(s) = store() else {
        eprintln!("SKIP: font package missing");
        return;
    };
    let Some(fid) = cjk_serif_font(&s) else {
        eprintln!("SKIP: no CJK serif face");
        return;
    };
    let f = s.get(fid).unwrap();
    let shaped = syncpdf_font::shape(f, "斜", 24.0, false, &[]);
    assert_eq!(shaped.len(), 1);
    let gid = shaped[0].gid;
    let shear = 10f32.to_radians().tan();
    let mk = |shear_x: f32, x: f32| PlacedGlyph {
        font: fid.0,
        gid,
        text: "斜".into(),
        x,
        y: 700.0,
        size: 24.0,
        scale_x: 1.0,
        shear_x,
        style: StyleId(1),
        color: None,
    };
    let upright = mk(0.0, 100.0);
    let slanted = mk(shear, 200.0);

    let (mut doc, page_id) = doc_with_page();
    let mut w = Writer::new(&s);
    w.write_paragraphs(&mut doc, 1, &[para(vec![upright, slanted])], 842.0)
        .unwrap();
    w.finalize(&mut doc).unwrap();

    // 内容流：正体 Tm 无剪切分量（字节级 "1 0 0 1"），斜体含 tan(10°)。
    let text = content_text(&doc, page_id);
    let upright_tm = "1 0 0 1 100 700 Tm".to_string();
    let slanted_tm = format!("1 0 {:.4} 1 200 700 Tm", shear)
        .trim_end_matches('0')
        .trim_end_matches('.')
        .to_string();
    assert!(
        text.contains(&upright_tm),
        "正体 Tm 不应引入剪切分量：{text}"
    );
    assert!(
        text.contains(&slanted_tm),
        "斜体 Tm 的 c 分量应为 tan(10°)={shear}：{text}"
    );

    // 保存后渲染读回，用实际墨迹验证剪切方向：斜体字形顶部墨迹相对底部
    // 右移（向右倾斜），正体顶部/底部队列对齐。
    if syncpdf_core::fixtures::pdfium_lib_dir().is_none() {
        eprintln!("SKIP: pdfium 动态库缺失");
        return;
    }
    let worker = match PdfiumWorker::spawn() {
        Ok(worker) => worker,
        Err(e) => {
            eprintln!("SKIP: pdfium worker 启动失败：{e}");
            return;
        }
    };
    let tmp = tempfile::tempdir().unwrap();
    let out = tmp.path().join("shear.pdf");
    save(&mut doc, &out).unwrap();
    let doc_id = worker.open(&out).expect("打开输出失败");
    let bmp = worker.render_page(doc_id, 0, 150.0).expect("render");
    let scale = 150.0f32 / 72.0;
    let px = |x_pt: f32| (x_pt * scale) as i32;
    let row = |y_pt: f32| ((842.0 - y_pt) * scale) as i32;
    // 字形窗口（两个互不重叠，含剪切外扩余量）与墨迹行范围。
    let upright_shift = ink_top_bottom_shift(&bmp, px(96.0), px(128.0), row(722.0), row(696.0));
    let slant_shift = ink_top_bottom_shift(&bmp, px(196.0), px(230.0), row(722.0), row(696.0));
    assert!(
        upright_shift.abs() < 2.0,
        "正体顶部/底部墨迹应垂直对齐：{upright_shift}"
    );
    assert!(
        slant_shift > 3.0 && slant_shift > upright_shift + 3.0,
        "剪切应使字形上部墨迹右移：upright={upright_shift} slant={slant_shift}"
    );
    worker.close(doc_id);
}

/// 窗口内顶部 1/4 墨迹行与底部 1/4 墨迹行的 x 均值差（像素）；
/// 正值 = 顶部相对底部右移（右倾斜），负值 = 左倾斜。
fn ink_top_bottom_shift(
    bmp: &syncpdf_pdf::pdfium::RgbaBitmap,
    x_lo: i32,
    x_hi: i32,
    row_lo: i32,
    row_hi: i32,
) -> f32 {
    let row_mean = |r: i32| -> Option<f32> {
        let mut sum = 0.0f32;
        let mut n = 0u32;
        for x in x_lo..x_hi {
            let idx = (r as usize * bmp.width as usize + x as usize) * 4;
            let [red, green, blue] = [bmp.data[idx], bmp.data[idx + 1], bmp.data[idx + 2]];
            if u32::from(red) + u32::from(green) + u32::from(blue) < 3 * 128 {
                sum += x as f32;
                n += 1;
            }
        }
        (n > 0).then(|| sum / n as f32)
    };
    let ink: Vec<f32> = (row_lo..row_hi).filter_map(row_mean).collect::<Vec<_>>();
    assert!(ink.len() >= 8, "墨迹行不足：{ink:?}");
    let band = ink.len() / 4;
    let top: f32 = ink[..band].iter().sum::<f32>() / band as f32;
    let bottom: f32 = ink[ink.len() - band..].iter().sum::<f32>() / band as f32;
    top - bottom
}
