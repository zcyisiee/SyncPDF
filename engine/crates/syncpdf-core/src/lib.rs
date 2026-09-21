//! `syncpdf-core`：全引擎共享的基础类型。
//!
//! 设计基准：docs/reports/2026-09-22-rust-electron-rewrite/02-技术路径与架构.md §4。
//! 本 crate 不依赖任何业务 crate，只提供 id、几何、颜色、坐标系、错误与夹具定位。
#![forbid(unsafe_code)]
#![warn(missing_debug_implementations, rust_2018_idioms)]

pub mod color;
pub mod error;
pub mod fixtures;
pub mod geom;
pub mod hash;
pub mod ids;
pub mod ir;

pub use color::Color;
pub use error::{CoreError, Result};
pub use geom::{CoordSystem, Matrix, Point, Rect};
pub use ids::{AtomId, GlyphId, ObjRef, OpKey, PageId, ParagraphId, StyleId};
