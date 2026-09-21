//! `syncpdf-layout`: 见 docs/reports/2026-09-22-rust-electron-rewrite/02-技术路径与架构.md。
#![forbid(unsafe_code)]
#![warn(missing_debug_implementations, rust_2018_idioms)]

pub mod coverage;
pub mod detect;
pub mod order;
pub mod paragraph;
pub mod postprocess;
pub mod session;
