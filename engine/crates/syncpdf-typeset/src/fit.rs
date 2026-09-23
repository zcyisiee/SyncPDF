//! 容纳判断与邻接空隙扩框。默认保持请求字号与行距，无法容纳时返回 Overflow。
//! 显式传入较小 min_scale 的库调用仍可使用阶梯，但生产默认不自动缩字号。

use crate::breaks::Lang;
use crate::layout::{self, BreaksCache, LayoutInput, LayoutOut};
use crate::shaper::{Shaper, StyleSpec};
use crate::widen;
use syncpdf_core::ir::Align;
use syncpdf_core::{AtomId, Color, ParagraphId, Rect, StyleId};

/// fit 阶梯选项。
#[derive(Debug, Clone, PartialEq)]
pub struct FitOptions {
    /// 最小字号缩放（默认1.0：保持请求字号）。
    pub min_scale: f32,
    /// 缩放步长（默认 0.05）。
    pub scale_step: f32,
    /// 行距倍数阶梯（默认 `[1.0]`：保持请求行距）。
    pub line_height_steps: Vec<f32>,
    /// 兼容字段；溢出始终报告，不作为成功容纳（默认0）。
    pub allow_overflow_lines: u32,
}

impl Default for FitOptions {
    fn default() -> Self {
        Self {
            min_scale: 1.0,
            scale_step: 0.05,
            line_height_steps: vec![1.0],
            allow_overflow_lines: 0,
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
    /// 溢出 `lines` 行；默认仍保持请求字号。
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
    /// Stable source/requested first baseline; None anchors using glyph metrics.
    pub first_baseline: Option<f32>,
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
    /// A source PDF drawing placed with its original baseline and dimensions.
    SourceAtom {
        id: AtomId,
        source: syncpdf_core::ir::SourceAtom,
    },
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
            first_baseline: spec.first_baseline,
            align: spec.align,
            first_indent: spec.first_indent,
            is_rtl: spec.is_rtl,
            color: spec.color,
            lang: spec.lang,
            styles: &spec.styles,
        };
        let lh = spec.line_height;
        let cache = BreaksCache::new();

        // 1. 原框阶梯。保留最终失败档，避免为报告 Overflow 重排同一段。
        let mut failed_out = None;
        if let Some(out) = self.try_ladder(&input, inlines, &spec.bbox, lh, &cache, &mut failed_out)
        {
            return self.finish(out, None, Vec::new());
        }

        // 2. 加宽后再走一遍阶梯。
        if spec.first_baseline.is_none() {
            if let Some(widened_rect) =
                widen::widen(&spec.bbox, &obstacles.neighbors, &obstacles.rects)
            {
                if let Some(out) =
                    self.try_ladder(&input, inlines, &widened_rect, lh, &cache, &mut None)
                {
                    let by = widened_rect.union(&spec.bbox).height() - spec.bbox.height();
                    return self.finish(
                        out,
                        Some(widened_rect),
                        vec![TypesetIssue::Widened { by }],
                    );
                }
            }
        }

        // 3. 溢出：min_scale 档、阶梯内行距，报告溢出行数。
        let (out, mut issues) =
            self.overflow_layout(&input, inlines, &spec.bbox, lh, &cache, failed_out);
        if self.opts.min_scale < 1.0 {
            issues.push(TypesetIssue::MinScaleHit);
        }
        self.finish(out, None, issues)
    }

    /// 阶梯搜索：返回首个放得下的档位。
    fn try_ladder(
        &self,
        input: &LayoutInput<'_>,
        inlines: &[Inline],
        bbox: &Rect,
        lh: f32,
        cache: &BreaksCache,
        failed_out: &mut Option<LayoutOut>,
    ) -> Option<LayoutOut> {
        let mut scale = 1.0f32;
        loop {
            for &lh_step in &self.opts.line_height_steps {
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
                if scale <= self.opts.min_scale + 1e-6
                    && failed_out
                        .as_ref()
                        .is_none_or(|saved| out.paragraph.line_height < saved.paragraph.line_height)
                {
                    *failed_out = Some(out);
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
        failed_out: Option<LayoutOut>,
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
        let out = failed_out.unwrap_or_else(|| {
            layout::layout(
                self.shaper,
                input,
                inlines,
                bbox,
                scale,
                lh * lh_step,
                cache,
            )
        });
        let capacity = out.line_capacity;
        let over = ((out.lines as f32 - capacity).ceil().max(0.0)) as u32;
        let mut issues = Vec::new();
        if out.overflowed {
            issues.push(TypesetIssue::Overflow { lines: over.max(1) });
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
