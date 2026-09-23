//! `syncpdf-pdf`: 见 docs/reports/2026-09-22-rust-electron-rewrite/02-技术路径与架构.md。
#![forbid(unsafe_code)]
#![warn(missing_debug_implementations, rust_2018_idioms)]

pub mod content;
pub mod pdfium;

pub mod bind;
pub mod embed;
pub mod links;
pub mod patch;
pub mod source_atom;
pub mod validate;
pub mod writer;

// 便捷再导出（上游 crate 对接用）。
pub use bind::{bind_page, BindError, BindStats, BoundPage, FormDo};
pub use embed::{embed_font_lopdf, EmbedError, EmbeddedFontObj};
pub use links::links_check;
pub use patch::{PatchError, PatchSet, PatchStats};
pub use validate::{self_check, Report, ValidateError};
pub use writer::{save, FontStats, WriteError, Writer};
