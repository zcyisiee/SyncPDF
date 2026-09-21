//! `Store`：连接管理、PRAGMA、迁移（`schema_version` 表）。

use crate::{assets::Assets, Result, StoreError};
use rusqlite::Connection;
use std::path::{Path, PathBuf};

/// 当前 schema 版本。每次破坏性变更 +1，并在 `migrate` 里加分支。
const SCHEMA_VERSION: i64 = 1;

/// 建表 DDL（v1）。IF NOT EXISTS 保证幂等。
const SCHEMA_V1: &str = r#"
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    doc_id        TEXT PRIMARY KEY,
    source_sha256 TEXT NOT NULL,
    path          TEXT NOT NULL,
    pages         INTEGER NOT NULL,
    created_at    REAL NOT NULL DEFAULT (unixepoch())
);

-- 每文档一份草稿状态（revision 单调 +1）
CREATE TABLE IF NOT EXISTS drafts (
    doc_id        TEXT PRIMARY KEY REFERENCES documents(doc_id) ON DELETE CASCADE,
    revision      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS runs (
    run_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id     TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    started_at REAL NOT NULL DEFAULT (unixepoch()),
    finished_at REAL,
    ok         INTEGER
);

-- 事件日志：seq 单调（每 run 从 1 递增），前端断线续传游标
CREATE TABLE IF NOT EXISTS run_events (
    run_id  INTEGER NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    seq     INTEGER NOT NULL,
    ts      REAL NOT NULL,
    json    TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);

CREATE TABLE IF NOT EXISTS paragraphs (
    doc_id         TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    paragraph_id   TEXT NOT NULL,
    page           INTEGER NOT NULL,
    status         TEXT NOT NULL,
    translated_html TEXT,
    PRIMARY KEY (doc_id, paragraph_id)
);

-- 阶段产物缓存：键 = 输入哈希 + stage 名；值为 CBOR+zstd BLOB
CREATE TABLE IF NOT EXISTS stages (
    key_sha256 TEXT NOT NULL,
    stage      TEXT NOT NULL,
    blob       BLOB NOT NULL,
    created_at REAL NOT NULL DEFAULT (unixepoch()),
    PRIMARY KEY (key_sha256, stage)
);

-- 资产登记：内容寻址 + 引用计数（文件本体在 assets/<aa>/<hash>）
CREATE TABLE IF NOT EXISTS assets (
    sha256   TEXT PRIMARY KEY,
    size     INTEGER NOT NULL,
    refcount INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL DEFAULT (unixepoch())
);
"#;

/// app.db 句柄。所有表操作都挂在这里（模块按领域拆分 impl 块）。
#[derive(Debug)]
pub struct Store {
    conn: Connection,
    /// 资产根目录（`assets/` 的父目录）。内存库时在测试 tempdir 下。
    root: PathBuf,
    /// 仅 `open_memory` 设置：drop 时递归删除的 tempdir。
    cleanup_on_drop: Option<PathBuf>,
}

impl Drop for Store {
    fn drop(&mut self) {
        if let Some(dir) = self.cleanup_on_drop.take() {
            let _ = std::fs::remove_dir_all(dir);
        }
    }
}

impl Store {
    /// 打开（或创建）`path` 处的数据库；启用 WAL 与外键；跑迁移。
    pub fn open(path: &Path) -> Result<Self> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| StoreError::io(parent, e))?;
        }
        let conn = Connection::open(path)?;
        let root = path
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .map(Path::to_path_buf)
            .unwrap_or_else(|| PathBuf::from("."));
        let store = Self {
            conn,
            root,
            cleanup_on_drop: None,
        };
        store.configure_and_migrate()?;
        Ok(store)
    }

    /// 内存库（测试用）。资产根目录是系统 tempdir 下的私有目录
    /// `syncpdf-store-mem-<pid>-<n>/`，`Store` drop 时整个删除。
    pub fn open_memory() -> Result<Self> {
        use std::sync::atomic::{AtomicU64, Ordering};
        static COUNTER: AtomicU64 = AtomicU64::new(0);
        let n = COUNTER.fetch_add(1, Ordering::Relaxed);
        let root =
            std::env::temp_dir().join(format!("syncpdf-store-mem-{}-{}", std::process::id(), n));
        let conn = Connection::open_in_memory()?;
        let store = Self {
            conn,
            root: root.clone(),
            cleanup_on_drop: Some(root),
        };
        store.configure_and_migrate()?;
        Ok(store)
    }

    fn configure_and_migrate(&self) -> Result<()> {
        self.conn.pragma_update(None, "journal_mode", "WAL")?;
        self.conn.pragma_update(None, "synchronous", "NORMAL")?;
        self.conn.pragma_update(None, "foreign_keys", "ON")?;
        self.migrate()
    }

    /// 版本迁移：`schema_version` 表记录当前版本；未知更高版本报错（降级打开不允许）。
    fn migrate(&self) -> Result<()> {
        self.conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);",
        )?;
        let current: Option<i64> = self
            .conn
            .query_row("SELECT version FROM schema_version LIMIT 1", [], |r| {
                r.get(0)
            })
            .map(Some)
            .or_else(|e| match e {
                rusqlite::Error::QueryReturnedNoRows => Ok(None),
                other => Err(other),
            })?;
        match current {
            None => {
                // 全新建库
                self.conn.execute_batch(SCHEMA_V1)?;
                self.conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?1)",
                    [SCHEMA_VERSION],
                )?;
            }
            Some(v) if v == SCHEMA_VERSION => {
                // 幂等重开：确保表都在（例如外部删表）
                self.conn.execute_batch(SCHEMA_V1)?;
            }
            Some(v) if v < SCHEMA_VERSION => {
                // 顺序升级：v1 是最低版本，无中间迁移
                self.conn.execute_batch(SCHEMA_V1)?;
                self.conn
                    .execute("UPDATE schema_version SET version = ?1", [SCHEMA_VERSION])?;
            }
            Some(v) => {
                return Err(StoreError::Invalid(format!(
                    "app.db schema version {v} is newer than supported {SCHEMA_VERSION}"
                )));
            }
        }
        Ok(())
    }

    /// 当前 schema 版本。
    pub fn schema_version(&self) -> Result<i64> {
        self.conn
            .query_row("SELECT version FROM schema_version LIMIT 1", [], |r| {
                r.get(0)
            })
            .map_err(Into::into)
    }

    /// 底层连接（供 `Assets` 等模块复用；不建议上游直接写 SQL）。
    pub(crate) fn conn(&self) -> &Connection {
        &self.conn
    }

    /// 资产模块视图。
    pub fn assets(&self) -> Assets<'_> {
        Assets::new(&self.root, &self.conn)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn open_memory_migrates_to_v1() {
        let store = Store::open_memory().unwrap();
        assert_eq!(store.schema_version().unwrap(), 1);
    }

    #[test]
    fn open_file_creates_and_reopens() {
        let tmp = tempfile::tempdir().unwrap();
        let db = tmp.path().join("sub").join("app.db");
        {
            let store = Store::open(&db).unwrap();
            assert_eq!(store.schema_version().unwrap(), 1);
        }
        // 重开：版本不变、不重复插入
        let store = Store::open(&db).unwrap();
        assert_eq!(store.schema_version().unwrap(), 1);
        let n: i64 = store
            .conn()
            .query_row("SELECT COUNT(*) FROM schema_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(n, 1);
    }

    #[test]
    fn foreign_keys_enforced() {
        let store = Store::open_memory().unwrap();
        let fk: i64 = store
            .conn()
            .query_row("PRAGMA foreign_keys", [], |r| r.get(0))
            .unwrap();
        assert_eq!(fk, 1);
        // drafts 挂在不存在的文档上必须失败
        let err = store.conn().execute(
            "INSERT INTO drafts (doc_id, revision) VALUES ('ghost', 0)",
            [],
        );
        assert!(err.is_err());
    }

    #[test]
    fn wal_mode_enabled_for_file_db() {
        let tmp = tempfile::tempdir().unwrap();
        let db = tmp.path().join("app.db");
        let store = Store::open(&db).unwrap();
        let mode: String = store
            .conn()
            .query_row("PRAGMA journal_mode", [], |r| r.get(0))
            .unwrap();
        assert_eq!(mode, "wal");
    }

    #[test]
    fn newer_schema_version_rejected() {
        let tmp = tempfile::tempdir().unwrap();
        let db = tmp.path().join("app.db");
        {
            let store = Store::open(&db).unwrap();
            store
                .conn()
                .execute("UPDATE schema_version SET version = 99", [])
                .unwrap();
        }
        let err = Store::open(&db);
        assert!(matches!(err, Err(StoreError::Invalid(_))), "{err:?}");
    }
}
