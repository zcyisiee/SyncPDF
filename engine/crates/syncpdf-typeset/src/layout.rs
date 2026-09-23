//! 单档排版：断行、放置、对齐。设计基准：02-技术路径与架构.md §8.2。
//!
//! 给定 (scale, line_height) 的一档，把 inlines 排进 bbox：
//! 1. Text run 塑形为字形流（bidi 视觉重排）；Atom 作为不可断整体；Br 强制换行。
//! 2. 依据 break_opportunities 贪心断行（first_indent 收窄首行可用宽）。
//! 3. 行从框顶向下放：首行基线 y = bbox.y1 - ascent·size。
//! 4. 对齐：Justify 拉丁词间空格拉伸 / CJK 字距均分（标点不参与），末行左对齐；
//!    Center / Right 平移；Left 不动。

use crate::breaks::{break_opportunities, is_forbidden_line_end, is_forbidden_line_start, Lang};
use crate::fit::Inline;
use crate::shaper::{is_cjk_char, FontMetrics, ShapedGlyph, Shaper, StyleSpec};
use syncpdf_core::ir::Align;
use syncpdf_core::ir::{LineBox, PlacedGlyph, TypesetParagraph};
use syncpdf_core::{AtomId, Color, ParagraphId, Rect, StyleId};
use unicode_bidi::BidiInfo;

/// 排版输入（不随档位变化的参数）。
pub(crate) struct LayoutInput<'a> {
    pub id: ParagraphId,
    pub font_size: f32,
    pub align: Align,
    /// 首行缩进（pt，正值为右侧缩进）。
    pub first_indent: f32,
    pub is_rtl: bool,
    pub color: Color,
    pub lang: Lang,
    pub styles: &'a [(StyleId, StyleSpec)],
}

/// 单档排版输出。
pub(crate) struct LayoutOut {
    pub paragraph: TypesetParagraph,
    pub scale: f32,
    pub lines: usize,
    /// 框能容纳的行数（首行含 descent 之后的整数容量）。
    pub line_capacity: f32,
    pub overflowed: bool,
}

/// 字形槽：断行与放置的最小单位。
#[derive(Debug, Clone)]
enum Item {
    Glyph {
        glyph: ShapedGlyph,
        text: char,
        size: f32,
        font: u32,
        style: StyleId,
        is_space: bool,
        is_cjk: bool,
        is_cjk_punct: bool,
    },
    /// 原子占位：不可断整体，宽度固定。
    Atom { id: AtomId, width: f32, height: f32 },
}

impl Item {
    fn width(&self) -> f32 {
        match self {
            Item::Glyph { glyph, .. } => glyph.x_advance,
            Item::Atom { width, .. } => *width,
        }
    }

    fn is_space(&self) -> bool {
        matches!(self, Item::Glyph { is_space: true, .. })
    }

    fn is_glyph(&self) -> bool {
        matches!(self, Item::Glyph { .. })
    }
}

/// 同一段落多档尝试之间复用的中间结果：
/// - `items_base`：按 `input.font_size`（scale = 1）塑形的 items；塑形结果对字号线性，
///   其他档位按 scale 等比缩放即可得到与直接塑形相同的结果；
/// - `breaks`：断点只取决于文本与 items 下标，与字号/行距无关。
#[derive(Default)]
pub(crate) struct BreaksCache {
    items_base: std::cell::OnceCell<Vec<Item>>,
    breaks: std::cell::OnceCell<Vec<BreakAt>>,
    font_metrics: std::cell::OnceCell<FontMetrics>,
}

impl BreaksCache {
    pub(crate) fn new() -> Self {
        Self::default()
    }

    /// 容量与基线使用实际段内样式选择的字体，不依赖字体表的加载序号。
    pub(crate) fn metrics(
        &self,
        shaper: &dyn Shaper,
        input: &LayoutInput<'_>,
        inlines: &[Inline],
    ) -> FontMetrics {
        *self.font_metrics.get_or_init(|| {
            let mut metrics = FontMetrics {
                ascent: 0.0,
                descent: 0.0,
            };
            for inline in inlines {
                if let Inline::Text { text, style } = inline {
                    if text.is_empty() {
                        continue;
                    }
                    let spec = input
                        .styles
                        .iter()
                        .find(|(id, _)| id == style)
                        .map(|(_, spec)| *spec)
                        .unwrap_or_default();
                    let font = spec.font.unwrap_or_else(|| shaper.font_for(&spec));
                    let size = spec.size.unwrap_or(input.font_size);
                    let m = shaper.metrics(font);
                    metrics.ascent = metrics.ascent.max(m.ascent * size / input.font_size);
                    metrics.descent = metrics.descent.max(m.descent * size / input.font_size);
                }
            }
            if metrics.ascent + metrics.descent == 0.0 {
                return shaper.metrics(shaper.font_for(&StyleSpec::default()));
            }
            metrics
        })
    }
}

fn scaled_items(base: &[Item], scale: f32) -> Vec<Item> {
    if (scale - 1.0).abs() < 1e-6 {
        return base.to_vec();
    }
    base.iter()
        .map(|it| match it {
            Item::Glyph {
                glyph,
                text,
                size,
                font,
                style,
                is_space,
                is_cjk,
                is_cjk_punct,
            } => Item::Glyph {
                glyph: ShapedGlyph {
                    x_advance: glyph.x_advance * scale,
                    x_offset: glyph.x_offset * scale,
                    y_offset: glyph.y_offset * scale,
                    ..*glyph
                },
                text: *text,
                size: size * scale,
                font: *font,
                style: *style,
                is_space: *is_space,
                is_cjk: *is_cjk,
                is_cjk_punct: *is_cjk_punct,
            },
            Item::Atom { id, width, height } => Item::Atom {
                id: *id,
                width: *width,
                height: *height,
            },
        })
        .collect()
}

/// 单档排版主函数。
pub(crate) fn layout(
    shaper: &dyn Shaper,
    input: &LayoutInput<'_>,
    inlines: &[Inline],
    bbox: &Rect,
    scale: f32,
    line_height_mult: f32,
    breaks_cache: &BreaksCache,
) -> LayoutOut {
    let profiling = std::env::var("SYNCPDF_TYPESET_PROFILE").is_ok();
    let t_start = std::time::Instant::now();
    let size = input.font_size * scale;
    let metrics = breaks_cache.metrics(shaper, input, inlines);
    let ascent = metrics.ascent * size;
    let descent = metrics.descent * size;
    let line_h = size * line_height_mult;

    // 1. 塑形 + bidi 视觉序。
    let items = scaled_items(
        breaks_cache
            .items_base
            .get_or_init(|| shape_inlines(shaper, input, inlines, input.font_size)),
        scale,
    );

    let t_shape = t_start.elapsed();
    // 2. 断点集合（items 下标）。
    // 断点只取决于文本与 items 下标，与字号/行距无关：跨阶梯档位复用。
    let breaks = breaks_cache
        .breaks
        .get_or_init(|| compute_breaks(input, inlines, &items));
    let t_breaks = t_start.elapsed();

    // 3. 贪心断行。
    let lines = break_lines(&items, breaks, input, bbox.width(), size);
    let t_lines = t_start.elapsed();

    // 4. 容量：首行占 ascent+descent，其后每行 line_h。
    let capacity = if line_h <= f32::EPSILON {
        0.0
    } else {
        let first = ascent + descent;
        if bbox.height() + 1e-3 < first {
            0.0
        } else {
            1.0 + ((bbox.height() - first) / line_h).floor()
        }
    };

    // 5. 放置。
    let n = lines.len();
    let mut line_boxes: Vec<LineBox> = Vec::with_capacity(n);
    let mut used = if n == 0 {
        *bbox
    } else {
        Rect::new(bbox.x1, bbox.y1, bbox.x0, bbox.y0)
    };
    for (i, line) in lines.iter().enumerate() {
        let baseline_y = bbox.y1 - ascent - (i as f32) * line_h;
        let lb = place_line(line, input, bbox, baseline_y, i + 1 == n, ascent, descent);
        used = used.union(&lb.bbox);
        line_boxes.push(lb);
    }

    let overflowed = (n as f32) > capacity + 1e-4;

    if profiling {
        eprintln!(
            "layout: shape={:?} breaks={:?} lines={:?} place={:?} total={:?} n_items={}",
            t_shape,
            t_breaks - t_shape,
            t_lines - t_breaks,
            t_start.elapsed() - t_lines,
            t_start.elapsed(),
            items.len()
        );
    }
    LayoutOut {
        paragraph: TypesetParagraph {
            id: input.id.clone(),
            lines: line_boxes,
            font_scale: scale,
            line_height: line_h,
            color: input.color,
            used_bbox: used,
            overflow: overflowed,
        },
        scale,
        lines: n,
        line_capacity: capacity,
        overflowed,
    }
}

/// 塑形全部 inlines，产出视觉序字形槽流。
fn shape_inlines(
    shaper: &dyn Shaper,
    input: &LayoutInput<'_>,
    inlines: &[Inline],
    size: f32,
) -> Vec<Item> {
    let mut items: Vec<Item> = Vec::new();
    let spec_of = |sid: StyleId| -> StyleSpec {
        input
            .styles
            .iter()
            .find(|(id, _)| *id == sid)
            .map(|(_, s)| *s)
            .unwrap_or_default()
    };

    for inline in inlines {
        match inline {
            // Br 不产生 item；断点在 compute_breaks 以 mandatory 记录。
            Inline::Br => {}
            Inline::Atom { id, width, height } => {
                items.push(Item::Atom {
                    id: *id,
                    width: *width,
                    height: *height,
                });
            }
            Inline::Text { text, style } => {
                let spec = spec_of(*style);
                let font = spec.font.unwrap_or_else(|| shaper.font_for(&spec));
                let size = spec.size.unwrap_or(size);
                for chunk in reorder(text, input.is_rtl) {
                    let glyphs = shaper.shape(font, &chunk.text, size, chunk.is_rtl);
                    // cluster → 字符映射（cluster 为 chunk 内字节偏移）。
                    let mut chars: Vec<char> = chunk.text.chars().collect();
                    let mut byte_pos: Vec<usize> = {
                        let mut v = Vec::with_capacity(chars.len() + 1);
                        let mut b = 0;
                        v.push(0);
                        for c in &chars {
                            b += c.len_utf8();
                            v.push(b);
                        }
                        v
                    };
                    // RTL 视觉序下字符已反转；glyph 顺序与 chunk.text 顺序一致，
                    // 因此直接按 cluster 定位。
                    for g in glyphs {
                        let ci = byte_pos
                            .iter()
                            .position(|&b| b as u32 == g.cluster)
                            .unwrap_or(chars.len().saturating_sub(1));
                        let c = chars.get(ci).copied().unwrap_or(' ');
                        items.push(Item::Glyph {
                            glyph: g,
                            text: c,
                            size,
                            font,
                            style: *style,
                            is_space: c == ' ',
                            is_cjk: is_cjk_char(c),
                            is_cjk_punct: is_forbidden_line_start(c) || is_forbidden_line_end(c),
                        });
                    }
                    let _ = (&mut chars, &mut byte_pos);
                }
            }
        }
    }
    items
}

/// bidi 视觉重排：无 RTL 字符时原样返回。
fn reorder(text: &str, _is_rtl: bool) -> Vec<VisualChunk> {
    let ltr = VisualChunk {
        text: text.to_string(),
        is_rtl: false,
    };
    if !text.chars().any(is_rtl_char) {
        return vec![ltr];
    }
    let bidi = BidiInfo::new(text, None);
    let Some(para) = bidi.paragraphs.first() else {
        return vec![ltr];
    };
    let (levels, runs) = bidi.visual_runs(para, para.range.clone());
    let mut chunks = Vec::with_capacity(runs.len());
    for run in runs {
        if run.is_empty() || !text.is_char_boundary(run.start) || !text.is_char_boundary(run.end) {
            continue;
        }
        let run_rtl = levels.get(run.start).map(|l| l.is_rtl()).unwrap_or(false);
        let mut piece = text[run.start..run.end].to_string();
        if run_rtl {
            piece = piece.chars().rev().collect();
        }
        chunks.push(VisualChunk {
            text: piece,
            is_rtl: run_rtl,
        });
    }
    if chunks.is_empty() {
        vec![ltr]
    } else {
        chunks
    }
}

struct VisualChunk {
    text: String,
    is_rtl: bool,
}

fn is_rtl_char(c: char) -> bool {
    matches!(
        c,
        '\u{0590}'..='\u{05FF}'
            | '\u{0600}'..='\u{06FF}'
            | '\u{0700}'..='\u{074F}'
            | '\u{0750}'..='\u{077F}'
            | '\u{08A0}'..='\u{08FF}'
            | '\u{FB50}'..='\u{FDFF}'
            | '\u{FE70}'..='\u{FEFF}'
    )
}

/// 断点：items 下标 `after` 之后可断。
#[derive(Debug, Clone, Copy)]
pub(crate) struct BreakAt {
    after: usize,
    mandatory: bool,
}

/// 计算断点集合，映射到 items 下标。
///
/// 下标对齐规则：shape_inlines 按视觉序展开 Text（字符集合不变，仅顺序可能变），
/// 断点字节偏移按字符序号对齐视觉序（RTL 段的断行在视觉序上进行，
/// 即先重排、再按视觉序断行 —— 与 hjfy 的 LogicalParagraph→visual 一致）。
fn compute_breaks(input: &LayoutInput<'_>, inlines: &[Inline], items: &[Item]) -> Vec<BreakAt> {
    let mut breaks: Vec<BreakAt> = Vec::new();
    let mut idx = 0usize;

    let mut prev_text_end: Option<usize> = None; // 上一个 Text 结束后的 items 下标
    for inline in inlines {
        match inline {
            Inline::Br => {
                if idx > 0 {
                    breaks.push(BreakAt {
                        after: idx - 1,
                        mandatory: true,
                    });
                }
            }
            Inline::Atom { .. } => {
                // 原子前可断（若前面有内容）。
                if let Some(end) = prev_text_end {
                    if end == idx {
                        breaks.push(BreakAt {
                            after: idx - 1,
                            mandatory: false,
                        });
                    }
                }
                idx += 1;
                breaks.push(BreakAt {
                    after: idx - 1,
                    mandatory: false,
                });
            }
            Inline::Text { text, style: _ } => {
                // 视觉序文本：与 shape_inlines 相同的重排。
                let visual: String = reorder(text, input.is_rtl)
                    .iter()
                    .map(|c| c.text.as_str())
                    .collect();
                let opps = break_opportunities(&visual, input.lang);
                // 字节偏移 → 视觉序字符序号（一次建表，二分查找）。
                let char_starts: Vec<u32> = visual.char_indices().map(|(b, _)| b as u32).collect();
                for opp in opps {
                    let ci = char_starts.partition_point(|&b| b < opp.byte);
                    if ci > 0 {
                        breaks.push(BreakAt {
                            after: idx + ci - 1,
                            mandatory: opp.mandatory,
                        });
                    }
                }
                idx += visual.chars().count();
                prev_text_end = Some(idx);
            }
        }
    }
    // 尾部：items 结束不需要断点。
    breaks.sort_by_key(|b| b.after);
    // 同一下标去重：mandatory 优先（Br 与文本断点重合时按强制换行处理）。
    // dedup_by 的闭包参数 a 为当前待比较元素、b 为已保留元素，无法把 a 的
    // mandatory 转移给 b，故用前向扫描重建。
    let mut merged: Vec<BreakAt> = Vec::with_capacity(breaks.len());
    for b in breaks.drain(..) {
        match merged.last_mut() {
            Some(prev) if prev.after == b.after => prev.mandatory |= b.mandatory,
            _ => merged.push(b),
        }
    }
    let max = items.len().saturating_sub(1);
    merged.retain(|b| b.after <= max);
    merged
}

/// 一行（items 切片 + 首行标记）。
struct Line<'a> {
    items: &'a [Item],
    first: bool,
}

/// 贪心断行（含软断点回退与禁则兜底）。
fn break_lines<'a>(
    items: &'a [Item],
    breaks: &[BreakAt],
    input: &LayoutInput<'_>,
    width: f32,
    size: f32,
) -> Vec<Line<'a>> {
    let mut lines: Vec<Line<'a>> = Vec::new();
    if items.is_empty() {
        return lines;
    }
    let _ = size;
    let indent = input.first_indent.max(0.0);
    // 断点查表：after -> 是否断点 / 是否强制。
    use std::collections::HashMap;
    let mut break_map: HashMap<usize, bool> = HashMap::with_capacity(breaks.len());
    for b in breaks {
        break_map.insert(b.after, b.mandatory);
    }
    let can_break_after = |i: usize| break_map.contains_key(&i);

    let mut start = 0usize;
    let mut first = true;

    'outer: loop {
        let avail = (width - if first { indent } else { 0.0 }).max(1.0);
        let mut w = 0.0f32;
        let mut i = start;
        let mut best_end: Option<usize> = None; // 最后一个可用断点之后的位置
        let mut last_fit = start; // 能放下的最大位置（硬切兜底）

        while i < items.len() {
            let iw = items[i].width();
            if i > start && w + iw > avail + 1e-3 {
                break;
            }
            w += iw;
            if w > avail + 1e-3 {
                // 首个 item 就放不下：仍放入（不可断原子/超长串）。
                last_fit = i + 1;
                i += 1;
                continue;
            }
            last_fit = i + 1;
            if can_break_after(i) {
                best_end = Some(i + 1);
            }
            if break_map.get(&i) == Some(&true) {
                // 强制换行：行止于 i（含）。
                let slice = trim_trailing_spaces(&items[start..i + 1]);
                if !slice.is_empty() || lines.is_empty() {
                    lines.push(Line {
                        items: slice,
                        first,
                    });
                    first = false;
                }
                let mut next = i + 1;
                while next < items.len() && items[next].is_space() {
                    next += 1;
                }
                if next >= items.len() {
                    break 'outer;
                }
                start = next;
                continue 'outer;
            }
            i += 1;
        }

        // 自然结束或宽度耗尽。
        if i >= items.len() {
            // 剩余全部放得下（或无断点直到结尾）。
            if start < items.len() {
                lines.push(Line {
                    items: trim_trailing_spaces(&items[start..]),
                    first,
                });
            }
            break;
        }

        // 宽度耗尽：取最后一个断点。
        let end = match best_end {
            Some(e) if e > start => e,
            _ => last_fit.max(start + 1),
        };
        // 禁则兜底：行首是禁首标点 → 回退一个字符。
        let mut end = end.min(items.len());
        while end > start + 1
            && matches!(&items[end - 1], Item::Glyph { text, .. } if is_forbidden_line_end_inline(*text))
        {
            end -= 1;
        }
        while end < items.len()
            && matches!(&items[end], Item::Glyph { text, .. } if is_forbidden_line_start(*text))
        {
            end += 1;
        }
        lines.push(Line {
            items: trim_trailing_spaces(&items[start..end]),
            first,
        });
        first = false;
        start = end;
        while start < items.len() && items[start].is_space() {
            start += 1;
        }
        if start >= items.len() {
            break;
        }
    }
    lines
}

fn is_forbidden_line_end_inline(c: char) -> bool {
    is_forbidden_line_end(c)
}

fn trim_trailing_spaces(items: &[Item]) -> &[Item] {
    let mut end = items.len();
    while end > 0 && items[end - 1].is_space() {
        end -= 1;
    }
    &items[..end]
}

/// 放置一行：先自然宽排，再按对齐方式调整。
fn place_line(
    line: &Line<'_>,
    input: &LayoutInput<'_>,
    bbox: &Rect,
    baseline_y: f32,
    is_last: bool,
    ascent: f32,
    descent: f32,
) -> LineBox {
    let indent = if line.first { input.first_indent } else { 0.0 };
    let x_start = bbox.x0 + indent;
    let right_edge = bbox.x1;

    // 自然序放置。
    let mut glyphs: Vec<PlacedGlyph> = Vec::new();
    let mut kept_atoms: Vec<AtomId> = Vec::new();
    let mut atom_spans: Vec<(f32, f32, f32)> = Vec::new(); // (x, width, height)
    let mut x = x_start;
    for item in line.items {
        match item {
            Item::Glyph {
                glyph,
                text,
                size,
                font,
                style,
                ..
            } => {
                glyphs.push(PlacedGlyph {
                    font: *font,
                    gid: glyph.gid,
                    text: text.to_string(),
                    x: x + glyph.x_offset,
                    y: baseline_y + glyph.y_offset,
                    size: *size,
                    scale_x: 1.0,
                    style: *style,
                    color: input
                        .styles
                        .iter()
                        .find(|(id, _)| id == style)
                        .and_then(|(_, s)| s.color),
                });
                x += glyph.x_advance;
            }
            Item::Atom {
                id, width, height, ..
            } => {
                atom_spans.push((x, *width, *height));
                kept_atoms.push(*id);
                x += *width;
            }
        }
    }
    let content_w = x - x_start;
    let slack = right_edge - (x_start + content_w);

    match input.align {
        Align::Justify if !is_last && slack > 0.01 => {
            justify_line(&mut glyphs, line, slack);
        }
        Align::Center => {
            let dx = slack / 2.0;
            for g in &mut glyphs {
                g.x += dx;
            }
        }
        Align::Right => {
            for g in &mut glyphs {
                g.x += slack;
            }
        }
        _ => {}
    }

    // 行 bbox。
    let mut b = Rect::new(
        x_start,
        baseline_y - descent,
        x_start + content_w,
        baseline_y + ascent,
    );
    for g in &glyphs {
        b = b.union(&Rect::new(
            g.x,
            g.y - 0.2 * g.size,
            g.x + 0.5 * g.size,
            g.y + 0.8 * g.size,
        ));
    }
    for &(ax, aw, ah) in &atom_spans {
        // 原子按原字形几何原地保留：以基线为底向上排。
        b = b.union(&Rect::new(
            ax,
            baseline_y - descent.min(ah * 0.3),
            ax + aw,
            baseline_y + ah,
        ));
    }

    LineBox {
        bbox: b,
        baseline_y,
        glyphs,
        kept_atoms,
    }
}

/// Justify：拉丁按词间空格拉伸；CJK（无空格）按字距均分，标点不参与。
fn justify_line(glyphs: &mut [PlacedGlyph], line: &Line<'_>, slack: f32) {
    // glyphs 与 line.items 中 Glyph 的出现顺序一一对应。
    let space_glyph_idx: Vec<usize> = line
        .items
        .iter()
        .enumerate()
        .filter(|(_, it)| it.is_space())
        .map(|(i, _)| i)
        .collect();

    // 可分摊的空格数（行首/行尾空格已被 trim，这里全部参与）。
    if !space_glyph_idx.is_empty() {
        let per = slack / space_glyph_idx.len() as f32;
        // glyph 下标 = items 中第 k 个 Glyph。
        let mut glyph_of_item: Vec<Option<usize>> = Vec::with_capacity(line.items.len());
        let mut gi = 0usize;
        for it in line.items.iter() {
            if it.is_glyph() {
                glyph_of_item.push(Some(gi));
                gi += 1;
            } else {
                glyph_of_item.push(None);
            }
        }
        for &item_i in &space_glyph_idx {
            let Some(g) = glyph_of_item[item_i] else {
                continue;
            };
            // 该空格及其后所有字形右移 per。
            for gg in &mut glyphs[g..] {
                gg.x += per;
            }
        }
        return;
    }

    // CJK 字距均分：统计非标点 CJK 锚点，每两个相邻锚点之间插 slack/gaps；
    // 任一字形的位移 = 它前面「已完成的锚点间隙数」× per。
    let anchor_count = line
        .items
        .iter()
        .filter(|it| {
            matches!(
                it,
                Item::Glyph {
                    is_cjk: true,
                    is_cjk_punct: false,
                    ..
                }
            )
        })
        .count();
    if anchor_count < 2 {
        return;
    }
    let gaps = anchor_count - 1;
    let per = slack / gaps as f32;
    let mut anchors_seen = 0usize;
    let mut glyph_i = 0usize; // glyphs 内的当前下标（只数 Glyph item）
    for it in line.items.iter() {
        if !it.is_glyph() {
            continue;
        }
        // 当前字形的位移 = 它前面的锚点间隙数 × per。
        let gaps_before = anchors_seen.min(gaps);
        if let Some(g) = glyphs.get_mut(glyph_i) {
            g.x += gaps_before as f32 * per;
        }
        if matches!(
            it,
            Item::Glyph {
                is_cjk: true,
                is_cjk_punct: false,
                ..
            }
        ) {
            anchors_seen += 1;
        }
        glyph_i += 1;
    }
}
