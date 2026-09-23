//! pi CLI 翻译通道。
//!
//! 调用方式与现版完全一致（babeldoc_tools/harnesses.py:60-75, 130-200），全部
//! flag 已在本机 pi 0.86.1 的 `--help` 里核对存在：
//!
//! ```text
//! pi -p --no-session --mode json --model <m> --thinking <t> \
//!    --no-tools --no-extensions --no-skills --no-prompt-templates \
//!    --no-context-files --no-approve --offline --system-prompt <s>
//! ```
//!
//! - **提示词走 stdin，不进 argv**：既躲开命令行长度上限，也不把正文暴露在
//!   进程表里（现版规约 #15）。写完即关闭 stdin。
//! - cwd 用临时目录：pi 不该看到工程里的任何文件。
//! - stdout 每行一个 JSON；`assistantMessageEvent.type == "text_delta"` 的
//!   `delta` 就是增量文本，最终文本 = 全部 delta 拼接。
//! - stderr 直接丢弃：里面可能有认证信息与提示词片段，不进日志。
//! - 退出码非 0 → `TranslateError::HarnessFailed`，**错误文案不含提示词正文**。

use std::path::PathBuf;
use std::process::Stdio;
use std::time::Duration;

use async_trait::async_trait;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::Command;

use crate::prompt::DocumentPrompt;
use crate::translator::{DeltaSink, TranslateError, Translator};

/// 默认模型与思考档位（现版同款）。
pub const DEFAULT_MODEL: &str = "deepseek/deepseek-flash";
pub const DEFAULT_THINKING: &str = "low";
/// 默认超时：整文档 one-shot 可能要跑很久。
pub const DEFAULT_TIMEOUT: Duration = Duration::from_secs(1810);

/// 经本机 `pi` CLI 翻译。
#[derive(Debug, Clone)]
pub struct PiTranslator {
    /// 可执行文件，默认 `pi`（走 PATH）。
    pub program: PathBuf,
    pub model: String,
    pub thinking: String,
    pub timeout: Duration,
    /// `name()` 的缓存串（`pi/<model 尾段>`）。
    label: String,
}

impl Default for PiTranslator {
    fn default() -> Self {
        Self::new("pi", DEFAULT_MODEL, DEFAULT_THINKING)
    }
}

impl PiTranslator {
    pub fn new(
        program: impl Into<PathBuf>,
        model: impl Into<String>,
        thinking: impl Into<String>,
    ) -> Self {
        let model = model.into();
        let label = format!("pi/{}", model.rsplit('/').next().unwrap_or(&model));
        Self {
            program: program.into(),
            model,
            thinking: thinking.into(),
            timeout: DEFAULT_TIMEOUT,
            label,
        }
    }

    pub fn with_timeout(mut self, timeout: Duration) -> Self {
        self.timeout = timeout;
        self
    }

    /// 构造 argv（不含程序名）。公开出来便于上游审计与测试。
    pub fn args(&self, system_prompt: &str) -> Vec<String> {
        [
            "-p",
            "--no-session",
            "--mode",
            "json",
            "--model",
            &self.model,
            "--thinking",
            &self.thinking,
            "--no-tools",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-context-files",
            "--no-approve",
            "--offline",
            "--system-prompt",
            system_prompt,
        ]
        .iter()
        .map(|s| (*s).to_string())
        .collect()
    }

    async fn run(
        &self,
        prompt: &DocumentPrompt,
        on_delta: DeltaSink<'_>,
    ) -> Result<String, TranslateError> {
        let cwd = TempCwd::new()?;
        let mut child = Command::new(&self.program)
            .args(self.args(&prompt.system))
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
                _ => TranslateError::Unavailable(format!("cannot start pi: {}", e.kind())),
            })?;

        // 提示词经 stdin 传入后立刻关闭，pi 据此知道输入结束。
        let mut stdin = child
            .stdin
            .take()
            .ok_or_else(|| TranslateError::HarnessFailed("pi stdin unavailable".into()))?;
        let body = prompt.text.clone();
        let writer = tokio::spawn(async move {
            let _ = stdin.write_all(body.as_bytes()).await;
            let _ = stdin.shutdown().await;
        });

        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| TranslateError::HarnessFailed("pi stdout unavailable".into()))?;

        let mut text = String::new();
        let mut terminal_error: Option<String> = None;
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
                        "cannot read pi stdout: {}",
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
                Event::Failed(reason) => terminal_error = Some(reason),
                Event::Ignore => {}
            }
        }
        let _ = writer.await;

        let status = tokio::time::timeout(self.timeout, child.wait())
            .await
            .map_err(|_| TranslateError::Timeout(self.timeout))?
            .map_err(|e| TranslateError::HarnessFailed(format!("cannot reap pi: {}", e.kind())))?;

        if !status.success() {
            // 只报退出码，不带 stderr / 提示词正文。
            return Err(TranslateError::HarnessFailed(format!(
                "pi exited with {status}"
            )));
        }
        if let Some(reason) = terminal_error {
            return Err(TranslateError::HarnessFailed(format!(
                "pi returned an incomplete response ({reason})"
            )));
        }
        if text.trim().is_empty() {
            return Err(TranslateError::HarnessFailed(
                "pi returned an empty response".into(),
            ));
        }
        Ok(text)
    }
}

#[async_trait]
impl Translator for PiTranslator {
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
    /// 增量文本。
    Delta(String),
    /// 终态显示这次回合没正常完成。
    Failed(String),
    /// 与文本无关的事件（thinking、工具、心跳……）。
    Ignore,
}

/// 解析一行 JSONL。坏行一律忽略（pi 偶尔会混入非 JSON 的启动噪声）。
fn parse_line(line: &str) -> Event {
    let line = line.trim();
    if line.is_empty() {
        return Event::Ignore;
    }
    let Ok(v) = serde_json::from_str::<serde_json::Value>(line) else {
        return Event::Ignore;
    };
    // 增量：assistantMessageEvent.type == "text_delta" → delta
    if let Some(ev) = v.get("assistantMessageEvent") {
        if ev.get("type").and_then(|t| t.as_str()) == Some("text_delta") {
            if let Some(d) = ev.get("delta").and_then(|d| d.as_str()) {
                if !d.is_empty() {
                    return Event::Delta(d.to_string());
                }
            }
        }
        return Event::Ignore;
    }
    // 终态：message_end 的 assistant 消息必须 stopReason == "stop" 且无 errorMessage。
    if v.get("type").and_then(|t| t.as_str()) == Some("message_end") {
        let Some(m) = v.get("message") else {
            return Event::Ignore;
        };
        if m.get("role").and_then(|r| r.as_str()) != Some("assistant") {
            return Event::Ignore;
        }
        if m.get("errorMessage").and_then(|e| e.as_str()).is_some() {
            return Event::Failed("errorMessage".into());
        }
        match m.get("stopReason").and_then(|s| s.as_str()) {
            Some("stop") | None => return Event::Ignore,
            Some(other) => return Event::Failed(format!("stopReason={other}")),
        }
    }
    Event::Ignore
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
            "syncpdf-pi-{}-{}",
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
    fn argv_matches_the_current_implementation() {
        let t = PiTranslator::default();
        let a = t.args("SYS");
        assert_eq!(
            a,
            vec![
                "-p",
                "--no-session",
                "--mode",
                "json",
                "--model",
                "deepseek/deepseek-flash",
                "--thinking",
                "low",
                "--no-tools",
                "--no-extensions",
                "--no-skills",
                "--no-prompt-templates",
                "--no-context-files",
                "--no-approve",
                "--offline",
                "--system-prompt",
                "SYS",
            ]
        );
        assert_eq!(t.name(), "pi/deepseek-flash");
        // 提示词绝不进 argv。
        assert!(!a.iter().any(|s| s.contains("DOCUMENT:")));
    }

    #[test]
    fn parse_line_picks_text_deltas_only() {
        assert_eq!(
            parse_line(r#"{"assistantMessageEvent":{"type":"text_delta","delta":"<p id="}}"#),
            Event::Delta("<p id=".into())
        );
        assert_eq!(
            parse_line(r#"{"assistantMessageEvent":{"type":"thinking_delta","delta":"hmm"}}"#),
            Event::Ignore
        );
        assert_eq!(parse_line("not json at all"), Event::Ignore);
        assert_eq!(parse_line("   "), Event::Ignore);
        assert_eq!(
            parse_line(
                r#"{"type":"message_end","message":{"role":"assistant","stopReason":"stop","content":[]}}"#
            ),
            Event::Ignore
        );
        assert_eq!(
            parse_line(
                r#"{"type":"message_end","message":{"role":"assistant","stopReason":"length"}}"#
            ),
            Event::Failed("stopReason=length".into())
        );
        assert_eq!(
            parse_line(
                r#"{"type":"message_end","message":{"role":"assistant","errorMessage":"nope"}}"#
            ),
            Event::Failed("errorMessage".into())
        );
    }

    // ── 假 pi 脚本：读 stdin 丢弃，printf 预录 JSONL ──────────────────────
    #[cfg(unix)]
    mod fake_cli {
        use super::*;
        use std::io::Write;
        use std::os::unix::fs::PermissionsExt;

        /// 把一个 `#!/bin/sh` 假 pi 写进 tempdir 并返回路径。
        pub fn script(dir: &std::path::Path, name: &str, body: &str) -> PathBuf {
            let p = dir.join(name);
            let mut f = std::fs::File::create(&p).unwrap();
            // `cat > /dev/null` 把提示词读走丢弃，模拟 pi 消费 stdin。
            write!(f, "#!/bin/sh\ncat > /dev/null\n{body}\n").unwrap();
            drop(f);
            std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o755)).unwrap();
            p
        }

        /// 预录的 delta 序列：故意在标签中间切开。
        pub fn recorded_deltas() -> Vec<&'static str> {
            vec![
                "Sure:\n```html\n<p id=\"P0",
                "1-001\">壹</p>\n<p ",
                "id=\"P01-002\">",
                "贰</p>\n``",
                "`\n",
            ]
        }

        /// 把 delta 序列变成 pi 的 JSONL stdout。
        pub fn jsonl() -> String {
            let mut out = String::new();
            out.push_str("printf '%s\\n' ");
            for d in recorded_deltas() {
                let ev = serde_json::json!({
                    "assistantMessageEvent": { "type": "text_delta", "delta": d }
                });
                // 单引号包裹；预录内容里没有单引号。
                out.push_str(&format!("'{}' ", serde_json::to_string(&ev).unwrap()));
            }
            // 混一条无关事件与一条终态事件，确认解析器不受干扰。
            out.push_str(&format!(
                "'{}' ",
                serde_json::json!({"assistantMessageEvent":{"type":"thinking_delta","delta":"…"}})
            ));
            out.push_str(&format!(
                "'{}'",
                serde_json::json!({
                    "type": "message_end",
                    "message": { "role": "assistant", "stopReason": "stop", "content": [] }
                })
            ));
            out
        }
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn fake_pi_streams_deltas_and_final_text_matches() {
        let dir = tempfile::tempdir().unwrap();
        let exe = fake_cli::script(dir.path(), "pi", &fake_cli::jsonl());
        let t = PiTranslator::new(&exe, "deepseek/deepseek-flash", "low");

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

        // delta 流与预录一致。
        assert_eq!(got, fake_cli::recorded_deltas());
        // 最终文本 = 全部 delta 拼接。
        assert_eq!(full, fake_cli::recorded_deltas().concat());
        // 块解析跨 delta 边界仍然正确。
        assert_eq!(blocks.len(), 2);
        assert_eq!(blocks[0].html, "<p id=\"P01-001\">壹</p>");
        assert_eq!(blocks[1].html, "<p id=\"P01-002\">贰</p>");
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn non_zero_exit_reports_harness_failed_without_prompt_text() {
        let dir = tempfile::tempdir().unwrap();
        let body = format!(
            "{}\necho 'boom: secret prompt text' >&2\nexit 1",
            fake_cli::jsonl()
        );
        let exe = fake_cli::script(dir.path(), "pi", &body);
        let t = PiTranslator::new(&exe, "m", "low");
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
        let exe = fake_cli::script(dir.path(), "pi", "true");
        let t = PiTranslator::new(&exe, "m", "low");
        let mut sink = |_: &str| {};
        assert!(matches!(
            t.translate(&prompt(), &mut sink).await,
            Err(TranslateError::HarnessFailed(_))
        ));
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn incomplete_turn_is_a_harness_failure() {
        let dir = tempfile::tempdir().unwrap();
        let ev = serde_json::json!({
            "assistantMessageEvent": { "type": "text_delta", "delta": "<p id=\"P01-001\">x" }
        });
        let end = serde_json::json!({
            "type": "message_end",
            "message": { "role": "assistant", "stopReason": "length" }
        });
        let body = format!("printf '%s\\n' '{ev}' '{end}'");
        let exe = fake_cli::script(dir.path(), "pi", &body);
        let t = PiTranslator::new(&exe, "m", "low");
        let mut sink = |_: &str| {};
        match t.translate(&prompt(), &mut sink).await {
            Err(TranslateError::HarnessFailed(m)) => {
                assert!(m.contains("stopReason=length"), "{m}")
            }
            other => panic!("expected HarnessFailed, got {other:?}"),
        }
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn timeout_kills_the_child() {
        let dir = tempfile::tempdir().unwrap();
        let exe = fake_cli::script(dir.path(), "pi", "sleep 30");
        let t = PiTranslator::new(&exe, "m", "low").with_timeout(Duration::from_millis(150));
        let mut sink = |_: &str| {};
        assert_eq!(
            t.translate(&prompt(), &mut sink).await,
            Err(TranslateError::Timeout(Duration::from_millis(150)))
        );
    }

    #[tokio::test]
    async fn missing_program_reports_unavailable() {
        let t = PiTranslator::new("syncpdf-no-such-binary-xyz", "m", "low");
        let mut sink = |_: &str| {};
        assert!(matches!(
            t.translate(&prompt(), &mut sink).await,
            Err(TranslateError::Unavailable(_))
        ));
    }
}
