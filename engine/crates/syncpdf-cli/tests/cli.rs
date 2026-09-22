//! `syncpdf-cli` 二进制的端到端冒烟测试。
//!
//! 用 `CARGO_BIN_EXE_syncpdf-cli` 直接起进程，覆盖：
//! - `version` 退出码 0，且 stdout 只有一行版本号；
//! - `run` 喂 configure + run 后关 stdin（EOF）→ stdout 是合法事件 JSONL，
//!   首行 `run_started`、末行 `run_finished`，且 `seq` 单调递增。

use std::io::Write;
use std::process::{Command, Stdio};

use syncpdf_protocol::{decode_request, Request, TranslatorKind};

const BIN: &str = env!("CARGO_BIN_EXE_syncpdf-cli");

/// 把整段 stdin 写进去并等待进程结束，返回 (stdout, stderr, 退出码)。
fn run_with_stdin(args: &[&str], stdin: &str) -> (String, String, i32) {
    let mut child = Command::new(BIN)
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("启动 syncpdf-cli 失败");
    child
        .stdin
        .as_mut()
        .expect("stdin")
        .write_all(stdin.as_bytes())
        .expect("写 stdin 失败");
    // 丢掉 stdin 句柄 → 子进程读到 EOF。
    drop(child.stdin.take());
    let out = child.wait_with_output().expect("等待子进程失败");
    (
        String::from_utf8_lossy(&out.stdout).into_owned(),
        String::from_utf8_lossy(&out.stderr).into_owned(),
        out.status.code().unwrap_or(-1),
    )
}

/// 取一行 JSONL 的 `type` 字段。
fn kind_of(line: &str) -> String {
    let v: serde_json::Value = serde_json::from_str(line).expect("事件行不是合法 JSON");
    v.get("type")
        .and_then(|t| t.as_str())
        .unwrap_or("<缺失>")
        .to_string()
}

#[test]
fn version_exits_zero() {
    let out = Command::new(BIN)
        .arg("version")
        .output()
        .expect("运行 version 失败");
    assert!(out.status.success(), "version 应以 0 退出");
    let stdout = String::from_utf8_lossy(&out.stdout);
    let lines: Vec<&str> = stdout.lines().filter(|l| !l.trim().is_empty()).collect();
    assert_eq!(lines.len(), 1, "version 的 stdout 应只有一行：{stdout:?}");
    assert!(lines[0].contains("syncpdf-cli"), "版本行：{:?}", lines[0]);
}

#[test]
fn run_emits_run_started_first_and_run_finished_last() {
    // ci-test.pdf 缺失时跳过（不写死路径，交给 fixtures 定位）。
    let Some(input) = syncpdf_core::fixtures::path("ci-test.pdf") else {
        eprintln!("SKIP: fixture ci-test.pdf 缺失");
        return;
    };
    let output = std::env::temp_dir().join("syncpdf-cli-test-out.pdf");
    let _ = std::fs::remove_file(&output);

    let configure = Request::Configure {
        provider: syncpdf_protocol::TranslateProvider::Http,
        base_url: None,
        model: String::new(),
        api_key: None,
        concurrency: 1,
        cache_dir: std::env::temp_dir().join("syncpdf-cli-test-cache"),
        translator: TranslatorKind::Fake {
            name: "echo".to_string(),
        },
    };
    let run = Request::Run {
        doc_id: "cli-test".to_string(),
        input,
        output,
        source_lang: "en".to_string(),
        target_lang: "zh-CN".to_string(),
        pages: Some(vec![0]),
        font_profile: None,
        terminology: None,
        mode: syncpdf_protocol::Mode::Full,
    };
    let stdin = format!(
        "{}\n{}\n",
        syncpdf_protocol::encode_line(&configure),
        syncpdf_protocol::encode_line(&run)
    );

    let (stdout, stderr, code) = run_with_stdin(&["run"], &stdin);
    assert_eq!(code, 0, "run 应以 0 退出；stderr:\n{stderr}");

    let events: Vec<&str> = stdout.lines().filter(|l| !l.trim().is_empty()).collect();
    assert!(!events.is_empty(), "stdout 应有事件；stderr:\n{stderr}");

    // 每行都是合法 JSON 且带 "type"。
    for (i, line) in events.iter().enumerate() {
        let v: serde_json::Value = serde_json::from_str(line)
            .unwrap_or_else(|e| panic!("第 {} 行不是合法 JSON：{e}\n{line}", i + 1));
        assert!(v.get("type").is_some(), "第 {} 行缺 type：{line}", i + 1);
        assert!(v.get("seq").is_some(), "第 {} 行缺 seq：{line}", i + 1);
    }

    assert_eq!(kind_of(events[0]), "run_started", "首行必须是 run_started");
    assert_eq!(
        kind_of(events[events.len() - 1]),
        "run_finished",
        "末行必须是 run_finished"
    );

    // seq 从 1 起、严格递增。
    let mut prev = 0u64;
    for (i, line) in events.iter().enumerate() {
        let v: serde_json::Value = serde_json::from_str(line).unwrap();
        let seq = v.get("seq").and_then(|s| s.as_u64()).expect("seq 应为整数");
        assert_eq!(seq, prev + 1, "第 {} 行 seq 不连续：{seq}", i + 1);
        prev = seq;
    }
}

#[test]
fn run_before_configure_reports_protocol_error() {
    let run = Request::Run {
        doc_id: "no-configure".to_string(),
        input: "/nonexistent.pdf".into(),
        output: "/tmp/never.pdf".into(),
        source_lang: "en".to_string(),
        target_lang: "zh-CN".to_string(),
        pages: None,
        font_profile: None,
        terminology: None,
        mode: syncpdf_protocol::Mode::Full,
    };
    let stdin = format!("{}\n", syncpdf_protocol::encode_line(&run));
    let (stdout, _stderr, code) = run_with_stdin(&["run"], &stdin);
    assert_eq!(code, 0, "缺 configure 时仍应正常退出");
    let kinds: Vec<String> = stdout
        .lines()
        .filter(|l| !l.trim().is_empty())
        .map(kind_of)
        .collect();
    assert!(
        kinds.iter().any(|k| k == "error"),
        "应产出 error 事件（run 前无 configure）：{kinds:?}"
    );
}

#[test]
fn cancel_line_does_not_crash() {
    let stdin = format!("{}\n", syncpdf_protocol::encode_line(&Request::Cancel));
    let (stdout, _stderr, code) = run_with_stdin(&["run"], &stdin);
    assert_eq!(code, 0, "cancel 应正常退出");
    // cancel 在 run 之前到达：没有任务可取消，允许没有任何事件。
    let _ = stdout;
}

#[test]
fn translate_rejects_unknown_translator() {
    let out = Command::new(BIN)
        .args([
            "translate",
            "--input",
            "/nonexistent.pdf",
            "--output",
            "/tmp/never.pdf",
            "--translator",
            "fake:definitely-not-a-translator",
        ])
        .output()
        .expect("运行 translate 失败");
    assert!(!out.status.success(), "未知翻译器应报错退出");
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("未知翻译器") || stderr.contains("fake:") || stderr.contains("error"),
        "stderr 应说明翻译器非法：{stderr}"
    );
}

#[test]
fn inspect_missing_input_fails_cleanly() {
    let out = Command::new(BIN)
        .args(["inspect", "--input", "/nonexistent.pdf"])
        .output()
        .expect("运行 inspect 失败");
    assert!(!out.status.success(), "不存在的输入应非 0 退出");
}

#[test]
fn decode_roundtrip_matches_protocol() {
    // 防止测试与协议实现漂移：能解回 Configure。
    let configure = Request::Configure {
        provider: syncpdf_protocol::TranslateProvider::Http,
        base_url: None,
        model: String::new(),
        api_key: None,
        concurrency: 1,
        cache_dir: std::env::temp_dir(),
        translator: TranslatorKind::Fake {
            name: "echo".to_string(),
        },
    };
    let line = syncpdf_protocol::encode_line(&configure);
    assert!(matches!(
        decode_request(&line),
        Ok(Request::Configure { .. })
    ));
}
