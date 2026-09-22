//! layout_analysis 阶段：渲染 → 模型检测 → 转 PDF 用户空间 → 覆盖门禁。
//!
//! 设计基准：02-技术路径与架构.md §3（layout_analysis 行）与 §6。
//! 门禁：未被任何区域覆盖的非白字形比例 > `coverage_limit`（默认 0.5%）时，
//! 把「未覆盖字形并集框」追加成一个兜底区域，避免整段文字漏译。

use syncpdf_core::ir::{PageIR, Region, RegionKind};
use syncpdf_core::{PageId, Rect};
use syncpdf_layout::{coverage, to_pdf_space, xy_cut_order, Detection, DetectOpts, LayoutModel, RawImage};
use syncpdf_pdf::pdfium::{DocId, PageInfo, PdfiumWorker};

use super::PipelineError;

/// layout_analysis 的可调参数。
#[derive(Debug, Clone, Copy)]
pub struct LayoutOpts {
    /// 渲染 dpi（默认 150）。
    pub dpi: f32,
    /// 检测分数阈值（默认 0.4）。
    pub score_threshold: f32,
    /// 未覆盖比例上限（默认 0.005 = 0.5%）。
    pub coverage_limit: f32,
}

impl Default for LayoutOpts {
    fn default() -> Self {
        Self {
            dpi: 150.0,
            score_threshold: 0.4,
            coverage_limit: 0.005,
        }
    }
}

impl LayoutOpts {
    /// 转成 layout crate 的检测参数。
    pub fn detect_opts(&self) -> DetectOpts {
        DetectOpts {
            score_threshold: self.score_threshold,
            ..DetectOpts::default()
        }
    }
}

/// 渲染一页 → 检测 → 转 PDF 用户空间 → 按 XY-cut 定阅读顺序。
///
/// 返回的区域 `index` 按检测序编（与 `order` 无关），`order` 为阅读顺序。
/// 调用方接着应调 [`apply_coverage_fallback`] 补兜底区域。
pub fn detect_regions(
    model: &mut LayoutModel,
    worker: &PdfiumWorker,
    doc: DocId,
    page: u32,
    info: &PageInfo,
    opts: &LayoutOpts,
) -> Result<Vec<Region>, PipelineError> {
    let bitmap = worker.render_page(doc, page, opts.dpi)?;
    let img = RawImage {
        width: bitmap.width,
        height: bitmap.height,
        rgba: &bitmap.data,
    };
    let detections = model.detect(&img, &opts.detect_opts())?;
    Ok(regions_from_detections(
        &detections,
        bitmap.width,
        bitmap.height,
        info,
        page,
    ))
}

/// 把检测结果变成区域（纯函数，便于单测）。
pub fn regions_from_detections(
    detections: &[Detection],
    img_w: u32,
    img_h: u32,
    info: &PageInfo,
    page: u32,
) -> Vec<Region> {
    let boxes: Vec<Rect> = detections
        .iter()
        .map(|d| to_pdf_space(d, img_w, img_h, info.width, info.height))
        .collect();
    // 阅读顺序：优先用模型自带的 order，缺省时用 XY-cut。
    let orders = xy_cut_order(&boxes, info.height);
    detections
        .iter()
        .enumerate()
        .map(|(i, d)| Region {
            page: PageId(page),
            index: i as u32,
            kind: d.kind,
            bbox: boxes[i],
            score: d.score,
            order: d.order.unwrap_or(orders[i]),
        })
        .collect()
}

/// 覆盖率门禁：未覆盖比例 > `limit` 时追加「兜底区域」。
///
/// 兜底区域 = 未被任何区域覆盖的非白字形 bbox 并集，`kind = Text`、
/// `index` 续编、`order = u32::MAX`（永远排在最后）。
///
/// 返回覆盖率报告；`regions` 未变时返回的也仍是报告（供 issue 事件用）。
/// 不修改 `regions` 的情形：无未覆盖字形，或比例未超限。
pub fn apply_coverage_fallback(
    regions: &mut Vec<Region>,
    page_ir: &PageIR,
    page: u32,
    limit: f32,
) -> syncpdf_layout::CoverageReport {
    let glyph_boxes: Vec<(Rect, bool)> = page_ir
        .glyphs()
        .filter(|g| !g.flags.invisible && !g.flags.outside_clip)
        .map(|g| (g.bbox, is_white_glyph(g)))
        .collect();
    let covers: Vec<Rect> = regions.iter().map(|r| r.bbox).collect();
    let report = coverage(&glyph_boxes, &covers);
    if report.ratio <= limit {
        return report;
    }
    if let Some(union) = uncovered_union(&glyph_boxes, &covers) {
        let index = regions.iter().map(|r| r.index).max().map_or(0, |m| m + 1);
        regions.push(Region {
            page: PageId(page),
            index,
            kind: RegionKind::Text,
            bbox: union,
            score: 0.0,
            order: u32::MAX,
        });
    }
    report
}

/// 未覆盖非白字形的并集框。
fn uncovered_union(glyph_boxes: &[(Rect, bool)], covers: &[Rect]) -> Option<Rect> {
    let mut acc: Option<Rect> = None;
    for (bbox, is_white) in glyph_boxes {
        if *is_white {
            continue;
        }
        let c = bbox.center();
        let covered = covers
            .iter()
            .any(|r| c.x >= r.x0 && c.x <= r.x1 && c.y >= r.y0 && c.y <= r.y1);
        if !covered {
            acc = Some(match acc {
                Some(a) => a.union(bbox),
                None => *bbox,
            });
        }
    }
    acc
}

/// 白字形判定：纯空白（空格）或不可见渲染模式、裁掉的字形。
pub fn is_white_glyph(g: &syncpdf_core::ir::Glyph) -> bool {
    g.flags.is_space || g.flags.invisible || g.flags.outside_clip || g.unicode.is_empty()
}

#[cfg(test)]
mod tests {
    use super::*;
    use syncpdf_core::ir::{DisplayItem, Glyph, GlyphFlags, GlyphSource};
    use syncpdf_core::{Matrix, PageId};
    use syncpdf_core::require_fixture;

    fn mk_glyph(ordinal: u16, text: &str, x: f32, y: f32, w: f32, h: f32, flags: GlyphFlags) -> Glyph {
        Glyph {
            id: syncpdf_core::GlyphId {
                page: PageId(0),
                op: syncpdf_core::OpKey::new(syncpdf_core::ObjRef::new(1, 0), 0),
                ordinal,
            },
            unicode: text.chars().collect(),
            code: ordinal as u32,
            font: 0,
            size: h,
            matrix: Matrix::new(1.0, 0.0, 0.0, 1.0, 0.0, 0.0),
            bbox: Rect::new(x, y, x + w, y + h),
            advance: w,
            fill: Default::default(),
            render_mode: 0,
            source: GlyphSource {
                element_index: 0,
                string_operand_range: (0, 0),
                decoded_code_range: (0, 0),
            },
            flags,
        }
    }

    fn page_ir(glyphs: Vec<Glyph>) -> PageIR {
        PageIR {
            page: PageId(0),
            media_box: Rect::new(0.0, 0.0, 612.0, 792.0),
            crop_box: Rect::new(0.0, 0.0, 612.0, 792.0),
            rotation: 0,
            fonts: vec![],
            items: vec![DisplayItem::Text { glyphs }],
        }
    }

    fn region(index: u32, bbox: Rect) -> Region {
        Region {
            page: PageId(0),
            index,
            kind: RegionKind::Text,
            bbox,
            score: 0.9,
            order: index,
        }
    }

    #[test]
    fn fallback_added_when_uncovered_ratio_exceeds_limit() {
        // 两个字形都在区域外 → 比例 1.0 > 0.005。
        let ir = page_ir(vec![
            mk_glyph(0, "A", 10.0, 700.0, 8.0, 10.0, GlyphFlags::default()),
            mk_glyph(1, "B", 20.0, 700.0, 8.0, 10.0, GlyphFlags::default()),
        ]);
        let mut regions = vec![region(0, Rect::new(0.0, 0.0, 100.0, 100.0))];
        let report = apply_coverage_fallback(&mut regions, &ir, 0, 0.005);
        assert_eq!(report.total_glyphs, 2);
        assert_eq!(report.uncovered, 2);
        assert!((report.ratio - 1.0).abs() < 1e-6);
        assert_eq!(regions.len(), 2);
        let fb = regions.last().unwrap();
        assert_eq!(fb.kind, RegionKind::Text);
        assert_eq!(fb.order, u32::MAX);
        assert_eq!(fb.index, 1, "index 续编");
        // 兜底框覆盖两个未覆盖字形。
        assert!(fb.bbox.contains(Rect::new(10.0, 700.0, 18.0, 710.0).center()));
        assert!(fb.bbox.contains(Rect::new(20.0, 700.0, 28.0, 710.0).center()));
    }

    #[test]
    fn no_fallback_when_all_glyphs_covered() {
        let ir = page_ir(vec![mk_glyph(
            0,
            "A",
            10.0,
            50.0,
            8.0,
            10.0,
            GlyphFlags::default(),
        )]);
        let mut regions = vec![region(0, Rect::new(0.0, 0.0, 100.0, 100.0))];
        let report = apply_coverage_fallback(&mut regions, &ir, 0, 0.005);
        assert_eq!(report.uncovered, 0);
        assert_eq!(report.ratio, 0.0);
        assert_eq!(regions.len(), 1, "不该追加兜底区域");
    }

    #[test]
    fn white_glyphs_do_not_count_toward_coverage() {
        // 空格字形在区域外：规则要求它既不计入 uncovered 也不计入分母。
        let space = mk_glyph(
            0,
            " ",
            500.0,
            700.0,
            3.0,
            10.0,
            GlyphFlags {
                is_space: true,
                ..Default::default()
            },
        );
        let ir = page_ir(vec![space]);
        let mut regions = vec![region(0, Rect::new(0.0, 0.0, 100.0, 100.0))];
        let report = apply_coverage_fallback(&mut regions, &ir, 0, 0.005);
        assert_eq!(report.total_glyphs, 1);
        assert_eq!(report.white_excluded, 1);
        assert_eq!(report.uncovered, 0);
        assert_eq!(report.ratio, 0.0);
        assert_eq!(regions.len(), 1, "白字形不触发兜底");
    }

    #[test]
    fn small_uncovered_ratio_stays_below_limit() {
        // 100 个字形只有 1 个未覆盖 → 0.01 > 0.005（仍然超限，触发兜底）。
        let mut glyphs: Vec<Glyph> = (0..100)
            .map(|i| {
                mk_glyph(
                    i,
                    "x",
                    10.0 + (i as f32 % 10.0) * 9.0,
                    50.0 + (i as f32 / 10.0) * 12.0,
                    8.0,
                    10.0,
                    GlyphFlags::default(),
                )
            })
            .collect();
        // 第 100 个字形放到区域外。
        glyphs.push(mk_glyph(
            100,
            "y",
            600.0,
            700.0,
            8.0,
            10.0,
            GlyphFlags::default(),
        ));
        let ir = page_ir(glyphs);
        let mut regions = vec![region(0, Rect::new(0.0, 0.0, 150.0, 200.0))];
        let report = apply_coverage_fallback(&mut regions, &ir, 0, 0.005);
        assert_eq!(report.uncovered, 1);
        assert!(report.ratio > 0.005 && report.ratio < 0.02, "{}", report.ratio);
        assert_eq!(regions.len(), 2);
    }

    #[test]
    fn invisible_and_clipped_glyphs_are_excluded() {
        let ir = page_ir(vec![
            mk_glyph(
                0,
                "A",
                500.0,
                700.0,
                8.0,
                10.0,
                GlyphFlags {
                    invisible: true,
                    ..Default::default()
                },
            ),
            mk_glyph(
                1,
                "B",
                500.0,
                700.0,
                8.0,
                10.0,
                GlyphFlags {
                    outside_clip: true,
                    ..Default::default()
                },
            ),
        ]);
        let mut regions = vec![region(0, Rect::new(0.0, 0.0, 100.0, 100.0))];
        // 不可见 / 裁掉的字形在进 coverage 前就被过滤掉：既不算覆盖也不触发兜底。
        let report = apply_coverage_fallback(&mut regions, &ir, 0, 0.005);
        assert_eq!(report.total_glyphs, 0);
        assert_eq!(report.ratio, 0.0);
        assert_eq!(regions.len(), 1);
    }

    #[test]
    fn fallback_index_continues_after_existing_max() {
        let ir = page_ir(vec![mk_glyph(
            0,
            "A",
            600.0,
            700.0,
            8.0,
            10.0,
            GlyphFlags::default(),
        )]);
        let mut regions = vec![
            region(0, Rect::new(0.0, 0.0, 10.0, 10.0)),
            region(5, Rect::new(20.0, 20.0, 30.0, 30.0)),
        ];
        apply_coverage_fallback(&mut regions, &ir, 0, 0.005);
        assert_eq!(regions.last().unwrap().index, 6);
    }

    #[test]
    fn detect_regions_on_up_vns_first_page() {
        let path = require_fixture!("up-vns.pdf");
        let Some(models) = syncpdf_core::fixtures::models_dir() else {
            eprintln!("SKIP: 模型目录缺失");
            return;
        };
        let model_path = models.join("pp_doc_layoutv3.onnx");
        if !model_path.is_file() {
            eprintln!("SKIP: 布局模型缺失");
            return;
        }
        let Ok(worker) = PdfiumWorker::spawn() else {
            eprintln!("SKIP: pdfium 不可用");
            return;
        };
        let Ok(mut model) = LayoutModel::load(&model_path, 2) else {
            eprintln!("SKIP: 模型加载失败");
            return;
        };
        let doc = worker.open(&path).unwrap();
        let info = worker.page_info(doc, 0).unwrap();
        let regions = detect_regions(&mut model, &worker, doc, 0, &info, &LayoutOpts::default())
            .expect("第 1 页检测应成功");
        assert!(!regions.is_empty(), "up-vns 第 1 页应至少检出 1 个区域");
        for r in &regions {
            assert!(!r.bbox.is_empty(), "区域框非空：{:?}", r.bbox);
            // 允许 1pt 容差（浮点缩放）。
            assert!(
                r.bbox.x0 >= -1.0
                    && r.bbox.y0 >= -1.0
                    && r.bbox.x1 <= info.width + 1.0
                    && r.bbox.y1 <= info.height + 1.0,
                "区域框应在页内：{:?} / {:?}",
                r.bbox,
                info
            );
        }
        assert_eq!(regions.iter().map(|r| r.index).collect::<Vec<_>>()[0], 0);
        worker.close(doc);
    }
}
