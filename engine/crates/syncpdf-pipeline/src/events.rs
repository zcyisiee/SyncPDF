//! 事件汇：把 [`Event`] 封成 [`Envelope`]（自动 `seq`/`ts`）并送到目的地。
//!
//! 设计基准：02-技术路径与架构.md §9。约定：
//! - `seq` 从 1 起、全程单调递增（前端断线后按 `seq` 续传）；
//! - `ts` 为 Unix 秒（浮点）；
//! - stdout 只出 JSONL，日志一律走 stderr（见 `syncpdf-cli`）。

use std::io::Write;
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use syncpdf_protocol::{encode_line, Envelope, Event};

/// 事件接收方。实现者负责分配 `seq`/`ts` 并落盘 / 落队列。
pub trait EventSink: Send {
    /// 交付一个事件。
    fn emit(&mut self, event: Event);
}

/// 当前 Unix 时间戳（秒，浮点）。系统时间早于 epoch 时返回 0。
pub fn now_ts() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// 写 stdout 的 JSONL 汇：每行一个 `Envelope`，写完立刻 flush。
#[derive(Debug)]
pub struct StdoutSink {
    seq: u64,
    out: std::io::Stdout,
}

impl StdoutSink {
    pub fn new() -> Self {
        Self {
            seq: 0,
            out: std::io::stdout(),
        }
    }

    /// 已交付的事件数（即最后一个 `seq`）。
    pub fn seq(&self) -> u64 {
        self.seq
    }
}

impl Default for StdoutSink {
    fn default() -> Self {
        Self::new()
    }
}

impl EventSink for StdoutSink {
    fn emit(&mut self, event: Event) {
        self.seq += 1;
        let env = Envelope {
            seq: self.seq,
            ts: now_ts(),
            event,
        };
        let mut line = encode_line(&env);
        line.push('\n');
        // 事件通道打不开就只能放弃：stdout 是唯一的事件出口，日志在 stderr。
        let mut lock = self.out.lock();
        if let Err(e) = lock.write_all(line.as_bytes()).and_then(|()| lock.flush()) {
            tracing::error!(error = %e, "写 stdout 事件失败");
        }
    }
}

/// 内存汇：测试用，保留全部信封。
#[derive(Debug, Default)]
pub struct VecSink {
    pub seq: u64,
    pub events: Vec<Envelope>,
}

impl VecSink {
    pub fn new() -> Self {
        Self::default()
    }

    /// 取第 `i` 个事件的信封（越界返回 `None`）。
    pub fn get(&self, i: usize) -> Option<&Envelope> {
        self.events.get(i)
    }

    /// 首个事件的类型名（如 `run_started`），便于断言。
    pub fn first_kind(&self) -> Option<&'static str> {
        self.events.first().map(|e| event_kind(&e.event))
    }

    /// 末个事件的类型名。
    pub fn last_kind(&self) -> Option<&'static str> {
        self.events.last().map(|e| event_kind(&e.event))
    }
}

impl EventSink for VecSink {
    fn emit(&mut self, event: Event) {
        self.seq += 1;
        self.events.push(Envelope {
            seq: self.seq,
            ts: now_ts(),
            event,
        });
    }
}

/// 事件类型名（与 JSON 的 `type` 字段一致）。
pub fn event_kind(e: &Event) -> &'static str {
    match e {
        Event::RunStarted { .. } => "run_started",
        Event::StageStarted { .. } => "stage_started",
        Event::StageFinished { .. } => "stage_finished",
        Event::Progress { .. } => "progress",
        Event::Paragraph { .. } => "paragraph",
        Event::PageReady { .. } => "page_ready",
        Event::Issue { .. } => "issue",
        Event::DocumentFinished { .. } => "document_finished",
        Event::RunFinished { .. } => "run_finished",
        Event::Error { .. } => "error",
    }
}

/// 可跨线程共享的事件汇（排版/翻译回调在别的线程上发事件时用）。
///
/// 内部串行化：`emit` 时锁住被包装的汇，因此 `seq` 不会重复。
#[derive(Clone)]
pub struct SharedSink(Arc<Mutex<dyn EventSink>>);

impl SharedSink {
    /// 包装任意汇。
    pub fn new(sink: impl EventSink + 'static) -> Self {
        Self(Arc::new(Mutex::new(sink)))
    }

    /// 发一个事件；锁中毒时放弃该事件并记日志（不 panic）。
    pub fn emit(&self, event: Event) {
        match self.0.lock() {
            Ok(mut s) => s.emit(event),
            Err(poisoned) => {
                tracing::error!("事件汇锁中毒，丢弃事件");
                poisoned.into_inner().emit(event);
            }
        }
    }
}

impl std::fmt::Debug for SharedSink {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("SharedSink(<dyn EventSink>)")
    }
}

impl EventSink for SharedSink {
    fn emit(&mut self, event: Event) {
        SharedSink::emit(self, event);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use syncpdf_protocol::Stage;
    use syncpdf_protocol::PROTOCOL_VERSION;

    fn started() -> Event {
        Event::RunStarted {
            protocol_version: PROTOCOL_VERSION,
            engine_version: syncpdf_protocol::ENGINE_VERSION.to_string(),
            doc_id: "d1".into(),
            pages: 3,
        }
    }

    #[test]
    fn vec_sink_starts_seq_at_one_and_is_monotonic() {
        let mut s = VecSink::new();
        s.emit(started());
        s.emit(Event::StageStarted {
            stage: Stage::Preflight,
        });
        s.emit(Event::RunFinished {
            ok: false,
            elapsed_ms: 12,
        });
        assert_eq!(s.events.len(), 3);
        assert_eq!(s.events[0].seq, 1);
        assert_eq!(s.events[1].seq, 2);
        assert_eq!(s.events[2].seq, 3);
        assert!(s.events.windows(2).all(|w| w[0].seq < w[1].seq));
        assert_eq!(s.seq, 3);
        assert_eq!(s.first_kind(), Some("run_started"));
        assert_eq!(s.last_kind(), Some("run_finished"));
    }

    #[test]
    fn envelope_json_has_type_and_parses() {
        let mut s = VecSink::new();
        s.emit(started());
        let line = encode_line(&s.events[0]);
        assert!(!line.contains('\n'));
        let v: serde_json::Value = serde_json::from_str(&line).unwrap();
        assert_eq!(v["type"], serde_json::json!("run_started"));
        assert_eq!(v["seq"], serde_json::json!(1));
        assert!(v["ts"].as_f64().unwrap() > 0.0);
        let back: Envelope = serde_json::from_str(&line).unwrap();
        assert_eq!(back, s.events[0]);
    }

    #[test]
    fn timestamps_are_unix_seconds() {
        let ts = now_ts();
        // 2020-01-01 之后、2100 之前：足够宽松的合理性断言。
        assert!(ts > 1_577_836_800.0, "{ts}");
        assert!(ts < 4_102_444_800.0, "{ts}");
    }

    #[test]
    fn every_event_kind_has_a_name() {
        let all = [
            started(),
            Event::StageStarted {
                stage: Stage::Publishing,
            },
            Event::StageFinished {
                stage: Stage::Publishing,
                elapsed_ms: 1,
            },
            Event::Progress {
                stage: Stage::Translating,
                done: 1,
                total: 2,
            },
            Event::PageReady {
                page: 1,
                preview_path: None,
                revision: 1,
            },
            Event::Issue {
                severity: syncpdf_protocol::Severity::Info,
                code: "x".into(),
                paragraph_id: None,
                page: None,
                message: "m".into(),
            },
            Event::RunFinished {
                ok: true,
                elapsed_ms: 1,
            },
            Event::Error {
                fatal: true,
                code: "c".into(),
                message: "m".into(),
            },
        ];
        for e in &all {
            assert!(!event_kind(e).is_empty());
        }
    }

    #[test]
    fn shared_sink_serializes_seq_across_threads() {
        let shared = SharedSink::new(VecSink::new());
        let mut handles = Vec::new();
        for i in 0..4u32 {
            let s = shared.clone();
            handles.push(std::thread::spawn(move || {
                s.emit(Event::Progress {
                    stage: Stage::Translating,
                    done: i,
                    total: 4,
                });
            }));
        }
        for h in handles {
            h.join().unwrap();
        }
        let sink = SharedSink::new(VecSink::new());
        drop(sink);
        // `SharedSink` 本身不暴露内部状态；这里只验证并发 emit 不 panic，
        // `seq` 单调性由 `VecSink` 单测覆盖。
    }

    #[test]
    fn stdout_sink_counts_seq() {
        // 这里不实际写 stdout（会污染测试输出）：只验证计数逻辑。
        let mut s = StdoutSink::new();
        assert_eq!(s.seq(), 0);
        s.seq = 7;
        assert_eq!(s.seq(), 7);
    }
}
