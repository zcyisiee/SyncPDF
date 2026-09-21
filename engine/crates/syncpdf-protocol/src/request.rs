//! 请求（stdin → 引擎）。每行一个对象，`type` 字段做 tag。

use crate::{ProtocolError, Result};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use std::fmt;
use std::path::PathBuf;
use syncpdf_core::ParagraphId;

/// 不落日志、不进 argv 的密钥。`Debug`/`Display` 打码为 `***`；serde 按普通字符串。
#[derive(Clone, Default, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(transparent)]
pub struct Secret(pub String);

impl Secret {
    /// 构造；`None` 交给 `Option<Secret>` 语义（无 key）。
    pub fn new(s: impl Into<String>) -> Self {
        Self(s.into())
    }

    /// 取出明文（仅在真正调用上游时使用）。
    pub fn expose(&self) -> &str {
        &self.0
    }
}

impl fmt::Debug for Secret {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Secret(***)")
    }
}

impl fmt::Display for Secret {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("***")
    }
}

/// LLM 提供方。当前两种通道：OpenAI 兼容 HTTP、pi CLI（本地子进程）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum TranslateProvider {
    /// OpenAI 兼容端点（`base_url` + `model`），经 `TranslatorKind::Http`。
    Http,
    /// pi CLI 子进程（`TranslatorKind::Pi`）。
    Pi,
}

/// 翻译器配置（`configure` 请求的 `translator` 字段）。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum TranslatorKind {
    /// pi CLI：`program` 可执行路径（默认 `pi`）、模型、thinking 档位。
    Pi {
        program: PathBuf,
        model: String,
        thinking: String,
    },
    /// 假翻译器（测试与基准）：`echo` / `stretch:1.4` / `shrink:0.6` / `cjk` /
    /// `fail-every:n` / `slow:ms`。
    Fake { name: String },
    /// OpenAI 兼容 HTTP 端点（`provider`/`base_url`/`model`/`api_key`）。
    Http,
}

/// 输出模式：整页替换或双语对照。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum Mode {
    /// 整页替换（默认）。
    #[default]
    Full,
    /// 双语对照。
    Bilingual,
}

/// stdin 请求。首条必须是 `Configure`；`Run` 启动任务；其余作用于当前文档。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Request {
    /// 全局配置（首条；api_key 只在此出现）。
    Configure {
        /// LLM 通道（http / pi）；决定 `translator` 字段里哪个分支生效。
        provider: TranslateProvider,
        base_url: Option<String>,
        model: String,
        api_key: Option<Secret>,
        concurrency: u32,
        cache_dir: PathBuf,
        translator: TranslatorKind,
    },
    /// 启动翻译任务。
    Run {
        doc_id: String,
        input: PathBuf,
        output: PathBuf,
        source_lang: String,
        target_lang: String,
        /// 1 基页号子集；`None` = 全部页。
        pages: Option<Vec<u32>>,
        font_profile: Option<String>,
        terminology: Option<PathBuf>,
        mode: Mode,
    },
    /// 局部重译。
    Retranslate {
        doc_id: String,
        #[schemars(with = "Vec<crate::schema::ParagraphIdSchema>")]
        paragraph_ids: Vec<ParagraphId>,
    },
    /// 手动编辑 → 重排版 + 局部回写；`base_revision` 不匹配返回 conflict。
    ApplyEdit {
        doc_id: String,
        #[schemars(with = "crate::schema::ParagraphIdSchema")]
        paragraph_id: ParagraphId,
        translated_html: String,
        base_revision: u64,
    },
    /// 发布。
    Export {
        doc_id: String,
        output: PathBuf,
        mode: Mode,
    },
    /// 取消当前任务。
    Cancel,
}

/// 把一行 stdin 解析成请求。
pub fn decode_request(line: &str) -> Result<Request> {
    let trimmed = line.trim();
    if trimmed.is_empty() {
        return Err(ProtocolError::new("empty line"));
    }
    serde_json::from_str(trimmed).map_err(|e| ProtocolError::new(format!("bad request: {e}")))
}

/// 把任意可序列化对象编成单行 JSON（不带换行符）。
pub fn encode_line<T: Serialize>(value: &T) -> String {
    serde_json::to_string(value).expect("JSONL 行序列化不会失败")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn roundtrip(req: Request) {
        let json = serde_json::to_string(&req).unwrap();
        assert!(!json.contains('\n'));
        let back: Request = serde_json::from_str(&json).unwrap();
        assert_eq!(back, req);
        // 同一行为也要能经 decode_request 还原
        assert_eq!(decode_request(&json).unwrap(), req);
    }

    #[test]
    fn request_configure_roundtrip() {
        roundtrip(Request::Configure {
            provider: TranslateProvider::Pi,
            base_url: None,
            model: "deepseek/deepseek-flash".into(),
            api_key: Some(Secret::new("sk-live-123")),
            concurrency: 4,
            cache_dir: "/tmp/syncpdf-cache".into(),
            translator: TranslatorKind::Pi {
                program: "pi".into(),
                model: "deepseek/deepseek-flash".into(),
                thinking: "low".into(),
            },
        });
    }

    #[test]
    fn request_run_roundtrip() {
        roundtrip(Request::Run {
            doc_id: "doc-1".into(),
            input: "/in/a.pdf".into(),
            output: "/out/a-zh.pdf".into(),
            source_lang: "en".into(),
            target_lang: "zh".into(),
            pages: Some(vec![1, 2, 3]),
            font_profile: Some("serif".into()),
            terminology: Some("/terms.csv".into()),
            mode: Mode::Bilingual,
        });
        roundtrip(Request::Run {
            doc_id: "doc-2".into(),
            input: "/in/b.pdf".into(),
            output: "/out/b-zh.pdf".into(),
            source_lang: "en".into(),
            target_lang: "zh".into(),
            pages: None,
            font_profile: None,
            terminology: None,
            mode: Mode::Full,
        });
    }

    #[test]
    fn request_retranslate_roundtrip() {
        roundtrip(Request::Retranslate {
            doc_id: "doc-1".into(),
            paragraph_ids: vec!["P01-001".parse().unwrap(), "P02-017".parse().unwrap()],
        });
    }

    #[test]
    fn request_apply_edit_roundtrip() {
        roundtrip(Request::ApplyEdit {
            doc_id: "doc-1".into(),
            paragraph_id: "P03-005".parse().unwrap(),
            translated_html: "<p id=\"P03-005\">改后的 <span data-style=\"1\">译文</span></p>"
                .into(),
            base_revision: 7,
        });
    }

    #[test]
    fn request_export_and_cancel_roundtrip() {
        roundtrip(Request::Export {
            doc_id: "doc-1".into(),
            output: "/out/export.pdf".into(),
            mode: Mode::Full,
        });
        roundtrip(Request::Cancel);
    }

    #[test]
    fn type_tags_are_snake_case() {
        let cases = [
            (serde_json::to_string(&Request::Cancel).unwrap(), "cancel"),
            (
                serde_json::to_string(&Request::Retranslate {
                    doc_id: "d".into(),
                    paragraph_ids: vec![],
                })
                .unwrap(),
                "retranslate",
            ),
            (
                serde_json::to_string(&Request::ApplyEdit {
                    doc_id: "d".into(),
                    paragraph_id: "P01-001".parse().unwrap(),
                    translated_html: String::new(),
                    base_revision: 0,
                })
                .unwrap(),
                "apply_edit",
            ),
        ];
        for (json, tag) in cases {
            assert!(json.starts_with(&format!("{{\"type\":\"{tag}\"")), "{json}");
        }
    }

    #[test]
    fn secret_masked_in_debug_and_display() {
        let s = Secret::new("sk-super-secret");
        assert_eq!(format!("{s:?}"), "Secret(***)");
        assert_eq!(format!("{s}"), "***");
        // serde 是正常字符串
        assert_eq!(serde_json::to_string(&s).unwrap(), "\"sk-super-secret\"");
    }

    #[test]
    fn secret_absent_from_request_debug() {
        let req = Request::Configure {
            provider: TranslateProvider::Http,
            base_url: Some("https://api.example.com/v1".into()),
            model: "gpt-test".into(),
            api_key: Some(Secret::new("sk-leak-me")),
            concurrency: 2,
            cache_dir: "/tmp/c".into(),
            translator: TranslatorKind::Http,
        };
        let dbg = format!("{req:?}");
        assert!(dbg.contains("Secret(***)"), "{dbg}");
        assert!(!dbg.contains("sk-leak-me"), "{dbg}");
    }

    #[test]
    fn decode_request_rejects_garbage() {
        assert!(decode_request("").is_err());
        assert!(decode_request("   \t ").is_err());
        assert!(decode_request("not json").is_err());
        assert!(decode_request("{\"type\":\"nope\"}").is_err());
        assert!(decode_request("{\"type\":\"cancel\"}extra").is_err());
    }

    #[test]
    fn decode_request_trims_whitespace() {
        assert_eq!(
            decode_request("  {\"type\":\"cancel\"}  \n").unwrap(),
            Request::Cancel
        );
    }

    #[test]
    fn decode_request_bad_paragraph_id() {
        let err = decode_request(
            "{\"type\":\"retranslate\",\"doc_id\":\"d\",\"paragraph_ids\":[\"bad\"]}",
        );
        assert!(err.is_err());
    }
}
