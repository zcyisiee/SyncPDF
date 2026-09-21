//! JSON Schema 导出（M1-01）。
//!
//! `syncpdf-core` 的类型（`ParagraphId`/`Rect`/`CoordSystem`/`ParagraphStatus`）不改，
//! 这里用 `#[schemars(with = "...")]` 指向本地镜像类型，保证 schema 与 serde 行为一致：
//! - `ParagraphId`：`"P{page:02}-{seq:03}"` 格式字符串；
//! - `Rect`：四个 f32 字段的对象；
//! - `CoordSystem`：`"pdf_user" | "image_top_left"`；
//! - `ParagraphStatus`：五种 snake_case 枚举值。

use crate::{Event, Request};
use schemars::JsonSchema;
use schemars::SchemaGenerator;

/// 镜像 `syncpdf_core::ParagraphId` 的 JSON 形态（字符串，`P{page:02}-{seq:03}`）。
#[derive(Debug, Clone, JsonSchema)]
pub struct ParagraphIdSchema(pub String);

/// 镜像 `syncpdf_core::Rect` 的 JSON 形态。
#[derive(Debug, Clone, JsonSchema)]
pub struct RectSchema {
    pub x0: f32,
    pub y0: f32,
    pub x1: f32,
    pub y1: f32,
}

/// 镜像 `syncpdf_core::CoordSystem` 的 JSON 形态。
#[derive(Debug, Clone, Copy, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum CoordSystemSchema {
    PdfUser,
    ImageTopLeft,
}

/// 镜像 `syncpdf_core::ir::ParagraphStatus` 的 JSON 形态。
#[derive(Debug, Clone, Copy, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum ParagraphStatusSchema {
    Pending,
    Translated,
    Typeset,
    NotReplaced,
    Fallback,
}

/// 生成整个协议（请求 + 事件）的 JSON Schema 根文档。
///
/// 返回 `{"$schema":…,"oneOf":[request,event],"$defs":{…}}`，可直接写为
/// `engine/schema/protocol.schema.json`。
#[must_use]
pub fn protocol_schema() -> serde_json::Value {
    let request = SchemaGenerator::default().into_root_schema_for::<Request>();
    let event = SchemaGenerator::default().into_root_schema_for::<Event>();
    let mut value = serde_json::json!({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "SyncPDF protocol",
        "description": "syncpdf-protocol 请求与事件的 JSON Schema（JSONL，每行一个对象，type 字段做 tag）",
        "oneOf": [
            { "$ref": "#/$defs/Request" },
            { "$ref": "#/$defs/Event" },
        ],
        "$defs": {
            "Request": request,
            "Event": event,
        },
    });
    // 挖出两侧子 schema 的 $defs 上提到根（子 $ref 已按 `#/$defs/…` 生成，上提后不断链），
    // 并把子文档里的 `title`/`$schema`/`description` 顶层字段移除，只保留 schema 本体。
    let mut root_defs = serde_json::Map::new();
    for side in ["Request", "Event"] {
        let pointer = format!("/$defs/{side}");
        if let Some(sub) = value.pointer_mut(&pointer).and_then(|v| v.as_object_mut()) {
            if let Some(defs) = sub.remove("$defs") {
                if let Some(obj) = defs.as_object() {
                    for (k, v) in obj {
                        root_defs.insert(k.clone(), v.clone());
                    }
                }
            }
            sub.remove("$schema");
            sub.remove("title");
        }
    }
    if let Some(defs) = value.get_mut("$defs").and_then(|v| v.as_object_mut()) {
        defs.extend(root_defs);
    }
    value
}

/// 把协议 schema 写到 `path`（pretty JSON + 换行结尾）。父目录不存在则创建。
pub fn write_schema_file(path: &std::path::Path) -> std::io::Result<()> {
    use std::io::Write;
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let json = serde_json::to_string_pretty(&protocol_schema()).unwrap();
    let mut f = std::fs::File::create(path)?;
    f.write_all(json.as_bytes())?;
    f.write_all(b"\n")?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// schema 文件输出路径：`engine/schema/protocol.schema.json`。
    fn schema_path() -> std::path::PathBuf {
        std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../schema/protocol.schema.json")
    }

    #[test]
    fn schema_contains_all_type_tags() {
        let schema = protocol_schema();
        let text = serde_json::to_string(&schema).unwrap();
        for tag in [
            // 请求
            "configure",
            "run",
            "retranslate",
            "apply_edit",
            "export",
            "cancel",
            // 事件
            "run_started",
            "stage_started",
            "stage_finished",
            "progress",
            "paragraph",
            "page_ready",
            "issue",
            "document_finished",
            "run_finished",
            "error",
        ] {
            assert!(
                text.contains(&format!("\"{tag}\"")),
                "schema 缺少 tag {tag}"
            );
        }
        // 阶段名
        for stage in [
            "preflight",
            "source_analysis",
            "layout_analysis",
            "paragraph_analysis",
            "translating",
            "typesetting",
            "validating",
            "publishing",
        ] {
            assert!(
                text.contains(&format!("\"{stage}\"")),
                "schema 缺少 stage {stage}"
            );
        }
        // 翻译器分支与模式
        for word in ["pi", "fake", "http", "full", "bilingual"] {
            assert!(text.contains(&format!("\"{word}\"")), "schema 缺少 {word}");
        }
        // 段落状态与坐标系
        for word in [
            "pending",
            "translated",
            "typeset",
            "not_replaced",
            "fallback",
            "pdf_user",
            "image_top_left",
        ] {
            assert!(text.contains(&format!("\"{word}\"")), "schema 缺少 {word}");
        }
    }

    #[test]
    fn schema_defs_are_flat() {
        let schema = protocol_schema();
        // 两侧子 schema 的 $defs 已上提，根 $defs 不再嵌套 $defs
        let defs = schema.pointer("/$defs").unwrap();
        for (name, sub) in defs.as_object().unwrap() {
            assert!(
                sub.get("$defs").is_none(),
                "根 $defs/{name} 内仍有嵌套 $defs"
            );
        }
    }

    /// 生成 `engine/schema/protocol.schema.json`。测试即生成器（brief 指定，不改 xtask）。
    #[test]
    fn export_schema_file() {
        let path = schema_path();
        write_schema_file(&path).expect("写 schema 文件失败");
        let text = std::fs::read_to_string(&path).unwrap();
        assert!(text.starts_with("{\n"));
        assert!(text.trim_end().ends_with('}'));
        let parsed: serde_json::Value = serde_json::from_str(&text).unwrap();
        assert!(parsed.get("oneOf").is_some());
        assert!(parsed.pointer("/$defs/Request").is_some());
        assert!(parsed.pointer("/$defs/Event").is_some());
    }
}
