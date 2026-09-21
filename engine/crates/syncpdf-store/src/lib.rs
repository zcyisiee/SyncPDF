//! `syncpdf-store`：rusqlite 存储（app.db）+ 阶段缓存 + 内容寻址资产。
//!
//! 设计基准：docs/reports/2026-09-22-rust-electron-rewrite/02-技术路径与架构.md §10。
//! 表：documents、runs、run_events（seq 游标）、drafts（revision 单调 +1）、
//! paragraphs、stages（CBOR+zstd BLOB，键 = 输入哈希）、assets（引用计数）。
//! 资产文件存 `<root>/assets/<aa>/<sha256>`，`root` 为数据库文件所在目录。
#![forbid(unsafe_code)]
#![warn(missing_debug_implementations, rust_2018_idioms)]

pub mod assets;
pub mod db;
pub mod documents;
pub mod drafts;
pub mod paragraphs;
pub mod runs;
pub mod stages;

pub use assets::Assets;
pub use db::Store;
pub use documents::DocumentRow;
pub use drafts::DraftError;
pub use paragraphs::ParagraphRow;
pub use runs::RunId;

/// 存储层错误。
#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    #[error("sqlite error: {0}")]
    Sqlite(#[from] rusqlite::Error),
    #[error("io error at {path}: {source}")]
    Io {
        path: std::path::PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error("serialization error: {0}")]
    Serde(String),
    /// `base_revision` 与当前 revision 不一致（协议映射为 `error{code: conflict}`）。
    #[error("revision conflict: current={current}")]
    Conflict { current: u64 },
    #[error("not found: {0}")]
    NotFound(String),
    #[error("invalid data: {0}")]
    Invalid(String),
}

/// 存储层 Result。
pub type Result<T, E = StoreError> = std::result::Result<T, E>;

impl StoreError {
    pub fn io(path: impl Into<std::path::PathBuf>, source: std::io::Error) -> Self {
        Self::Io {
            path: path.into(),
            source,
        }
    }
}

impl From<ciborium::de::Error<std::io::Error>> for StoreError {
    fn from(e: ciborium::de::Error<std::io::Error>) -> Self {
        Self::Serde(format!("cbor decode: {e}"))
    }
}

impl From<ciborium::ser::Error<std::io::Error>> for StoreError {
    fn from(e: ciborium::ser::Error<std::io::Error>) -> Self {
        Self::Serde(format!("cbor encode: {e}"))
    }
}

/// 阶段缓存值压缩包装：`CBOR(bytes) → zstd(level 3)`。
pub(crate) fn compress_value<T: serde::Serialize>(value: &T) -> Result<Vec<u8>> {
    let mut cbor = Vec::new();
    ciborium::into_writer(value, &mut cbor)?;
    zstd::encode_all(cbor.as_slice(), 3).map_err(|e| StoreError::Serde(format!("zstd: {e}")))
}

/// 解压阶段缓存值。
pub(crate) fn decompress_value<T: serde::de::DeserializeOwned>(bytes: &[u8]) -> Result<T> {
    let cbor = zstd::decode_all(bytes).map_err(|e| StoreError::Serde(format!("zstd: {e}")))?;
    Ok(ciborium::from_reader(cbor.as_slice())?)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn compress_roundtrip_small() {
        let data = vec![1u32, 2, 3, 4];
        let bytes = compress_value(&data).unwrap();
        let back: Vec<u32> = decompress_value(&bytes).unwrap();
        assert_eq!(back, data);
    }

    #[test]
    fn compress_error_on_garbage() {
        assert!(decompress_value::<Vec<u32>>(&[0xff, 0xee]).is_err());
    }
}
