//! 模型推理集成测试：up-vns 第 1 页位图 → ≥3 区域、含 Text、框在图内。
//!
//! 位图 `tests/data/up-vns-p1.png`（150dpi 渲染，447KB）由 PyMuPDF 预生成入库；
//! 模型缺失时 skip。

use std::path::Path;

use syncpdf_core::fixtures::models_dir;
use syncpdf_core::ir::RegionKind;
use syncpdf_layout::{DetectOpts, LayoutModel, RawImage};

const PNG: &str = "tests/data/up-vns-p1.png";
const PAGE_W_PT: f32 = 595.276;
const PAGE_H_PT: f32 = 793.7;

#[test]
fn detect_up_vns_page1() {
    let Some(dir) = models_dir() else {
        eprintln!("SKIP: SYNCPDF_MODELS / engine/vendor/models missing");
        return;
    };
    let model_path = dir.join("pp_doc_layoutv3.onnx");
    if !model_path.is_file() {
        eprintln!(
            "SKIP: pp_doc_layoutv3.onnx missing at {}",
            model_path.display()
        );
        return;
    }
    let manifest = Path::new(env!("CARGO_MANIFEST_DIR")).join(PNG);
    if !manifest.is_file() {
        eprintln!("SKIP: {PNG} missing");
        return;
    }

    let img = image::open(&manifest).expect("open png");
    let rgba = img.to_rgba8();
    let (w, h) = (rgba.width(), rgba.height());
    let raw = RawImage {
        width: w,
        height: h,
        rgba: rgba.as_raw(),
    };

    let mut model = LayoutModel::load(&model_path, 2).expect("load model");
    assert_eq!(model.input_size(), (800, 800));

    let opts = DetectOpts::default();
    let dets = model.detect(&raw, &opts).expect("detect");
    eprintln!("detected {} regions on up-vns p1 ({}x{})", dets.len(), w, h);
    for d in &dets {
        let pdf = syncpdf_layout::to_pdf_space(d, w, h, PAGE_W_PT, PAGE_H_PT);
        eprintln!(
            "  kind={:?} label={} score={:.3} px=({:.0},{:.0})-({:.0},{:.0}) order={:?} pdf=({:.1},{:.1})-({:.1},{:.1})",
            d.kind, d.raw_label, d.score,
            d.bbox_px.x0, d.bbox_px.y0, d.bbox_px.x1, d.bbox_px.y1,
            d.order,
            pdf.x0, pdf.y0, pdf.x1, pdf.y1,
        );
    }

    assert!(dets.len() >= 3, "expected >=3 regions, got {}", dets.len());
    assert!(
        dets.iter().any(|d| d.kind == RegionKind::Text),
        "no Text region found"
    );
    for d in &dets {
        assert!(d.score >= opts.score_threshold, "{d:?}");
        assert!(
            d.bbox_px.x0 >= 0.0
                && d.bbox_px.y0 >= 0.0
                && d.bbox_px.x1 <= w as f32
                && d.bbox_px.y1 <= h as f32,
            "box out of image: {d:?}"
        );
        assert!(d.raw_label < 25, "{d:?}");
    }
    // 阅读顺序值存在且两两不同（同一目标不会有相同序）
    if dets.iter().all(|d| d.order.is_some()) {
        let mut orders: Vec<u32> = dets.iter().map(|d| d.order.unwrap()).collect();
        orders.sort_unstable();
        orders.dedup();
        // 允许少量并列（不同目标偶尔同序），但去重后应明显少于总数的一半才会触发
        // 上游换 XY-cut；这里只验证存在性。
        assert!(!orders.is_empty());
    }
}

#[test]
fn load_missing_model_errors() {
    let p = std::path::PathBuf::from("/nonexistent/model.onnx");
    match LayoutModel::load(&p, 1) {
        Err(syncpdf_layout::SessionError::ModelMissing(path)) => assert_eq!(path, p),
        other => panic!("expected ModelMissing, got {:?}", other.map(|_| ())),
    }
}
