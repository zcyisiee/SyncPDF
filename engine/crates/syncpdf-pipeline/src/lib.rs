//! `syncpdf-pipeline`: 见 docs/reports/2026-09-22-rust-electron-rewrite/02-技术路径与架构.md。
#![forbid(unsafe_code)]
#![warn(missing_debug_implementations, rust_2018_idioms)]

pub mod cancel;
pub mod events;
pub mod run;
pub mod schedule;
pub mod stages;
