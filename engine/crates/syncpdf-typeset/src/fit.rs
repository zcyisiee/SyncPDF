//! fit 阶梯与加宽调度。设计基准：02-技术路径与架构.md §8.2 步骤 4-6；
//! typeset.md「fit.rs：阶梯搜索」。
//!
//! 单一实现，无快路（规约 #10）：
//! scale 从 1.0 按 step 递减到 min_scale，每个 scale 依次试 line_height_steps；
//! 首个「行数 × 行高 <= 框高」的档位胜出；都不行 → 加宽再试；
//! 仍不行 → 允许溢出 allow_overflow_lines 行并记 Overflow；最终回到 min_scale 档。

use crate::breaks::Lang;
use crate::layout::{self, BreaksCache, LayoutInput, LayoutOut};
use crate::shaper::{Shaper, StyleSpec};
use crate::widen;
use syncpdf_core::ir::Align;
use syncpdf_core::{AtomId, Color, ParagraphId, Rect, StyleId};

/// fit 阶梯选项。
#[derive(Debug, Clone, PartialEq)]
pub struct FitOptions {
    /// 最小字号缩放（默认 0.6）。
    pub min_scale: f32,
    /// 缩放步长（默认 0.05）。
    pub scale_step: f32,
    /// 行距倍数阶梯（默认 `[1.0, 0.95, 0.9]`）。
    pub line_height_steps: Vec<f32>,
    /// 允许溢出的行数（默认 1）。
    pub allow_overflow_lines: u32,
}

impl Default for FitOptions {
    fn default() -> Self {
        Self {
            min_scale: 0.6,
            scale_step: 0.05,
            line_height_steps: vec![1.0, 0.95, 0.9],
            allow_overflow_lines: 1,
        }
    }
}

/// 障碍与邻居。
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Obstacles {
    /// 图像 / 路径 / 其他不可侵入区域。
    pub rects: Vec<Rect>,
    /// 同列相邻文本区域，用于加宽上限。
    pub neighbors: Vec<Rect>,
}

/// 排版问题。
#[derive(Debug, Clone, PartialEq)]
pub enum TypesetIssue {
    /// 溢出 `lines` 行（已按最小档放置）。
    Overflow { lines: u32 },
    /// 字号缩到 min_scale 仍未完整放下（无溢出配置时的兜底）。
    MinScaleHit,
    /// 加宽生效，`by` 为扩展量（pt，总高度增量）。
    Widened { by: f32 },
}

/// 排版结果。
#[derive(Debug, Clone, PartialEq)]
pub struct TypesetResult {
    pub paragraph: syncpdf_core::ir::TypesetParagraph,
    pub scale: f32,
    pub widened: Option<Rect>,
    pub issues: Vec<TypesetIssue>,
}

/// 段落规格（brief 交付物签名）。
#[derive(Debug, Clone, PartialEq)]
pub struct ParagraphSpec {
    pub bbox: Rect,
    pub font_size: f32,
    /// 行距倍数（相对字号）。
    pub line_height: f32,
    pub align: Align,
    pub first_indent: f32,
    pub is_rtl: bool,
    pub color: Color,
    pub styles: Vec<(StyleId, StyleSpec)>,
    pub lang: Lang,
}

/// 段内联内容（brief：解耦 ParsedUnit）。
#[derive(Debug, Clone, PartialEq)]
pub enum Inline {
    /// 纯文本 + 样式。
    Text { text: String, style: StyleId },
    /// 原子占位：按原字形几何原地保留，不塑形。width/height 为 pt。
    Atom { id: AtomId, width: f32, height: f32 },
    /// 硬换行。
    Br,
}

/// 排版入口。
pub struct Typeset<'a> {
    shaper: &'a dyn Shaper,
    opts: FitOptions,
}

impl std::fmt::Debug for Typeset<'_> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Typeset")
            .field("shaper", &"<dyn Shaper>")
            .field("opts", &self.opts)
            .finish()
    }
}

impl<'a> Typeset<'a> {
    pub fn new(shaper: &'a dyn Shaper, opts: FitOptions) -> Self {
        Self { shaper, opts }
    }

    /// 排进段落框。见模块文档的阶梯顺序。
    pub fn layout(
        &self,
        id: ParagraphId,
        spec: &ParagraphSpec,
        inlines: &[Inline],
        obstacles: &Obstacles,
    ) -> TypesetResult {
        let input = LayoutInput {
            id,
            font_size: spec.font_size,
            align: spec.align,
            first_indent: spec.first_indent,
            is_rtl: spec.is_rtl,
            color: spec.color,
            lang: spec.lang,
            styles: &spec.styles,
        };
        let lh = spec.line_height;
        let cache = BreaksCache::new();

        // 1. 原框阶梯。
        if let Some(out) = self.try_ladder(&input, inlines, &spec.bbox, lh, &cache) {
            return self.finish(out, None, Vec::new());
        }

        // 2. 加宽后再走一遍阶梯。
        if let Some(widened_rect) = widen::widen(&spec.bbox, &obstacles.neighbors, &obstacles.rects)
        {
            if let Some(out) = self.try_ladder(&input, inlines, &widened_rect, lh, &cache) {
                let by = widened_rect.union(&spec.bbox).height() - spec.bbox.height();
                return self.finish(out, Some(widened_rect), vec![TypesetIssue::Widened { by }]);
            }
        }

        // 3. 溢出：min_scale 档、阶梯内行距，报告溢出行数。
        let (out, mut issues) = self.overflow_layout(&input, inlines, &spec.bbox, lh, &cache);
        issues.push(TypesetIssue::MinScaleHit);
        self.finish(out, None, issues)
    }

    /// 与 layout::layout 相同的容量公式：首行占 ascent+descent，其后每行 line_h。
    fn capacity(&self, bbox: &Rect, size: f32, line_height_mult: f32) -> f32 {
        let m = self.shaper.metrics(0);
        let first = (m.ascent + m.descent) * size;
        let line_h = size * line_height_mult;
        if line_h <= f32::EPSILON || bbox.height() + 1e-3 < first {
            0.0
        } else {
            1.0 + ((bbox.height() - first) / line_h).floor()
        }
    }

    /// 阶梯搜索：返回首个放得下的档位。
    fn try_ladder(
        &self,
        input: &LayoutInput<'_>,
        inlines: &[Inline],
        bbox: &Rect,
        lh: f32,
        cache: &BreaksCache,
    ) -> Option<LayoutOut> {
        let mut scale = 1.0f32;
        loop {
            // 行数只取决于宽度与字号，与行距无关：同一 scale 下只排一次，
            // 其余行距档先用容量公式判定，命中才真正重排（结果与逐档相同）。
            let mut first_out: Option<LayoutOut> = None;
            for (k, &lh_step) in self.opts.line_height_steps.iter().enumerate() {
                if k == 0 {
                    let out = layout::layout(
                        self.shaper,
                        input,
                        inlines,
                        bbox,
                        scale,
                        lh * lh_step,
                        cache,
                    );
                    if !out.overflowed {
                        return Some(out);
                    }
                    first_out = Some(out);
                    continue;
                }
                let lines = first_out.as_ref().map_or(0, |o| o.lines);
                if lines as f32 > self.capacity(bbox, input.font_size * scale, lh * lh_step) {
                    continue;
                }
                let out = layout::layout(
                    self.shaper,
                    input,
                    inlines,
                    bbox,
                    scale,
                    lh * lh_step,
                    cache,
                );
                if !out.overflowed {
                    return Some(out);
                }
            }
            if scale <= self.opts.min_scale + 1e-6 {
                return None;
            }
            scale = (scale - self.opts.scale_step).max(self.opts.min_scale);
        }
    }

    /// 溢出模式：min_scale、选行数最少的行距档；报告溢出行数。
    fn overflow_layout(
        &self,
        input: &LayoutInput<'_>,
        inlines: &[Inline],
        bbox: &Rect,
        lh: f32,
        cache: &BreaksCache,
    ) -> (LayoutOut, Vec<TypesetIssue>) {
        let scale = self.opts.min_scale;
        // 行数与行距无关：最紧的行距档（最小值）溢出行数最少，直接选它。
        let lh_step = self
            .opts
            .line_height_steps
            .iter()
            .copied()
            .fold(f32::INFINITY, f32::min);
        let lh_step = if lh_step.is_finite() { lh_step } else { 1.0 };
        let out = layout::layout(
            self.shaper,
            input,
            inlines,
            bbox,
            scale,
            lh * lh_step,
            cache,
        );
        let capacity = out.line_capacity;
        let over = ((out.lines as f32 - capacity).ceil().max(0.0)) as u32;
        let mut issues = Vec::new();
        if over > 0 {
            issues.push(TypesetIssue::Overflow { lines: over });
        }
        (out, issues)
    }

    fn finish(
        &self,
        out: LayoutOut,
        widened: Option<Rect>,
        issues: Vec<TypesetIssue>,
    ) -> TypesetResult {
        TypesetResult {
            paragraph: out.paragraph,
            scale: out.scale,
            widened,
            issues,
        }
    }
}
