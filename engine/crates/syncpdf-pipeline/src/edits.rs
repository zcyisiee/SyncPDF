//! 编辑请求（`apply_edit` / `retranslate`）：只写本篇库的 `block_edits`，
//! 由随后的 `run` 读出应用（手改段不进模型、待重译段绕过缓存、单块排版按段生效）。

use syncpdf_protocol::Request;

use crate::run::Pipeline;
use crate::stages::{typeset, PipelineError};

impl Pipeline {
    /// 保存一条编辑请求；其它请求类型是协议错误。
    pub fn save_edit(&self, req: &Request) -> Result<(), PipelineError> {
        let store_err = |e: syncpdf_store::StoreError| PipelineError::Store(e.to_string());
        match req {
            Request::ApplyEdit {
                doc_id,
                store,
                paragraph_id,
                translated_html,
                style,
            } => {
                typeset::check_block_style(style).map_err(PipelineError::Protocol)?;
                let style_json = (!style.is_empty())
                    .then(|| serde_json::to_string(style).expect("BlockStyle 序列化不会失败"));
                self.open_edit_store(store.as_deref())?
                    .set_block_edit(
                        doc_id,
                        paragraph_id,
                        translated_html.as_deref(),
                        style_json.as_deref(),
                    )
                    .map_err(store_err)
            }
            Request::Retranslate {
                doc_id,
                store,
                paragraph_ids,
            } => self
                .open_edit_store(store.as_deref())?
                .mark_retranslate(doc_id, paragraph_ids)
                .map_err(store_err),
            _ => Err(PipelineError::Protocol("不是编辑请求".into())),
        }
    }

    fn open_edit_store(
        &self,
        store: Option<&std::path::Path>,
    ) -> Result<syncpdf_store::Store, PipelineError> {
        crate::run::open_store(store.unwrap_or(&self.store_path))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use syncpdf_protocol::{BlockStyle, FontFamily};

    fn pipeline(store: &std::path::Path) -> Pipeline {
        Pipeline {
            store_path: store.to_path_buf(),
            ..Pipeline::default()
        }
    }

    #[test]
    fn apply_edit_and_retranslate_write_the_doc_store() {
        let dir = tempfile::tempdir().unwrap();
        let db = dir.path().join("doc").join("store.db");
        let p = pipeline(&dir.path().join("default.db"));
        let id: syncpdf_core::ParagraphId = "P02-004".parse().unwrap();
        p.save_edit(&Request::ApplyEdit {
            doc_id: "d".into(),
            store: Some(db.clone()),
            paragraph_id: id.clone(),
            translated_html: Some("<p id=\"P02-004\">改</p>".into()),
            style: BlockStyle {
                font_family: Some(FontFamily::Sans),
                ..BlockStyle::default()
            },
        })
        .unwrap();
        let rows = syncpdf_store::Store::open(&db)
            .unwrap()
            .list_block_edits("d")
            .unwrap();
        assert_eq!(
            rows[0].translated_html.as_deref(),
            Some("<p id=\"P02-004\">改</p>")
        );
        assert_eq!(rows[0].style.as_deref(), Some("{\"font_family\":\"sans\"}"));

        p.save_edit(&Request::Retranslate {
            doc_id: "d".into(),
            store: Some(db.clone()),
            paragraph_ids: vec![id],
        })
        .unwrap();
        let rows = syncpdf_store::Store::open(&db)
            .unwrap()
            .list_block_edits("d")
            .unwrap();
        assert!(rows[0].retranslate && rows[0].translated_html.is_none());
        assert!(
            !dir.path().join("default.db").exists(),
            "显式本篇库时不碰默认库"
        );
    }

    #[test]
    fn invalid_style_is_rejected_before_writing() {
        let dir = tempfile::tempdir().unwrap();
        let db = dir.path().join("store.db");
        let err = pipeline(&db)
            .save_edit(&Request::ApplyEdit {
                doc_id: "d".into(),
                store: None,
                paragraph_id: "P01-001".parse().unwrap(),
                translated_html: None,
                style: BlockStyle {
                    line_height: Some(0.0),
                    ..BlockStyle::default()
                },
            })
            .unwrap_err();
        assert_eq!(err.code(), "protocol");
        assert!(!db.exists());
    }
}
