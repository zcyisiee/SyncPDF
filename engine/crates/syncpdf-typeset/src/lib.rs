//! `syncpdf-typeset`：排版引擎。
//!
//! 设计基准：docs/reports/2026-09-22-rust-electron-rewrite/02-技术路径与架构.md §8。
//! 把已校验的译文（`Inline` 形态：文本 / 样式 / 原子 / 硬换行）排进段落框：
//! 断行（icu_segmenter Strict + hypher 软断点）、fit 阶梯（字号 × 行距）、
//! 两端对齐、首行缩进、邻接间隙加宽、溢出策略；输出
//! `syncpdf_core::ir::TypesetParagraph`（逐字形位置）。
//!
//! 不依赖 `syncpdf-font`（并行开发）：塑形通过 [`shaper::Shaper`] 抽象注入，
//! 测试用 [`shaper::MonoShaper`]。
#![forbid(unsafe_code)]
#![warn(missing_debug_implementations, rust_2018_idioms)]

pub mod breaks;
pub mod fit;
pub mod knuth_plass;
pub mod layout;
pub mod shaper;
pub mod widen;

pub use breaks::{break_opportunities, BreakOpp, Lang};
pub use fit::{FitOptions, Inline, Obstacles, ParagraphSpec, Typeset, TypesetIssue, TypesetResult};
pub use shaper::{FontMetrics, ShapedGlyph, Shaper, StyleSpec};
