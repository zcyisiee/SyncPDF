//! documents 表：文档登记与查询。

use crate::db::Store;
use crate::Result;
use std::path::PathBuf;
use syncpdf_core::hash::Sha256Hash;

/// documents 一行。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DocumentRow {
    pub doc_id: String,
    pub source_sha256: Sha256Hash,
    pub path: PathBuf,
    pub pages: u32,
}

impl Store {
    /// 登记或更新文档（同 doc_id 重跑 upsert：哈希/路径/页数以最新为准）。
    pub fn upsert_document(
        &self,
        doc_id: &str,
        source_sha: &Sha256Hash,
        path: &std::path::Path,
        pages: u32,
    ) -> Result<()> {
        self.conn().execute(
            "INSERT INTO documents (doc_id, source_sha256, path, pages)
             VALUES (?1, ?2, ?3, ?4)
             ON CONFLICT(doc_id) DO UPDATE SET
               source_sha256 = excluded.source_sha256,
               path = excluded.path,
               pages = excluded.pages",
            rusqlite::params![
                doc_id,
                source_sha.to_hex(),
                path.display().to_string(),
                pages
            ],
        )?;
        Ok(())
    }

    /// 取文档；不存在返回 `None`。
    pub fn get_document(&self, doc_id: &str) -> Result<Option<DocumentRow>> {
        let mut stmt = self.conn().prepare(
            "SELECT doc_id, source_sha256, path, pages FROM documents WHERE doc_id = ?1",
        )?;
        let mut rows = stmt.query([doc_id])?;
        let Some(row) = rows.next()? else {
            return Ok(None);
        };
        let sha: String = row.get(1)?;
        Ok(Some(DocumentRow {
            doc_id: row.get(0)?,
            source_sha256: Sha256Hash::try_from(sha).map_err(crate::StoreError::Invalid)?,
            path: PathBuf::from(row.get::<_, String>(2)?),
            pages: row
                .get::<_, i64>(3)?
                .try_into()
                .map_err(|_| crate::StoreError::Invalid("pages out of u32 range".into()))?,
        }))
    }

    /// 删除文档（级联删 drafts/paragraphs/runs）。
    pub fn delete_document(&self, doc_id: &str) -> Result<()> {
        self.conn()
            .execute("DELETE FROM documents WHERE doc_id = ?1", [doc_id])?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sha(tag: u8) -> Sha256Hash {
        Sha256Hash::of([tag; 32])
    }

    #[test]
    fn upsert_and_get() {
        let store = Store::open_memory().unwrap();
        assert_eq!(store.get_document("d1").unwrap(), None);
        store
            .upsert_document("d1", &sha(1), std::path::Path::new("/in/a.pdf"), 12)
            .unwrap();
        let row = store.get_document("d1").unwrap().unwrap();
        assert_eq!(row.doc_id, "d1");
        assert_eq!(row.source_sha256, sha(1));
        assert_eq!(row.path, PathBuf::from("/in/a.pdf"));
        assert_eq!(row.pages, 12);
    }

    #[test]
    fn upsert_updates_existing() {
        let store = Store::open_memory().unwrap();
        store
            .upsert_document("d1", &sha(1), std::path::Path::new("/old.pdf"), 1)
            .unwrap();
        store
            .upsert_document("d1", &sha(2), std::path::Path::new("/new.pdf"), 58)
            .unwrap();
        let row = store.get_document("d1").unwrap().unwrap();
        assert_eq!(row.source_sha256, sha(2));
        assert_eq!(row.pages, 58);
        // 只有一行
        let n: i64 = store
            .conn()
            .query_row("SELECT COUNT(*) FROM documents", [], |r| r.get(0))
            .unwrap();
        assert_eq!(n, 1);
    }

    #[test]
    fn delete_cascades() {
        let store = Store::open_memory().unwrap();
        store
            .upsert_document("d1", &sha(1), std::path::Path::new("/a.pdf"), 3)
            .unwrap();
        store.delete_document("d1").unwrap();
        assert_eq!(store.get_document("d1").unwrap(), None);
    }
}
