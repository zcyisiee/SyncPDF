//! `syncpdf-layout`: 页位图 → 布局区域（ort + PP-DocLayoutV3）、覆盖率门禁、
//! XY-cut 阅读顺序、区域内行聚类。
//!
//! 设计基准：docs/reports/2026-09-22-rust-electron-rewrite/02-技术路径与架构.md
//! §3 layout_analysis 阶段；任务拆分 M1-03 / M1-05 / M1-06（部分）。
#![forbid(unsafe_code)]
#![warn(missing_debug_implementations, rust_2018_idioms)]

pub mod coverage;
pub mod detect;
pub mod labels;
pub mod order;
pub mod paragraph;
pub mod postprocess;
pub mod session;

pub use coverage::{coverage, CoverageReport};
pub use detect::{to_pdf_space, DetectError, DetectOpts, Detection, RawImage};
pub use order::xy_cut_order;
pub use paragraph::group_lines;
pub use session::{LayoutModel, SessionError, INPUT_SIZE};

/// 本 crate 统一错误。
#[derive(Debug, thiserror::Error)]
pub enum LayoutError {
    #[error(transparent)]
    Session(#[from] SessionError),
    #[error(transparent)]
    Detect(#[from] DetectError),
}

pub type Result<T> = std::result::Result<T, LayoutError>;
