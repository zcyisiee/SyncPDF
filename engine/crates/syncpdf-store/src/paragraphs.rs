//! paragraphs 表：段落状态与最新译文。

use crate::db::Store;
use crate::Result;
use syncpdf_core::ids::ParseParagraphIdError;
use syncpdf_core::ParagraphId;

/// paragraphs 一行。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParagraphRow {
    pub paragraph_id: ParagraphId,
    pub page: u32,
    /// snake_case 状态（与 `syncpdf_core::ir::ParagraphStatus` 的 serde 形态一致）。
    pub status: String,
    pub translated_html: Option<String>,
}

impl Store {
    /// 写入或更新段落（同 `(doc_id, paragraph_id)` upsert）。
    /// `status` 用字符串以避免 store 依赖 `syncpdf_core::ir` 的枚举语义
    /// （`ParagraphStatus` 的 serde 产物就是 snake_case 字符串，等价）。
    pub fn upsert_paragraph(
        &self,
        doc_id: &str,
        id: &ParagraphId,
        page: u32,
        status: &str,
        translated_html: Option<&str>,
    ) -> Result<()> {
        self.conn().execute(
            "INSERT INTO paragraphs (doc_id, paragraph_id, page, status, translated_html)
             VALUES (?1, ?2, ?3, ?4, ?5)
             ON CONFLICT(doc_id, paragraph_id) DO UPDATE SET
               page = excluded.page,
               status = excluded.status,
               translated_html = excluded.translated_html",
            rusqlite::params![doc_id, id.to_string(), page as i64, status, translated_html],
        )?;
        Ok(())
    }

    /// 列出文档全部段落（按页、段号升序）。
    pub fn list_paragraphs(&self, doc_id: &str) -> Result<Vec<ParagraphRow>> {
        let mut stmt = self.conn().prepare(
            "SELECT paragraph_id, page, status, translated_html
             FROM paragraphs
             WHERE doc_id = ?1
             ORDER BY page ASC, paragraph_id ASC",
        )?;
        let rows = stmt.query_map([doc_id], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, i64>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, Option<String>>(3)?,
            ))
        })?;
        let mut out = Vec::new();
        for row in rows {
            let (pid, page, status, html) = row?;
            out.push(ParagraphRow {
                paragraph_id: pid.parse().map_err(|e| {
                    crate::StoreError::Invalid(format!("bad paragraph id in db: {e}"))
                })?,
                page: u32::try_from(page)
                    .map_err(|_| crate::StoreError::Invalid("page out of u32 range".into()))?,
                status,
                translated_html: html,
            });
        }
        Ok(out)
    }

    /// 取单个段落。
    pub fn get_paragraph(&self, doc_id: &str, id: &ParagraphId) -> Result<Option<ParagraphRow>> {
        let mut stmt = self.conn().prepare(
            "SELECT paragraph_id, page, status, translated_html
             FROM paragraphs
             WHERE doc_id = ?1 AND paragraph_id = ?2",
        )?;
        let mut rows = stmt.query(rusqlite::params![doc_id, id.to_string()])?;
        let Some(row) = rows.next()? else {
            return Ok(None);
        };
        Ok(Some(ParagraphRow {
            paragraph_id: row
                .get::<_, String>(0)?
                .parse()
                .map_err(|e: ParseParagraphIdError| crate::StoreError::Invalid(e.to_string()))?,
            page: u32::try_from(row.get::<_, i64>(1)?)
                .map_err(|_| crate::StoreError::Invalid("page out of u32 range".into()))?,
            status: row.get(2)?,
            translated_html: row.get(3)?,
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn store_with_doc() -> (Store, String) {
        let store = Store::open_memory().unwrap();
        let doc = "doc-1".to_string();
        store
            .upsert_document(
                &doc,
                &Sha256Hash::of(b"d"),
                std::path::Path::new("/a.pdf"),
                3,
            )
            .unwrap();
        (store, doc)
    }

    use syncpdf_core::hash::Sha256Hash;

    #[test]
    fn upsert_and_list() {
        let (store, doc) = store_with_doc();
        let p1: ParagraphId = "P01-002".parse().unwrap();
        let p2: ParagraphId = "P01-001".parse().unwrap();
        let p3: ParagraphId = "P02-001".parse().unwrap();
        store
            .upsert_paragraph(&doc, &p1, 1, "pending", None)
            .unwrap();
        store
            .upsert_paragraph(&doc, &p2, 1, "translated", Some("<p>一</p>"))
            .unwrap();
        store
            .upsert_paragraph(&doc, &p3, 2, "typeset", Some("<p>二</p>"))
            .unwrap();

        let rows = store.list_paragraphs(&doc).unwrap();
        assert_eq!(rows.len(), 3);
        // 同页内按 paragraph_id 字符串序（P01-001 < P01-002），页升序
        assert_eq!(rows[0].paragraph_id, p2);
        assert_eq!(rows[1].paragraph_id, p1);
        assert_eq!(rows[2].paragraph_id, p3);
        assert_eq!(rows[0].status, "translated");
        assert_eq!(rows[0].translated_html.as_deref(), Some("<p>一</p>"));
    }

    #[test]
    fn upsert_overwrites() {
        let (store, doc) = store_with_doc();
        let p: ParagraphId = "P01-001".parse().unwrap();
        store
            .upsert_paragraph(&doc, &p, 1, "pending", None)
            .unwrap();
        store
            .upsert_paragraph(&doc, &p, 1, "typeset", Some("<p>新</p>"))
            .unwrap();
        let rows = store.list_paragraphs(&doc).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].status, "typeset");
        assert_eq!(rows[0].translated_html.as_deref(), Some("<p>新</p>"));
        assert_eq!(store.get_paragraph(&doc, &p).unwrap().unwrap(), rows[0]);
    }

    #[test]
    fn get_missing_returns_none() {
        let (store, doc) = store_with_doc();
        let p: ParagraphId = "P01-001".parse().unwrap();
        assert_eq!(store.get_paragraph(&doc, &p).unwrap(), None);
        assert!(store.list_paragraphs(&doc).unwrap().is_empty());
    }
}
