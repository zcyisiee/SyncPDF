//! stages 表：阶段产物缓存（CBOR+zstd BLOB，键 = 输入哈希 + stage 名，规约 #4）。

use crate::db::Store;
use crate::{compress_value, decompress_value, Result};
use serde::de::DeserializeOwned;
use serde::Serialize;
use syncpdf_core::hash::Sha256Hash;

impl Store {
    /// 写阶段产物（同键覆盖）。值经 CBOR 序列化后 zstd level 3 压缩。
    pub fn put_stage<T: Serialize>(&self, key: &Sha256Hash, stage: &str, value: &T) -> Result<()> {
        let blob = compress_value(value)?;
        self.conn().execute(
            "INSERT INTO stages (key_sha256, stage, blob)
             VALUES (?1, ?2, ?3)
             ON CONFLICT(key_sha256, stage) DO UPDATE SET
               blob = excluded.blob,
               created_at = unixepoch()",
            rusqlite::params![key.to_hex(), stage, blob],
        )?;
        Ok(())
    }

    /// 读阶段产物；键不存在（或解压失败）返回 `None`。
    pub fn get_stage<T: DeserializeOwned>(
        &self,
        key: &Sha256Hash,
        stage: &str,
    ) -> Result<Option<T>> {
        let blob: Option<Vec<u8>> = self
            .conn()
            .query_row(
                "SELECT blob FROM stages WHERE key_sha256 = ?1 AND stage = ?2",
                [key.to_hex(), stage.to_string()],
                |r| r.get(0),
            )
            .map(Some)
            .or_else(|e| match e {
                rusqlite::Error::QueryReturnedNoRows => Ok(None),
                other => Err(other),
            })?;
        match blob {
            None => Ok(None),
            Some(bytes) => decompress_value(&bytes)
                .map(Some)
                .map_err(|e| crate::StoreError::Serde(format!("stage {stage}: {e}"))),
        }
    }

    /// 缓存里存了多少个阶段条目（统计/测试用）。
    pub fn stage_entries(&self) -> Result<usize> {
        let n: i64 = self
            .conn()
            .query_row("SELECT COUNT(*) FROM stages", [], |r| r.get(0))?;
        Ok(n as usize)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde::{Deserialize, Serialize};

    #[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
    struct BigStruct {
        items: Vec<(u32, f64, String)>,
    }

    fn big(n: usize) -> BigStruct {
        BigStruct {
            items: (0..n)
                .map(|i| (i as u32, i as f64 * 0.5, format!("item-{i:06}")))
                .collect(),
        }
    }

    #[test]
    fn stage_roundtrip() {
        let store = Store::open_memory().unwrap();
        let key = Sha256Hash::of(b"input-bytes");
        assert_eq!(
            store
                .get_stage::<Vec<u32>>(&key, "paragraph_analysis")
                .unwrap(),
            None
        );
        let value = vec![10u32, 20, 30];
        store.put_stage(&key, "paragraph_analysis", &value).unwrap();
        assert_eq!(
            store
                .get_stage::<Vec<u32>>(&key, "paragraph_analysis")
                .unwrap(),
            Some(value)
        );
        // 同 key 不同 stage 不串
        assert_eq!(
            store
                .get_stage::<Vec<u32>>(&key, "layout_analysis")
                .unwrap(),
            None
        );
    }

    #[test]
    fn stage_overwrite() {
        let store = Store::open_memory().unwrap();
        let key = Sha256Hash::of(b"k");
        store.put_stage(&key, "preflight", &1u32).unwrap();
        store.put_stage(&key, "preflight", &2u32).unwrap();
        assert_eq!(store.get_stage::<u32>(&key, "preflight").unwrap(), Some(2));
        assert_eq!(store.stage_entries().unwrap(), 1);
    }

    #[test]
    fn stage_big_struct_compressed_smaller_than_json_half() {
        let store = Store::open_memory().unwrap();
        let value = big(10_000);
        let key = Sha256Hash::of(b"big");
        store.put_stage(&key, "paragraph_analysis", &value).unwrap();

        // 落盘 BLOB 长度
        let blob_len: usize = store
            .conn()
            .query_row(
                "SELECT LENGTH(blob) FROM stages WHERE key_sha256 = ?1 AND stage = ?2",
                [key.to_hex(), "paragraph_analysis".to_string()],
                |r| r.get::<_, i64>(0),
            )
            .unwrap() as usize;

        let json = serde_json::to_vec(&value).unwrap();
        assert!(
            blob_len * 2 < json.len(),
            "压缩后 {blob_len} 字节应小于 JSON {} 字节的一半",
            json.len()
        );

        // 往返一致
        let back: BigStruct = store
            .get_stage(&key, "paragraph_analysis")
            .unwrap()
            .expect("缓存应在");
        assert_eq!(back, value);
        assert_eq!(back.items.len(), 10_000);
    }

    #[test]
    fn stage_different_types_same_key() {
        let store = Store::open_memory().unwrap();
        let key = Sha256Hash::of(b"multi");
        store
            .put_stage(&key, "typesetting", &vec![1.5f64, 2.5])
            .unwrap();
        store
            .put_stage(&key, "validating", &"report".to_string())
            .unwrap();
        assert_eq!(
            store.get_stage::<Vec<f64>>(&key, "typesetting").unwrap(),
            Some(vec![1.5, 2.5])
        );
        assert_eq!(
            store.get_stage::<String>(&key, "validating").unwrap(),
            Some("report".into())
        );
        assert_eq!(store.stage_entries().unwrap(), 2);
    }

    #[test]
    fn stage_type_mismatch_is_serde_error_not_none() {
        let store = Store::open_memory().unwrap();
        let key = Sha256Hash::of(b"mismatch");
        store.put_stage(&key, "s", &vec![1u32, 2]).unwrap();
        // 存的是数组，按结构体读 → Serde 错误（缓存损坏应当显式失败，不静默 miss）
        assert!(store.get_stage::<BigStruct>(&key, "s").is_err());
    }
}
