//! drafts 表：草稿 revision 单调 +1 与 `base_revision` 乐观并发（规约 #5）。

use crate::db::Store;
use crate::{Result, StoreError};
use syncpdf_core::ParagraphId;

/// drafts / paragraphs 操作错误（`Conflict` 映射协议 `error{code: conflict}`）。
pub type DraftError = StoreError;

impl Store {
    /// 当前 revision；文档无草稿行时为 0。
    pub fn get_revision(&self, doc_id: &str) -> Result<u64> {
        let rev: Option<i64> = self
            .conn()
            .query_row(
                "SELECT revision FROM drafts WHERE doc_id = ?1",
                [doc_id],
                |r| r.get(0),
            )
            .map(Some)
            .or_else(|e| match e {
                rusqlite::Error::QueryReturnedNoRows => Ok(None),
                other => Err(other),
            })?;
        Ok(rev.unwrap_or(0) as u64)
    }

    /// 手动编辑落草稿：`base_revision` 必须等于当前 revision，否则
    /// `StoreError::Conflict{current}`；成功写入段落译文并 revision +1，返回新 revision。
    pub fn apply_edit(
        &self,
        doc_id: &str,
        paragraph_id: &ParagraphId,
        html: &str,
        base_revision: u64,
    ) -> Result<u64> {
        let tx = self.conn().unchecked_transaction()?;
        let current: Option<i64> = tx
            .query_row(
                "SELECT revision FROM drafts WHERE doc_id = ?1",
                [doc_id],
                |r| r.get(0),
            )
            .map(Some)
            .or_else(|e| match e {
                rusqlite::Error::QueryReturnedNoRows => Ok(None),
                other => Err(other),
            })?;
        let current = current.unwrap_or(0) as u64;
        if current != base_revision {
            return Err(StoreError::Conflict { current });
        }
        let new_rev = current + 1;
        tx.execute(
            "INSERT INTO drafts (doc_id, revision) VALUES (?1, ?2)
             ON CONFLICT(doc_id) DO UPDATE SET revision = excluded.revision",
            rusqlite::params![doc_id, new_rev as i64],
        )?;
        // 段落必须已存在（apply_edit 只编辑已有段落）
        let changed = tx.execute(
            "UPDATE paragraphs
             SET translated_html = ?3, status = 'typeset'
             WHERE doc_id = ?1 AND paragraph_id = ?2",
            rusqlite::params![doc_id, paragraph_id.to_string(), html],
        )?;
        if changed == 0 {
            return Err(StoreError::NotFound(format!(
                "paragraph {paragraph_id} in doc {doc_id}"
            )));
        }
        tx.commit()?;
        Ok(new_rev)
    }

    /// 删除段落译文（同样 revision +1）。
    pub fn delete_paragraph_translation(
        &self,
        doc_id: &str,
        paragraph_id: &ParagraphId,
        base_revision: u64,
    ) -> Result<u64> {
        let tx = self.conn().unchecked_transaction()?;
        let current: Option<i64> = tx
            .query_row(
                "SELECT revision FROM drafts WHERE doc_id = ?1",
                [doc_id],
                |r| r.get(0),
            )
            .or_else(|e| match e {
                rusqlite::Error::QueryReturnedNoRows => Ok(None),
                other => Err(other),
            })?;
        let current = current.unwrap_or(0) as u64;
        if current != base_revision {
            return Err(StoreError::Conflict { current });
        }
        let new_rev = current + 1;
        tx.execute(
            "INSERT INTO drafts (doc_id, revision) VALUES (?1, ?2)
             ON CONFLICT(doc_id) DO UPDATE SET revision = excluded.revision",
            rusqlite::params![doc_id, new_rev as i64],
        )?;
        let changed = tx.execute(
            "UPDATE paragraphs SET translated_html = NULL, status = 'pending'
             WHERE doc_id = ?1 AND paragraph_id = ?2",
            rusqlite::params![doc_id, paragraph_id.to_string()],
        )?;
        if changed == 0 {
            return Err(StoreError::NotFound(format!(
                "paragraph {paragraph_id} in doc {doc_id}"
            )));
        }
        tx.commit()?;
        Ok(new_rev)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use syncpdf_core::hash::Sha256Hash;

    fn store_with_paragraphs() -> (Store, String) {
        let store = Store::open_memory().unwrap();
        let doc = "doc-1".to_string();
        store
            .upsert_document(
                &doc,
                &Sha256Hash::of(b"d"),
                std::path::Path::new("/a.pdf"),
                2,
            )
            .unwrap();
        store
            .upsert_paragraph(&doc, &"P01-001".parse().unwrap(), 1, "pending", None)
            .unwrap();
        store
            .upsert_paragraph(&doc, &"P01-002".parse().unwrap(), 1, "pending", None)
            .unwrap();
        (store, doc)
    }

    #[test]
    fn revision_starts_at_zero_and_increments() {
        let (store, doc) = store_with_paragraphs();
        assert_eq!(store.get_revision(&doc).unwrap(), 0);
        let id: ParagraphId = "P01-001".parse().unwrap();
        let r1 = store.apply_edit(&doc, &id, "<p>一</p>", 0).unwrap();
        assert_eq!(r1, 1);
        let r2 = store.apply_edit(&doc, &id, "<p>二</p>", 1).unwrap();
        assert_eq!(r2, 2);
        assert_eq!(store.get_revision(&doc).unwrap(), 2);
        // 译文已写回段落表
        let rows = store.list_paragraphs(&doc).unwrap();
        assert_eq!(rows[0].translated_html.as_deref(), Some("<p>二</p>"));
        assert_eq!(rows[0].status, "typeset");
    }

    #[test]
    fn stale_base_revision_conflicts() {
        let (store, doc) = store_with_paragraphs();
        let id: ParagraphId = "P01-001".parse().unwrap();
        store.apply_edit(&doc, &id, "<p>一</p>", 0).unwrap();
        let err = store.apply_edit(&doc, &id, "<p>写</p>", 0).unwrap_err();
        assert!(
            matches!(err, StoreError::Conflict { current: 1 }),
            "{err:?}"
        );
        // revision 不变
        assert_eq!(store.get_revision(&doc).unwrap(), 1);
        // 段落译文不被污染
        let rows = store.list_paragraphs(&doc).unwrap();
        assert_eq!(rows[0].translated_html.as_deref(), Some("<p>一</p>"));
    }

    #[test]
    fn base_revision_from_future_conflicts() {
        let (store, doc) = store_with_paragraphs();
        let id: ParagraphId = "P01-001".parse().unwrap();
        let err = store.apply_edit(&doc, &id, "<p>x</p>", 5).unwrap_err();
        assert!(matches!(err, StoreError::Conflict { current: 0 }));
    }

    #[test]
    fn delete_bumps_revision() {
        let (store, doc) = store_with_paragraphs();
        let id: ParagraphId = "P01-001".parse().unwrap();
        store.apply_edit(&doc, &id, "<p>一</p>", 0).unwrap();
        let r = store.delete_paragraph_translation(&doc, &id, 1).unwrap();
        assert_eq!(r, 2);
        let rows = store.list_paragraphs(&doc).unwrap();
        assert_eq!(rows[0].translated_html, None);
        assert_eq!(rows[0].status, "pending");
        // 再用旧 revision 删 → conflict
        let err = store
            .delete_paragraph_translation(&doc, &id, 1)
            .unwrap_err();
        assert!(matches!(err, StoreError::Conflict { current: 2 }));
    }

    #[test]
    fn apply_edit_unknown_paragraph_not_found() {
        let (store, doc) = store_with_paragraphs();
        let id: ParagraphId = "P99-001".parse().unwrap();
        let err = store.apply_edit(&doc, &id, "<p>x</p>", 0).unwrap_err();
        assert!(matches!(err, StoreError::NotFound(_)), "{err:?}");
        // revision 不变（事务回滚）
        assert_eq!(store.get_revision(&doc).unwrap(), 0);
    }
}
