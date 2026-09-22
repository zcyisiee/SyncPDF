//! 修复回归（R1-layout）：渲染位图像素坐标 → PDF 用户空间的映射契约。
//!
//! 实测锚点（pdfium，macOS arm64，engine/vendor/pdfium）：
//! - `render_page` 位图是 `/Rotate` 后的可视页面，覆盖 CropBox；
//! - `PageInfo::width/height` 是旋转后的 CropBox 尺寸；
//! - 字形框（`FPDFText_GetLooseCharBox`，bind 阶段同源）在未旋转、
//!   MediaBox 原点的用户空间。
//!
//! 因此 `px_to_user_space` 必须做：像素→可视 pt（等比 + y 翻转）→逆转旋转
//! →加 CropBox 原点。本文件用纯函数用例 + 真 pdfium 渲染的双重证据锁住该契约。

use syncpdf_core::Rect;
use syncpdf_layout::px_to_user_space;
use syncpdf_pdf::pdfium::{PageInfo, PdfiumWorker};

fn info(rot: i32, crop: Rect) -> PageInfo {
    let (w, h) = if rot % 180 == 0 {
        (crop.x1 - crop.x0, crop.y1 - crop.y0)
    } else {
        (crop.y1 - crop.y0, crop.x1 - crop.x0)
    };
    PageInfo {
        width: w,
        height: h,
        media_box: Rect::new(0.0, 0.0, 612.0, 792.0),
        crop_box: crop,
        rotation: rot,
    }
}

#[test]
fn full_bitmap_maps_to_crop_box_for_every_rotation() {
    // 整幅位图（0,0)-(W,H) 无论怎么旋转，都应恰好映射回 CropBox 本身。
    let crop = Rect::new(61.0, 79.0, 551.0, 713.0); // 原点非 0 的 CropBox
    for rot in [0, 90, 180, 270] {
        let i = info(rot, crop);
        // 模拟该旋转下 pdfium 的位图尺寸（宽高随旋转交换）。
        let (iw, ih) = (i.width as u32, i.height as u32);
        let r = px_to_user_space(Rect::new(0.0, 0.0, iw as f32, ih as f32), iw, ih, &i);
        assert!(
            (r.x0 - crop.x0).abs() < 1e-3
                && (r.y0 - crop.y0).abs() < 1e-3
                && (r.x1 - crop.x1).abs() < 1e-3
                && (r.y1 - crop.y1).abs() < 1e-3,
            "rot {rot}: {r:?} != {crop:?}"
        );
    }
}

#[test]
fn rot90_corner_matches_derivation() {
    // 未旋转页面 300x200，/Rotate 90 后可视页面 200x300。
    // 推导（CW 90°）：可视 (vx, vy) ← 用户 (cx1 - vy, cy0 + vx)。
    // 位图 200x300px（可视），kx=ky=1。用户空间框（例如 HELLO）：
    // x 50..115.6，y 145.8..168.1 → 可视 vx 145.8..168.1，
    // vy 184.4..250 → 位图 px（左上原点）x 145.8..168.1，y 50..115.6。
    let i = info(90, Rect::new(0.0, 0.0, 300.0, 200.0));
    let r = px_to_user_space(Rect::new(145.8, 50.0, 168.1, 115.6), 200, 300, &i);
    assert!((r.x0 - 50.0).abs() < 1e-3, "{r:?}");
    assert!((r.y0 - 145.8).abs() < 1e-3, "{r:?}");
    assert!((r.x1 - 115.6).abs() < 1e-3, "{r:?}");
    assert!((r.y1 - 168.1).abs() < 1e-3, "{r:?}");
}

#[test]
fn rot180_and_rot270_corners() {
    // 180：vx ← cx1 - vx；270：user_x = cx0 + vy，user_y = cy1 - vx。
    let crop = Rect::new(10.0, 20.0, 310.0, 220.0); // 300x200 未旋转
    let i = info(180, crop);
    // 位图 300x200px（180 不交换宽高）。px 框 (100,40)-(200,120) →
    // vx 100..200，vy 80..160 → user (310-200..310-100, 220-160..220-80)。
    let r = px_to_user_space(Rect::new(100.0, 40.0, 200.0, 120.0), 300, 200, &i);
    assert!(
        (r.x0 - 110.0).abs() < 1e-3 && (r.x1 - 210.0).abs() < 1e-3,
        "{r:?}"
    );
    assert!(
        (r.y0 - 60.0).abs() < 1e-3 && (r.y1 - 140.0).abs() < 1e-3,
        "{r:?}"
    );

    let i = info(270, crop);
    // 位图 200x300px。px 框 (30,100)-(80,250) → vx 30..80，vy 50..200 →
    // user_x = 10 + 50..10 + 200，user_y = 220 - 80..220 - 30。
    let r = px_to_user_space(Rect::new(30.0, 100.0, 80.0, 250.0), 200, 300, &i);
    assert!(
        (r.x0 - 60.0).abs() < 1e-3 && (r.x1 - 210.0).abs() < 1e-3,
        "{r:?}"
    );
    assert!(
        (r.y0 - 140.0).abs() < 1e-3 && (r.y1 - 190.0).abs() < 1e-3,
        "{r:?}"
    );
}

#[test]
fn crop_offset_is_added_in_user_space() {
    // rot=0、CropBox (61,79,551,713)（490x634pt），位图 490x634px（kx=ky=1）。
    // 可视 pt 框（39.8,13.0)-(436.7,627.8) 应平移到用户空间
    // (100.8,85.2)-(497.7,700.0)：与旧行为（原点当 0）恰好差 (61,79)。
    let i = info(0, Rect::new(61.0, 79.0, 551.0, 713.0));
    let r = px_to_user_space(Rect::new(39.8, 13.0, 436.7, 627.8), 490, 634, &i);
    assert!((r.x0 - 100.8).abs() < 1e-3, "{r:?}");
    assert!((r.y0 - 85.2).abs() < 1e-3, "{r:?}");
    assert!((r.x1 - 497.7).abs() < 1e-3, "{r:?}");
    assert!((r.y1 - 700.0).abs() < 1e-3, "{r:?}");
    assert!(r.x0 >= 61.0 && r.x1 <= 551.0, "x 落在 CropBox 内：{r:?}");
    assert!(r.y0 >= 79.0 && r.y1 <= 713.0, "y 落在 CropBox 内：{r:?}");
}

#[test]
fn degenerate_inputs_pass_through() {
    let i = info(0, Rect::new(0.0, 0.0, 100.0, 100.0));
    let b = Rect::new(1.0, 2.0, 3.0, 4.0);
    assert_eq!(px_to_user_space(b, 0, 10, &i), b, "空位图原样返回");
    assert_eq!(px_to_user_space(b, 10, 0, &i), b, "空位图原样返回");
}

// ---------------------------------------------------------------------------
// 真 pdfium 渲染：位图墨迹框经 px_to_user_space 后必须与 pdfium 字形框对齐
// ---------------------------------------------------------------------------

/// 极简 PDF 写出器：一页、Helvetica、可选 MediaBox/CropBox/Rotate 与内容流。
fn build_pdf(media: [f32; 4], crop: Option<[f32; 4]>, rotate: i32, content: &str) -> Vec<u8> {
    let mut objs: Vec<String> = Vec::new();
    objs.push("<< /Type /Catalog /Pages 2 0 R >>".into());
    objs.push("<< /Type /Pages /Kids [3 0 R] /Count 1 >>".into());
    let mut page = format!(
        "<< /Type /Page /Parent 2 0 R /MediaBox [{} {} {} {}] /Rotate {} \
         /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        media[0], media[1], media[2], media[3], rotate
    );
    if let Some(c) = crop {
        page = page.replace(
            "/Contents 5 0 R",
            &format!(
                "/CropBox [{} {} {} {}] /Contents 5 0 R",
                c[0], c[1], c[2], c[3]
            ),
        );
    }
    objs.push(page);
    objs.push("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>".into());
    objs.push(format!(
        "<< /Length {} >>\nstream\n{}\nendstream",
        content.len(),
        content
    ));
    let mut out = String::from("%PDF-1.4\n");
    let mut offsets = Vec::new();
    for (i, body) in objs.iter().enumerate() {
        offsets.push(out.len());
        out.push_str(&format!("{} 0 obj\n{}\nendobj\n", i + 1, body));
    }
    let xref = out.len();
    out.push_str(&format!(
        "xref\n0 {}\n0000000000 65535 f \n",
        objs.len() + 1
    ));
    for off in offsets {
        out.push_str(&format!("{off:010} 00000 n \n"));
    }
    out.push_str(&format!(
        "trailer << /Size {} /Root 1 0 R >>\nstartxref {}\n%%EOF\n",
        objs.len() + 1,
        xref
    ));
    out.into_bytes()
}

fn ink_bbox(b: &syncpdf_pdf::pdfium::RgbaBitmap) -> Rect {
    let (mut x0, mut y0, mut x1, mut y1) = (f32::MAX, f32::MAX, f32::MIN, f32::MIN);
    for y in 0..b.height {
        for x in 0..b.width {
            let i = ((y * b.width + x) * 4) as usize;
            let lum = b.data[i] as u32 + b.data[i + 1] as u32 + b.data[i + 2] as u32;
            if lum < 3 * 128 {
                x0 = x0.min(x as f32);
                y0 = y0.min(y as f32);
                x1 = x1.max(x as f32);
                y1 = y1.max(y as f32);
            }
        }
    }
    Rect::new(x0, y0, x1, y1)
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

fn probe_alignment(name: &str, pdf: &[u8], min_iou: f32) {
    let Ok(worker) = PdfiumWorker::spawn() else {
        eprintln!("SKIP: pdfium 不可用");
        return;
    };
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join(name);
    std::fs::write(&path, pdf).expect("write probe pdf");
    let doc = worker.open(&path).expect("open probe pdf");
    let info = worker.page_info(doc, 0).expect("page_info");
    let bm = worker.render_page(doc, 0, 150.0).expect("render");
    // pdfium 字形框并集（未旋转用户空间）。不能用作弊哨兵 Rect：Rect::new
    // 会把 x0/x1 参数交叉归一，哨兵直接爆成 ±MAX；用 Option 累加。
    let mut chars: Option<Rect> = None;
    let mut n = 0usize;
    for o in &worker.page_text_objects(doc, 0).expect("text objects") {
        for c in &o.chars {
            if c.unicode.as_deref().is_some_and(|u| !u.trim().is_empty()) {
                chars = Some(match chars {
                    Some(a) => a.union(&c.bbox),
                    None => c.bbox,
                });
                n += 1;
            }
        }
    }
    worker.close(doc);
    assert!(n > 0, "{name}: 应有可见字形");
    let chars = chars.expect("n>0 时必有并集");
    let ink = ink_bbox(&bm);
    let mapped = px_to_user_space(ink, bm.width, bm.height, &info);
    let v = iou(&mapped, &chars);
    eprintln!(
        "{name}: rot={} bitmap={}x{} ink_px=({:.0},{:.0})-({:.0},{:.0}) mapped=({:.1},{:.1})-({:.1},{:.1}) chars=({:.1},{:.1})-({:.1},{:.1}) iou={:.3}",
        info.rotation, bm.width, bm.height,
        ink.x0, ink.y0, ink.x1, ink.y1,
        mapped.x0, mapped.y0, mapped.x1, mapped.y1,
        chars.x0, chars.y0, chars.x1, chars.y1, v
    );
    assert!(
        chars.contains(mapped.center()),
        "{name}: 映射框中心 {:?} 应落在字形框内",
        mapped.center()
    );
    assert!(v >= min_iou, "{name}: IoU {v:.3} < {min_iou}");
}

#[test]
fn rot90_ink_maps_onto_glyph_boxes() {
    // 300x200 未旋转页，/Rotate 90；HELLO 在用户 (50,150)，XY 在 (250,20)。
    let pdf = build_pdf(
        [0.0, 0.0, 300.0, 200.0],
        None,
        90,
        "BT /F1 20 Tf 50 150 Td (HELLO) Tj ET\nBT /F1 20 Tf 250 20 Td (XY) Tj ET",
    );
    // 宽松字形框（含 ascent/descent）比墨迹高，IoU 阈值放宽。
    probe_alignment("rot90-probe.pdf", &pdf, 0.45);
}

#[test]
fn crop_offset_ink_maps_onto_glyph_boxes() {
    // MediaBox 612x792，CropBox (61,79,551,713)；文字在用户空间 (100,92)/(400,692)。
    let pdf = build_pdf(
        [0.0, 0.0, 612.0, 792.0],
        Some([61.0, 79.0, 551.0, 713.0]),
        0,
        "BT /F1 20 Tf 100 92 Td (CROPTOP) Tj ET\nBT /F1 20 Tf 400 692 Td (CROPBOT) Tj ET",
    );
    probe_alignment("cropoff-probe.pdf", &pdf, 0.45);
}

#[test]
fn unrotated_reference_alignment() {
    // 基准：rot=0、无 CropBox 偏移时同样对齐（排除「恰好在旋转页碰巧对」）。
    let pdf = build_pdf(
        [0.0, 0.0, 612.0, 792.0],
        None,
        0,
        "BT /F1 24 Tf 100 700 Td (PLAIN) Tj ET",
    );
    probe_alignment("plain-probe.pdf", &pdf, 0.5);
}
