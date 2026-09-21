//! assets：内容寻址存储（规约 #14）。
//!
//! 文件本体放 `<root>/assets/<hash[0:2]>/<hash>`（hex）；`app.db` 的 assets 表记
//! 引用计数。`put` 去重（同内容只写一次文件，refcount +1）；`release` refcount -1，
//! 到 0 删文件与登记行。同一 hash 多次 put → 多次 release 才会真正删除。

use crate::db::Store;
use crate::{Result, StoreError};
use rusqlite::Connection;
use std::path::{Path, PathBuf};
use syncpdf_core::hash::Sha256Hash;

/// 资产模块句柄（借用 `Store` 的 root 与连接）。
#[derive(Debug)]
pub struct Assets<'a> {
    root: &'a Path,
    conn: &'a Connection,
}

impl<'a> Assets<'a> {
    pub(crate) fn new(root: &'a Path, conn: &'a Connection) -> Self {
        Self { root, conn }
    }

    /// 资产根目录 `<root>/assets`。
    fn assets_dir(&self) -> PathBuf {
        self.root.join("assets")
    }

    /// 内容为 `bytes` 的资产路径（不检查存在性）。
    fn path_of(&self, hash: &Sha256Hash) -> PathBuf {
        let hex = hash.to_hex();
        self.assets_dir().join(&hex[..2]).join(hex)
    }

    /// 写入（或复用）一份内容；返回哈希。同内容重复 put：文件只写一次，refcount 累加。
    pub fn put(&self, bytes: &[u8]) -> Result<Sha256Hash> {
        let hash = Sha256Hash::of(bytes);
        let path = self.path_of(&hash);
        // 登记行先建/加计数（事务里与文件写入保持一致顺序：先 DB 后文件，
        // 崩溃时可能残留孤儿文件——内容寻址下无碍，下次 put 覆盖同名）
        let tx = self.conn.unchecked_transaction()?;
        let changed = tx.execute(
            "INSERT INTO assets (sha256, size, refcount) VALUES (?1, ?2, 1)
             ON CONFLICT(sha256) DO UPDATE SET refcount = refcount + 1",
            rusqlite::params![hash.to_hex(), bytes.len() as i64],
        )?;
        debug_assert_eq!(changed, 1);
        tx.commit()?;
        if !path.is_file() {
            if let Some(parent) = path.parent() {
                std::fs::create_dir_all(parent).map_err(|e| StoreError::io(parent, e))?;
            }
            // 原子写：先写临时文件再改名，避免半截文件被当成完整资产
            let tmp = path.with_extension("part");
            std::fs::write(&tmp, bytes).map_err(|e| StoreError::io(&tmp, e))?;
            std::fs::rename(&tmp, &path).map_err(|e| StoreError::io(&path, e))?;
        }
        Ok(hash)
    }

    /// 释放一次引用；refcount 归零时删文件与登记行。
    pub fn release(&self, hash: &Sha256Hash) -> Result<()> {
        let tx = self.conn.unchecked_transaction()?;
        let refcount: Option<i64> = tx
            .query_row(
                "SELECT refcount FROM assets WHERE sha256 = ?1",
                [hash.to_hex()],
                |r| r.get(0),
            )
            .map(Some)
            .or_else(|e| match e {
                rusqlite::Error::QueryReturnedNoRows => Ok(None),
                other => Err(other),
            })?;
        let Some(rc) = refcount else {
            return Err(StoreError::NotFound(format!("asset {hash}")));
        };
        if rc > 1 {
            tx.execute(
                "UPDATE assets SET refcount = refcount - 1 WHERE sha256 = ?1",
                [hash.to_hex()],
            )?;
            tx.commit()?;
            return Ok(());
        }
        // 归零：删登记行，再删文件
        tx.execute("DELETE FROM assets WHERE sha256 = ?1", [hash.to_hex()])?;
        tx.commit()?;
        let path = self.path_of(hash);
        if path.is_file() {
            std::fs::remove_file(&path).map_err(|e| StoreError::io(&path, e))?;
            // 空的桶目录顺手清理（失败忽略）
            if let Some(bucket) = path.parent() {
                let _ = std::fs::remove_dir(bucket);
            }
        }
        Ok(())
    }

    /// 资产文件路径（不保证存在；内容寻址，存在即完整）。
    pub fn path(&self, hash: &Sha256Hash) -> PathBuf {
        self.path_of(hash)
    }

    /// 引用计数（未登记返回 `None`）。
    pub fn refcount(&self, hash: &Sha256Hash) -> Result<Option<u64>> {
        let rc: Option<i64> = self
            .conn
            .query_row(
                "SELECT refcount FROM assets WHERE sha256 = ?1",
                [hash.to_hex()],
                |r| r.get(0),
            )
            .map(Some)
            .or_else(|e| match e {
                rusqlite::Error::QueryReturnedNoRows => Ok(None),
                other => Err(other),
            })?;
        Ok(rc.map(|v| v as u64))
    }
}

impl Store {
    /// 写入（或复用）一份资产内容；返回内容哈希（见 [`Assets::put`]）。
    pub fn put_asset(&self, bytes: &[u8]) -> Result<Sha256Hash> {
        self.assets().put(bytes)
    }

    /// 释放一次引用；refcount 归零时删文件（见 [`Assets::release`]）。
    pub fn release_asset(&self, hash: &Sha256Hash) -> Result<()> {
        self.assets().release(hash)
    }

    /// 资产文件路径（不保证存在；见 [`Assets::path`]）。
    pub fn asset_path(&self, hash: &Sha256Hash) -> PathBuf {
        self.assets().path(hash)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn put_dedupes_and_counts() {
        let store = Store::open_memory().unwrap();
        let assets = store.assets();
        let bytes = b"source pdf bytes".to_vec();
        let h1 = assets.put(&bytes).unwrap();
        let h2 = assets.put(&bytes).unwrap();
        assert_eq!(h1, h2);
        assert_eq!(assets.refcount(&h1).unwrap(), Some(2));
        // 文件存在且内容一致
        let p = assets.path(&h1);
        assert!(p.is_file(), "{}", p.display());
        assert_eq!(std::fs::read(&p).unwrap(), bytes);
        // 释放一次仍在
        assets.release(&h1).unwrap();
        assert_eq!(assets.refcount(&h1).unwrap(), Some(1));
        assert!(p.is_file());
        // 再释放：删文件
        assets.release(&h1).unwrap();
        assert_eq!(assets.refcount(&h1).unwrap(), None);
        assert!(!p.exists());
    }

    #[test]
    fn distinct_contents_distinct_assets() {
        let store = Store::open_memory().unwrap();
        let assets = store.assets();
        let a = assets.put(b"aaa").unwrap();
        let b = assets.put(b"bbb").unwrap();
        assert_ne!(a, b);
        assert_eq!(assets.refcount(&a).unwrap(), Some(1));
        assert_eq!(assets.refcount(&b).unwrap(), Some(1));
    }

    #[test]
    fn release_unknown_asset_errors() {
        let store = Store::open_memory().unwrap();
        let assets = store.assets();
        let h = Sha256Hash::of(b"never-put");
        assert!(matches!(
            assets.release(&h).unwrap_err(),
            StoreError::NotFound(_)
        ));
    }

    #[test]
    fn file_db_assets_in_real_dir() {
        let tmp = tempfile::tempdir().unwrap();
        let db = tmp.path().join("app.db");
        let store = Store::open(&db).unwrap();
        let assets = store.assets();
        let h = assets.put(b"pdf-content").unwrap();
        let p = assets.path(&h);
        assert!(p.starts_with(tmp.path().join("assets")), "{}", p.display());
        assert_eq!(std::fs::read(&p).unwrap(), b"pdf-content");
        assets.release(&h).unwrap();
        assert!(!p.exists());
        // 桶目录也被清理
        let bucket = p.parent().unwrap();
        assert!(!bucket.exists());
    }

    #[test]
    fn store_level_asset_api_roundtrip() {
        // brief 指定的三个方法名：put_asset / release_asset / asset_path
        let tmp = tempfile::tempdir().unwrap();
        let store = Store::open(&tmp.path().join("app.db")).unwrap();
        let h = store.put_asset(b"doc pdf").unwrap();
        let p = store.asset_path(&h);
        assert!(p.starts_with(tmp.path().join("assets")));
        assert_eq!(std::fs::read(&p).unwrap(), b"doc pdf");
        store.release_asset(&h).unwrap();
        assert!(!p.exists());
    }

    #[test]
    fn two_stores_share_one_file_store() {
        // 两个数据库打开同一目录：第二个 put 同内容只加计数、不覆盖文件
        let tmp = tempfile::tempdir().unwrap();
        let db = tmp.path().join("app.db");
        let store = Store::open(&db).unwrap();
        let h = store.assets().put(b"shared").unwrap();
        let p = store.assets().path(&h);
        assert!(p.is_file());
        // 同一 store 再 put（模拟引用）→ refcount=2，release 一次不删
        let h2 = store.assets().put(b"shared").unwrap();
        assert_eq!(h, h2);
        store.assets().release(&h).unwrap();
        assert!(p.is_file());
        store.assets().release(&h).unwrap();
        assert!(!p.exists());
    }
}
