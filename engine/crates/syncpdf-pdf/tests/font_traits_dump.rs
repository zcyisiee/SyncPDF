//! 真实样本的四维字体特征对照（人工检查用，默认忽略）。
//!
//! 用法：
//!
//! ```bash
//! FONT_TRAITS_PDF=<pdf> cargo test -p syncpdf-pdf --test font_traits_dump \
//!     -- --ignored --nocapture
//! ```
//!
//! 输出每个字体资源修复后的 `(serif, fixed, italic, bold)`，供 before/after 对照；
//! 不依赖翻译模型，只走 bind 解析路径。

use std::collections::BTreeMap;
use std::path::PathBuf;

#[test]
#[ignore = "需要真实 PDF 与 pdfium，仅人工对照用"]
fn dump_font_traits() {
    let Some(pdf) = std::env::var_os("FONT_TRAITS_PDF").map(PathBuf::from) else {
        eprintln!("SKIP: 未设置 FONT_TRAITS_PDF");
        return;
    };
    if syncpdf_core::fixtures::pdfium_lib_dir().is_none() {
        eprintln!("SKIP: pdfium 动态库缺失");
        return;
    }
    let worker = match syncpdf_pdf::pdfium::PdfiumWorker::spawn() {
        Ok(w) => w,
        Err(e) => {
            eprintln!("SKIP: pdfium worker 启动失败：{e}");
            return;
        }
    };
    let doc = worker.open(&pdf).expect("打开 PDF 失败");
    let pages = worker.page_count(doc).expect("读取页数失败");
    let lo = lopdf::Document::load(&pdf).expect("lopdf 载入失败");

    // (base_font, resource_name) → (serif, fixed, italic, bold, 首次出现页)。
    let mut seen: BTreeMap<String, (bool, bool, bool, bool, u32)> = BTreeMap::new();
    for p in 1..=pages {
        let bound = match syncpdf_pdf::bind::bind_page(&worker, doc, &lo, p) {
            Ok(b) => b,
            Err(e) => {
                eprintln!("# page {p} bind 失败：{e}");
                continue;
            }
        };
        for f in &bound.ir.fonts {
            seen.entry(format!("{}\t{}", f.base_font, f.resource_name))
                .or_insert_with(|| (f.is_serif, f.is_fixed_pitch, f.is_italic, f.is_bold, p));
        }
    }
    println!("base_font\tresource\tserif\tfixed\titalic\tbold\tfirst_page");
    for (k, v) in &seen {
        println!("{k}\t{}\t{}\t{}\t{}\t{}", v.0, v.1, v.2, v.3, v.4);
    }
}
