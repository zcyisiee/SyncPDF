//! 各阶段实现：preflight / source_analysis / layout_analysis / paragraph_analysis /
//! translating / typesetting / validating / publishing。
//!
//! 设计基准：02-技术路径与架构.md §3（阶段表）与 §6（执行流程）。
//! 每阶段一个子模块、一个入口函数；**阶段 2 的接线点**用
//! [`PipelineError::NotYetAvailable`] 占位（`syncpdf-pdf` 的
//! `bind_page` / `PatchSet` / `Writer` / `self_check` 尚未合入）。

pub mod layout;
pub mod paragraph;
pub mod preflight;
pub mod translate;
pub mod typeset;

use std::path::Path;

use syncpdf_core::hash::Sha256Hash;

pub use preflight::{preflight, Preflight};
// 步骤 5-8 就位后逐个解注释：
pub use layout::{apply_coverage_fallback, detect_regions, regions_from_detections, LayoutOpts};
pub use paragraph::analyze_page;
pub use translate::{
    fake_from_name, glyph_text_lookup, make_translator, translate_all, translate_with_dyn,
    DynTranslator,
};
// pub use typeset::{inlines_from_parsed, spec_for, typeset_one, typeset_paragraph, StoreShaper};

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
    /// 请求的翻译通道尚未支持。
    #[error("暂不支持的翻译器：{0}")]
    UnsupportedTranslator(String),
    /// 任务被取消。
    #[error("任务已取消")]
    Cancelled,
    /// 该功能依赖尚未合入的 `syncpdf-pdf` 回写 API（阶段 2 接线点）。
    #[error("功能尚未就绪：{0}")]
    NotYetAvailable(&'static str),
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
            Self::UnsupportedTranslator(_) => "unsupported_translator",
            Self::Cancelled => "cancelled",
            Self::NotYetAvailable(_) => "not_yet_available",
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

// ---------------------------------------------------------------------------
// 阶段 2 接线点：以下函数壳依赖 `syncpdf-pdf` 的 bind_page / PatchSet /
// Writer / self_check，那些 API 由另一位实现者编写、尚未合入本分支。
// 签名已按设计文档 §3/§9 固定，阶段 2 只需把函数体接上，不用改签名。
// ---------------------------------------------------------------------------

/// 每源文件的阶段缓存键：输入文件字节的 sha256（规约 #4）。
pub fn source_key(bytes: &[u8]) -> Sha256Hash {
    Sha256Hash::of(bytes)
}

/// source_analysis 阶段：每页 `bind_page` → [`syncpdf_core::ir::PageIR`]。
///
/// **阶段 2 接线点**：`syncpdf_pdf::bind::bind_page(&mut lopdf::Document, page)`
/// （或等价入口）——需先把 PDF 用 lopdf 载入以拿到内容流与对象号。
/// 签名里的 `doc` 即该 lopdf 文档；每个 `PageIR` 由 rayon 并行绑定，
/// 但 pdfium 相关调用（如有）必须串行走 `worker`。
pub fn source_analysis(
    _worker: &syncpdf_pdf::pdfium::PdfiumWorker,
    _doc: &mut lopdf::Document,
    _pages: &[u32],
    _cancel: &crate::cancel::CancellationToken,
) -> Result<Vec<syncpdf_core::ir::PageIR>, PipelineError> {
    Err(PipelineError::NotYetAvailable("writeback::bind_page"))
}

/// 页回写：对一页做 `PatchSet.apply` + `Writer.write_paragraphs`，返回新快照路径。
///
/// **阶段 2 接线点**：`syncpdf_pdf::{patch::PatchSet, writer::Writer}`；
/// 快照用原子写（临时文件 + rename），中间快照允许重复嵌入字体（用
/// Writer 的「临时字体」全量不子集化嵌入并缓存对象 id），finalize 再完整保存一次。
pub fn writeback_page(
    _doc: &mut lopdf::Document,
    _page: u32,
    _typeset: &[syncpdf_core::ir::TypesetParagraph],
    _fonts: &syncpdf_font::FontStore,
    _profile: &syncpdf_font::FontProfile,
    _output: &Path,
    _finalize: bool,
) -> Result<(), PipelineError> {
    Err(PipelineError::NotYetAvailable("writeback::patch/writer"))
}

/// validating 阶段：对生成流做 `self_check`。
///
/// **阶段 2 接线点**：`syncpdf_pdf::validate::self_check`。
pub fn validate_output(_bytes: &[u8]) -> Result<(), PipelineError> {
    Err(PipelineError::NotYetAvailable("writeback::self_check"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn error_codes_and_fatality_are_stable() {
        assert_eq!(PipelineError::Cancelled.code(), "cancelled");
        assert!(!PipelineError::Cancelled.fatal());
        assert_eq!(
            PipelineError::Encrypted("x".into()).code(),
            "encrypted_pdf"
        );
        assert_eq!(
            PipelineError::NotYetAvailable("writeback").code(),
            "not_yet_available"
        );
        assert!(PipelineError::NotYetAvailable("writeback").fatal());
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
    fn source_key_is_content_addressed() {
        assert_eq!(source_key(b"a"), source_key(b"a"));
        assert_ne!(source_key(b"a"), source_key(b"b"));
    }
}
