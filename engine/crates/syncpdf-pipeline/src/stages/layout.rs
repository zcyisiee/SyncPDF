//! layout_analysis 阶段：渲染 → 模型检测 → 转 PDF 用户空间 → 覆盖门禁。
//!
//! 设计基准：02-技术路径与架构.md §3（layout_analysis 行）与 §6。
//! 门禁：未被任何区域覆盖的非白字形比例 > `coverage_limit`（默认 0.5%）时，
//! 只做两类有证据的归属修复——面版继承同一源绘图的图内文字、拆分同一正文物理行的
//! 遗漏续行；无法归属的字形仍报告缺口。

use syncpdf_core::ir::{DisplayItem, Glyph, PageIR, Region, RegionKind};
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
    super::ruled_code::refine_ruled_code_sidebars(regions, page_ir);
    recover_subcaptions(regions, page_ir);
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
    recover_form_panel_ink(regions, page_ir);
    repair_boundary_lines(regions, &glyph_boxes);
    let repaired: Vec<Rect> = regions.iter().map(|r| r.bbox).collect();
    coverage(&glyph_boxes, &repaired)
}

/// A short labeled line centered immediately above/below a detected table/figure
/// is a subcaption. Recover missed lines and give existing ones their panel width.
fn recover_subcaptions(regions: &mut Vec<Region>, ir: &PageIR) {
    use syncpdf_core::ir::RegionKind;
    let glyphs: Vec<_> = ir
        .glyphs()
        .filter(|g| !g.flags.invisible && !g.flags.outside_clip)
        .collect();
    let missed: Vec<_> = glyphs
        .iter()
        .filter(|g| !regions.iter().any(|r| r.bbox.contains(g.bbox.center())))
        .map(|g| (g.id, g.bbox))
        .collect();
    let mut candidates: Vec<(Option<usize>, Vec<syncpdf_core::GlyphId>)> = regions
        .iter()
        .enumerate()
        .filter(|(_, r)| r.kind == RegionKind::Caption)
        .map(|(i, r)| {
            (
                Some(i),
                glyphs
                    .iter()
                    .filter(|g| r.bbox.contains(g.bbox.center()))
                    .map(|g| g.id)
                    .collect(),
            )
        })
        .collect();
    candidates.extend(
        syncpdf_layout::group_lines(&missed, &ir.crop_box)
            .into_iter()
            .map(|ids| (None, ids)),
    );
    let label = regex::Regex::new(r"^\([a-zA-Z]\).*[A-Za-z]{2}").expect("subcaption pattern");
    for (index, ids) in candidates {
        let mut row: Vec<_> = glyphs
            .iter()
            .filter(|g| ids.contains(&g.id))
            .copied()
            .collect();
        row.sort_by(|a, b| a.bbox.x0.total_cmp(&b.bbox.x0));
        let Some(first) = row.first() else { continue };
        let bbox = row.iter().fold(first.bbox, |b, g| b.union(&g.bbox));
        let text: String = row.iter().flat_map(|g| g.unicode.iter()).collect();
        if !label.is_match(&text) || bbox.height() > first.size * 1.8 {
            continue;
        }
        let panel = regions
            .iter()
            .filter(|r| matches!(r.kind, RegionKind::Table | RegionKind::Figure))
            .filter(|r| {
                r.bbox.x0 <= bbox.x0
                    && bbox.x1 <= r.bbox.x1
                    && (r.bbox.center().x - bbox.center().x).abs() < r.bbox.width() * 0.15
            })
            .filter_map(|r| {
                let gap = if r.bbox.y0 >= bbox.y1 {
                    r.bbox.y0 - bbox.y1
                } else if bbox.y0 >= r.bbox.y1 {
                    bbox.y0 - r.bbox.y1
                } else {
                    return None;
                };
                (gap < first.size * 2.0).then_some((gap, r.bbox))
            })
            .min_by(|a, b| a.0.total_cmp(&b.0))
            .map(|(_, b)| b);
        let Some(panel) = panel else { continue };
        let frame = Rect::new(panel.x0, bbox.y0, panel.x1, bbox.y1);
        if let Some(i) = index {
            regions[i].bbox = frame;
        } else {
            let next = regions.iter().map(|r| r.index).max().unwrap_or(0) + 1;
            regions.push(Region {
                page: ir.page,
                index: next,
                kind: RegionKind::Caption,
                bbox: frame,
                score: 1.0,
                order: next,
            });
        }
    }
}

/// 一个已绘制字形，连同它被绘制时所在的源绘图路径。
struct DrawnGlyph<'a> {
    glyph: &'a Glyph,
    /// 外层到内层的 `FormBegin` 下标；空 = 直接画在页内容流里。
    /// 用**下标**而非 Form 资源名，才能区分同名 Form 的多次实例。
    drawing: Vec<u32>,
}

/// 一张非文字绘制，连同它所在的源绘图路径。
struct DrawnPaint {
    bbox: Rect,
    drawing: Vec<u32>,
}

fn starts_with(path: &[u32], prefix: &[u32]) -> bool {
    path.len() >= prefix.len() && path[..prefix.len()] == *prefix
}

/// 两个框的垂直间隙；纵向重叠时为 0。
fn vertical_gap(a: Rect, b: Rect) -> f32 {
    if a.y0 > b.y1 {
        a.y0 - b.y1
    } else if b.y0 > a.y1 {
        b.y0 - a.y1
    } else {
        0.0
    }
}

/// `indices` 所指字形框高度的中位数；没有可用高度返回 `None`。
fn median_glyph_height(glyphs: &[DrawnGlyph<'_>], indices: &[usize]) -> Option<f32> {
    let mut heights: Vec<f32> = indices
        .iter()
        .map(|&i| glyphs[i].glyph.bbox.height())
        .filter(|h| h.is_finite() && *h > 0.0)
        .collect();
    if heights.is_empty() {
        return None;
    }
    heights.sort_by(f32::total_cmp);
    Some(heights[heights.len() / 2])
}

/// 面版继承同一源绘图内的图内文字。
///
/// 检测出的 `Figure` 框有时裁掉面版自己的标题或坐标图例，因为那些字紧贴在绘图边缘。
/// 它们是图的一部分、不是漏掉的正文；但留在框外会触发覆盖门禁。
/// 只有同时满足下列证据才扩框：未覆盖文字与框内已覆盖文字**同属一个源绘图实例**，
/// 未覆盖文字是同一条贴着框的窄文字带，且不会顺带吃进其它区域、字形或绘制。
/// 返回实际扩框的 Figure 个数。
fn recover_form_panel_ink(regions: &mut [Region], ir: &PageIR) -> usize {
    // 距图框边缘的距离上限、以及所采纳文字带的厚度上限，单位是面版字形高度中位数
    // （PDF 用户空间绝对值不跨文档通用）。
    const MARGIN: f32 = 2.5;
    const CLUSTER: f32 = 2.0;

    let mut glyphs: Vec<DrawnGlyph<'_>> = Vec::new();
    let mut paint: Vec<DrawnPaint> = Vec::new();
    let mut stack: Vec<u32> = Vec::new();
    for (index, item) in ir.items.iter().enumerate() {
        match item {
            DisplayItem::FormBegin { .. } => stack.push(index as u32),
            // FormBegin/FormEnd 由 bind 成对产生；不成对时按空栈收尾，不猜身份。
            DisplayItem::FormEnd => {
                stack.pop();
            }
            DisplayItem::Text { glyphs: painted } => glyphs.extend(
                painted
                    .iter()
                    .filter(|g| !is_white_glyph(g))
                    .map(|g| DrawnGlyph {
                        glyph: g,
                        drawing: stack.clone(),
                    }),
            ),
            DisplayItem::Image { bbox } | DisplayItem::InlineImage { bbox } => {
                paint.push(DrawnPaint {
                    bbox: *bbox,
                    drawing: stack.clone(),
                });
            }
            DisplayItem::Path { bbox, .. } => paint.push(DrawnPaint {
                bbox: *bbox,
                drawing: stack.clone(),
            }),
        }
    }
    if glyphs.is_empty() {
        return 0;
    }

    let mut changed = 0;
    for figure in 0..regions.len() {
        if regions[figure].kind != RegionKind::Figure {
            continue;
        }
        let frame = regions[figure].bbox;
        let covered: Vec<usize> = glyphs
            .iter()
            .enumerate()
            .filter(|(_, g)| frame.contains(g.glyph.bbox.center()))
            .map(|(i, _)| i)
            .collect();
        if covered.len() < 2 {
            continue;
        }
        // 已覆盖文字只能属于这一个 Figure，否则两个面版可能都想认领同一批字。
        let exclusive = covered.iter().all(|&i| {
            let center = glyphs[i].glyph.bbox.center();
            let mut owners = regions
                .iter()
                .enumerate()
                .filter(|(_, r)| r.bbox.contains(center));
            owners.next().is_some_and(|(j, _)| j == figure) && owners.next().is_none()
        });
        if !exclusive {
            continue;
        }
        // 框内文字必须一致地来自同一个绘图实例：取其绘图路径的最长公共前缀。
        let mut shared = glyphs[covered[0]].drawing.clone();
        for &i in &covered[1..] {
            let other = &glyphs[i].drawing;
            let keep = shared.iter().zip(other).take_while(|(a, b)| a == b).count();
            shared.truncate(keep);
        }
        if shared.is_empty() {
            continue;
        }
        let Some(height) = median_glyph_height(&glyphs, &covered) else {
            continue;
        };
        let adopted: Vec<usize> = glyphs
            .iter()
            .enumerate()
            .filter(|(_, g)| {
                let center = g.glyph.bbox.center();
                let bbox = g.glyph.bbox;
                !regions.iter().any(|r| r.bbox.contains(center))
                    && starts_with(&g.drawing, &shared)
                    && frame.x0 <= bbox.x0
                    && bbox.x1 <= frame.x1
                    && vertical_gap(bbox, frame) <= height * MARGIN
            })
            .map(|(i, _)| i)
            .collect();
        if adopted.is_empty() {
            continue;
        }
        let adopted_set: std::collections::BTreeSet<usize> = adopted.iter().copied().collect();
        // A shared Form can wrap an entire page. Instance identity is evidence
        // of panel ownership only if that instance has no other visible text
        // outside this panel and the proposed narrow band.
        if glyphs.iter().enumerate().any(|(i, g)| {
            starts_with(&g.drawing, &shared)
                && !frame.contains(g.glyph.bbox.center())
                && !adopted_set.contains(&i)
        }) {
            continue;
        }
        let band = adopted
            .iter()
            .skip(1)
            .fold(glyphs[adopted[0]].glyph.bbox, |acc, &i| {
                acc.union(&glyphs[i].glyph.bbox)
            });
        if band.height() > height * CLUSTER {
            continue;
        }
        let expanded = frame.union(&band);
        if regions
            .iter()
            .enumerate()
            .any(|(i, r)| i != figure && expanded.intersects(&r.bbox))
        {
            continue;
        }
        // 扩框不得顺带吃进任何别的字形（含它原本不属于本图的正文）。
        let steals = glyphs.iter().enumerate().any(|(i, g)| {
            !adopted_set.contains(&i)
                && !frame.contains(g.glyph.bbox.center())
                && expanded.contains(g.glyph.bbox.center())
        });
        if steals {
            continue;
        }
        // 也不得吃进别的绘图：新增带里的非文字绘制必须与框内文字同源，或本就在框内。
        let foreign_paint = paint.iter().any(|p| {
            expanded.intersects(&p.bbox)
                && !frame.contains(p.bbox.center())
                && !starts_with(&p.drawing, &shared)
        });
        if foreign_paint {
            continue;
        }
        regions[figure].bbox = expanded;
        changed += 1;
    }
    changed
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
                assert_eq!(report.uncovered, 0);
                let code = repaired
                    .iter()
                    .find(|r| r.kind == RegionKind::Code)
                    .unwrap();
                let sidebar = repaired.last().unwrap();
                assert_eq!(sidebar.kind, RegionKind::Text);
                assert!(code.bbox.x1 > 340.0 && code.bbox.x1 < 344.0);
                assert!(sidebar.bbox.x0 > code.bbox.x1 && sidebar.bbox.y1 > 551.0);
                assert!(sidebar.bbox.y0 > 290.0 && sidebar.bbox.y0 < 300.0);
                let paragraphs = super::super::paragraph::analyze_page(&bound.ir, &repaired);
                let protected: std::collections::BTreeSet<_> = bound
                    .ir
                    .glyphs()
                    .filter(|g| {
                        repaired
                            .iter()
                            .any(|r| !r.kind.translatable() && r.bbox.contains(g.bbox.center()))
                    })
                    .map(|g| g.id)
                    .collect();
                assert!(
                    paragraphs
                        .iter()
                        .filter(|p| matches!(p.translatable, syncpdf_core::ir::Translatable::Yes))
                        .all(|p| p.glyphs.iter().all(|id| !protected.contains(id))),
                    "算法/公式源字形不可经可译段重复消费"
                );
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
            ink: None,
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

    /// 页面内容流：若干 `FormBegin(name)` 绘图、页级文字、以及可选的非文字绘制。
    struct FormPage {
        items: Vec<DisplayItem>,
    }

    impl FormPage {
        fn new() -> Self {
            Self { items: Vec::new() }
        }

        /// 在一次 Form 实例里画一批文字；同名 Form 可开多次，各自是独立实例。
        fn form(mut self, name: &str, glyphs: Vec<Glyph>) -> Self {
            self.items.push(DisplayItem::FormBegin {
                name: name.into(),
                ctm: Matrix::IDENTITY,
            });
            self.items.push(DisplayItem::Text { glyphs });
            self.items.push(DisplayItem::FormEnd);
            self
        }

        fn form_with_paint(mut self, name: &str, glyphs: Vec<Glyph>, paint: Rect) -> Self {
            self.items.push(DisplayItem::FormBegin {
                name: name.into(),
                ctm: Matrix::IDENTITY,
            });
            self.items.push(DisplayItem::Text { glyphs });
            self.items.push(DisplayItem::Path {
                bbox: paint,
                is_fill: true,
                is_stroke: false,
                stroke: None,
            });
            self.items.push(DisplayItem::FormEnd);
            self
        }

        fn page_text(mut self, glyphs: Vec<Glyph>) -> Self {
            self.items.push(DisplayItem::Text { glyphs });
            self
        }

        fn build(self) -> PageIR {
            PageIR {
                page: PageId(0),
                media_box: Rect::new(0.0, 0.0, 612.0, 792.0),
                crop_box: Rect::new(0.0, 0.0, 612.0, 792.0),
                rotation: 0,
                fonts: vec![],
                items: self.items,
            }
        }
    }

    /// 一行等宽字形，起点 (x,y)，字高 h。
    fn glyph_row(ordinal: u16, text: &str, x: f32, y: f32, w: f32, h: f32) -> Vec<Glyph> {
        text.chars()
            .enumerate()
            .map(|(i, c)| {
                mk_glyph(
                    ordinal + i as u16,
                    &c.to_string(),
                    x + i as f32 * w,
                    y,
                    w,
                    h,
                    GlyphFlags::default(),
                )
            })
            .collect()
    }

    fn figure_region(index: u32, bbox: Rect) -> Region {
        Region {
            page: PageId(0),
            index,
            kind: RegionKind::Figure,
            bbox,
            score: 0.9,
            order: index,
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
    fn figure_panel_title_in_the_same_form_is_adopted() {
        // 检测框收在图内标题下方：标题与图内其它文字同属一次 Form 实例 → 扩框收纳。
        let title = glyph_row(0, "Panel Title", 120.0, 410.0, 5.0, 8.0);
        let mut body = glyph_row(20, "axis labels", 100.0, 300.0, 5.0, 8.0);
        body.extend(glyph_row(40, "more ink", 100.0, 320.0, 5.0, 8.0));
        let ir = FormPage::new()
            .form_with_paint(
                "Im12",
                {
                    let mut g = title.clone();
                    g.extend(body.clone());
                    g
                },
                Rect::new(95.0, 295.0, 210.0, 405.0),
            )
            .build();
        let mut regions = vec![figure_region(0, Rect::new(95.0, 295.0, 210.0, 405.0))];
        let report = apply_coverage_fallback(&mut regions, &ir, 0, 0.005);
        assert_eq!(report.ratio, 0.0, "图内标题不应留在框外触发门禁");
        assert_eq!(regions.len(), 1, "只扩框，不追加区域");
        assert_eq!(regions[0].kind, RegionKind::Figure, "归属仍是图，不改种类");
        assert_eq!(regions[0].bbox, Rect::new(95.0, 295.0, 210.0, 418.0));
    }

    #[test]
    fn whole_page_form_does_not_prove_panel_ownership() {
        let mut text = glyph_row(0, "Panel title", 120.0, 410.0, 5.0, 8.0);
        text.extend(glyph_row(20, "axis labels", 100.0, 300.0, 5.0, 8.0));
        text.extend(glyph_row(40, "Ordinary body text", 100.0, 100.0, 5.0, 8.0));
        let ir = FormPage::new().form("WholePage", text).build();
        let mut regions = vec![figure_region(0, Rect::new(95.0, 295.0, 210.0, 405.0))];
        let before = regions.clone();
        assert_eq!(recover_form_panel_ink(&mut regions, &ir), 0);
        assert_eq!(regions, before);
    }

    #[test]
    fn panel_ink_from_a_different_form_is_not_adopted() {
        // 同名 Form 的第二次实例：框内文字与框外标题不在同一个实例里 → 不扩框。
        let inside = glyph_row(0, "axis labels", 100.0, 300.0, 5.0, 8.0);
        let mut inside = inside;
        inside.extend(glyph_row(20, "more ink", 100.0, 320.0, 5.0, 8.0));
        let legend = glyph_row(40, "Panel Title", 120.0, 410.0, 5.0, 8.0);
        let ir = FormPage::new()
            .form("Im12", inside)
            .form("Im12", legend)
            .build();
        let mut regions = vec![figure_region(0, Rect::new(95.0, 295.0, 210.0, 405.0))];
        let report = apply_coverage_fallback(&mut regions, &ir, 0, 0.005);
        assert!(
            report.ratio > 0.005,
            "无实例证据时必须保留缺口，实际 {}",
            report.ratio
        );
        assert_eq!(regions[0].bbox, Rect::new(95.0, 295.0, 210.0, 405.0));
    }

    #[test]
    fn page_level_ink_is_not_adopted_into_a_figure() {
        // 正文画在页内容流、图在 Form 里：没有共同绘图，不能当成图内文字。
        let mut inside = glyph_row(0, "axis labels", 100.0, 300.0, 5.0, 8.0);
        inside.extend(glyph_row(20, "more ink", 100.0, 320.0, 5.0, 8.0));
        let prose = glyph_row(40, "Body prose", 120.0, 410.0, 5.0, 8.0);
        let ir = FormPage::new()
            .form("Im12", inside)
            .page_text(prose)
            .build();
        let mut regions = vec![figure_region(0, Rect::new(95.0, 295.0, 210.0, 405.0))];
        let report = apply_coverage_fallback(&mut regions, &ir, 0, 0.005);
        assert!(report.ratio > 0.005, "页级正文不得被图框吞掉");
        assert_eq!(regions[0].bbox, Rect::new(95.0, 295.0, 210.0, 405.0));
    }

    #[test]
    fn panel_expansion_rejects_ink_owned_by_another_region() {
        // 扩框会覆到正文区域上 → 不扩，缺口保留。
        let mut inside = glyph_row(0, "axis labels", 100.0, 300.0, 5.0, 8.0);
        inside.extend(glyph_row(20, "more ink", 100.0, 320.0, 5.0, 8.0));
        let mut all = inside.clone();
        all.extend(glyph_row(40, "Panel Title", 120.0, 410.0, 5.0, 8.0));
        let ir = FormPage::new()
            .form_with_paint("Im12", all, Rect::new(95.0, 295.0, 210.0, 405.0))
            .build();
        let mut regions = vec![
            figure_region(0, Rect::new(95.0, 295.0, 210.0, 405.0)),
            region(1, Rect::new(110.0, 406.0, 300.0, 430.0)),
        ];
        apply_coverage_fallback(&mut regions, &ir, 0, 0.005);
        assert_eq!(regions[0].bbox, Rect::new(95.0, 295.0, 210.0, 405.0));
        assert_eq!(regions[1].bbox, Rect::new(110.0, 406.0, 300.0, 430.0));
    }

    #[test]
    fn panel_ink_owned_by_two_regions_is_ambiguous() {
        // 框内文字同时被另一个区域覆盖 → 归属不唯一，不擅自扩框。
        let mut inside = glyph_row(0, "axis labels", 100.0, 300.0, 5.0, 8.0);
        inside.extend(glyph_row(20, "more ink", 100.0, 320.0, 5.0, 8.0));
        let mut all = inside.clone();
        all.extend(glyph_row(40, "Panel Title", 120.0, 410.0, 5.0, 8.0));
        let figure = Rect::new(95.0, 295.0, 210.0, 405.0);
        let ir = FormPage::new()
            .form_with_paint("Im12", all, Rect::new(95.0, 295.0, 210.0, 405.0))
            .build();
        // 第二个区域也盖住框内的字。
        let mut regions = vec![
            figure_region(0, figure),
            region(1, Rect::new(90.0, 290.0, 215.0, 330.0)),
        ];
        apply_coverage_fallback(&mut regions, &ir, 0, 0.005);
        assert_eq!(regions[0].bbox, figure);
    }

    #[test]
    fn panel_adoption_stops_at_a_thick_ink_band() {
        // 框外文字离框太远（超过面版字形高度的 2.5 倍） → 不是贴边标题，不扩框。
        let mut inside = glyph_row(0, "axis labels", 100.0, 300.0, 5.0, 8.0);
        inside.extend(glyph_row(20, "more ink", 100.0, 320.0, 5.0, 8.0));
        let mut all = inside.clone();
        all.extend(glyph_row(40, "Far away", 120.0, 500.0, 5.0, 8.0));
        let figure = Rect::new(95.0, 295.0, 210.0, 405.0);
        let ir = FormPage::new()
            .form_with_paint("Im12", all, Rect::new(95.0, 295.0, 210.0, 405.0))
            .build();
        let mut regions = vec![figure_region(0, figure)];
        apply_coverage_fallback(&mut regions, &ir, 0, 0.005);
        assert_eq!(regions[0].bbox, figure, "远处文字必须留给正文/缺口判定");
    }

    #[test]
    fn panel_adoption_requires_a_horizontal_fit() {
        // 框外文字横向超出面版宽度（侧栏注记/邻栏正文） → 不扩框。
        let mut inside = glyph_row(0, "axis labels", 100.0, 300.0, 5.0, 8.0);
        inside.extend(glyph_row(20, "more ink", 100.0, 320.0, 5.0, 8.0));
        let mut all = inside.clone();
        all.extend(glyph_row(40, "Wide aside", 80.0, 410.0, 20.0, 8.0));
        let figure = Rect::new(95.0, 295.0, 210.0, 405.0);
        let ir = FormPage::new()
            .form_with_paint("Im12", all, Rect::new(95.0, 295.0, 210.0, 405.0))
            .build();
        let mut regions = vec![figure_region(0, figure)];
        apply_coverage_fallback(&mut regions, &ir, 0, 0.005);
        assert_eq!(regions[0].bbox, figure);
    }

    #[test]
    fn panel_adoption_rejects_foreign_paint_in_the_added_band() {
        // 新增带里有别的绘图（另一个 Form 的图） → 不扩框，避免吞邻图。
        let mut inside = glyph_row(0, "axis labels", 100.0, 300.0, 5.0, 8.0);
        inside.extend(glyph_row(20, "more ink", 100.0, 320.0, 5.0, 8.0));
        let mut all = inside.clone();
        all.extend(glyph_row(40, "Panel Title", 120.0, 410.0, 5.0, 8.0));
        let figure = Rect::new(95.0, 295.0, 210.0, 405.0);
        let ir = FormPage::new()
            .form_with_paint("Im12", all, Rect::new(95.0, 295.0, 210.0, 405.0))
            .form_with_paint("Im13", vec![], Rect::new(100.0, 407.0, 200.0, 415.0))
            .build();
        let mut regions = vec![figure_region(0, figure)];
        apply_coverage_fallback(&mut regions, &ir, 0, 0.005);
        assert_eq!(regions[0].bbox, figure);
    }

    #[test]
    fn panel_recovery_keeps_unattributable_ink_reported() {
        // 门禁的对外语义不变：没有证据归属时，比例仍超限且区域一字不改。
        let mut inside = glyph_row(0, "axis labels", 100.0, 300.0, 5.0, 8.0);
        inside.extend(glyph_row(20, "more ink", 100.0, 320.0, 5.0, 8.0));
        let figure = Rect::new(95.0, 295.0, 210.0, 405.0);
        let ir = FormPage::new()
            .form_with_paint("Im12", inside, Rect::new(95.0, 295.0, 210.0, 405.0))
            .page_text(glyph_row(40, "Unproven", 120.0, 410.0, 5.0, 8.0))
            .build();
        let mut regions = vec![figure_region(0, figure)];
        let report = apply_coverage_fallback(&mut regions, &ir, 0, 0.005);
        assert!(report.ratio > 0.005, "{} 应超过门禁", report.ratio);
        assert_eq!(regions.len(), 1);
        assert_eq!(regions[0].bbox, figure);
    }

    /// 真实首轮全文页：`tmp/paper-iteration/deepseek-inventory-v1`。
    /// 36/50 页的图内标题/图例被检测框切掉，应恢复归属而不是报覆盖缺口；
    /// 其余页面不得变更。
    #[test]
    #[ignore = "manual: set SYNCPDF_FIGURE_INVENTORY to the immutable inventory directory"]
    fn deepseek_panel_boundary_recovery_on_real_inventory() {
        let root = std::path::PathBuf::from(
            std::env::var_os("SYNCPDF_FIGURE_INVENTORY").expect("SYNCPDF_FIGURE_INVENTORY"),
        );
        let mut pages: Vec<PageIR> =
            serde_json::from_slice(&std::fs::read(root.join("source.json")).unwrap()).unwrap();
        pages.sort_by_key(|ir| ir.page.0);
        let mut recovered = Vec::new();
        let mut touched = Vec::new();
        let mut gaps = Vec::new();
        for ir in &pages {
            let name = format!("regions-{}.json", ir.page.0);
            let cached: Vec<Region> =
                serde_json::from_slice(&std::fs::read(root.join(&name)).unwrap()).unwrap();
            // 缓存里的检测结果直接来自真实运行；这里只跑覆盖率阶段，不重跑模型。
            let mut work = cached.clone();
            let before_uncovered = uncovered_count(ir, &cached);
            let report = apply_coverage_fallback(&mut work, ir, ir.page.0, 0.005);
            assert_eq!(
                work.iter().filter(|r| r.kind == RegionKind::Figure).count(),
                cached
                    .iter()
                    .filter(|r| r.kind == RegionKind::Figure)
                    .count(),
                "第 {} 页：只改图框几何，不改区域数量",
                ir.page.number()
            );
            let moved: Vec<u32> = cached
                .iter()
                .zip(&work)
                .filter(|(a, b)| a.bbox != b.bbox)
                .map(|(a, _)| a.index)
                .collect();
            if !moved.is_empty() {
                touched.push((ir.page.number(), moved));
            }
            // 图内文字不得变成可译正文，也不得改变任何字形的可译归属。
            let before = crate::stages::analyze_page(ir, &cached);
            let after = crate::stages::analyze_page(ir, &work);
            let translatable =
                |ps: &[syncpdf_core::ir::Paragraph]| -> std::collections::BTreeSet<_> {
                    ps.iter()
                        .filter(|p| matches!(p.translatable, syncpdf_core::ir::Translatable::Yes))
                        .flat_map(|p| p.glyphs.iter().copied())
                        .collect()
                };
            assert_eq!(
                translatable(&before),
                translatable(&after),
                "第 {} 页：可译字形集合不得变化（图内文字不进正文）",
                ir.page.number()
            );
            assert_eq!(
                before.len(),
                after.len(),
                "第 {} 页：段落数量不得变化",
                ir.page.number()
            );
            if before_uncovered > 0 {
                recovered.push((
                    ir.page.number(),
                    before_uncovered,
                    uncovered_count(ir, &work),
                ));
                // 被恢复的字形必须落在 Figure 区域内（即保持保护），不是被当成正文收走。
                for g in ir
                    .glyphs()
                    .filter(|g| !g.flags.invisible && !g.flags.outside_clip && !is_white_glyph(g))
                {
                    let center = g.bbox.center();
                    let was_open = !cached.iter().any(|r| r.bbox.contains(center));
                    let now_covered = work.iter().any(|r| r.bbox.contains(center));
                    if was_open && now_covered {
                        assert!(
                            work.iter().any(|r| {
                                r.kind == RegionKind::Figure && r.bbox.contains(center)
                            }),
                            "第 {} 页：恢复的图内文字必须归属 Figure，不得变成可译正文",
                            ir.page.number()
                        );
                    }
                }
                if report.ratio > 0.005 {
                    gaps.push(ir.page.number());
                }
            }
        }
        eprintln!("recovered={recovered:?} touched={touched:?} remaining_gaps={gaps:?}");
        assert_eq!(
            recovered,
            vec![(35, 1, 1), (36, 33, 0), (50, 24, 0)],
            "两处图内标题/图例应被唯一归属；第 35 页单个游离字形仍留在缺口统计里"
        );
        assert_eq!(
            touched,
            vec![(36, vec![0]), (50, vec![0])],
            "只有 36/50 页的 Figure 框被扩，其余页面不得变更"
        );
        assert!(gaps.is_empty(), "恢复后不应再报覆盖缺口：{gaps:?}");
    }

    /// 任意真实 inventory 上的通用不变量：面版归属规则只可能把 Figure 框扩大，
    /// 不新增/删除区域、不改可译段落、不制造新的未覆盖字形。
    /// 用于在第二篇真实论文上证明本规则不会因图纸差异误扩。
    #[test]
    #[ignore = "manual: set SYNCPDF_FIGURE_INVENTORY to the immutable inventory directory"]
    fn panel_recovery_invariants_on_any_real_inventory() {
        let root = std::path::PathBuf::from(
            std::env::var_os("SYNCPDF_FIGURE_INVENTORY").expect("SYNCPDF_FIGURE_INVENTORY"),
        );
        let pages: Vec<PageIR> =
            serde_json::from_slice(&std::fs::read(root.join("source.json")).unwrap()).unwrap();
        let mut expanded = Vec::new();
        for ir in &pages {
            let regions: Vec<Region> = serde_json::from_slice(
                &std::fs::read(root.join(format!("regions-{}.json", ir.page.0))).unwrap(),
            )
            .unwrap();
            // 只跑本规则，与既有 Caption/续行修复解耦，不把它们的旧行为算进来。
            let mut work = regions.clone();
            let changed = recover_form_panel_ink(&mut work, ir);
            assert_eq!(
                work.iter().map(|r| r.index).collect::<Vec<_>>(),
                regions.iter().map(|r| r.index).collect::<Vec<_>>(),
                "第 {} 页：不得新增/删除区域",
                ir.page.number()
            );
            let before = crate::stages::analyze_page(ir, &regions);
            let after = crate::stages::analyze_page(ir, &work);
            assert_eq!(
                before.len(),
                after.len(),
                "第 {} 页：段落数量不得变化",
                ir.page.number()
            );
            let translatable = |ps: &[syncpdf_core::ir::Paragraph]| {
                let mut ids: Vec<_> = ps
                    .iter()
                    .filter(|p| matches!(p.translatable, syncpdf_core::ir::Translatable::Yes))
                    .flat_map(|p| p.glyphs.iter().copied())
                    .collect();
                ids.sort();
                ids
            };
            assert_eq!(
                translatable(&before),
                translatable(&after),
                "第 {} 页：可译字形集合不得变化",
                ir.page.number()
            );
            assert!(
                uncovered_count(ir, &work) <= uncovered_count(ir, &regions),
                "第 {} 页：恢复不得制造新的未覆盖字形",
                ir.page.number()
            );
            let mut count = 0;
            for (old, new) in regions.iter().zip(&work) {
                if old.bbox == new.bbox {
                    continue;
                }
                assert_eq!(
                    old.kind,
                    RegionKind::Figure,
                    "第 {} 页：只能改 Figure",
                    ir.page.number()
                );
                assert!(
                    new.bbox.x0 <= old.bbox.x0
                        && new.bbox.y0 <= old.bbox.y0
                        && new.bbox.x1 >= old.bbox.x1
                        && new.bbox.y1 >= old.bbox.y1,
                    "第 {} 页：Figure 框只可扩大，不可缩小或平移",
                    ir.page.number()
                );
                count += 1;
            }
            assert_eq!(count, changed, "报告数应与实际改动一致");
            if count > 0 {
                expanded.push(ir.page.number());
            }
        }
        eprintln!("expanded_figures_on_pages={expanded:?}");
    }

    /// 某页未被任何区域覆盖的非白字形数（与覆盖率门禁同口径）。
    fn uncovered_count(ir: &PageIR, regions: &[Region]) -> u32 {
        let boxes: Vec<Rect> = regions.iter().map(|r| r.bbox).collect();
        let glyph_boxes: Vec<(Rect, bool)> = ir
            .glyphs()
            .filter(|g| !g.flags.invisible && !g.flags.outside_clip)
            .map(|g| (g.bbox, is_white_glyph(g)))
            .collect();
        coverage(&glyph_boxes, &boxes).uncovered
    }

    #[test]
    fn recover_only_labeled_subcaption_outside_a_detected_panel() {
        let mut glyphs: Vec<_> = "(a) Two tasks"
            .chars()
            .enumerate()
            .map(|(i, c)| {
                mk_glyph(
                    i as u16,
                    &c.to_string(),
                    120.0 + i as f32 * 5.0,
                    410.0,
                    5.0,
                    8.0,
                    GlyphFlags::default(),
                )
            })
            .collect();
        glyphs.push(mk_glyph(
            99,
            "PlotLabel",
            130.0,
            380.0,
            45.0,
            8.0,
            GlyphFlags::default(),
        ));
        glyphs.push(mk_glyph(
            100,
            "Author",
            120.0,
            520.0,
            45.0,
            8.0,
            GlyphFlags::default(),
        ));
        let ir = page_ir(glyphs);
        let mut panel = region(0, Rect::new(100.0, 300.0, 200.0, 400.0));
        panel.kind = RegionKind::Figure;
        let mut regions = vec![panel.clone()];
        recover_subcaptions(&mut regions, &ir);
        assert_eq!(regions.len(), 2);
        assert_eq!(regions[0], panel);
        assert_eq!(regions[1].kind, RegionKind::Caption);
        assert_eq!(regions[1].bbox, Rect::new(100.0, 410.0, 200.0, 418.0));
        assert!(!regions[1]
            .bbox
            .contains(syncpdf_core::Point::new(140.0, 384.0)));
        assert!(!regions[1]
            .bbox
            .contains(syncpdf_core::Point::new(140.0, 524.0)));
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
