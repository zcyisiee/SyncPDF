//! `syncpdf-pdf`: 见 docs/reports/2026-09-22-rust-electron-rewrite/02-技术路径与架构.md。
#![forbid(unsafe_code)]
#![warn(missing_debug_implementations, rust_2018_idioms)]

pub mod content;
pub mod pdfium;

pub mod bind;
pub mod embed;
pub mod links;
pub mod patch;
pub mod validate;
pub mod writer;
