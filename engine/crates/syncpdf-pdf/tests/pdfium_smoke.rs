//! pdfium 封装的冒烟测试。
//!
//! 缺 pdfium 动态库或缺夹具时打印原因并 skip（不失败），这样 CI 无 vendor 也能跑。

use std::path::PathBuf;

use syncpdf_core::Rect;
use syncpdf_pdf::pdfium::{DocId, PdfiumWorker};

/// 取得 worker；缺库时返回 `None` 并打印 skip 原因。
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

fn fixture(name: &str) -> Option<PathBuf> {
    match syncpdf_core::fixtures::path(name) {
        Some(p) => Some(p),
        None => {
            eprintln!("SKIP: 夹具 {name} 缺失；运行 `cargo xtask fixtures`");
            None
        }
    }
}

/// 打开夹具并打印每页统计；返回 (worker, doc, 页数)。
fn open(name: &str) -> Option<(PdfiumWorker, DocId, u32)> {
    let w = worker()?;
    let path = fixture(name)?;
    let doc = w.open(&path).expect("打开夹具失败");
    let count = w.page_count(doc).expect("读取页数失败");
    Some((w, doc, count))
}

/// bbox 是否落在页框内（留 1pt 容差，loose char box 会贴着字体上下沿）。
fn inside(bbox: Rect, page: Rect) -> bool {
    let p = page.inflate(1.0);
    bbox.x0 >= p.x0 && bbox.y0 >= p.y0 && bbox.x1 <= p.x1 && bbox.y1 <= p.y1
}

#[test]
fn ci_test_opens_and_reports() {
    let Some((w, doc, count)) = open("ci-test.pdf") else {
        return;
    };
    assert_eq!(count, 1, "ci-test.pdf 应为 1 页");
    let info = w.page_info(doc, 0).expect("page_info 失败");
    assert!(info.width > 0.0 && info.height > 0.0, "{info:?}");
    assert_eq!(info.rotation, 0);
    assert!(!info.media_box.is_empty(), "{info:?}");
    let objects = w.page_text_objects(doc, 0).expect("page_text_objects 失败");
    let chars: usize = objects.iter().map(|o| o.chars.len()).sum();
    println!(
        "ci-test.pdf p0: {} 页, 文本对象 {}, 字符 {}",
        count,
        objects.len(),
        chars
    );
    assert!(!w.has_signatures(doc).expect("has_signatures 失败"));
    assert!(!w.is_encrypted(doc).expect("is_encrypted 失败"));
    w.close(doc);
    // 关闭后句柄失效。
    assert!(w.page_count(doc).is_err());
}

#[test]
fn up_vns_has_twelve_pages_with_text() {
    let Some((w, doc, count)) = open("up-vns.pdf") else {
        return;
    };
    assert_eq!(count, 12, "up-vns.pdf 应为 12 页");
    assert!(!w.has_signatures(doc).expect("has_signatures 失败"));
    assert!(!w.is_encrypted(doc).expect("is_encrypted 失败"));

    let mut total_objects = 0usize;
    let mut total_chars = 0usize;
    let mut out_of_page = 0usize;
    let mut in_forms = 0usize;
    for page in 0..count {
        let info = w.page_info(doc, page).expect("page_info 失败");
        let objects = w
            .page_text_objects(doc, page)
            .expect("page_text_objects 失败");
        let chars: usize = objects.iter().map(|o| o.chars.len()).sum();
        assert!(chars > 0, "第 {page} 页字符数为 0");
        // 内容流顺序：index 必须是 0..n 的连续序列。
        for (i, o) in objects.iter().enumerate() {
            assert_eq!(o.index as usize, i, "文本对象顺序不连续");
            if !o.form_path.is_empty() {
                in_forms += 1;
            }
            assert!(o.font_size >= 0.0, "字号异常：{o:?}");
        }
        for o in &objects {
            for c in &o.chars {
                // 生成字符（pdfium 补的空格/换行）可能落在页面边缘外，不计入。
                if !c.is_generated && !inside(c.bbox, info.media_box) {
                    out_of_page += 1;
                }
                assert!(c.width >= 0.0);
            }
        }
        println!(
            "up-vns.pdf p{page}: {}x{} 文本对象 {} 字符 {}",
            info.width,
            info.height,
            objects.len(),
            chars
        );
        total_objects += objects.len();
        total_chars += chars;
    }
    println!(
        "up-vns.pdf 合计：文本对象 {total_objects}，字符 {total_chars}，Form 内对象 {in_forms}，越界 bbox {out_of_page}"
    );
    assert!(
        out_of_page * 100 <= total_chars,
        "超过 1% 的字符 bbox 落在页框外：{out_of_page}/{total_chars}"
    );
    w.close(doc);
}

#[test]
fn render_at_72dpi_matches_page_points() {
    let Some((w, doc, _)) = open("up-vns.pdf") else {
        return;
    };
    let info = w.page_info(doc, 0).expect("page_info 失败");
    let bitmap = w.render_page(doc, 0, 72.0).expect("render_page 失败");
    let expected_w = info.width.round() as u32;
    assert_eq!(bitmap.width, expected_w, "72dpi 下像素宽应等于页宽 pt");
    let expected_h = info.height.round() as u32;
    assert!(
        bitmap.height.abs_diff(expected_h) <= 1,
        "72dpi 下像素高应约等于页高 pt：{} vs {expected_h}",
        bitmap.height
    );
    assert_eq!(
        bitmap.data.len(),
        (bitmap.width * bitmap.height * 4) as usize,
        "RGBA 缓冲长度不符"
    );
    // 144dpi 应当正好翻倍。
    let double = w.render_page(doc, 0, 144.0).expect("render_page 失败");
    assert!(
        double.width.abs_diff(bitmap.width * 2) <= 1,
        "{} vs {}",
        double.width,
        bitmap.width * 2
    );
    println!(
        "render 72dpi -> {}x{}，144dpi -> {}x{}",
        bitmap.width, bitmap.height, double.width, double.height
    );
    w.close(doc);
}

#[test]
fn four_threads_call_concurrently() {
    let Some((w, doc, count)) = open("up-vns.pdf") else {
        return;
    };
    let handles: Vec<_> = (0..4u32)
        .map(|t| {
            let w = w.clone();
            std::thread::spawn(move || {
                for round in 0..8u32 {
                    let page = (t + round) % count;
                    let info = w.page_info(doc, page).expect("page_info 失败");
                    assert!(info.width > 0.0);
                    let objects = w
                        .page_text_objects(doc, page)
                        .expect("page_text_objects 失败");
                    assert!(!objects.is_empty(), "第 {page} 页无文本对象");
                    let n = w.page_count(doc).expect("page_count 失败");
                    assert_eq!(n, count);
                    // 逃生舱 API 也要能并发走通。
                    let ok = w.call(|_pdfium| true).expect("call 失败");
                    assert!(ok);
                }
                t
            })
        })
        .collect();
    for h in handles {
        assert!(h.join().is_ok(), "并发线程 panic");
    }
    println!("4 线程并发 call 通过");
    w.close(doc);
}
