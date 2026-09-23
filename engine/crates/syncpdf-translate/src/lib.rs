//! `syncpdf-translate`：翻译单元、校验器、one-shot 提示词、流式块解析、
//! 翻译通道（pi CLI / 假翻译器）与 SQLite 缓存。
//!
//! 设计基准：docs/reports/2026-09-22-rust-electron-rewrite/02-技术路径与架构.md §5。
//!
//! 产品核心是 **one-shot + 流式**：整份文档的全部翻译单元放进一个提示词一次发给
//! 模型（调用方显式容量上限超出时返回错误）；模型流式输出时，每凑齐一个
//! 完整 Markdown 块就立刻校验并通过回调交给上游，上游据此立即排版回写该段。
//!
//! ```ignore
//! let units: Vec<Unit> = paragraphs.iter().map(|p| build_unit(p, &glyph_text)).collect();
//! let ctx = ContextMap::from_units(&units);
//! let engine = Engine::new(PiTranslator::default());
//! let result = engine
//!     .translate_document(&PromptSpec::new("en", "zh-CN"), units, ctx, Some(&cache), |block| {
//!         // 立刻排版回写 block
//!     })
//!     .await?;
//! ```
#![forbid(unsafe_code)]
#![warn(missing_debug_implementations, rust_2018_idioms)]

pub mod cache;
pub mod fake;
pub mod markdown;
pub mod pi;
pub mod prompt;
pub mod stream;
pub mod translator;
pub mod unit;
pub mod validate;

pub use cache::{Cache, CacheError, ORIGIN_MANUAL};
pub use fake::{FakeTranslator, RecordingTranslator};
pub use pi::PiTranslator;
pub use prompt::{build_document_prompts, system_prompt, DocumentPrompt, PromptError, PromptSpec};
pub use stream::{BlockStream, RawBlock};
pub use translator::{
    BlockStatus, ContextMap, DeltaSink, DocumentResult, Engine, Stats, TranslateError,
    TranslatedBlock, Translator,
};
pub use unit::{build_unit, parse_unit_html, ParsedUnit, Segment, Unit, UnitParseError};
pub use validate::{validate, ValidateCtx, Violation};
