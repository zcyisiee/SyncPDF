//! `syncpdf-cli` 二进制的端到端冒烟测试。
//!
//! 用 `CARGO_BIN_EXE_syncpdf-cli` 直接起进程，覆盖：
//! - `version` 退出码 0，且 stdout 只有一行版本号；
//! - `run` 喂 configure + run 后关 stdin（EOF）→ stdout 是合法事件 JSONL，
//!   首行 `run_started`、末行 `run_finished`，且 `seq` 单调递增。

use std::io::{BufRead, BufReader, Write};
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

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
        store: None,
    };
    let stdin = format!(
        "{}\n{}\n",
        syncpdf_protocol::encode_line(&configure),
        syncpdf_protocol::encode_line(&run)
    );

    let (stdout, stderr, code) = run_with_stdin(&["run"], &stdin);
    assert_ne!(code, 0, "stdin EOF 取消应非零退出；stderr:\n{stderr}");

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
        store: None,
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

/// `run --protocol 1`（前端约定的版本）必须正常：喂 configure + run 后关 stdin，
/// 事件序列合法；本测试随后关闭 stdin，因此取消并非零退出。
#[test]
fn run_protocol_1_is_accepted() {
    let Some(input) = syncpdf_core::fixtures::path("ci-test.pdf") else {
        eprintln!("SKIP: fixture ci-test.pdf 缺失");
        return;
    };
    let output = std::env::temp_dir().join("syncpdf-cli-proto1.pdf");
    let _ = std::fs::remove_file(&output);
    let stdin = configure_plus_run_stdin(&input, &output, "echo", Some(vec![0]));
    let (stdout, stderr, code) = run_with_stdin(&["run", "--protocol", "1"], &stdin);
    assert_ne!(
        code, 0,
        "--protocol 1 已接受，但 EOF 取消应非零退出；stderr:\n{stderr}"
    );
    let kinds: Vec<String> = stdout
        .lines()
        .filter(|l| !l.trim().is_empty())
        .map(kind_of)
        .collect();
    assert_eq!(kinds.first().map(String::as_str), Some("run_started"));
    assert_eq!(kinds.last().map(String::as_str), Some("run_finished"));
}

/// `run --protocol 2`：只支持 1，必须以非 0 退出，且先给一条 fatal error 事件。
#[test]
fn run_protocol_2_exits_nonzero() {
    let (stdout, stderr, code) = run_with_stdin(&["run", "--protocol", "2"], "");
    assert_ne!(code, 0, "--protocol 2 必须非 0 退出；stderr:\n{stderr}");

    // 即使没有 configure，也必须先吐一条 fatal error 事件（前端按 JSONL 解析）。
    let first = stdout
        .lines()
        .find(|l| !l.trim().is_empty())
        .unwrap_or_else(|| panic!("--protocol 2 应有 error 事件；stderr:\n{stderr}"));
    let v: serde_json::Value = serde_json::from_str(first).expect("事件行应为合法 JSON");
    assert_eq!(v.get("type").and_then(|t| t.as_str()), Some("error"));
    assert_eq!(v.get("fatal").and_then(|f| f.as_bool()), Some(true));
    assert_eq!(
        v.get("code").and_then(|c| c.as_str()),
        Some("protocol_unsupported")
    );
}

/// `translate` 没有 `--protocol` 参数：传了应被 clap 拒绝（非 0 退出）。
#[test]
fn translate_agy_rejects_thinking_before_running_anything() {
    // agy 的档位在模型名里；传 --thinking 必须在启动任何进程前报错，
    // 否则会默默丢掉档位设置。
    let out = Command::new(BIN)
        .args([
            "translate",
            "--input",
            "/nonexistent.pdf",
            "--output",
            "/tmp/never.pdf",
            "--translator",
            "agy",
            "--thinking",
            "low",
        ])
        .output()
        .expect("运行 translate 失败");
    assert!(!out.status.success(), "agy + --thinking 应报错退出");
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(stderr.contains("agy"), "stderr: {stderr}");
    assert!(stderr.contains("thinking"), "stderr: {stderr}");
    assert!(!Path::new("/tmp/never.pdf").exists());
}

#[test]
fn translate_rejects_unknown_flag() {
    let out = Command::new(BIN)
        .args([
            "translate",
            "--input",
            "/nonexistent.pdf",
            "--output",
            "/tmp/never.pdf",
            "--protocol",
            "1",
        ])
        .output()
        .expect("运行 translate 失败");
    assert!(
        !out.status.success(),
        "translate 不应接受 --protocol（clap 应报未知参数）"
    );
}

/// stdin EOF 取消：给 `fake:slow:2000` 的前 3 页任务，300ms 后关掉 stdin
/// （= EOF）→ 进程应在 5s 内自行退出，且最后一行是 `run_finished{ok:false}`。
///
/// 关键：**不往 stdin 写任何东西**，让进程一开始就卡在等请求上——不行，那样
/// 它会直接 EOF 返回。所以要先写 configure + run，再关（EOF 只在读线程下一次
/// 读到时才取消）。
#[test]
fn stdin_eof_cancels_running_task() {
    let Some(input) = syncpdf_core::fixtures::path("up-vns.pdf") else {
        eprintln!("SKIP: fixture up-vns.pdf 缺失");
        return;
    };
    let output = std::env::temp_dir().join("syncpdf-cli-eof-cancel.pdf");
    let _ = std::fs::remove_file(&output);

    let stdin_payload = configure_plus_run_stdin(&input, &output, "slow:2000", Some(vec![0, 1, 2]));
    let mut child = Command::new(BIN)
        .args(["run", "--protocol", "1"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("启动 syncpdf-cli 失败");
    {
        let mut sink = child.stdin.take().expect("stdin");
        sink.write_all(stdin_payload.as_bytes()).expect("写 stdin");
        sink.flush().ok();
        // 300ms 后**关掉** stdin：读线程下一次读到 EOF 时会 cancel。
        std::thread::sleep(Duration::from_millis(300));
    }

    // 起一个读 stdout 的线程，避免管道写满阻塞子进程。
    let stdout = child.stdout.take().expect("stdout");
    let reader = std::thread::spawn(move || {
        let mut lines = Vec::new();
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            if !line.trim().is_empty() {
                lines.push(line);
            }
        }
        lines
    });

    let started = Instant::now();
    let status = child.wait().expect("等待子进程失败");
    let elapsed = started.elapsed();
    let lines = reader.join().expect("读线程");
    assert!(
        elapsed < Duration::from_secs(5),
        "EOF 后应在 5s 内退出，实际 {:?}；事件：{lines:?}",
        elapsed
    );
    assert!(!status.success(), "EOF 取消必须非零退出，与 ok:false 一致");
    let last = lines.last().expect("应至少有 run_started/run_finished");
    assert_eq!(
        kind_of(last),
        "run_finished",
        "最后一行应是 run_finished：{last}"
    );
    let v: serde_json::Value = serde_json::from_str(last).unwrap();
    assert_eq!(
        v.get("ok").and_then(|o| o.as_bool()),
        Some(false),
        "EOF 取消应以 ok:false 收尾：{last}"
    );
}

/// 构造一份 configure + run 的 stdin 内容。
fn configure_plus_run_stdin(
    input: &std::path::Path,
    output: &std::path::Path,
    fake_name: &str,
    pages: Option<Vec<u32>>,
) -> String {
    let configure = Request::Configure {
        provider: syncpdf_protocol::TranslateProvider::Http,
        base_url: None,
        model: String::new(),
        api_key: None,
        concurrency: 1,
        cache_dir: std::env::temp_dir().join("syncpdf-cli-test-cache"),
        translator: TranslatorKind::Fake {
            name: fake_name.to_string(),
        },
    };
    let run = Request::Run {
        doc_id: "cli-eof".to_string(),
        input: input.to_path_buf(),
        output: output.to_path_buf(),
        source_lang: "en".to_string(),
        target_lang: "zh-CN".to_string(),
        pages,
        font_profile: None,
        terminology: None,
        mode: syncpdf_protocol::Mode::Full,
        store: None,
    };
    format!(
        "{}\n{}\n",
        syncpdf_protocol::encode_line(&configure),
        syncpdf_protocol::encode_line(&run)
    )
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

#[test]
fn translate_save_failure_exits_nonzero_and_does_not_publish_success() {
    let input = syncpdf_core::fixtures::path("ci-test.pdf").expect("ci-test fixture required");
    let dir = tempfile::tempdir().unwrap();
    let previous = dir.path().join("previous.pdf");
    std::fs::write(&previous, b"previous output").unwrap();
    let out = Command::new(BIN)
        .arg("translate")
        .arg("--input")
        .arg(input)
        .arg("--output")
        .arg(previous.join("blocked.pdf"))
        .arg("--cache-dir")
        .arg(dir.path().join("cache"))
        .arg("--translator")
        .arg("fake:echo")
        .output()
        .unwrap();
    assert!(!out.status.success());
    assert_eq!(std::fs::read(previous).unwrap(), b"previous output");
    let events: Vec<serde_json::Value> = String::from_utf8(out.stdout)
        .unwrap()
        .lines()
        .filter(|s| !s.is_empty())
        .map(|s| serde_json::from_str(s).unwrap())
        .collect();
    assert_eq!(events.last().unwrap()["type"], "run_finished");
    assert_eq!(events.last().unwrap()["ok"], false);
    assert!(!events
        .iter()
        .any(|e| e["type"] == "page_ready" || e["type"] == "document_finished"));
}

/// 子进程级回归（Unix）：原生组件绕过 tracing 直接写 fd1 时，事件 JSONL
/// 仍独占原 stdout，原生输出全部落 stderr——包括 `run_finished` 之后的
/// 滞后日志。隔离发生在子进程内，父测试进程不改任何 fd，不影响并行测试。
#[cfg(unix)]
#[test]
fn jsonl_events_stay_isolated_from_native_stdout() {
    if let Ok(mode) = std::env::var("SYNCPDF_STDIO_CHILD") {
        stdio_probe_child(&mode);
        return;
    }
    // 见证缺陷：不隔离时原生噪声确实混进 stdout（说明下面的断言有效）。
    let (legacy_stdout, _, _) = stdio_probe("legacy");
    assert!(
        legacy_stdout.contains("NATIVE_STDOUT_NOISE"),
        "未隔离时原生输出应混入 stdout：{legacy_stdout:?}"
    );

    let (stdout, stderr, code) = stdio_probe("isolated");
    assert_eq!(code, 0, "隔离子进程应正常退出；stderr:\n{stderr}");
    assert!(
        !stdout.contains("NATIVE"),
        "stdout 不得混入原生输出：{stdout}"
    );
    // 隔离在测试函数体内生效，之前的 libtest 横幅行（`running …`/`test …`）
    // 仍可能出现在 stdout；除此之外每行必须是合法事件 JSONL。
    let mut events = Vec::new();
    for (i, line) in stdout.lines().filter(|l| !l.trim().is_empty()).enumerate() {
        match serde_json::from_str::<serde_json::Value>(line) {
            Ok(v) => {
                assert!(v.get("type").is_some(), "第 {} 行缺 type：{line}", i + 1);
                assert!(v.get("seq").is_some(), "第 {} 行缺 seq：{line}", i + 1);
                events.push(v);
            }
            Err(_) => assert!(
                line.starts_with("running ") || line.starts_with("test "),
                "stdout 混入非事件、非测试框架行：{line}"
            ),
        }
    }
    assert_eq!(events.len(), 2, "应恰好 2 条事件：{events:?}");
    assert_eq!(events[0]["type"], "run_started");
    assert_eq!(events[0]["seq"], 1);
    assert_eq!(events[1]["type"], "run_finished");
    assert_eq!(events[1]["seq"], 2);
    assert!(
        stderr.contains("NATIVE_STDOUT_NOISE"),
        "原生日志应落 stderr：{stderr}"
    );
    assert!(
        stderr.contains("NATIVE_AFTER_FINISH"),
        "run_finished 之后的滞后原生日志也应落 stderr：{stderr}"
    );
}

/// 用子进程重跑本测试（改 fd 只在子进程内发生），返回 (stdout, stderr, code)。
#[cfg(unix)]
fn stdio_probe(mode: &str) -> (String, String, i32) {
    let exe = std::env::current_exe().expect("当前测试二进制路径");
    let out = Command::new(exe)
        .arg("jsonl_events_stay_isolated_from_native_stdout")
        .arg("--exact")
        .env("SYNCPDF_STDIO_CHILD", mode)
        .output()
        .expect("启动隔离子进程失败");
    (
        String::from_utf8_lossy(&out.stdout).into_owned(),
        String::from_utf8_lossy(&out.stderr).into_owned(),
        out.status.code().unwrap_or(-1),
    )
}

/// 子进程内：`isolated` 走生产隔离路径，`legacy` 用原始 `StdoutSink::new()`
/// 作未隔离对照。写 fd1 的 `NATIVE_*` 行模拟原生库绕过 tracing 的输出。
#[cfg(unix)]
fn stdio_probe_child(mode: &str) {
    use std::io::Write as _;
    use syncpdf_pipeline::events::{SharedSink, StdoutSink};
    use syncpdf_protocol::Event;

    let sink = match mode {
        "legacy" => StdoutSink::new(),
        _ => StdoutSink::isolated().expect("stdout 隔离失败必须向上传播"),
    };
    let sink = SharedSink::new(sink);
    sink.emit(Event::RunStarted {
        protocol_version: syncpdf_protocol::PROTOCOL_VERSION,
        engine_version: "stdio-test".into(),
        doc_id: "stdio".into(),
        pages: 1,
    });
    let mut raw = std::io::stdout();
    let _ = raw
        .write_all(b"NATIVE_STDOUT_NOISE\n")
        .and_then(|()| raw.flush());
    sink.emit(Event::RunFinished {
        ok: true,
        elapsed_ms: 0,
    });
    // run_finished 之后（含析构阶段）的滞后原生日志同样必须被隔离。
    let _ = raw
        .write_all(b"NATIVE_AFTER_FINISH\n")
        .and_then(|()| raw.flush());
}

/// 长驻会话：一次 configure 后连发两个 run，第二个在第一个运行中到达，
/// 应排队等第一个的 `run_finished` 之后再开始；两者都成功，空闲时 EOF 正常退出。
#[test]
fn run_session_queues_runs_serially() {
    let Some(input) = syncpdf_core::fixtures::path("ci-test.pdf") else {
        eprintln!("SKIP: fixture ci-test.pdf 缺失");
        return;
    };
    let dir = tempfile::tempdir().unwrap();
    let first = configure_plus_run_stdin(&input, &dir.path().join("a.pdf"), "echo", Some(vec![0]));
    let second = configure_plus_run_stdin(&input, &dir.path().join("b.pdf"), "echo", Some(vec![0]));
    let second_run = second.lines().nth(1).unwrap();

    let mut child = Command::new(BIN)
        .args(["run", "--protocol", "1"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .expect("启动 syncpdf-cli 失败");
    let mut stdin = child.stdin.take().unwrap();
    stdin
        .write_all(format!("{first}{second_run}\n").as_bytes())
        .unwrap();
    stdin.flush().unwrap();

    let mut kinds = Vec::new();
    let mut oks = Vec::new();
    for line in BufReader::new(child.stdout.take().unwrap()).lines() {
        let line = line.unwrap();
        let v: serde_json::Value = serde_json::from_str(&line).unwrap();
        kinds.push(kind_of(&line));
        if kinds.last().unwrap() == "run_finished" {
            oks.push(v["ok"].as_bool().unwrap());
            if oks.len() == 2 {
                break;
            }
        }
    }
    assert_eq!(oks, [true, true], "{kinds:?}");
    let starts: Vec<usize> = (0..kinds.len())
        .filter(|&i| kinds[i] == "run_started")
        .collect();
    let ends: Vec<usize> = (0..kinds.len())
        .filter(|&i| kinds[i] == "run_finished")
        .collect();
    assert_eq!(starts.len(), 2, "{kinds:?}");
    assert!(
        ends[0] < starts[1],
        "第二个任务必须在第一个结束后才开始：{kinds:?}"
    );

    drop(stdin);
    assert!(child.wait().unwrap().success(), "空闲时 EOF 应正常退出");
}

/// 会话里的编辑请求写入指定的本篇库，不报 unsupported；非法排版只回一条
/// 非致命 `edit_failed`，进程照常退出。
#[test]
fn session_saves_edits_and_reports_invalid_ones() {
    let dir = std::env::temp_dir().join(format!("syncpdf-cli-edits-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    let store = dir.join("store.db");
    let edit = |line_height: Option<f32>| Request::ApplyEdit {
        doc_id: "d".into(),
        store: Some(store.clone()),
        paragraph_id: "P01-001".parse().unwrap(),
        translated_html: Some("<p id=\"P01-001\">改</p>".into()),
        style: syncpdf_protocol::BlockStyle {
            line_height,
            ..Default::default()
        },
    };
    let retranslate = Request::Retranslate {
        doc_id: "d".into(),
        store: Some(store.clone()),
        paragraph_ids: vec!["P01-002".parse().unwrap()],
    };
    let stdin = [edit(None), retranslate, edit(Some(0.0))]
        .iter()
        .map(|r| format!("{}\n", syncpdf_protocol::encode_line(r)))
        .collect::<String>();
    let (stdout, stderr, code) = run_with_stdin(&["run"], &stdin);
    assert_eq!(code, 0, "stderr:\n{stderr}");
    let errors: Vec<serde_json::Value> = stdout
        .lines()
        .filter(|l| !l.trim().is_empty())
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    assert_eq!(errors.len(), 1, "只有非法那条报错：{stdout}");
    assert_eq!(errors[0]["code"], "edit_failed");
    assert_eq!(errors[0]["fatal"], false);
    assert!(store.is_file(), "合法编辑应写入本篇库");
}
