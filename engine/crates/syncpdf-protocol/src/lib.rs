//! `syncpdf-protocol`：主进程 ↔ 引擎 sidecar 的 JSONL 协议类型。
//!
//! 设计基准：docs/reports/2026-09-22-rust-electron-rewrite/02-技术路径与架构.md §9。
//! 约定：
//! - 每行一个 JSON 对象；请求与事件都用 `type` 字段做 tag（serde internally tagged）。
//! - `Envelope` 把事件包上 `seq`（单调递增）与 `ts`（Unix 秒，浮点）。
//! - 与 `syncpdf-core` 共享的类型（`ParagraphId` / `Rect` / `CoordSystem` /
//!   `ParagraphStatus`）通过 `schema` 模块的镜像类型进 JSON Schema；序列化直接复用
//!   core 的 serde 实现，两侧行为一致。
#![forbid(unsafe_code)]
#![warn(missing_debug_implementations, rust_2018_idioms)]

pub mod event;
pub mod request;
pub mod schema;

pub use event::{Envelope, Event, Severity, Stage, Stats, PROTOCOL_VERSION, PROTOCOL_VERSION_NAME};
pub use request::{decode_request, encode_line, Mode, Request, TranslateProvider, TranslatorKind};

/// JSONL 行解码/编码错误。
#[derive(Debug, thiserror::Error)]
#[error("{0}")]
pub struct ProtocolError(pub String);

impl ProtocolError {
    fn new(msg: impl Into<String>) -> Self {
        Self(msg.into())
    }
}

/// 协议层 Result。
pub type Result<T, E = ProtocolError> = std::result::Result<T, E>;

/// 引擎版本（与 `syncpdf-cli --version` 一致）。
pub const ENGINE_VERSION: &str = env!("CARGO_PKG_VERSION");

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn engine_version_is_semver_like() {
        assert!(ENGINE_VERSION.split('.').count() >= 2);
    }
}
