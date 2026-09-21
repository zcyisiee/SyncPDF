//! `syncpdf-translate`: 见 docs/reports/2026-09-22-rust-electron-rewrite/02-技术路径与架构.md。
#![forbid(unsafe_code)]
#![warn(missing_debug_implementations, rust_2018_idioms)]

pub mod cache;
pub mod fake;
pub mod pi;
pub mod prompt;
pub mod stream;
pub mod translator;
pub mod unit;
pub mod validate;
