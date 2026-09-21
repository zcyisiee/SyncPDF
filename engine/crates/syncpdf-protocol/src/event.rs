//! 事件（引擎 → stdout）。每行一个对象，`type` 字段做 tag；`Envelope` 加 `seq`/`ts`。

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use syncpdf_core::ir::ParagraphStatus;
use syncpdf_core::{CoordSystem, ParagraphId, Rect};

/// 当前协议版本（`run_started.protocol_version`）。
pub const PROTOCOL_VERSION: u32 = 1;

/// `PROTOCOL_VERSION` 的字面名，供文档引用。
pub const PROTOCOL_VERSION_NAME: &str = "protocol_version";

/// 处理阶段名（snake_case，与 hjfy 事件语义一致）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum Stage {
    Preflight,
    SourceAnalysis,
    LayoutAnalysis,
    ParagraphAnalysis,
    Translating,
    Typesetting,
    Validating,
    Publishing,
}

/// issue 严重级别。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum Severity {
    /// 提示性信息（不影响产物）。
    Info,
    /// 可恢复问题（回退 / 降级已发生）。
    Warning,
    /// 阻断该段或该页的问题。
    Error,
}

/// `document_finished` 的统计（§9.3：字体数、膨胀比、fallback 数）。
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize, Default, JsonSchema)]
pub struct Stats {
    /// 嵌入的目标字体数。
    pub fonts: u32,
    /// 输出 / 输入字节数之比。
    pub expansion_ratio: f64,
    /// 回退原文的段落数。
    pub fallbacks: u32,
}

/// stdout 事件（不含 `seq`/`ts` 信封字段）。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Event {
    /// 任务开始。
    RunStarted {
        protocol_version: u32,
        engine_version: String,
        doc_id: String,
        pages: u32,
    },
    /// 阶段开始。
    StageStarted { stage: Stage },
    /// 阶段结束（`elapsed_ms` 为该阶段耗时）。
    StageFinished { stage: Stage, elapsed_ms: u64 },
    /// 进度（不编造 ETA）。
    Progress { stage: Stage, done: u32, total: u32 },
    /// 段落状态更新。
    Paragraph {
        #[schemars(with = "crate::schema::ParagraphIdSchema")]
        paragraph_id: ParagraphId,
        page: u32,
        #[schemars(with = "crate::schema::ParagraphStatusSchema")]
        status: ParagraphStatus,
        /// `None` = 未识别；`Some([])` = 识别了但无框（规约 #12/#13）。
        #[schemars(with = "Option<Vec<crate::schema::RectSchema>>")]
        boxes: Option<Vec<Rect>>,
        #[schemars(with = "crate::schema::CoordSystemSchema")]
        coord_system: CoordSystem,
        translated_html: Option<String>,
    },
    /// 一页完成（可选增量预览）。
    PageReady {
        page: u32,
        preview_path: Option<PathBuf>,
        revision: u64,
    },
    /// 问题报告。
    Issue {
        severity: Severity,
        code: String,
        #[schemars(with = "Option<crate::schema::ParagraphIdSchema>")]
        paragraph_id: Option<ParagraphId>,
        page: Option<u32>,
        message: String,
    },
    /// 文档完成。
    DocumentFinished { output: PathBuf, stats: Stats },
    /// 任务结束。
    RunFinished { ok: bool, elapsed_ms: u64 },
    /// 错误（`fatal` = 任务无法继续）。
    Error {
        fatal: bool,
        code: String,
        message: String,
    },
}

/// 事件信封：`{"seq":n,"ts":f,...事件字段}`。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct Envelope {
    /// 单调递增序号；前端断线后用 `seq` 续传。
    pub seq: u64,
    /// Unix 时间戳（秒，浮点）。
    pub ts: f64,
    #[serde(flatten)]
    pub event: Event,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn envelope(event: Event) -> Envelope {
        Envelope {
            seq: 123,
            ts: 1727000000.123,
            event,
        }
    }

    fn roundtrip(event: Event) {
        let env = envelope(event.clone());
        let json = serde_json::to_string(&env).unwrap();
        assert!(!json.contains('\n'));
        let back: Envelope = serde_json::from_str(&json).unwrap();
        assert_eq!(back, env);
        // 事件本体也要能独立往返
        let event_json = serde_json::to_string(&event).unwrap();
        assert_eq!(serde_json::from_str::<Event>(&event_json).unwrap(), event);
    }

    #[test]
    fn event_run_started_roundtrip() {
        roundtrip(Event::RunStarted {
            protocol_version: PROTOCOL_VERSION,
            engine_version: "0.1.0".into(),
            doc_id: "doc-1".into(),
            pages: 12,
        });
    }

    #[test]
    fn event_stage_roundtrip() {
        roundtrip(Event::StageStarted {
            stage: Stage::Translating,
        });
        roundtrip(Event::StageFinished {
            stage: Stage::Preflight,
            elapsed_ms: 4200,
        });
    }

    #[test]
    fn event_progress_roundtrip() {
        roundtrip(Event::Progress {
            stage: Stage::Translating,
            done: 42,
            total: 169,
        });
    }

    #[test]
    fn event_paragraph_boxes_three_states() {
        let id: ParagraphId = "P12-169".parse().unwrap();
        // None = 未识别 → JSON null
        roundtrip(Event::Paragraph {
            paragraph_id: id.clone(),
            page: 12,
            status: ParagraphStatus::Translated,
            boxes: None,
            coord_system: CoordSystem::PdfUser,
            translated_html: Some("<p id=\"P12-169\">你好</p>".into()),
        });
        // Some([]) = 识别了但无框 → JSON []
        let json = serde_json::to_string(&Event::Paragraph {
            paragraph_id: id.clone(),
            page: 12,
            status: ParagraphStatus::NotReplaced,
            boxes: Some(vec![]),
            coord_system: CoordSystem::PdfUser,
            translated_html: None,
        })
        .unwrap();
        assert!(json.contains("\"boxes\":[]"), "{json}");
        // 有框
        roundtrip(Event::Paragraph {
            paragraph_id: id.clone(),
            page: 12,
            status: ParagraphStatus::Typeset,
            boxes: Some(vec![Rect::new(10.0, 20.0, 110.0, 32.0)]),
            coord_system: CoordSystem::ImageTopLeft,
            translated_html: None,
        });
    }

    #[test]
    fn event_page_ready_roundtrip() {
        roundtrip(Event::PageReady {
            page: 3,
            preview_path: Some("/cache/preview-3.pdf".into()),
            revision: 9,
        });
        roundtrip(Event::PageReady {
            page: 4,
            preview_path: None,
            revision: 0,
        });
    }

    #[test]
    fn event_issue_roundtrip() {
        roundtrip(Event::Issue {
            severity: Severity::Warning,
            code: "overflow".into(),
            paragraph_id: Some("P02-017".parse().unwrap()),
            page: Some(2),
            message: "paragraph overflowed its box".into(),
        });
        roundtrip(Event::Issue {
            severity: Severity::Error,
            code: "conflict".into(),
            paragraph_id: None,
            page: None,
            message: "base_revision mismatch".into(),
        });
    }

    #[test]
    fn event_document_finished_roundtrip() {
        roundtrip(Event::DocumentFinished {
            output: "/out/a-zh.pdf".into(),
            stats: Stats {
                fonts: 5,
                expansion_ratio: 1.42,
                fallbacks: 3,
            },
        });
    }

    #[test]
    fn event_terminal_roundtrip() {
        roundtrip(Event::RunFinished {
            ok: true,
            elapsed_ms: 15_000,
        });
        roundtrip(Event::Error {
            fatal: true,
            code: "encrypted_pdf".into(),
            message: "input is encrypted".into(),
        });
    }

    #[test]
    fn progress_line_exact_string() {
        // 与 02-技术路径与架构.md §9.3 的示例行完全一致（字段顺序 seq, ts, type, …）
        let json = serde_json::to_string(&Envelope {
            seq: 123,
            ts: 1727000000.123,
            event: Event::Progress {
                stage: Stage::Translating,
                done: 42,
                total: 169,
            },
        })
        .unwrap();
        assert_eq!(
            json,
            "{\"seq\":123,\"ts\":1727000000.123,\"type\":\"progress\",\"stage\":\"translating\",\"done\":42,\"total\":169}"
        );
    }

    #[test]
    fn cancel_line_exact_string() {
        let json = serde_json::to_string(&Event::Error {
            fatal: false,
            code: "conflict".into(),
            message: "revision mismatch".into(),
        })
        .unwrap();
        assert_eq!(
            json,
            "{\"type\":\"error\",\"fatal\":false,\"code\":\"conflict\",\"message\":\"revision mismatch\"}"
        );
    }

    #[test]
    fn run_started_line_exact_string() {
        let json = serde_json::to_string(&Envelope {
            seq: 1,
            ts: 0.0,
            event: Event::RunStarted {
                protocol_version: 1,
                engine_version: "0.1.0".into(),
                doc_id: "d".into(),
                pages: 2,
            },
        })
        .unwrap();
        assert_eq!(
            json,
            "{\"seq\":1,\"ts\":0.0,\"type\":\"run_started\",\"protocol_version\":1,\"engine_version\":\"0.1.0\",\"doc_id\":\"d\",\"pages\":2}"
        );
    }

    #[test]
    fn paragraph_null_vs_empty_array_distinguished() {
        let id: ParagraphId = "P01-001".parse().unwrap();
        let null_boxes = serde_json::to_string(&Event::Paragraph {
            paragraph_id: id.clone(),
            page: 1,
            status: ParagraphStatus::Pending,
            boxes: None,
            coord_system: CoordSystem::PdfUser,
            translated_html: None,
        })
        .unwrap();
        assert!(null_boxes.contains("\"boxes\":null"), "{null_boxes}");

        let empty_boxes = serde_json::to_string(&Event::Paragraph {
            paragraph_id: id.clone(),
            page: 1,
            status: ParagraphStatus::Pending,
            boxes: Some(vec![]),
            coord_system: CoordSystem::PdfUser,
            translated_html: None,
        })
        .unwrap();
        assert!(empty_boxes.contains("\"boxes\":[]"), "{empty_boxes}");
        assert_ne!(null_boxes, empty_boxes);
    }

    #[test]
    fn envelope_flattens_event_fields() {
        let value: serde_json::Value = serde_json::to_value(&Envelope {
            seq: 5,
            ts: 1.5,
            event: Event::StageStarted {
                stage: Stage::Publishing,
            },
        })
        .unwrap();
        let obj = value.as_object().unwrap();
        assert_eq!(obj.len(), 4); // seq, ts, type, stage
        assert_eq!(obj["seq"], serde_json::json!(5));
        assert_eq!(obj["ts"], serde_json::json!(1.5));
        assert_eq!(obj["type"], serde_json::json!("stage_started"));
        assert_eq!(obj["stage"], serde_json::json!("publishing"));
    }

    #[test]
    fn protocol_version_is_one() {
        assert_eq!(PROTOCOL_VERSION, 1);
        assert_eq!(PROTOCOL_VERSION_NAME, "protocol_version");
    }
}
