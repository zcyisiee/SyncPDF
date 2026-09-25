"""云端数据根的目录布局与 SQLite 元数据（只存元数据，文件在磁盘上）。

布局（``--root``）::

    app.db                              SQLite（WAL）
    sources/<sha256>.pdf                原文（按内容去重）
    translations/<tid>/translated.pdf   译文
    translations/<tid>/events.jsonl.gz  原始引擎事件（保留 7 天）
    work/<tid>/                         运行中的引擎 workdir，结束即删
    preview/<key>/<page>.webp           预览缓存（可重建，有总量上限）
    dual/<tid>.pdf                      中英对照短期缓存
    tmp/                                上传中的临时文件
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

__all__ = ["DEFAULT_ROOT", "Database", "Paths", "now"]

DEFAULT_ROOT = Path("~/.bdt-cloud").expanduser()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY,
    invite_code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    daily_quota INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions(
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sources(
    sha256 TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    pages INTEGER NOT NULL,
    page_sizes TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS translations(
    id TEXT PRIMARY KEY,
    source_sha TEXT NOT NULL REFERENCES sources(sha256),
    model TEXT NOT NULL,
    thinking TEXT NOT NULL,
    engine_sha TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    translated_sha TEXT,
    result_json TEXT,
    stats TEXT,
    created_at REAL NOT NULL,
    queued_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS translations_key
    ON translations(source_sha, model, thinking, engine_sha);
CREATE INDEX IF NOT EXISTS translations_queue ON translations(status, queued_at);
CREATE TABLE IF NOT EXISTS jobs(
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    translation_id TEXT NOT NULL REFERENCES translations(id),
    filename TEXT NOT NULL,
    status TEXT NOT NULL,
    cache_hit INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    deleted_at REAL
);
CREATE INDEX IF NOT EXISTS jobs_user ON jobs(user_id, created_at);
CREATE INDEX IF NOT EXISTS jobs_translation ON jobs(translation_id);
CREATE TABLE IF NOT EXISTS job_events(
    job_id TEXT NOT NULL REFERENCES jobs(id),
    seq INTEGER NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    scope TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY(job_id, seq)
);
"""


@dataclass(frozen=True)
class Paths:
    """数据根下各目录；:meth:`ensure` 建齐。"""

    root: Path

    @property
    def db(self) -> Path:
        return self.root / "app.db"

    @property
    def sources(self) -> Path:
        return self.root / "sources"

    @property
    def translations(self) -> Path:
        return self.root / "translations"

    @property
    def work(self) -> Path:
        return self.root / "work"

    @property
    def preview(self) -> Path:
        return self.root / "preview"

    @property
    def dual(self) -> Path:
        return self.root / "dual"

    @property
    def tmp(self) -> Path:
        return self.root / "tmp"

    def source(self, sha: str) -> Path:
        return self.sources / f"{sha}.pdf"

    def translated(self, tid: str) -> Path:
        return self.translations / tid / "translated.pdf"

    def ensure(self) -> Paths:
        for path in (self.sources, self.translations, self.work, self.preview, self.dual, self.tmp):
            path.mkdir(parents=True, exist_ok=True)
        return self


class Database:
    """单连接 + 锁：请求线程、runner 线程与清理线程共用；写入都很小。"""

    def __init__(self, path: Path) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """``BEGIN IMMEDIATE`` 事务；异常回滚。嵌套调用复用外层事务。"""
        with self._lock:
            if self._conn.in_transaction:
                yield self._conn
                return
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def now() -> float:
    return time.time()
