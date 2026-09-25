//! block_edits 表：按段覆盖（手改译文、待重译标记、单块排版）。
//!
//! 编辑请求只写这张表；`run` 读出后应用：手改段不进模型，待重译段绕过翻译
//! 缓存，单块排版在排版阶段按段生效。`style` 由上游序列化（store 不依赖协议类型）。

use crate::db::Store;
use crate::Result;
use syncpdf_core::ParagraphId;

/// block_edits 一行。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BlockEdit {
    pub paragraph_id: ParagraphId,
    /// 手改译文；`None` = 用模型译文。
    pub translated_html: Option<String>,
    /// 下一次 run 绕过翻译缓存重新请求模型。
    pub retranslate: bool,
    /// 单块排版 JSON；`None` = 无覆盖。
    pub style: Option<String>,
}

impl Store {
    /// 整体替换一段的覆盖（清掉待重译标记）；两者皆空时删行。
    pub fn set_block_edit(
        &self,
        doc_id: &str,
        id: &ParagraphId,
        translated_html: Option<&str>,
        style: Option<&str>,
    ) -> Result<()> {
        if translated_html.is_none() && style.is_none() {
            self.conn().execute(
                "DELETE FROM block_edits WHERE doc_id = ?1 AND paragraph_id = ?2",
                rusqlite::params![doc_id, id.to_string()],
            )?;
            return Ok(());
        }
        self.conn().execute(
            "INSERT INTO block_edits (doc_id, paragraph_id, translated_html, retranslate, style)
             VALUES (?1, ?2, ?3, 0, ?4)
             ON CONFLICT(doc_id, paragraph_id) DO UPDATE SET
               translated_html = excluded.translated_html,
               retranslate = 0,
               style = excluded.style",
            rusqlite::params![doc_id, id.to_string(), translated_html, style],
        )?;
        Ok(())
    }

    /// 标记待重译：清掉手改译文，保留单块排版。
    pub fn mark_retranslate(&self, doc_id: &str, ids: &[ParagraphId]) -> Result<()> {
        let tx = self.conn().unchecked_transaction()?;
        for id in ids {
            tx.execute(
                "INSERT INTO block_edits (doc_id, paragraph_id, translated_html, retranslate)
                 VALUES (?1, ?2, NULL, 1)
                 ON CONFLICT(doc_id, paragraph_id) DO UPDATE SET
                   translated_html = NULL,
                   retranslate = 1",
                rusqlite::params![doc_id, id.to_string()],
            )?;
        }
        tx.commit()?;
        Ok(())
    }

    /// 重译已拿到新译文（已进翻译缓存）：清标记；无排版覆盖的行随之删除。
    pub fn clear_retranslate(&self, doc_id: &str, id: &ParagraphId) -> Result<()> {
        let tx = self.conn().unchecked_transaction()?;
        tx.execute(
            "UPDATE block_edits SET retranslate = 0 WHERE doc_id = ?1 AND paragraph_id = ?2",
            rusqlite::params![doc_id, id.to_string()],
        )?;
        tx.execute(
            "DELETE FROM block_edits WHERE doc_id = ?1 AND paragraph_id = ?2
               AND translated_html IS NULL AND style IS NULL AND retranslate = 0",
            rusqlite::params![doc_id, id.to_string()],
        )?;
        tx.commit()?;
        Ok(())
    }

    /// 本篇全部覆盖（按段 id 升序）。
    pub fn list_block_edits(&self, doc_id: &str) -> Result<Vec<BlockEdit>> {
        let mut stmt = self.conn().prepare(
            "SELECT paragraph_id, translated_html, retranslate, style
             FROM block_edits WHERE doc_id = ?1 ORDER BY paragraph_id ASC",
        )?;
        let rows = stmt.query_map([doc_id], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, Option<String>>(1)?,
                row.get::<_, i64>(2)?,
                row.get::<_, Option<String>>(3)?,
            ))
        })?;
        let mut out = Vec::new();
        for row in rows {
            let (pid, translated_html, retranslate, style) = row?;
            out.push(BlockEdit {
                paragraph_id: pid.parse().map_err(|e| {
                    crate::StoreError::Invalid(format!("bad paragraph id in db: {e}"))
                })?,
                translated_html,
                retranslate: retranslate != 0,
                style,
            });
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pid(s: &str) -> ParagraphId {
        s.parse().unwrap()
    }

    #[test]
    fn edit_replaces_whole_override_and_empty_edit_deletes() {
        let store = Store::open_memory().unwrap();
        store
            .set_block_edit("d", &pid("P01-002"), Some("<p id=\"P01-002\">改</p>"), None)
            .unwrap();
        store
            .set_block_edit("d", &pid("P01-002"), None, Some("{\"font_scale\":0.9}"))
            .unwrap();
        assert_eq!(
            store.list_block_edits("d").unwrap(),
            vec![BlockEdit {
                paragraph_id: pid("P01-002"),
                translated_html: None,
                retranslate: false,
                style: Some("{\"font_scale\":0.9}".into()),
            }]
        );
        store
            .set_block_edit("d", &pid("P01-002"), None, None)
            .unwrap();
        assert!(store.list_block_edits("d").unwrap().is_empty());
    }

    #[test]
    fn retranslate_drops_manual_text_keeps_style_and_clears_after_success() {
        let store = Store::open_memory().unwrap();
        store
            .set_block_edit(
                "d",
                &pid("P01-001"),
                Some("<p id=\"P01-001\">改</p>"),
                Some("{}"),
            )
            .unwrap();
        store
            .mark_retranslate("d", &[pid("P01-001"), pid("P02-003")])
            .unwrap();
        let rows = store.list_block_edits("d").unwrap();
        assert_eq!(rows.len(), 2);
        assert!(rows
            .iter()
            .all(|r| r.retranslate && r.translated_html.is_none()));
        assert_eq!(rows[0].style.as_deref(), Some("{}"));

        store.clear_retranslate("d", &pid("P01-001")).unwrap();
        store.clear_retranslate("d", &pid("P02-003")).unwrap();
        let rows = store.list_block_edits("d").unwrap();
        // 有排版覆盖的行留下，纯重译标记的行删除
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].paragraph_id, pid("P01-001"));
        assert!(!rows[0].retranslate);
    }

    #[test]
    fn edits_are_scoped_per_document() {
        let store = Store::open_memory().unwrap();
        store
            .set_block_edit("a", &pid("P01-001"), Some("x"), None)
            .unwrap();
        assert!(store.list_block_edits("b").unwrap().is_empty());
    }
}
