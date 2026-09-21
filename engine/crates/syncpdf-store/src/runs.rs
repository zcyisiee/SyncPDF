//! runs / run_events 表：任务登记与事件日志（seq 游标续传，规约 #12）。

use crate::db::Store;
use crate::Result;
use syncpdf_protocol::{Envelope, Event};

/// 任务 id（runs 表自增主键）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct RunId(pub i64);

impl Store {
    /// 建任务（documents 需已有该 doc_id）。
    pub fn create_run(&self, doc_id: &str) -> Result<RunId> {
        self.conn()
            .execute("INSERT INTO runs (doc_id) VALUES (?1)", [doc_id])?;
        Ok(RunId(self.conn().last_insert_rowid()))
    }

    /// 结束任务（`ok` = 是否成功）。
    pub fn finish_run(&self, run: RunId, ok: bool) -> Result<()> {
        self.conn().execute(
            "UPDATE runs SET finished_at = unixepoch(), ok = ?2 WHERE run_id = ?1",
            rusqlite::params![run.0, ok],
        )?;
        Ok(())
    }

    /// 追加事件。`Envelope.seq` 若为 0 则由表内自增游标分配（保证每 run 单调 +1）；
    /// 非零则按调用方给的 seq 原样写入（引擎自身计数与持久层一致时用）。
    /// 返回实际落盘的 seq。
    pub fn append_event(&self, run: RunId, envelope: &Envelope) -> Result<u64> {
        let seq = if envelope.seq == 0 {
            let next: i64 = self.conn().query_row(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM run_events WHERE run_id = ?1",
                [run.0],
                |r| r.get(0),
            )?;
            next as u64
        } else {
            envelope.seq
        };
        // 写入的 JSON 必须带最终 seq（调用方给的 envelope 可能 seq=0 表示"由表分配"）
        let stored = Envelope {
            seq,
            ts: envelope.ts,
            event: envelope.event.clone(),
        };
        let json =
            serde_json::to_string(&stored).map_err(|e| crate::StoreError::Serde(e.to_string()))?;
        self.conn().execute(
            "INSERT INTO run_events (run_id, seq, ts, json) VALUES (?1, ?2, ?3, ?4)",
            rusqlite::params![run.0, seq as i64, stored.ts, json],
        )?;
        Ok(seq)
    }

    /// 取 `after` 之后的事件（不含 `after`，升序）。`after = 0` = 从头。
    pub fn events_since(&self, run: RunId, after: u64) -> Result<Vec<Envelope>> {
        let mut stmt = self.conn().prepare(
            "SELECT json FROM run_events WHERE run_id = ?1 AND seq > ?2 ORDER BY seq ASC",
        )?;
        let rows = stmt.query_map([run.0, after as i64], |row| {
            let json: String = row.get(0)?;
            serde_json::from_str::<Envelope>(&json).map_err(|e| {
                rusqlite::Error::FromSqlConversionFailure(
                    0,
                    rusqlite::types::Type::Text,
                    Box::new(std::io::Error::new(
                        std::io::ErrorKind::InvalidData,
                        e.to_string(),
                    )),
                )
            })
        })?;
        let mut out: Vec<Envelope> = Vec::new();
        for row in rows {
            out.push(row?);
        }
        Ok(out)
    }

    /// 当前 run 已持久化的最大 seq（无事件返回 0）。
    pub fn last_event_seq(&self, run: RunId) -> Result<u64> {
        let max: i64 = self.conn().query_row(
            "SELECT COALESCE(MAX(seq), 0) FROM run_events WHERE run_id = ?1",
            [run.0],
            |r| r.get(0),
        )?;
        Ok(max as u64)
    }

    /// run 是否存在。
    pub fn run_exists(&self, run: RunId) -> Result<bool> {
        let n: i64 = self.conn().query_row(
            "SELECT COUNT(*) FROM runs WHERE run_id = ?1",
            [run.0],
            |r| r.get(0),
        )?;
        Ok(n > 0)
    }

    /// 取 run 所属 doc_id（不存在返回 `None`）。
    pub fn run_doc(&self, run: RunId) -> Result<Option<String>> {
        let mut stmt = self
            .conn()
            .prepare("SELECT doc_id FROM runs WHERE run_id = ?1")?;
        let mut rows = stmt.query([run.0])?;
        Ok(match rows.next()? {
            Some(row) => Some(row.get(0)?),
            None => None,
        })
    }
}

/// 把 `Event` 包成带默认 seq/ts 的 `Envelope`（供测试与简单调用方）。
pub fn envelope_for(event: Event) -> Envelope {
    Envelope {
        seq: 0,
        ts: std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0),
        event,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use syncpdf_core::hash::Sha256Hash;
    use syncpdf_protocol::{Event, Stage};

    fn store_with_doc() -> (crate::db::Store, &'static str) {
        let store = Store::open_memory().unwrap();
        let doc = Box::leak(Box::new("doc-1".to_string()));
        store
            .upsert_document(
                doc,
                &Sha256Hash::of(b"x"),
                std::path::Path::new("/a.pdf"),
                2,
            )
            .unwrap();
        (store, doc)
    }

    fn progress(seq: u64, done: u32) -> Envelope {
        Envelope {
            seq,
            ts: 1000.0 + done as f64,
            event: Event::Progress {
                stage: Stage::Translating,
                done,
                total: 10,
            },
        }
    }

    #[test]
    fn create_and_finish_run() {
        let (store, doc) = store_with_doc();
        let run = store.create_run(doc).unwrap();
        assert!(store.run_exists(run).unwrap());
        assert_eq!(store.run_doc(run).unwrap().as_deref(), Some(doc));
        store.finish_run(run, true).unwrap();
        let ok: Option<i64> = store
            .conn()
            .query_row("SELECT ok FROM runs WHERE run_id = ?1", [run.0], |r| {
                r.get(0)
            })
            .unwrap();
        assert_eq!(ok, Some(1));
    }

    #[test]
    fn create_run_requires_document() {
        let store = Store::open_memory().unwrap();
        assert!(store.create_run("ghost").is_err());
    }

    #[test]
    fn events_seq_monotonic_and_replay() {
        let (store, doc) = store_with_doc();
        let run = store.create_run(doc).unwrap();
        let mut last = 0u64;
        for done in 1..=5 {
            let seq = store.append_event(run, &progress(0, done)).unwrap();
            assert_eq!(seq, done as u64, "seq 必须从 1 单调 +1");
            assert!(seq > last);
            last = seq;
        }
        // 显式 seq 原样写入
        assert_eq!(store.append_event(run, &progress(42, 6)).unwrap(), 42);

        // events_since 全量
        let all = store.events_since(run, 0).unwrap();
        assert_eq!(all.len(), 6);
        // 返回的 Envelope 反序列化完整
        assert_eq!(all[0].seq, 1);
        assert_eq!(all[0].event, progress(0, 1).event);
        assert!((all[0].ts - 1001.0).abs() < 1e-9);

        // 游标续传：after=3 → 4,5,42
        let tail = store.events_since(run, 3).unwrap();
        let seqs: Vec<u64> = tail.iter().map(|e| e.seq).collect();
        assert_eq!(seqs, vec![4, 5, 42]);
        // after 到头 → 空
        assert!(store.events_since(run, 42).unwrap().is_empty());
        assert_eq!(store.last_event_seq(run).unwrap(), 42);
    }

    #[test]
    fn events_per_run_independent() {
        let (store, doc) = store_with_doc();
        let run_a = store.create_run(doc).unwrap();
        let run_b = store.create_run(doc).unwrap();
        store.append_event(run_a, &progress(0, 1)).unwrap();
        store.append_event(run_a, &progress(0, 2)).unwrap();
        // run_b 的 seq 从 1 重新计数
        assert_eq!(store.append_event(run_b, &progress(0, 1)).unwrap(), 1);
        assert_eq!(store.last_event_seq(run_a).unwrap(), 2);
        assert_eq!(store.last_event_seq(run_b).unwrap(), 1);
        assert_eq!(store.events_since(run_b, 0).unwrap().len(), 1);
    }
}
