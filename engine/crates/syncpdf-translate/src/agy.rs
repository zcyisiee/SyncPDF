//! agy CLI 翻译通道（Antigravity print mode + stream-json）。
//!
//! 本机 `agy` 的可用语法已用真实小探针核实（不是从文档猜的）：
//!
//! ```text
//! agy --model <m> --disable-slash-commands --sandbox \
//!     --input-format stream-json --output-format stream-json --print-timeout 180s
//! ```
//!
//! - **提示词走 stdin 的一行 NDJSON**，不进 argv：`{"event":"user","message":{"role":"user",
//!   "content":[{"type":"text","text":"…"}]}}`。写完整行后关闭 stdin，print 模式在该回合
//!   结束后正常退出。
//! - 该通道没有 `--system-prompt`，所以 `DocumentPrompt.system` 拼在正文之前同一条消息里。
//! - stdout 每行一个事件对象（探针实测字段）：
//!   `{"event":"init",…}`、`{"event":"step_update","step_update":{"step_type":
//!   "agent_response","state":"ACTIVE","text_delta":"…"}}`、`{"event":"result","result":
//!   {"status":"SUCCESS|ERROR|CANCELLED","response":"…"}}`。
//!   增量文本只取 `step_type == "agent_response"` 的 `text_delta`；`result.response` 是同一
//!   串文本的最终快照，**不再追加**，否则每个块都会被写两遍。
//! - `user_input` / `thinking` 不属于译文；`tool` / `tool_calls` 立即中止，本通道只允许纯文本翻译。
//! - 只有出现终止 `result` 事件本次请求才算正常结束；缺它按通道失败处理，不把截断流当成功。
//! - cwd 用临时目录（工具看不到本仓库）；`--sandbox` 限制终端；不授予
//!   `--dangerously-skip-permissions`。stderr 丢弃：可能含认证与提示词片段。

use std::path::PathBuf;
use std::process::Stdio;
use std::time::Duration;

use async_trait::async_trait;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::Command;

use crate::prompt::DocumentPrompt;
use crate::translator::{DeltaSink, TranslateError, Translator};

/// 默认模型（含档位的模型名，`agy models` 实际列出）。
pub const DEFAULT_MODEL: &str = "gemini-3.8-flash-low";
/// 默认超时：整文档 one-shot 可能要跑很久（与 pi 通道一致）。
pub const DEFAULT_TIMEOUT: Duration = Duration::from_secs(1810);

/// 经本机 `agy` CLI（print + stream-json）翻译。
#[derive(Debug, Clone)]
pub struct AgyTranslator {
    /// 可执行文件，默认 `agy`（走 PATH）。
    pub program: PathBuf,
    pub model: String,
    pub timeout: Duration,
    /// `name()` 的缓存串（`agy/<model>`）。
    label: String,
}

impl Default for AgyTranslator {
    fn default() -> Self {
        Self::new("agy", DEFAULT_MODEL)
    }
}

impl AgyTranslator {
    pub fn new(program: impl Into<PathBuf>, model: impl Into<String>) -> Self {
        let model = model.into();
        let label = format!("agy/{model}");
        Self {
            program: program.into(),
            model,
            timeout: DEFAULT_TIMEOUT,
            label,
        }
    }

    pub fn with_timeout(mut self, timeout: Duration) -> Self {
        self.timeout = timeout;
        self
    }

    /// 构造 argv（不含程序名）。提示词绝不进 argv，便于上游审计与测试。
    pub fn args(&self) -> Vec<String> {
        vec![
            "--model".to_string(),
            self.model.clone(),
            // 模型名已带档位（如 -low），再传 --effort 会与之冲突，故不传。
            "--disable-slash-commands".to_string(),
            "--sandbox".to_string(),
            "--input-format".to_string(),
            "stream-json".to_string(),
            "--output-format".to_string(),
            "stream-json".to_string(),
            // 让 agy 自己也有上界；本地读超时是兜底。
            "--print-timeout".to_string(),
            format!("{}s", self.timeout.as_secs()),
        ]
    }

    /// stdin 那一行 NDJSON（不含换行）。system 与正文拼在一条 user 消息里。
    pub fn request(&self, prompt: &DocumentPrompt) -> String {
        let text = format!(
            "Translate the supplied document as text only. Do not use any tools, inspect files, browse, or execute commands.\n\n{}\n\n{}",
            prompt.system, prompt.text
        );
        serde_json::json!({
            "event": "user",
            "message": {
                "role": "user",
                "content": [{ "type": "text", "text": text }],
            }
        })
        .to_string()
    }

    async fn run(
        &self,
        prompt: &DocumentPrompt,
        on_delta: DeltaSink<'_>,
    ) -> Result<String, TranslateError> {
        let cwd = TempCwd::new()?;
        let mut child = Command::new(&self.program)
            .args(self.args())
            .current_dir(cwd.path())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            // 认证细节与提示词片段可能出现在 stderr，一律丢弃。
            .stderr(Stdio::null())
            .kill_on_drop(true)
            .spawn()
            .map_err(|e| match e.kind() {
                std::io::ErrorKind::NotFound => TranslateError::Unavailable(format!(
                    "{} not found in PATH",
                    self.program.display()
                )),
                _ => TranslateError::Unavailable(format!("cannot start agy: {}", e.kind())),
            })?;

        let mut stdin = child
            .stdin
            .take()
            .ok_or_else(|| TranslateError::HarnessFailed("agy stdin unavailable".into()))?;
        let body = format!("{}\n", self.request(prompt));
        let writer = tokio::spawn(async move {
            let _ = stdin.write_all(body.as_bytes()).await;
            let _ = stdin.shutdown().await;
        });

        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| TranslateError::HarnessFailed("agy stdout unavailable".into()))?;

        let mut text = String::new();
        let mut terminal_error: Option<String> = None;
        let mut saw_result = false;
        let mut lines = BufReader::new(stdout).lines();
        loop {
            let next = tokio::time::timeout(self.timeout, lines.next_line()).await;
            let line = match next {
                Err(_) => {
                    let _ = child.start_kill();
                    writer.abort();
                    return Err(TranslateError::Timeout(self.timeout));
                }
                Ok(Err(e)) => {
                    let _ = child.start_kill();
                    writer.abort();
                    return Err(TranslateError::HarnessFailed(format!(
                        "cannot read agy stdout: {}",
                        e.kind()
                    )));
                }
                Ok(Ok(None)) => break,
                Ok(Ok(Some(l))) => l,
            };
            match parse_line(&line) {
                Event::Delta(d) => {
                    text.push_str(&d);
                    on_delta(&d);
                }
                Event::Finished(Err(reason)) => {
                    saw_result = true;
                    terminal_error = Some(reason);
                }
                Event::Finished(Ok(())) => saw_result = true,
                Event::ToolCall => {
                    let _ = child.start_kill();
                    writer.abort();
                    return Err(TranslateError::HarnessFailed(
                        "agy attempted a tool call during text-only translation".into(),
                    ));
                }
                Event::Ignore => {}
            }
        }
        let _ = writer.await;

        let status = tokio::time::timeout(self.timeout, child.wait())
            .await
            .map_err(|_| TranslateError::Timeout(self.timeout))?
            .map_err(|e| TranslateError::HarnessFailed(format!("cannot reap agy: {}", e.kind())))?;

        if !status.success() {
            // 只报退出码，不带 stderr / 提示词正文。
            return Err(TranslateError::HarnessFailed(format!(
                "agy exited with {status}"
            )));
        }
        if let Some(reason) = terminal_error {
            return Err(TranslateError::HarnessFailed(format!(
                "agy returned an incomplete response ({reason})"
            )));
        }
        if !saw_result {
            // 没有终止 result 事件的流不能当成功：可能中途断连。
            return Err(TranslateError::HarnessFailed(
                "agy stream ended without a terminal result event".into(),
            ));
        }
        if text.trim().is_empty() {
            return Err(TranslateError::HarnessFailed(
                "agy returned an empty response".into(),
            ));
        }
        Ok(text)
    }
}

#[async_trait]
impl Translator for AgyTranslator {
    async fn translate(
        &self,
        prompt: &DocumentPrompt,
        on_delta: DeltaSink<'_>,
    ) -> Result<String, TranslateError> {
        self.run(prompt, on_delta).await
    }

    fn name(&self) -> &str {
        &self.label
    }
}

/// stdout 一行 JSON 的解读结果。
#[derive(Debug, PartialEq, Eq)]
enum Event {
    /// 增量译文文本。
    Delta(String),
    /// 终止 `result` 事件：`Ok` = 正常，`Err` = 状态不是 SUCCESS。
    Finished(Result<(), String>),
    /// 纯文本翻译不得调用工具。
    ToolCall,
    /// 与译文无关的事件（init、user_input、thinking）。
    Ignore,
}

/// 解析一行 JSONL。坏行一律忽略（CLI 可能混入非 JSON 噪声）。
fn parse_line(line: &str) -> Event {
    let line = line.trim();
    if line.is_empty() {
        return Event::Ignore;
    }
    let Ok(v) = serde_json::from_str::<serde_json::Value>(line) else {
        return Event::Ignore;
    };
    match v.get("event").and_then(|e| e.as_str()) {
        // 增量只认 agent_response；thinking / user_input / tool_calls 不是译文。
        Some("step_update") => {
            let Some(step) = v.get("step_update") else {
                return Event::Ignore;
            };
            match step.get("step_type").and_then(|t| t.as_str()) {
                Some("tool" | "tool_calls") => return Event::ToolCall,
                Some("agent_response") => {}
                _ => return Event::Ignore,
            }
            match step.get("text_delta").and_then(|d| d.as_str()) {
                Some(d) if !d.is_empty() => Event::Delta(d.to_string()),
                _ => Event::Ignore,
            }
        }
        Some("result") => match v
            .get("result")
            .and_then(|r| r.get("status"))
            .and_then(|s| s.as_str())
        {
            Some("SUCCESS") => Event::Finished(Ok(())),
            Some(other) => Event::Finished(Err(format!("status={other}"))),
            None => Event::Finished(Err("missing result status".into())),
        },
        _ => Event::Ignore,
    }
}

/// 进程 cwd 用的临时目录；`Drop` 时尽力删除。
#[derive(Debug)]
struct TempCwd(PathBuf);

impl TempCwd {
    fn new() -> Result<Self, TranslateError> {
        // 不引入运行期 tempfile 依赖：pid + 单调计数即可保证本机唯一。
        use std::sync::atomic::{AtomicU64, Ordering};
        static N: AtomicU64 = AtomicU64::new(0);
        let p = std::env::temp_dir().join(format!(
            "syncpdf-agy-{}-{}",
            std::process::id(),
            N.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir_all(&p)
            .map_err(|e| TranslateError::Unavailable(format!("cannot create cwd: {}", e.kind())))?;
        Ok(Self(p))
    }

    fn path(&self) -> &std::path::Path {
        &self.0
    }
}

impl Drop for TempCwd {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::prompt::{build_document_prompts, PromptSpec};
    use crate::stream::BlockStream;
    use crate::unit::Unit;
    use std::collections::HashMap;

    fn unit(id: &str, body: &str) -> Unit {
        Unit {
            id: id.parse().unwrap(),
            html: format!("<p id=\"{id}\">{body}</p>"),
            styles: 0,
            atoms: vec![],
            breaks: 0,
        }
    }

    fn prompt() -> DocumentPrompt {
        let us = vec![unit("P01-001", "one"), unit("P01-002", "two")];
        build_document_prompts(&PromptSpec::new("en", "zh-CN"), &us, &HashMap::new())
            .unwrap()
            .remove(0)
    }

    #[test]
    fn argv_carries_no_prompt_and_streams_over_stdin() {
        let t = AgyTranslator::new("agy", "gemini-3.8-flash-low");
        assert_eq!(
            t.args(),
            vec![
                "--model",
                "gemini-3.8-flash-low",
                "--disable-slash-commands",
                "--sandbox",
                "--input-format",
                "stream-json",
                "--output-format",
                "stream-json",
                "--print-timeout",
                "1810s",
            ]
        );
        assert_eq!(t.name(), "agy/gemini-3.8-flash-low");
        assert!(!t.args().iter().any(|a| a.contains("DOCUMENT:")));
        // 提示词在 stdin 的那行 NDJSON 里，system 与正文同一条 user 消息。
        let req: serde_json::Value = serde_json::from_str(&t.request(&prompt())).unwrap();
        assert_eq!(req["event"], "user");
        let text = req["message"]["content"][0]["text"].as_str().unwrap();
        assert!(text.contains("DOCUMENT:"));
        assert!(text.contains("Translate natural-language text"));
        // NDJSON 请求必须恰好一行：换行在 JSON 字符串里转义。
        assert_eq!(t.request(&prompt()).lines().count(), 1);
    }

    #[test]
    fn parse_line_takes_agent_response_deltas_only() {
        assert_eq!(
            parse_line(
                r#"{"event":"step_update","step_update":{"step_index":1,"state":"ACTIVE","step_type":"agent_response","text_delta":"<p id="}}"#
            ),
            Event::Delta("<p id=".into())
        );
        // thinking / user_input 不是译文；工具调用明确拒绝。
        assert_eq!(
            parse_line(
                r#"{"event":"step_update","step_update":{"step_type":"thinking","text_delta":"hmm"}}"#
            ),
            Event::Ignore
        );
        assert_eq!(
            parse_line(
                r#"{"event":"step_update","step_update":{"step_type":"user_input","state":"DONE"}}"#
            ),
            Event::Ignore
        );
        assert_eq!(
            parse_line(
                r#"{"event":"step_update","step_update":{"step_type":"tool_calls","text_delta":"x"}}"#
            ),
            Event::ToolCall
        );
        assert_eq!(
            parse_line(r#"{"event":"init","init":{"model":"gemini-3.8-flash-low"}}"#),
            Event::Ignore
        );
        assert_eq!(parse_line("not json at all"), Event::Ignore);
        assert_eq!(parse_line("   "), Event::Ignore);
        // 终态：只有 SUCCESS 算正常。
        assert_eq!(
            parse_line(r#"{"event":"result","result":{"status":"SUCCESS","response":"x"}}"#),
            Event::Finished(Ok(()))
        );
        assert_eq!(
            parse_line(r#"{"event":"result","result":{"status":"ERROR","response":""}}"#),
            Event::Finished(Err("status=ERROR".into()))
        );
        assert_eq!(
            parse_line(r#"{"event":"result","result":{"status":"CANCELLED"}}"#),
            Event::Finished(Err("status=CANCELLED".into()))
        );
    }

    // ── 假 agy 脚本：读 stdin 丢弃，printf 预录 JSONL ─────────────────────
    #[cfg(unix)]
    mod fake_cli {
        use super::*;
        use std::io::Write;
        use std::os::unix::fs::PermissionsExt;

        /// 把一个 `#!/bin/sh` 假 agy 写进 tempdir 并返回路径。
        pub fn script(dir: &std::path::Path, name: &str, body: &str) -> PathBuf {
            let p = dir.join(name);
            let mut f = std::fs::File::create(&p).unwrap();
            // `cat > /dev/null` 把提示词读走丢弃，模拟 agy 消费 stdin。
            write!(f, "#!/bin/sh\ncat > /dev/null\n{body}\n").unwrap();
            drop(f);
            std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o755)).unwrap();
            p
        }

        /// 预录的增量序列：故意在标签中间切开。
        pub fn recorded_deltas() -> Vec<&'static str> {
            vec![
                "Sure:\n```html\n<p id=\"P0",
                "1-001\">壹</p>\n<p ",
                "id=\"P01-002\">",
                "贰</p>\n``",
                "`\n",
            ]
        }

        /// 把增量序列变成 agy 的 NDJSON stdout（含 init 与终止 result）。
        pub fn jsonl() -> String {
            let response: String = recorded_deltas().concat();
            let mut out = String::new();
            out.push_str("printf '%s\\n' ");
            out.push_str(&format!(
                "'{}' ",
                serde_json::json!({"event":"init","init":{"model":"gemini-3.8-flash-low"}})
            ));
            out.push_str(&format!(
                "'{}' ",
                serde_json::json!({"event":"step_update","step_update":{"step_index":0,"state":"DONE","step_type":"user_input"}})
            ));
            for d in recorded_deltas() {
                out.push_str(&format!(
                    "'{}' ",
                    serde_json::json!({"event":"step_update","step_update":{"step_index":1,"state":"ACTIVE","step_type":"agent_response","text_delta":d}})
                ));
            }
            // 一条与译文无关的 thinking 事件，确认解析器不受干扰。
            out.push_str(&format!(
                "'{}' ",
                serde_json::json!({"event":"step_update","step_update":{"step_type":"thinking","text_delta":"…"}})
            ));
            out.push_str(&format!(
                "'{}'",
                serde_json::json!({"event":"result","result":{"status":"SUCCESS","response":response}})
            ));
            out
        }

        /// 假 agy：把 stdin 那一行 NDJSON 存到 `disk`，再输出预录事件。
        /// 注意不能复用 `script()`：它的 `cat > /dev/null` 前导会把 stdin 提前吃掉。
        pub fn script_capturing_stdin(dir: &std::path::Path, disk: &std::path::Path) -> PathBuf {
            let body = format!("cat > '{}'\n{}", disk.display(), jsonl());
            let p = dir.join("agy-capturing");
            let mut f = std::fs::File::create(&p).unwrap();
            write!(f, "#!/bin/sh\n{body}\n").unwrap();
            drop(f);
            std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o755)).unwrap();
            p
        }
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn fake_agy_streams_deltas_and_final_text_matches() {
        let dir = tempfile::tempdir().unwrap();
        let exe = fake_cli::script(dir.path(), "agy", &fake_cli::jsonl());
        let t = AgyTranslator::new(&exe, "gemini-3.8-flash-low");

        let mut got: Vec<String> = Vec::new();
        let mut stream = BlockStream::new();
        let mut blocks = Vec::new();
        let full = {
            let mut sink = |d: &str| {
                got.push(d.to_string());
                blocks.extend(stream.push(d));
            };
            t.translate(&prompt(), &mut sink).await.unwrap()
        };
        blocks.extend(stream.finish().0);

        // 增量流与预录一致：result.response 没有被再追加一遍。
        assert_eq!(got, fake_cli::recorded_deltas());
        assert_eq!(full, fake_cli::recorded_deltas().concat());
        // 块在进程结束前就已闭合交付（流式），此处断言最终解析结果。
        assert_eq!(blocks.len(), 2);
        assert_eq!(blocks[0].html, "<p id=\"P01-001\">壹</p>");
        assert_eq!(blocks[1].html, "<p id=\"P01-002\">贰</p>");
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn prompt_reaches_stdin_as_one_stream_json_user_message() {
        let dir = tempfile::tempdir().unwrap();
        let disk = dir.path().join("stdin.ndjson");
        let exe = fake_cli::script_capturing_stdin(dir.path(), &disk);
        let t = AgyTranslator::new(&exe, "gemini-3.8-flash-low");
        let mut sink = |_: &str| {};
        t.translate(&prompt(), &mut sink).await.unwrap();
        let captured = std::fs::read_to_string(&disk).unwrap();
        assert_eq!(captured.lines().count(), 1, "只允许一行 NDJSON 请求");
        let v: serde_json::Value = serde_json::from_str(captured.trim_end()).unwrap();
        assert_eq!(v["event"], "user");
        let text = v["message"]["content"][0]["text"].as_str().unwrap();
        assert!(text.contains("<!-- syncpdf:block P01-001 -->"));
        assert!(text.contains("<!-- syncpdf:block P01-002 -->"));
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn non_zero_exit_reports_harness_failed_without_prompt_text() {
        let dir = tempfile::tempdir().unwrap();
        let body = format!(
            "{}\necho 'boom: secret prompt text' >&2\nexit 1",
            fake_cli::jsonl()
        );
        let exe = fake_cli::script(dir.path(), "agy", &body);
        let t = AgyTranslator::new(&exe, "m");
        let mut sink = |_: &str| {};
        let err = t.translate(&prompt(), &mut sink).await.unwrap_err();
        match err {
            TranslateError::HarnessFailed(m) => {
                assert!(m.contains("exited"), "{m}");
                assert!(!m.contains("secret"), "错误文案不得含 stderr 正文: {m}");
                assert!(!m.contains("DOCUMENT"), "错误文案不得含提示词: {m}");
            }
            other => panic!("expected HarnessFailed, got {other:?}"),
        }
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn empty_output_is_a_harness_failure() {
        let dir = tempfile::tempdir().unwrap();
        let body = format!(
            "'{}'",
            serde_json::json!({"event":"result","result":{"status":"SUCCESS","response":""}})
        );
        let exe = fake_cli::script(dir.path(), "agy", &format!("printf '%s\\n' {body}"));
        let t = AgyTranslator::new(&exe, "m");
        let mut sink = |_: &str| {};
        assert!(matches!(
            t.translate(&prompt(), &mut sink).await,
            Err(TranslateError::HarnessFailed(_))
        ));
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn missing_terminal_result_is_a_harness_failure() {
        // 只有增量、没有终止 result：中途断连不得当成功。
        let dir = tempfile::tempdir().unwrap();
        let ev = serde_json::json!({
            "event":"step_update",
            "step_update":{"step_type":"agent_response","text_delta":"<p id=\"P01-001\">x"}
        });
        let exe = fake_cli::script(dir.path(), "agy", &format!("printf '%s\\n' '{ev}'"));
        let t = AgyTranslator::new(&exe, "m");
        let mut sink = |_: &str| {};
        match t.translate(&prompt(), &mut sink).await {
            Err(TranslateError::HarnessFailed(m)) => assert!(m.contains("terminal result"), "{m}"),
            other => panic!("expected HarnessFailed, got {other:?}"),
        }
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn non_success_result_status_is_a_harness_failure() {
        let dir = tempfile::tempdir().unwrap();
        let delta = serde_json::json!({
            "event":"step_update",
            "step_update":{"step_type":"agent_response","text_delta":"<p id=\"P01-001\">x</p>"}
        });
        let end = serde_json::json!({"event":"result","result":{"status":"ERROR","response":""}});
        let exe = fake_cli::script(
            dir.path(),
            "agy",
            &format!("printf '%s\\n' '{delta}' '{end}'"),
        );
        let t = AgyTranslator::new(&exe, "m");
        let mut sink = |_: &str| {};
        match t.translate(&prompt(), &mut sink).await {
            Err(TranslateError::HarnessFailed(m)) => assert!(m.contains("status=ERROR"), "{m}"),
            other => panic!("expected HarnessFailed, got {other:?}"),
        }
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn missing_status_fails_even_after_valid_deltas() {
        let dir = tempfile::tempdir().unwrap();
        let body = format!(
            "{}\nprintf '%s\\n' '{{\"event\":\"result\",\"result\":{{}}}}'",
            fake_cli::jsonl()
        );
        let exe = fake_cli::script(dir.path(), "agy", &body);
        let t = AgyTranslator::new(&exe, "m");
        let mut sink = |_: &str| {};
        assert!(matches!(t.translate(&prompt(), &mut sink).await,
            Err(TranslateError::HarnessFailed(m)) if m.contains("missing result status")));
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn tool_calls_fail_immediately_without_waiting_for_result() {
        for step_type in ["tool", "tool_calls"] {
            let dir = tempfile::tempdir().unwrap();
            let event = serde_json::json!({"event": "step_update", "step_update": {"step_type": step_type}});
            let exe = fake_cli::script(
                dir.path(),
                "agy",
                &format!("printf '%s\\n' '{event}'\nsleep 30"),
            );
            let t = AgyTranslator::new(&exe, "m").with_timeout(Duration::from_secs(5));
            let mut sink = |_: &str| {};
            let result = t.translate(&prompt(), &mut sink).await;
            assert!(
                matches!(&result,
                Err(TranslateError::HarnessFailed(m)) if m.contains("tool call")),
                "{step_type} must fail before the sleeping process exits: {result:?}"
            );
            assert!(t.request(&prompt()).contains("Do not use any tools"));
        }
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn closed_markdown_block_is_delivered_before_process_exit() {
        let dir = tempfile::tempdir().unwrap();
        let release = dir.path().join("release");
        let delta = serde_json::json!({"event":"step_update","step_update":{
            "step_type":"agent_response", "text_delta":"<!-- syncpdf:block P01-001 -->\n壹\n<!-- syncpdf:end P01-001 -->\n"}});
        let body = format!("printf '%s\\n' '{delta}'\nwhile [ ! -f '{}' ]; do sleep 0.02; done\nprintf '%s\\n' '{{\"event\":\"result\",\"result\":{{\"status\":\"SUCCESS\"}}}}'", release.display());
        let exe = fake_cli::script(dir.path(), "agy", &body);
        // The handshake, not a short wall-clock deadline, proves streaming.
        // Allow process startup contention when the full crate runs in parallel.
        let t = AgyTranslator::new(&exe, "m").with_timeout(Duration::from_secs(15));
        let mut stream = crate::markdown::MarkdownStream::new();
        let mut delivered = 0;
        let mut sink = |d: &str| {
            for block in stream.push(d) {
                assert_eq!(block.unwrap().id.to_string(), "P01-001");
                delivered += 1;
                std::fs::write(&release, "closed").unwrap();
            }
        };
        t.translate(&prompt(), &mut sink).await.unwrap();
        assert_eq!(delivered, 1);
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn timeout_kills_the_child() {
        let dir = tempfile::tempdir().unwrap();
        let exe = fake_cli::script(dir.path(), "agy", "sleep 30");
        let t = AgyTranslator::new(&exe, "m").with_timeout(Duration::from_millis(150));
        let mut sink = |_: &str| {};
        assert_eq!(
            t.translate(&prompt(), &mut sink).await,
            Err(TranslateError::Timeout(Duration::from_millis(150)))
        );
    }

    #[tokio::test]
    async fn missing_program_reports_unavailable() {
        let t = AgyTranslator::new("syncpdf-no-such-binary-xyz", "m");
        let mut sink = |_: &str| {};
        assert!(matches!(
            t.translate(&prompt(), &mut sink).await,
            Err(TranslateError::Unavailable(_))
        ));
    }
}
