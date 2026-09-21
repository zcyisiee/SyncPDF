//! `syncpdf-font`：字体加载、度量、塑形、子集化与 PDF 嵌入。
//!
//! 设计基准：docs/reports/2026-09-22-rust-electron-rewrite/02-技术路径与架构.md §7
//! 与 research/02-hjfy-engine-deep-dive.md §3.4。
//!
//! - [`loader`]：fontdb 加载内置字体包（notocjk/inter/pt/arabic），可附加系统字体；
//! - [`metrics`]：skrifa 全局度量与字形步进；
//! - [`shape`]：harfrust 塑形（输出单位 pt）；
//! - [`subset`]：subsetter 子集化（gid 重映射，见模块注释）；
//! - [`embed`]：pdf-writer 生成 Type0/CIDFontType2 + 自定义 CMap + ToUnicode + /W；
//! - [`profile`]：角色 → 字体链（Body/DocTitle/ParagraphTitle/Mono/Raster）。
#![forbid(unsafe_code)]
#![warn(missing_debug_implementations, rust_2018_idioms)]

pub mod embed;
pub mod loader;
pub mod metrics;
pub mod profile;
pub mod shape;
pub mod subset;

pub use embed::{embed_font, encode_gids, EmbeddedFont};
pub use loader::{FontId, FontQuery, FontStore, LoadedFont};
pub use metrics::{advance, gid_for, has_char, metrics, Metrics};
pub use profile::{default_profile, FontProfile, Role};
pub use shape::{shape, shape_runs, ShapedGlyph};
pub use subset::{subset, SubsetResult};

/// 本 crate 错误类型。
#[derive(Debug, thiserror::Error)]
pub enum FontError {
    /// 字体数据解析失败（skrifa/read-fonts）。
    #[error("font parse error: {0}")]
    Parse(String),
    /// IO / 路径错误。
    #[error("io error at {path}: {source}")]
    Io {
        /// 出错路径。
        path: std::path::PathBuf,
        /// 底层 IO 错误。
        source: std::io::Error,
    },
    /// resources.json 或字体包结构不符合预期。
    #[error("invalid font package: {0}")]
    InvalidPackage(String),
    /// 请求的字体/变体不存在。
    #[error("font not found: {0}")]
    NotFound(String),
    /// 子集化失败。
    #[error("subsetting failed: {0}")]
    Subset(String),
    /// 嵌入 PDF 对象生成失败。
    #[error("embedding failed: {0}")]
    Embed(String),
}

impl FontError {
    /// 构造带路径的 IO 错误。
    pub fn io(path: impl Into<std::path::PathBuf>, source: std::io::Error) -> Self {
        Self::Io {
            path: path.into(),
            source,
        }
    }
}

/// 结果别名。
pub type Result<T, E = FontError> = std::result::Result<T, E>;

impl From<FontError> for syncpdf_core::CoreError {
    fn from(e: FontError) -> Self {
        match e {
            FontError::Io { path, source } => syncpdf_core::CoreError::Io { path, source },
            other => syncpdf_core::CoreError::Unsupported(other.to_string()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn error_display() {
        let e = FontError::NotFound("nope".into());
        assert!(e.to_string().contains("nope"));
    }
}
