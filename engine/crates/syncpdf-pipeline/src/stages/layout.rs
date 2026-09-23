//! layout_analysis 阶段：渲染 → 模型检测 → 转 PDF 用户空间 → 覆盖门禁。
//!
//! 设计基准：02-技术路径与架构.md §3（layout_analysis 行）与 §6。
//! 门禁：未被任何区域覆盖的非白字形比例 > `coverage_limit`（默认 0.5%）时，
//! 只拆出能够证明属于同一正文物理行的遗漏续行；无法归属的字形仍报告缺口。

use syncpdf_core::ir::{PageIR, Region, RegionKind};
use syncpdf_core::{PageId, Rect};
use syncpdf_layout::{
    coverage, px_to_user_space, xy_cut_order, DetectOpts, Detection, LayoutModel, RawImage,
};
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
/// 调用方接着应调 [`apply_coverage_fallback`] 核查区域边界。
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
///
/// 检测框是渲染位图像素坐标（左上原点、含 `/Rotate` 的可视页面）；
/// [`px_to_user_space`] 把它换到与字形框同一空间：未旋转、左下原点、
/// MediaBox 原点系（含 CropBox 原点偏移与旋转逆转，实测见
/// syncpdf-layout 的 tests/repair_layout_coords.rs）。
pub fn regions_from_detections(
    detections: &[Detection],
    img_w: u32,
    img_h: u32,
    info: &PageInfo,
    page: u32,
) -> Vec<Region> {
    let boxes: Vec<Rect> = detections
        .iter()
        .map(|d| px_to_user_space(d.bbox_px, img_w, img_h, info))
        .collect();
    // 阅读顺序：优先用模型自带的 order，缺省时用 XY-cut。
    // 页顶参考是用户空间 CropBox 的 y1（旋转后区域的 y 已在未旋转空间）。
    let orders = xy_cut_order(&boxes, info.crop_box.y1);
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

/// 覆盖率门禁：超限时修正与现有文本类区域相邻的遗漏行。
/// 返回修正后的报告；真正漏检或归属不明时保留缺口供 issue 事件报告。
pub fn apply_coverage_fallback(
    regions: &mut Vec<Region>,
    page_ir: &PageIR,
    _page: u32,
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
    repair_boundary_lines(regions, &glyph_boxes);
    let repaired: Vec<Rect> = regions.iter().map(|r| r.bbox).collect();
    coverage(&glyph_boxes, &repaired)
}

fn repair_boundary_lines(regions: &mut Vec<Region>, glyphs: &[(Rect, bool)]) {
    let mut missed: Vec<Rect> = glyphs
        .iter()
        .filter(|(b, white)| !*white && !regions.iter().any(|r| r.bbox.contains(b.center())))
        .map(|(b, _)| *b)
        .collect();
    missed.sort_by(|a, b| {
        b.center()
            .y
            .total_cmp(&a.center().y)
            .then(a.x0.total_cmp(&b.x0))
    });
    let mut rows: Vec<Vec<Rect>> = Vec::new();
    for b in missed {
        if let Some(row) = rows.last_mut() {
            let first = row[0];
            if (b.center().y - first.center().y).abs() <= first.height().min(b.height()) * 0.35 {
                row.push(b);
                continue;
            }
        }
        rows.push(vec![b]);
    }
    for mut row in rows {
        row.sort_by(|a, b| a.x0.total_cmp(&b.x0));
        let mut start = 0;
        while start < row.len() {
            let mut end = start + 1;
            while end < row.len() && row[end].x0 - row[end - 1].x1 <= row[end - 1].height() * 1.5 {
                end += 1;
            }
            repair_one_line(regions, glyphs, &row[start..end]);
            start = end;
        }
    }
}

fn repair_one_line(regions: &mut Vec<Region>, glyphs: &[(Rect, bool)], line: &[Rect]) {
    if line.len() < 2 {
        return;
    }
    let span = line.iter().skip(1).fold(line[0], |acc, b| acc.union(b));
    if span.width() < span.height() * 3.0 {
        return;
    }
    // 连续的物理行：用所有可见字形找与遗漏段同一基线、同一水平连通分量。
    let mut row: Vec<Rect> = glyphs
        .iter()
        .filter(|(b, white)| {
            !*white
                && (b.center().y - span.center().y).abs() <= b.height().min(span.height()) * 0.35
        })
        .map(|(b, _)| *b)
        .collect();
    row.sort_by(|a, b| a.x0.total_cmp(&b.x0));
    let mut component: Option<&[Rect]> = None;
    let mut start = 0;
    while start < row.len() {
        let mut end = start + 1;
        while end < row.len() && row[end].x0 - row[end - 1].x1 <= row[end - 1].height() * 1.5 {
            end += 1;
        }
        if row[start].x0 <= span.x0 && row[end - 1].x1 >= span.x1 {
            if component.is_some() {
                return;
            }
            component = Some(&row[start..end]);
        }
        start = end;
    }
    let Some(component) = component else {
        return;
    };
    let whole = component
        .iter()
        .skip(1)
        .fold(component[0], |acc, b| acc.union(b));
    let mut source: Option<usize> = None;
    let mut covered = 0;
    let mut seen_missed = false;
    for b in component {
        let owners: Vec<usize> = regions
            .iter()
            .enumerate()
            .filter(|(_, r)| r.bbox.contains(b.center()))
            .map(|(i, _)| i)
            .collect();
        match owners.as_slice() {
            [] => seen_missed = true,
            [i] if !seen_missed && regions[*i].kind == RegionKind::Text => {
                if source.is_some_and(|s| s != *i) {
                    return;
                }
                source = Some(*i);
                covered += 1;
            }
            _ => return,
        }
    }
    if covered < 2 || !seen_missed {
        return;
    }
    let Some(i) = source else {
        return;
    };
    // 只能拆检测框底部的一行，且下一行与它有可见空隙。
    let old = regions[i].bbox;
    let y = whole.center().y;
    let mut next: Option<f32> = None;
    for (b, _) in glyphs {
        let c = b.center();
        if old.contains(c) && c.y < y - whole.height() * 0.35 {
            return;
        }
        if old.contains(c) && c.y > y + whole.height() * 0.35 {
            next = Some(next.map_or(c.y, |n| n.min(c.y)));
        }
    }
    let Some(next) = next else {
        return;
    };
    let cut = (y + next) * 0.5;
    if cut <= whole.y1 || cut >= next || cut >= old.y1 {
        return;
    }
    // 新窄框只能收这一行；旧框裁掉的每个字形都必须被新框接走。
    for (b, white) in glyphs {
        let c = b.center();
        if old.contains(c) && c.y < cut && !whole.contains(c) {
            return;
        }
        if whole.contains(c) {
            if (c.y - y).abs() > whole.height() * 0.35 {
                return;
            }
            if !*white && !component.contains(b) {
                return;
            }
            if regions
                .iter()
                .enumerate()
                .any(|(j, r)| j != i && r.bbox.contains(c))
            {
                return;
            }
        }
    }
    let new_region = Region {
        page: regions[i].page,
        index: regions.iter().map(|r| r.index).max().map_or(0, |m| m + 1),
        kind: RegionKind::Text,
        bbox: whole,
        score: regions[i].score,
        order: regions[i].order.saturating_add(1),
    };
    regions[i].bbox.y0 = cut;
    regions.push(new_region);
}

/// 白字形判定：纯空白（空格）或不可见渲染模式、裁掉的字形。
pub fn is_white_glyph(g: &syncpdf_core::ir::Glyph) -> bool {
    g.flags.is_space || g.flags.invisible || g.flags.outside_clip || g.unicode.is_empty()
}

#[cfg(test)]
mod tests {
    use super::*;
    use syncpdf_core::ir::{DisplayItem, Glyph, GlyphFlags, GlyphSource};
    use syncpdf_core::require_fixture;
    use syncpdf_core::{Matrix, PageId};

    #[test]
    #[ignore = "manual 23-page probe: set SYNCPDF_TARGET_PDF"]
    fn target_paper_coverage_boundaries_all_pages() {
        let path = std::path::PathBuf::from(
            std::env::var_os("SYNCPDF_TARGET_PDF").expect("set SYNCPDF_TARGET_PDF"),
        );
        let path = path.as_path();
        if !path.is_file() {
            eprintln!("SKIP: target PDF missing");
            return;
        }
        let Some(models) = syncpdf_core::fixtures::models_dir() else {
            eprintln!("SKIP: model dir missing");
            return;
        };
        let model_path = models.join("pp_doc_layoutv3.onnx");
        if !model_path.is_file() {
            eprintln!("SKIP: model missing");
            return;
        }
        let worker = PdfiumWorker::spawn().expect("pdfium");
        let doc = worker.open(path).expect("open");
        let lo = lopdf::Document::load(path).expect("lopdf");
        let mut model = LayoutModel::load(&model_path, 2).expect("model");
        for page in 0..23 {
            let bound = syncpdf_pdf::bind::bind_page(&worker, doc, &lo, page + 1).expect("bind");
            let info = worker.page_info(doc, page).expect("info");
            let regions = detect_regions(
                &mut model,
                &worker,
                doc,
                page,
                &info,
                &LayoutOpts::default(),
            )
            .expect("detect");
            let boxes: Vec<_> = regions.iter().map(|r| r.bbox).collect();
            let glyphs: Vec<_> = bound
                .ir
                .glyphs()
                .filter(|g| !g.flags.invisible && !g.flags.outside_clip && !is_white_glyph(g))
                .collect();
            let missed: Vec<_> = glyphs
                .iter()
                .filter(|g| !boxes.iter().any(|b| b.contains(g.bbox.center())))
                .collect();
            eprintln!(
                "PAGE {} glyphs={} missed={} ratio={:.4} regions={}",
                page + 1,
                glyphs.len(),
                missed.len(),
                missed.len() as f32 / glyphs.len() as f32,
                regions.len()
            );
            if page == 6 || page == 14 {
                for (i, r) in regions.iter().enumerate() {
                    eprintln!(
                        "R {i} {:?} {:?} order={} {:.1},{:.1},{:.1},{:.1}",
                        r.kind, r.score, r.order, r.bbox.x0, r.bbox.y0, r.bbox.x1, r.bbox.y1
                    );
                }
                for g in missed {
                    eprintln!(
                        "G {:?} {:.1},{:.1},{:.1},{:.1}",
                        g.unicode.iter().collect::<String>(),
                        g.bbox.x0,
                        g.bbox.y0,
                        g.bbox.x1,
                        g.bbox.y1
                    );
                }
                if page == 6 {
                    let mut rows: std::collections::BTreeMap<i32, (usize, Rect, String)> =
                        std::collections::BTreeMap::new();
                    for g in &glyphs {
                        if g.bbox.center().y < 100.0 && g.bbox.center().y > 60.0 {
                            let key = (g.bbox.center().y * 10.0).round() as i32;
                            let entry = rows.entry(key).or_insert((0, g.bbox, String::new()));
                            entry.0 += 1;
                            entry.1 = entry.1.union(&g.bbox);
                            entry.2.extend(&g.unicode);
                        }
                    }
                    for (key, (n, b, s)) in rows {
                        eprintln!("ROW {key} n={n} {:?} {s}", b);
                    }
                }
            }
            let mut repaired = regions.clone();
            let report = apply_coverage_fallback(&mut repaired, &bound.ir, page, 0.005);
            eprintln!(
                "AFTER {} missed={} ratio={:.4}",
                page + 1,
                report.uncovered,
                report.ratio
            );
            for (old, new) in regions.iter().zip(&repaired) {
                if old.bbox != new.bbox {
                    eprintln!("EXPAND {} {:?} -> {:?}", old.index, old.bbox, new.bbox);
                }
            }
            if page == 6 {
                assert_eq!(report.uncovered, 0);
                assert_eq!(repaired.len(), regions.len() + 1);
                let line = repaired.last().unwrap();
                assert_eq!(line.kind, RegionKind::Text);
                assert!(line.bbox.x0 < 109.0 && line.bbox.x1 > 503.0);
                assert!(line.bbox.y0 > 69.0 && line.bbox.y1 < 80.0);
                assert_eq!(repaired[9].kind, RegionKind::Text);
                assert!(repaired[9].bbox.y0 > line.bbox.y1);
            } else if page == 14 {
                assert_eq!(report.uncovered, 30, "右侧说明标题仍缺可证明的独立类别");
                assert_eq!(repaired, regions, "不能并入左侧 Code 框");
            } else {
                assert_eq!(repaired, regions, "page {} 不应变更", page + 1);
            }
        }
        worker.close(doc);
    }

    fn mk_glyph(
        ordinal: u16,
        text: &str,
        x: f32,
        y: f32,
        w: f32,
        h: f32,
        flags: GlyphFlags,
    ) -> Glyph {
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
    fn distant_uncovered_text_remains_reported() {
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
        assert_eq!(regions.len(), 1, "真正遗漏不能用大框掩盖");
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
        assert!(
            report.ratio > 0.005 && report.ratio < 0.02,
            "{}",
            report.ratio
        );
        assert_eq!(regions.len(), 1);
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
    fn unresolved_glyph_does_not_create_region() {
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
        assert_eq!(regions.len(), 2);
        assert_eq!(regions.last().unwrap().index, 5);
    }

    #[test]
    fn adjacent_caption_without_same_line_source_stays_uncovered() {
        let glyphs = (0..10)
            .map(|i| {
                mk_glyph(
                    i,
                    "a",
                    240.0 + i as f32 * 8.0,
                    68.0,
                    7.0,
                    10.0,
                    GlyphFlags::default(),
                )
            })
            .collect();
        let ir = page_ir(glyphs);
        let mut left = region(0, Rect::new(100.0, 60.0, 230.0, 230.0));
        left.kind = RegionKind::Text;
        let mut caption = region(1, Rect::new(235.0, 82.0, 330.0, 160.0));
        caption.kind = RegionKind::Caption;
        let mut regions = vec![left.clone(), caption.clone()];
        let report = apply_coverage_fallback(&mut regions, &ir, 0, 0.005);
        assert_eq!(report.uncovered, 10);
        assert_eq!(regions[0].bbox, left.bbox);
        assert_eq!(regions[1].kind, RegionKind::Caption);
        assert_eq!(regions[1].bbox, caption.bbox);
        assert_eq!(regions.len(), 2);
    }

    fn continuation_fixture(extra_row: bool) -> PageIR {
        let mut glyphs = Vec::new();
        for i in 0..4 {
            glyphs.push(mk_glyph(
                i,
                "a",
                100.0 + i as f32 * 8.0,
                70.0,
                7.0,
                10.0,
                GlyphFlags::default(),
            ));
            glyphs.push(mk_glyph(
                i + 4,
                "b",
                100.0 + i as f32 * 8.0,
                82.0,
                7.0,
                10.0,
                GlyphFlags::default(),
            ));
        }
        for i in 0..6 {
            glyphs.push(mk_glyph(
                i + 8,
                "c",
                132.0 + i as f32 * 8.0,
                70.0,
                7.0,
                10.0,
                GlyphFlags::default(),
            ));
            if extra_row {
                glyphs.push(mk_glyph(
                    i + 14,
                    "d",
                    132.0 + i as f32 * 8.0,
                    58.0,
                    7.0,
                    10.0,
                    GlyphFlags::default(),
                ));
            }
        }
        page_ir(glyphs)
    }

    #[test]
    fn same_physical_text_line_is_split_without_enlarging_column() {
        let ir = continuation_fixture(false);
        let mut regions = vec![region(0, Rect::new(95.0, 68.0, 132.0, 130.0))];
        let report = apply_coverage_fallback(&mut regions, &ir, 0, 0.005);
        assert_eq!(report.uncovered, 0);
        assert_eq!(regions.len(), 2);
        assert_eq!(regions[0].bbox.x1, 132.0, "旧列不能横扩过空白");
        assert!(regions[0].bbox.y0 > regions[1].bbox.y1);
        assert_eq!(regions[1].bbox, Rect::new(100.0, 70.0, 179.0, 80.0));
        assert_eq!(regions[1].kind, RegionKind::Text);
    }

    #[test]
    fn second_unowned_row_cannot_cascade_from_split_line() {
        let ir = continuation_fixture(true);
        let mut regions = vec![region(0, Rect::new(95.0, 68.0, 132.0, 130.0))];
        let report = apply_coverage_fallback(&mut regions, &ir, 0, 0.005);
        assert_eq!(report.uncovered, 6);
        assert_eq!(regions.len(), 2);
    }

    #[test]
    fn row_with_two_source_regions_is_ambiguous() {
        let ir = continuation_fixture(false);
        let mut regions = vec![
            region(0, Rect::new(95.0, 68.0, 115.0, 130.0)),
            region(1, Rect::new(115.0, 68.0, 132.0, 130.0)),
        ];
        let before = regions.clone();
        let report = apply_coverage_fallback(&mut regions, &ir, 0, 0.005);
        assert_eq!(report.uncovered, 6);
        assert_eq!(regions, before);
    }

    #[test]
    fn column_and_figure_gaps_are_not_absorbed() {
        let glyphs = (0..8)
            .map(|i| {
                mk_glyph(
                    i,
                    "a",
                    260.0 + i as f32 * 8.0,
                    68.0,
                    7.0,
                    10.0,
                    GlyphFlags::default(),
                )
            })
            .collect();
        let ir = page_ir(glyphs);
        let left = region(0, Rect::new(100.0, 60.0, 230.0, 230.0));
        let mut figure = region(1, Rect::new(250.0, 82.0, 340.0, 160.0));
        figure.kind = RegionKind::Figure;
        let mut regions = vec![left.clone(), figure.clone()];
        let report = apply_coverage_fallback(&mut regions, &ir, 0, 0.005);
        assert_eq!(report.uncovered, 8);
        assert_eq!(regions[0].bbox, left.bbox);
        assert_eq!(regions[1].bbox, figure.bbox);
    }

    #[test]
    fn expansion_rejects_duplicate_assignment_to_neighbor() {
        let glyphs = vec![
            mk_glyph(0, "a", 230.0, 68.0, 7.0, 10.0, GlyphFlags::default()),
            mk_glyph(1, "b", 238.0, 68.0, 7.0, 10.0, GlyphFlags::default()),
            mk_glyph(2, "c", 246.0, 68.0, 7.0, 10.0, GlyphFlags::default()),
            mk_glyph(3, "d", 254.0, 68.0, 7.0, 10.0, GlyphFlags::default()),
            mk_glyph(4, "e", 250.0, 75.0, 7.0, 10.0, GlyphFlags::default()),
        ];
        let ir = page_ir(glyphs);
        let mut regions = vec![
            region(0, Rect::new(248.0, 78.0, 260.0, 90.0)),
            region(1, Rect::new(228.0, 82.0, 265.0, 160.0)),
        ];
        let before = regions.clone();
        apply_coverage_fallback(&mut regions, &ir, 0, 0.005);
        assert_eq!(regions[1].bbox, before[1].bbox);
    }

    #[test]
    fn regions_from_detections_full_bitmap_maps_to_crop_box_all_rotations() {
        use syncpdf_layout::Detection;
        // 整幅位图无论旋转多少度，都应恰好映射回 CropBox（用户空间）。
        let crop = Rect::new(61.0, 79.0, 551.0, 713.0);
        for rot in [0, 90, 180, 270] {
            let (w, h) = if rot % 180 == 0 {
                (crop.x1 - crop.x0, crop.y1 - crop.y0)
            } else {
                (crop.y1 - crop.y0, crop.x1 - crop.x0)
            };
            let info = PageInfo {
                width: w,
                height: h,
                media_box: Rect::new(0.0, 0.0, 612.0, 792.0),
                crop_box: crop,
                rotation: rot,
            };
            let det = Detection {
                kind: RegionKind::Text,
                raw_label: 22,
                score: 0.9,
                bbox_px: Rect::new(0.0, 0.0, w, h),
                order: None,
            };
            let regions = regions_from_detections(&[det], w as u32, h as u32, &info, 0);
            assert_eq!(regions.len(), 1);
            let b = regions[0].bbox;
            assert!(
                (b.x0 - crop.x0).abs() < 1e-3
                    && (b.y0 - crop.y0).abs() < 1e-3
                    && (b.x1 - crop.x1).abs() < 1e-3
                    && (b.y1 - crop.y1).abs() < 1e-3,
                "rot {rot}: {b:?} != {crop:?}"
            );
        }
    }

    #[test]
    fn regions_from_detections_rot90_orders_in_user_space() {
        use syncpdf_layout::Detection;
        // rot 90：可视页 200x300（未旋转 300x200）。逆转公式
        //（见 syncpdf_layout::px_to_user_space）：user_x = cx1 - vy，user_y = cy0 + vx。
        // 框 A：px (10,10)-(60,60) → user (10,10)-(60,60)，未旋转页**左下**；
        // 框 B：px (140,240)-(190,290) → user (240,140)-(290,190)，未旋转页**右上**。
        // 阅读顺序应在用户空间排：左列（A）先于右列（B）。
        let info = PageInfo {
            width: 200.0,
            height: 300.0,
            media_box: Rect::new(0.0, 0.0, 300.0, 200.0),
            crop_box: Rect::new(0.0, 0.0, 300.0, 200.0),
            rotation: 90,
        };
        let dets = vec![
            Detection {
                kind: RegionKind::Text,
                raw_label: 22,
                score: 0.9,
                bbox_px: Rect::new(10.0, 10.0, 60.0, 60.0),
                order: None,
            },
            Detection {
                kind: RegionKind::Text,
                raw_label: 22,
                score: 0.9,
                bbox_px: Rect::new(140.0, 240.0, 190.0, 290.0),
                order: None,
            },
        ];
        let regions = regions_from_detections(&dets, 200, 300, &info, 0);
        let (a, b) = (regions[0].bbox, regions[1].bbox);
        assert!(
            (a.x0 - 10.0).abs() < 1e-3 && (a.y0 - 10.0).abs() < 1e-3,
            "{a:?}"
        );
        assert!(
            (b.x0 - 240.0).abs() < 1e-3 && (b.y0 - 140.0).abs() < 1e-3,
            "{b:?}"
        );
        assert_eq!(regions[0].order, 0, "用户空间左列（A）先读");
        assert_eq!(regions[1].order, 1, "用户空间右列（B）后读");
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
