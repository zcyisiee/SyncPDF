//! 修复回归（R1-layout）：PP-DocLayoutV3 推理契约（im_shape / scale_factor /
//! 通道序）与真实文档上的语义落点。
//!
//! 根因（见 src/session.rs 模块注释）：该导出图的输出坐标空间由
//! `im_shape × /scale_factor` 组合决定；只有官方组合（im_shape=网络输入
//! (800,800)，scale_factor=网络输入/原图）才把框换算回**原图像素**。
//! 传原图尺寸会把框额外放大 `原图/800` 倍，再被夹回图内，落到错误位置——
//! 目标论文 2106.04690v2 全部 23 页 coverage_gap 的直接原因。

use syncpdf_core::Rect;
use syncpdf_layout::{DetectOpts, LayoutModel, RawImage};
use syncpdf_pdf::pdfium::PdfiumWorker;

const TARGET: &str = "/Users/zhengcaiyi/Downloads/2106.04690v2.pdf";
const DPI: f32 = 150.0;

fn model() -> Option<LayoutModel> {
    let dir = syncpdf_core::fixtures::models_dir()?;
    let path = dir.join("pp_doc_layoutv3.onnx");
    if !path.is_file() {
        eprintln!("SKIP: pp_doc_layoutv3.onnx 缺失（{}）", path.display());
        return None;
    }
    match LayoutModel::load(&path, 2) {
        Ok(m) => Some(m),
        Err(e) => {
            eprintln!("SKIP: 模型加载失败：{e}");
            None
        }
    }
}

/// 白底黑块合成页：`blocks` 是像素框（左上原点）。
fn synthetic_page(w: u32, h: u32, blocks: &[Rect]) -> Vec<u8> {
    let mut rgba = vec![255u8; (w * h * 4) as usize];
    for b in blocks {
        for y in b.y0.max(0.0) as u32..b.y1.min(h as f32) as u32 {
            for x in b.x0.max(0.0) as u32..b.x1.min(w as f32) as u32 {
                let i = ((y * w + x) * 4) as usize;
                rgba[i] = 0;
                rgba[i + 1] = 0;
                rgba[i + 2] = 0;
                rgba[i + 3] = 255;
            }
        }
    }
    rgba
}

fn iou(a: &Rect, b: &Rect) -> f32 {
    let ix0 = a.x0.max(b.x0);
    let iy0 = a.y0.max(b.y0);
    let ix1 = a.x1.min(b.x1);
    let iy1 = a.y1.min(b.y1);
    let inter = (ix1 - ix0).max(0.0) * (iy1 - iy0).max(0.0);
    let uni = a.width() * a.height() + b.width() * b.height() - inter;
    if uni <= 0.0 {
        0.0
    } else {
        inter / uni
    }
}

/// 非正方形/多尺度回归：模型输出必须落在**原图像素**坐标里。
///
/// 各尺寸分别对应：放大（>800）、缩小（<800）、极端纵横比。旧 bug
/// （im_shape 传原图）会把框放大约 `max(边)/800` 倍并被夹到图边
/// （1275x1650 的底部块会整框夹没，400x300 的 IoU 掉到 0.23），
/// 在这些用例上无法以 IoU≥0.35 命中黑色块。
///
/// 合成纯黑块在模型眼里是低置信 image（实测 0.3–0.9），阈值放到 0.25；
/// 这里考察的是坐标空间，不是检出质量。
#[test]
fn boxes_are_in_original_pixel_space_across_scales() {
    let Some(mut m) = model() else {
        return;
    };
    let cases: [(u32, u32, Vec<Rect>); 3] = [
        // 1275x1650（150dpi letter，目标论文的实际渲染尺寸）：双栏 + 页底大块。
        (
            1275,
            1650,
            vec![
                Rect::new(250.0, 300.0, 600.0, 700.0),
                Rect::new(650.0, 300.0, 1000.0, 700.0),
                Rect::new(250.0, 900.0, 1000.0, 1400.0),
            ],
        ),
        // 400x300（< 800，输入被放大）。
        (
            400,
            300,
            vec![
                Rect::new(40.0, 40.0, 180.0, 260.0),
                Rect::new(220.0, 40.0, 360.0, 260.0),
            ],
        ),
        // 2000x700（极端宽扁）。
        (
            2000,
            700,
            vec![
                Rect::new(150.0, 100.0, 950.0, 600.0),
                Rect::new(1050.0, 100.0, 1850.0, 600.0),
            ],
        ),
    ];
    let opts = DetectOpts {
        score_threshold: 0.25,
        ..DetectOpts::default()
    };
    for (w, h, blocks) in cases {
        let rgba = synthetic_page(w, h, &blocks);
        let img = RawImage {
            width: w,
            height: h,
            rgba: &rgba,
        };
        let dets = m.detect(&img, &opts).expect("detect");
        assert!(!dets.is_empty(), "{w}x{h}: 应检出区域");
        for (bi, b) in blocks.iter().enumerate() {
            let best = dets
                .iter()
                .map(|d| iou(&d.bbox_px, b))
                .fold(0.0f32, f32::max);
            assert!(
                best >= 0.35,
                "{w}x{h} 块 {bi} {b:?} 最佳 IoU {best:.3} < 0.35（输出不在原图像素空间）"
            );
        }
        for d in &dets {
            assert!(
                d.bbox_px.x0 >= -1.0
                    && d.bbox_px.y0 >= -1.0
                    && d.bbox_px.x1 <= w as f32 + 1.0
                    && d.bbox_px.y1 <= h as f32 + 1.0,
                "{w}x{h}: 框越界 {d:?}"
            );
        }
    }
}

/// 阈值与标签契约：低分框过滤、标签落在 0..24、阅读顺序值有效。
#[test]
fn score_and_label_contract_on_synthetic_page() {
    let Some(mut m) = model() else {
        return;
    };
    let blocks = [
        Rect::new(200.0, 150.0, 1100.0, 220.0),
        Rect::new(200.0, 300.0, 1100.0, 900.0),
    ];
    let rgba = synthetic_page(1300, 1000, &blocks);
    let img = RawImage {
        width: 1300,
        height: 1000,
        rgba: &rgba,
    };
    let opts = DetectOpts::default();
    let dets = m.detect(&img, &opts).expect("detect");
    assert!(!dets.is_empty());
    for d in &dets {
        assert!(d.score >= opts.score_threshold, "{d:?}");
        assert!(d.raw_label < 25, "{d:?}");
        if let Some(o) = d.order {
            assert!(o < 100_000, "阅读顺序值异常：{d:?}");
        }
    }
}

/// 目标论文语义回归：首页标题框/摘要框必须落在**对应原文**上，覆盖率过门禁。
///
/// 用 pdfium 字形框（bind 同源）做地面真值；模型缺失或论文缺失时打印原因
/// 并跳过（本仓库不携带该论文）。
#[test]
fn target_paper_page1_title_abstract_lands_on_text() {
    let Some(mut m) = model() else {
        return;
    };
    if !std::path::Path::new(TARGET).is_file() {
        eprintln!("SKIP: 目标论文缺失 {TARGET}");
        return;
    }
    let Ok(worker) = PdfiumWorker::spawn() else {
        eprintln!("SKIP: pdfium 不可用");
        return;
    };
    let doc = worker.open(std::path::Path::new(TARGET)).expect("open");
    let info = worker.page_info(doc, 0).expect("page_info");
    assert_eq!(info.rotation, 0, "该论文首页应无旋转");
    let bm = worker.render_page(doc, 0, DPI).expect("render");
    let img = RawImage {
        width: bm.width,
        height: bm.height,
        rgba: &bm.data,
    };
    let dets = m.detect(&img, &DetectOpts::default()).expect("detect");
    // px → 用户空间（本页 rot=0、CropBox 原点 0）。
    let kx = info.width / bm.width as f32;
    let ky = info.height / bm.height as f32;
    let to_pt = |b: Rect| {
        Rect::new(
            b.x0 * kx,
            info.height - b.y1 * ky,
            b.x1 * kx,
            info.height - b.y0 * ky,
        )
    };
    let boxes: Vec<Rect> = dets.iter().map(|d| to_pt(d.bbox_px)).collect();

    // pdfium 字形框（未旋转用户空间）。
    let chars: Vec<(Rect, String)> = worker
        .page_text_objects(doc, 0)
        .expect("text objects")
        .iter()
        .flat_map(|o| o.chars.iter())
        .filter(|c| c.bbox.width() > 0.0 && c.bbox.height() > 0.0)
        .map(|c| (c.bbox, c.unicode.clone().unwrap_or_default()))
        .collect();
    worker.close(doc);
    assert!(chars.len() > 100, "首页应有大量字形，got {}", chars.len());

    // 1) 标题：doc_title（label 6）框应覆盖页面顶部 15% 内的字形。
    let title_zone: Vec<usize> = chars
        .iter()
        .enumerate()
        .filter(|(_, (b, _))| b.center().y > info.height * 0.85)
        .map(|(i, _)| i)
        .collect();
    assert!(
        title_zone.len() >= 10,
        "页顶 15% 应有标题字形，got {}",
        title_zone.len()
    );
    let title_boxes: Vec<(usize, Rect)> = dets
        .iter()
        .enumerate()
        .filter(|(_, d)| d.raw_label == 6)
        .map(|(i, _)| (i, boxes[i]))
        .collect();
    assert!(
        !title_boxes.is_empty(),
        "首页应检出 doc_title（label 6），got {:?}",
        dets.iter().map(|d| d.raw_label).collect::<Vec<_>>()
    );
    let mut covered_by_title = 0usize;
    for &ti in &title_zone {
        let (b, _) = &chars[ti];
        if title_boxes.iter().any(|(_, tb)| tb.contains(b.center())) {
            covered_by_title += 1;
        }
    }
    assert!(
        covered_by_title * 100 >= title_zone.len() * 80,
        "doc_title 框应覆盖页顶字形 ≥80%：{covered_by_title}/{}",
        title_zone.len()
    );
    let title_text: String = title_zone.iter().map(|&i| chars[i].1.as_str()).collect();
    let alpha = title_text.chars().filter(|c| c.is_alphabetic()).count();
    assert!(alpha >= 10, "标题文本应为标题词：{title_text:?}");

    // 2) 摘要：abstract（label 0）框内应有大段正文字形。
    let abs_boxes: Vec<Rect> = dets
        .iter()
        .enumerate()
        .filter(|(_, d)| d.raw_label == 0)
        .map(|(i, _)| boxes[i])
        .collect();
    assert!(!abs_boxes.is_empty(), "首页应检出 abstract（label 0）");
    let abs_chars = chars
        .iter()
        .filter(|(b, _)| abs_boxes.iter().any(|t| t.contains(b.center())))
        .count();
    assert!(
        abs_chars >= 100,
        "摘要框内字形应 ≥100（实际正文中段），got {abs_chars}"
    );

    // 3) 覆盖率门禁（0.5%）：模型框对字形中心的覆盖。
    let covered = chars
        .iter()
        .filter(|(b, _)| boxes.iter().any(|t| t.contains(b.center())))
        .count();
    let ratio = 1.0 - covered as f32 / chars.len() as f32;
    assert!(
        ratio <= 0.005,
        "首页未覆盖字形比例 {ratio:.4} 应过 0.5% 门禁"
    );
    eprintln!(
        "target p1: dets={} title_text={:?} abs_chars={abs_chars} uncovered={ratio:.4}",
        dets.len(),
        title_text.trim()
    );
}

/// 目标论文另一页（第 5 页）：正文框对齐 + 覆盖率过门禁。
#[test]
fn target_paper_page5_coverage_passes_gate() {
    let Some(mut m) = model() else {
        return;
    };
    if !std::path::Path::new(TARGET).is_file() {
        eprintln!("SKIP: 目标论文缺失 {TARGET}");
        return;
    }
    let Ok(worker) = PdfiumWorker::spawn() else {
        eprintln!("SKIP: pdfium 不可用");
        return;
    };
    let doc = worker.open(std::path::Path::new(TARGET)).expect("open");
    let info = worker.page_info(doc, 4).expect("page_info");
    let bm = worker.render_page(doc, 4, DPI).expect("render");
    let img = RawImage {
        width: bm.width,
        height: bm.height,
        rgba: &bm.data,
    };
    let dets = m.detect(&img, &DetectOpts::default()).expect("detect");
    let kx = info.width / bm.width as f32;
    let ky = info.height / bm.height as f32;
    let boxes: Vec<Rect> = dets
        .iter()
        .map(|d| {
            Rect::new(
                d.bbox_px.x0 * kx,
                info.height - d.bbox_px.y1 * ky,
                d.bbox_px.x1 * kx,
                info.height - d.bbox_px.y0 * ky,
            )
        })
        .collect();
    let chars: Vec<Rect> = worker
        .page_text_objects(doc, 4)
        .expect("text objects")
        .iter()
        .flat_map(|o| o.chars.iter().map(|c| c.bbox))
        .filter(|b| b.width() > 0.0 && b.height() > 0.0)
        .collect();
    worker.close(doc);
    assert!(chars.len() > 100);
    assert!(
        dets.iter().filter(|d| d.raw_label == 22).count() >= 3,
        "第 5 页应检出多个正文 text 框"
    );
    let covered = chars
        .iter()
        .filter(|b| boxes.iter().any(|t| t.contains(b.center())))
        .count();
    let ratio = 1.0 - covered as f32 / chars.len() as f32;
    assert!(
        ratio <= 0.005,
        "第 5 页未覆盖字形比例 {ratio:.4} 应过 0.5% 门禁"
    );
    eprintln!(
        "target p5: dets={} chars={} uncovered={ratio:.4}",
        dets.len(),
        chars.len()
    );
}
