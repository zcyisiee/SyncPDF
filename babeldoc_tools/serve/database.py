"""SQLite metadata store for the local single-user serve application.

The filesystem remains the asset payload store.  This module owns durable metadata,
revision fencing, and the append-only event stream used by newer API consumers.
It is deliberately dependency-free so ``bdt`` keeps working without web extras.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2

_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS translation_blocks (document_id TEXT NOT NULL, block_id TEXT NOT NULL, job_id TEXT NOT NULL, revision INTEGER NOT NULL, target TEXT NOT NULL, PRIMARY KEY(document_id,block_id));
CREATE TABLE IF NOT EXISTS local_previews (document_id TEXT PRIMARY KEY, asset_sha256 TEXT NOT NULL, revision INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS local_pages (document_id TEXT NOT NULL, page INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(document_id,page));
CREATE TABLE IF NOT EXISTS drafts (document_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS job_snapshots (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS papers (id TEXT PRIMARY KEY, title TEXT, authors TEXT, doi TEXT, arxiv TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, paper_id TEXT NOT NULL, pdf_sha256 TEXT UNIQUE, byte_size INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'uploaded', parse_config_version TEXT, revision INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(paper_id) REFERENCES papers(id));
CREATE TABLE IF NOT EXISTS assets (sha256 TEXT PRIMARY KEY, relative_path TEXT NOT NULL, kind TEXT NOT NULL, byte_size INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS parse_results (document_id TEXT PRIMARY KEY, parser_version TEXT, status TEXT NOT NULL, snapshot_asset TEXT, prepared_pdf_asset TEXT, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(document_id) REFERENCES documents(id));
CREATE TABLE IF NOT EXISTS blocks (id TEXT NOT NULL, document_id TEXT NOT NULL, page INTEGER, source TEXT, original_bbox TEXT, layout TEXT, PRIMARY KEY(document_id,id), FOREIGN KEY(document_id) REFERENCES documents(id));
CREATE TABLE IF NOT EXISTS block_edits (document_id TEXT NOT NULL, block_id TEXT NOT NULL, target TEXT, bbox TEXT, manual INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(document_id,block_id));
CREATE TABLE IF NOT EXISTS compile_blocks (document_id TEXT NOT NULL, block_id TEXT NOT NULL, input_hash TEXT, target_size REAL, patch_asset TEXT, status TEXT NOT NULL DEFAULT 'none', error TEXT, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(document_id,block_id));
CREATE TABLE IF NOT EXISTS pages (document_id TEXT NOT NULL, page INTEGER NOT NULL, input_hash TEXT, page_asset TEXT, dirty INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(document_id,page));
CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, document_id TEXT NOT NULL, action TEXT NOT NULL, status TEXT NOT NULL, from_stage TEXT, revision INTEGER NOT NULL DEFAULT 0, error TEXT, progress TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS job_events (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, document_id TEXT NOT NULL, block_id TEXT, page INTEGER, seq INTEGER NOT NULL, type TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(job_id,seq));
CREATE TABLE IF NOT EXISTS exports (id INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL, asset_sha256 TEXT, revision INTEGER NOT NULL, status TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS idx_documents_sha ON documents(pdf_sha256);
CREATE INDEX IF NOT EXISTS idx_job_events_document ON job_events(document_id, id);
"""


class MetadataDB:
    def __init__(self, base: Path | str):
        self.path = Path(base) / "app.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.executescript(_SCHEMA)
        self._connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)",
            (SCHEMA_VERSION,),
        )
        self._connection.commit()

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def document_by_hash(self, digest: str) -> sqlite3.Row | None:
        with self._lock:
            return self._connection.execute(
                "SELECT * FROM documents WHERE pdf_sha256=?", (digest,)
            ).fetchone()

    def register_document(
        self, did: str, digest: str, size: int, relative_path: str = "source.pdf"
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO papers(id) VALUES (?)", (did,)
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO documents(id,paper_id,pdf_sha256,byte_size) VALUES (?,?,?,?)",
                (did, did, digest, size),
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO assets(sha256,relative_path,kind,byte_size) VALUES (?,?,?,?)",
                (digest, relative_path, "source", size),
            )

    def save_draft(self, did: str, payload: dict, *, expected_revision: int) -> None:
        from babeldoc_tools.common import ToolError

        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT revision FROM drafts WHERE document_id=?", (did,)
            ).fetchone()
            current = int(row[0]) if row else expected_revision
            if current != expected_revision:
                raise ToolError(
                    "revision_conflict",
                    "草稿已被其它会话修改",
                    current_revision=current,
                )
            self._connection.execute(
                "INSERT OR REPLACE INTO drafts VALUES (?,?,?)",
                (did, payload["revision"], json.dumps(payload, ensure_ascii=False)),
            )
            self._connection.execute(
                "UPDATE documents SET revision=? WHERE id=?", (payload["revision"], did)
            )
            self._connection.execute(
                "DELETE FROM block_edits WHERE document_id=?", (did,)
            )
            for pid, paragraph in payload["paragraphs"].items():
                self._connection.execute(
                    "INSERT INTO block_edits(document_id,block_id,target,bbox,manual,revision) VALUES (?,?,?,?,1,?)",
                    (
                        did,
                        pid,
                        paragraph.get("target"),
                        json.dumps(paragraph.get("layout")),
                        payload["revision"],
                    ),
                )

    def draft(self, did: str) -> dict | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM drafts WHERE document_id=?", (did,)
            ).fetchone()
            return json.loads(row[0]) if row else None

    def upsert_blocks(self, did: str, rows: list[dict[str, Any]]) -> None:
        with self._lock, self._connection:
            for row in rows:
                self._connection.execute(
                    "INSERT OR REPLACE INTO blocks(id,document_id,page,source,original_bbox,layout) VALUES (?,?,?,?,?,?)",
                    (
                        row.get("id"),
                        did,
                        row.get("page"),
                        row.get("source"),
                        json.dumps((row.get("geometry") or {}).get("src_box")),
                        json.dumps(row.get("geometry")),
                    ),
                )
                if row.get("target") is not None:
                    self._connection.execute(
                        "INSERT OR REPLACE INTO translation_blocks VALUES (?,?,?,?,?)",
                        (did, row["id"], "indexed", self.revision(did), row["target"]),
                    )

    def save_job(self, payload: dict) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO job_snapshots VALUES (?,?)",
                (payload["job_id"], json.dumps(payload, ensure_ascii=False)),
            )
            self._connection.execute(
                "INSERT INTO jobs(id,document_id,action,status,from_stage,revision,error) VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,error=excluded.error,updated_at=CURRENT_TIMESTAMP",
                (
                    payload["job_id"],
                    payload["did"],
                    payload["action"],
                    payload["status"],
                    payload.get("from_stage"),
                    payload.get("revision", 0),
                    payload.get("error_message"),
                ),
            )

    def job_snapshots(self) -> list[dict]:
        with self._lock:
            return [
                json.loads(row[0])
                for row in self._connection.execute("SELECT payload FROM job_snapshots")
            ]

    def set_revision(self, did: str, revision: int) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE documents SET revision=? WHERE id=?", (revision, did)
            )

    def revision(self, did: str) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT revision FROM documents WHERE id=?", (did,)
            ).fetchone()
            return int(row[0]) if row else 0

    def commit_translation(
        self,
        job_id: str,
        did: str,
        revision: int,
        pid: str,
        target: str,
        progress: dict,
    ) -> bool:
        """Validated complete block + progress + event are one atomic commit."""
        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            draft = self.draft(did) or {"revision": 0, "paragraphs": {}}
            if draft["revision"] != revision:
                return False
            row = self._connection.execute(
                "SELECT payload FROM job_snapshots WHERE id=?", (job_id,)
            ).fetchone()
            job = json.loads(row[0]) if row else {}
            if job.get("status") not in ("queued", "running") or job.get(
                "cancel_requested_at"
            ):
                return False
            manual = draft["paragraphs"].get(pid) or {}
            if manual.get("target") is None:
                self._connection.execute(
                    "INSERT OR REPLACE INTO translation_blocks VALUES (?,?,?,?,?)",
                    (did, pid, job_id, revision, target),
                )
            seq = self._connection.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM job_events WHERE job_id=?",
                (job_id,),
            ).fetchone()[0]
            data = {
                **progress,
                "paragraph_id": pid,
                "text": manual.get("target") or target,
                "revision": revision,
            }
            self._connection.execute(
                "INSERT INTO job_events(job_id,document_id,block_id,seq,type,data) VALUES (?,?,?,?,?,?)",
                (
                    job_id,
                    did,
                    pid,
                    seq,
                    "translation_block_completed",
                    json.dumps(data, ensure_ascii=False),
                ),
            )
            self._connection.execute(
                "UPDATE jobs SET progress=? WHERE id=?", (json.dumps(progress), job_id)
            )
            return True

    def append_event(
        self,
        job_id: str,
        did: str,
        event_type: str,
        data: dict[str, Any],
        *,
        block_id: str | None = None,
        page: int | None = None,
    ) -> int:
        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM job_events WHERE job_id=?",
                (job_id,),
            ).fetchone()
            seq = int(row[0])
            self._connection.execute(
                "INSERT INTO job_events(job_id,document_id,block_id,page,seq,type,data) VALUES (?,?,?,?,?,?,?)",
                (
                    job_id,
                    did,
                    block_id,
                    page,
                    seq,
                    event_type,
                    json.dumps(data, ensure_ascii=False),
                ),
            )
            return seq

    def events(self, did: str, after: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM job_events WHERE document_id=? AND id>? ORDER BY id",
                (did, after),
            ).fetchall()
            return [{**dict(row), "data": json.loads(row["data"])} for row in rows]

    #: 每张带 ``document_id`` 的表（删除文档时逐表清行）。``job_snapshots`` 是
    #: 按 payload JSON 存的，单独处理。新增表必须同步加进来，否则删除会留下孤儿行。
    DOCUMENT_TABLES = (
        "translation_blocks",
        "local_previews",
        "local_pages",
        "drafts",
        "parse_results",
        "blocks",
        "block_edits",
        "compile_blocks",
        "pages",
        "jobs",
        "job_events",
        "exports",
    )

    def delete_document(self, did: str) -> dict[str, int]:
        """删除该文档的全部数据库行（**不删资产文件**）；→ 各表删除行数。

        资产（``assets`` 表 + ``assets/<hash>`` 文件）按内容寻址、可能被其它文档共用，
        这里既不删文件也不删 ``assets`` 行（回收交给 ``bdt serve --cleanup``）。
        ``papers`` 行只在该 doc 是其最后引用者时删掉。

        单事务（``BEGIN IMMEDIATE``）：要么全删要么全不删，不留"删了一半"的文档。
        """
        counts: dict[str, int] = {}
        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            paragraph_scope = self._document_paper(did)
            for table in self.DOCUMENT_TABLES:
                cursor = self._connection.execute(
                    f"DELETE FROM {table} WHERE document_id=?",  # noqa: S608 - 表名是模块常量
                    (did,),
                )
                counts[table] = cursor.rowcount or 0
            # job_snapshots 没有 document_id 列：读 payload 里的 did 过滤。
            snapshots = [
                row["id"]
                for row in self._connection.execute(
                    "SELECT id,payload FROM job_snapshots"
                ).fetchall()
                if _snapshot_did(row["payload"]) == did
            ]
            for job_id in snapshots:
                self._connection.execute(
                    "DELETE FROM job_snapshots WHERE id=?", (job_id,)
                )
            counts["job_snapshots"] = len(snapshots)
            counts["documents"] = (
                self._connection.execute(
                    "DELETE FROM documents WHERE id=?", (did,)
                ).rowcount
                or 0
            )
            if paragraph_scope is not None:
                remaining = self._connection.execute(
                    "SELECT COUNT(*) FROM documents WHERE paper_id=?",
                    (paragraph_scope,),
                ).fetchone()[0]
                if not remaining:
                    counts["papers"] = (
                        self._connection.execute(
                            "DELETE FROM papers WHERE id=?", (paragraph_scope,)
                        ).rowcount
                        or 0
                    )
        return counts

    def _document_paper(self, did: str) -> str | None:
        """该文档引用的 paper id（没有 documents 行 → None）。"""
        row = self._connection.execute(
            "SELECT paper_id FROM documents WHERE id=?", (did,)
        ).fetchone()
        return row["paper_id"] if row else None


def _snapshot_did(payload: str) -> str | None:
    """job 快照 payload 里的 did；解析失败 → None（不匹配任何文档）。"""
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return None
    did = data.get("did") if isinstance(data, dict) else None
    return str(did) if did else None


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size
