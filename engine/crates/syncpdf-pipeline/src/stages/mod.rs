//! 各阶段实现：preflight / source_analysis / layout_analysis / paragraph_analysis /
//! translating / typesetting / validating / publishing。
//!
//! 设计基准：02-技术路径与架构.md §3（阶段表）与 §6（执行流程）。
//! 每阶段一个子模块、一个入口函数；端到端编排在 `run.rs`。

pub mod layout;
pub mod paragraph;
pub mod preflight;
mod ruled_code;
pub mod source;
pub mod source_policy;
pub mod translate;
pub mod typeset;
pub mod writeback;

use syncpdf_core::hash::Sha256Hash;

pub use layout::{apply_coverage_fallback, detect_regions, regions_from_detections, LayoutOpts};
pub use paragraph::analyze_page;
pub use preflight::{preflight, Preflight};
pub use source::source_analysis;
pub use translate::{
    fake_from_name, glyph_text_lookup, make_translator, translate_all, translate_with_dyn,
    DynTranslator,
};
pub use typeset::{
    inlines_from_parsed, load_fonts, spec_for, typeset_one, typeset_paragraph, StoreShaper,
};
pub use writeback::{delete_translated, render_snapshot, replay_into};

/// 阶段/编排层错误。
#[derive(Debug, thiserror::Error)]
pub enum PipelineError {
    /// pdfium 调用失败（打开、渲染、页信息）。
    #[error(transparent)]
    Pdfium(#[from] syncpdf_pdf::pdfium::PdfiumError),
    /// 输入文件是加密 PDF（当前不支持）。
    #[error("输入 PDF 已加密：{0}")]
    Encrypted(String),
    /// 输入文件不存在 / 不可读。
    #[error("io 错误 {path}：{source}")]
    Io {
        /// 出错路径。
        path: std::path::PathBuf,
        #[source]
        source: std::io::Error,
    },
    /// 布局模型推理失败。
    #[error("layout 阶段失败：{0}")]
    Layout(String),
    /// 翻译通道失败。
    #[error("translate 阶段失败：{0}")]
    Translate(String),
    /// 字体存储不可用（vendor/fonts 缺失或字体包损坏）。
    #[error("font 阶段失败：{0}")]
    Font(String),
    /// 存储层失败（阶段缓存）。
    #[error("store 阶段失败：{0}")]
    Store(String),
    /// 协议层失败（请求字段非法）。
    #[error("协议错误：{0}")]
    Protocol(String),
    /// 输出校验未通过，不能宣告完整成功。
    #[error("输出校验失败：{0}")]
    Validation(String),
    /// 请求的翻译通道尚未支持。
    #[error("暂不支持的翻译器：{0}")]
    UnsupportedTranslator(String),
    /// 任务被取消。
    #[error("任务已取消")]
    Cancelled,
}

impl PipelineError {
    /// 构造带路径的 IO 错误。
    pub fn io(path: impl Into<std::path::PathBuf>, source: std::io::Error) -> Self {
        Self::Io {
            path: path.into(),
            source,
        }
    }

    /// 事件的 `error.code`：稳定的机器可读标识。
    pub fn code(&self) -> &'static str {
        match self {
            Self::Pdfium(_) => "pdfium",
            Self::Encrypted(_) => "encrypted_pdf",
            Self::Io { .. } => "io",
            Self::Layout(_) => "layout",
            Self::Translate(_) => "translate",
            Self::Font(_) => "font",
            Self::Store(_) => "store",
            Self::Protocol(_) => "protocol",
            Self::Validation(_) => "validation",
            Self::UnsupportedTranslator(_) => "unsupported_translator",
            Self::Cancelled => "cancelled",
        }
    }

    /// 是否致命（任务无法继续）。
    pub fn fatal(&self) -> bool {
        !matches!(self, Self::Cancelled)
    }

    /// 构造错误事件（`code` 来自 [`Self::code`]）。
    pub fn to_event(&self) -> syncpdf_protocol::Event {
        syncpdf_protocol::Event::Error {
            fatal: self.fatal(),
            code: self.code().to_string(),
            message: self.to_string(),
        }
    }
}

impl From<syncpdf_layout::LayoutError> for PipelineError {
    fn from(e: syncpdf_layout::LayoutError) -> Self {
        Self::Layout(e.to_string())
    }
}

impl From<syncpdf_layout::DetectError> for PipelineError {
    fn from(e: syncpdf_layout::DetectError) -> Self {
        Self::Layout(e.to_string())
    }
}

impl From<syncpdf_store::StoreError> for PipelineError {
    fn from(e: syncpdf_store::StoreError) -> Self {
        Self::Store(e.to_string())
    }
}

/// 阶段边界检查取消；已取消返回 [`PipelineError::Cancelled`]。
///
/// 每个阶段的开始（与流式回调的每次迭代）都应先调它。
pub fn check_cancelled(cancel: &crate::cancel::CancellationToken) -> Result<(), PipelineError> {
    if cancel.is_cancelled() {
        Err(PipelineError::Cancelled)
    } else {
        Ok(())
    }
}

/// 每源文件的阶段缓存键：输入文件字节的 sha256（规约 #4）。
pub fn source_key(bytes: &[u8]) -> Sha256Hash {
    Sha256Hash::of(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn error_codes_and_fatality_are_stable() {
        assert_eq!(PipelineError::Cancelled.code(), "cancelled");
        assert!(!PipelineError::Cancelled.fatal());
        assert_eq!(PipelineError::Encrypted("x".into()).code(), "encrypted_pdf");
        assert!(PipelineError::Encrypted("x".into()).fatal());
        assert_eq!(PipelineError::Protocol("x".into()).code(), "protocol");
        assert!(PipelineError::Protocol("x".into()).fatal());
        let e = PipelineError::Cancelled.to_event();
        match e {
            syncpdf_protocol::Event::Error { fatal, code, .. } => {
                assert!(!fatal);
                assert_eq!(code, "cancelled");
            }
            other => panic!("期望 error 事件，得到 {other:?}"),
        }
    }

    #[test]
    fn check_cancelled_maps_to_cancelled_error() {
        let c = crate::cancel::CancellationToken::new();
        assert!(check_cancelled(&c).is_ok());
        c.cancel();
        let e = check_cancelled(&c).unwrap_err();
        assert!(matches!(e, PipelineError::Cancelled));
        assert_eq!(e.code(), "cancelled");
    }

    #[test]
    fn source_key_is_content_addressed() {
        assert_eq!(source_key(b"a"), source_key(b"a"));
        assert_ne!(source_key(b"a"), source_key(b"b"));
    }
}
