"""任务与译文：缓存命中、同键合并、配额、排队、取消、软删除与面向用户的事件。

两层实体：

- **translation**：一次按缓存键（原文 sha256 + 模型 + 思考强度 + 引擎 sha）的翻译，
  跨用户共享；状态 ``queued|running|done|partial|failed|canceled``。
- **job**：某个用户的一条历史记录，指向一个 translation；多出 ``canceled``，删除只打
  ``deleted_at``。translation 的状态变化同步到它名下所有仍在进行的 job。

``job_events`` 只存面向用户的里程碑、每页完成与提醒，供 SSE 回放与续传；``scope``
区分 job 自己的事件（上传、取消）与 translation 的事件（后加入的 job 复制后者）。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import secrets
import sqlite3
import threading
from zoneinfo import ZoneInfo

from babeldoc_tools.cloud.db import Database
from babeldoc_tools.cloud.db import Paths
from babeldoc_tools.cloud.db import now
from babeldoc_tools.common import ToolError

__all__ = [
    "ACTIVE",
    "DEFAULT_ETA_SECONDS",
    "FINISHED",
    "MODELS",
    "QUOTA_TZ",
    "THINKING",
    "Hub",
    "Service",
]

MODELS = ("deepseek/deepseek-flash", "uuapi-gemini/gemini-3.8-flash")
THINKING = ("low", "medium", "high")
ACTIVE = ("queued", "running", "recompile")
#: ACTIVE 状态过滤的 SQL 片段与整句（常量，无插值）。
_JOBS_OF_TRANSLATION = (
    "SELECT id FROM jobs WHERE translation_id = ? AND status IN (?,?,?)"
)
_JOBS_SIBLING = (
    "SELECT id FROM jobs WHERE translation_id = ? AND id != ? "
    "AND status IN (?,?,?) ORDER BY created_at LIMIT 1"
)
_WAITING_COUNT = (
    "SELECT COUNT(*) FROM jobs WHERE translation_id = ? AND status IN (?,?,?)"
)
#: 有可下载译文的终态（缓存命中只认这两种）。
FINISHED = ("done", "partial")
#: rerun 动作。
RERUN_ACTIONS = ("recompile", "retranslate")
#: 「每天 0 点恢复」按北京时间。
QUOTA_TZ = ZoneInfo("Asia/Shanghai")
#: 没有历史耗时时，排队预计按每篇 5 分钟。
DEFAULT_ETA_SECONDS = 300.0


class Hub:
    """线程安全的「有新事件」通知：写入方（任何线程）唤醒所有 SSE 连接去读数据库。"""

    def __init__(self) -> None:
        self._subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> tuple[asyncio.AbstractEventLoop, asyncio.Event]:
        token = (asyncio.get_running_loop(), asyncio.Event())
        with self._lock:
            self._subscribers.add(token)
        return token

    def unsubscribe(self, token: tuple[asyncio.AbstractEventLoop, asyncio.Event]) -> None:
        with self._lock:
            self._subscribers.discard(token)

    def notify(self) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for loop, event in subscribers:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:  # 事件循环已关闭
                self.unsubscribe((loop, event))


def _new_id() -> str:
    return secrets.token_hex(8)


def _today_start() -> float:
    today = dt.datetime.now(QUOTA_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return today.timestamp()


class Service:
    """数据库上的业务操作；HTTP 层与 runner 共用。"""

    def __init__(self, db: Database, paths: Paths, engine_sha: str, hub: Hub | None = None) -> None:
        self.db = db
        self.paths = paths
        self.engine_sha = engine_sha
        self.hub = hub or Hub()
        #: runner 注册的取消回调（translation 正在运行且已无人等待时调用）。
        self.on_cancel_running = lambda _tid: None
        #: 有新 translation 入队时唤醒 runner。
        self.on_enqueue = lambda: None

    # ---- 事件 -------------------------------------------------------------- #
    def _append(self, conn: sqlite3.Connection, job_id: str, kind: str, payload: dict, scope: str) -> None:
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM job_events WHERE job_id = ?", (job_id,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO job_events(job_id, seq, ts, kind, scope, payload) VALUES (?,?,?,?,?,?)",
            (job_id, seq, now(), kind, scope, json.dumps(payload, ensure_ascii=False)),
        )

    def emit(self, tid: str, events: list[tuple[str, dict]]) -> None:
        """把 translation 的事件追加到它名下所有仍在进行的 job。"""
        with self.db.transaction() as conn:
            jobs = conn.execute(
                _JOBS_OF_TRANSLATION, (tid, *ACTIVE)
            ).fetchall()
            for job in jobs:
                for kind, payload in events:
                    self._append(conn, job["id"], kind, payload, "tr")
        self.hub.notify()

    def events_after(self, job_id: str, seq: int) -> list[dict]:
        rows = self.db.all(
            "SELECT seq, ts, kind, payload FROM job_events WHERE job_id = ? AND seq > ? ORDER BY seq",
            (job_id, seq),
        )
        return [
            {"seq": r["seq"], "ts": r["ts"], "kind": r["kind"], **json.loads(r["payload"])}
            for r in rows
        ]

    # ---- 上传后建 job ------------------------------------------------------- #
    def quota_used(self, user_id: int) -> int:
        """今日已用篇数：缓存命中、取消、失败都不计。"""
        return self.db.one(
            "SELECT COUNT(*) FROM jobs WHERE user_id = ? AND created_at >= ? AND cache_hit = 0 "
            "AND status NOT IN ('canceled', 'failed')",
            (user_id, _today_start()),
        )[0]

    def add_source(self, sha: str, size: int, page_sizes: list[list[float]]) -> None:
        """登记原文；``page_sizes`` 是每页可见尺寸 ``[宽, 高]``（pt），前端据此排版预览。"""
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sources(sha256, size, pages, page_sizes, created_at) "
                "VALUES (?,?,?,?,?)",
                (sha, size, len(page_sizes), json.dumps(page_sizes), now()),
            )

    @staticmethod
    def check_options(model: str, thinking: str) -> None:
        if model not in MODELS:
            raise ToolError("invalid_model", f"不支持的模型：{model}")
        if thinking not in THINKING:
            raise ToolError("invalid_thinking", f"思考强度只能是 {'/'.join(THINKING)}")

    def create_job(self, user: sqlite3.Row, filename: str, sha: str, model: str, thinking: str) -> str:
        self.check_options(model, thinking)
        job_id = _new_id()
        at = now()
        enqueued = False
        with self.db.transaction() as conn:
            tr = conn.execute(
                "SELECT * FROM translations WHERE source_sha = ? AND model = ? AND thinking = ? "
                "AND engine_sha = ?",
                (sha, model, thinking, self.engine_sha),
            ).fetchone()

            def insert(tid: str, status: str, cache_hit: int, started: float | None) -> None:
                conn.execute(
                    "INSERT INTO jobs(id, user_id, translation_id, filename, status, cache_hit, "
                    "created_at, started_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (job_id, user["id"], tid, filename, status, cache_hit, at, started,
                     at if cache_hit else None),
                )
                self._append(conn, job_id, "milestone", {"text": f"已上传 {filename}"}, "job")

            if tr is not None and tr["status"] in FINISHED:
                insert(tr["id"], tr["status"], 1, at)
                self._append(conn, job_id, "hit", {
                    "text": "这篇论文已有译文，已直接加载 ⚡",
                    "sub": "相同论文、模型与思考强度的译文已存在",
                }, "job")
                self._append(conn, job_id, "milestone", {"text": "可以下载"}, "job")
                self._append(conn, job_id, "status", {"status": tr["status"]}, "job")
            else:
                if self.quota_used(user["id"]) >= user["daily_quota"]:
                    raise ToolError("quota_exceeded", "今日额度已用完，明天 0 点恢复")
                if tr is not None and tr["status"] in ACTIVE:
                    insert(tr["id"], tr["status"], 0, tr["started_at"])
                    sibling = conn.execute(
                        _JOBS_SIBLING, (tr["id"], job_id, *ACTIVE),
                    ).fetchone()
                    if sibling is not None:
                        for event in conn.execute(
                            "SELECT kind, payload FROM job_events WHERE job_id = ? AND scope = 'tr' "
                            "ORDER BY seq",
                            (sibling["id"],),
                        ).fetchall():
                            self._append(conn, job_id, event["kind"], json.loads(event["payload"]), "tr")
                    if tr["status"] == "queued":
                        self._append(conn, job_id, "milestone", {"text": "已进入排队"}, "job")
                else:
                    if tr is None:
                        tid = _new_id()
                        conn.execute(
                            "INSERT INTO translations(id, source_sha, model, thinking, engine_sha, "
                            "status, created_at, queued_at) VALUES (?,?,?,?,?,'queued',?,?)",
                            (tid, sha, model, thinking, self.engine_sha, at, at),
                        )
                    else:  # 之前失败或被取消：同一缓存键重新排队
                        tid = tr["id"]
                        conn.execute(
                            "UPDATE translations SET status = 'queued', queued_at = ?, started_at = NULL, "
                            "finished_at = NULL, result_json = NULL, stats = NULL WHERE id = ?",
                            (at, tid),
                        )
                    insert(tid, "queued", 0, None)
                    self._append(conn, job_id, "milestone", {"text": "已进入排队"}, "job")
                    enqueued = True
        self.hub.notify()
        if enqueued:
            self.on_enqueue()
        return job_id

    # ---- 查询 -------------------------------------------------------------- #
    def get_job(self, user_id: int, job_id: str) -> sqlite3.Row | None:
        """本人、未删除的 job（连同 translation 与原文信息）；否则 ``None``。"""
        return self.db.one(
            "SELECT jobs.*, t.source_sha, t.model, t.thinking, t.status AS tr_status, t.attempt, "
            "t.translated_sha, t.stats, t.queued_at, s.size, s.pages, s.page_sizes "
            "FROM jobs JOIN translations t ON t.id = jobs.translation_id "
            "JOIN sources s ON s.sha256 = t.source_sha "
            "WHERE jobs.id = ? AND jobs.user_id = ? AND jobs.deleted_at IS NULL",
            (job_id, user_id),
        )

    def queue_info(self, tid: str) -> dict | None:
        """排队中的 translation：前面还有几篇（含正在跑的）与预计开始秒数。"""
        tr = self.db.one("SELECT status, queued_at FROM translations WHERE id = ?", (tid,))
        if tr is None or tr["status"] not in ("queued", "recompile"):
            return None
        ahead = self.db.one(
            "SELECT COUNT(*) FROM translations WHERE status = 'running' "
            "OR (status IN ('queued', 'recompile') AND queued_at < ?)",
            (tr["queued_at"],),
        )[0]
        recent = self.db.all(
            "SELECT finished_at - started_at AS took FROM translations WHERE status IN ('done', 'partial') "
            "AND started_at IS NOT NULL ORDER BY finished_at DESC LIMIT 10"
        )
        average = sum(r["took"] for r in recent) / len(recent) if recent else DEFAULT_ETA_SECONDS
        return {"ahead": ahead, "eta_seconds": round(ahead * average)}

    def job_view(self, job: sqlite3.Row) -> dict:
        stats = json.loads(job["stats"]) if job["stats"] else None
        return {
            "id": job["id"],
            "filename": job["filename"],
            "status": job["status"],
            "cache_hit": bool(job["cache_hit"]),
            "rerun": bool(job["rerun"]) if "rerun" in job.keys() else False,
            "model": job["model"],
            "thinking": job["thinking"],
            "size": job["size"],
            "pages": job["pages"],
            "page_sizes": json.loads(job["page_sizes"]),
            "created_at": job["created_at"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
            "stats": stats if job["status"] in FINISHED + ("failed",) else None,
            # 完成后译文预览按译文 sha 缓存；翻译中用页事件里的 rev。
            "final_rev": job["translated_sha"][:16] if job["status"] in FINISHED else None,
            "queue": self.queue_info(job["translation_id"]) if job["status"] in ("queued", "recompile") else None,
        }

    def list_jobs(self, user_id: int) -> list[dict]:
        rows = self.db.all(
            "SELECT jobs.id, jobs.filename, jobs.status, jobs.cache_hit, jobs.created_at, "
            "jobs.finished_at, t.stats FROM jobs JOIN translations t ON t.id = jobs.translation_id "
            "WHERE jobs.user_id = ? AND jobs.deleted_at IS NULL ORDER BY jobs.created_at DESC",
            (user_id,),
        )
        items = []
        for r in rows:
            stats = json.loads(r["stats"]) if r["stats"] and r["status"] in FINISHED else {}
            items.append({
                "id": r["id"],
                "filename": r["filename"],
                "status": r["status"],
                "cache_hit": bool(r["cache_hit"]),
                "created_at": r["created_at"],
                "finished_at": r["finished_at"],
                "warnings": stats.get("warnings", 0),
            })
        return items

    # ---- 用户操作 ------------------------------------------------------------ #
    def cancel_job(self, user_id: int, job_id: str) -> None:
        kill = None
        with self.db.transaction() as conn:
            job = self.get_job(user_id, job_id)
            if job is None:
                raise ToolError("job_not_found", "找不到这条记录")
            if job["status"] not in ACTIVE:
                raise ToolError("job_not_active", "只有排队中或翻译中的任务可以取消")
            conn.execute(
                "UPDATE jobs SET status = 'canceled', finished_at = ? WHERE id = ?", (now(), job_id)
            )
            self._append(conn, job_id, "milestone", {"text": "已取消，本次不计入今日额度"}, "job")
            self._append(conn, job_id, "status", {"status": "canceled"}, "job")
            tid = job["translation_id"]
            waiting = conn.execute(
                _WAITING_COUNT, (tid, *ACTIVE)
            ).fetchone()[0]
            if not waiting:
                status = conn.execute("SELECT status FROM translations WHERE id = ?", (tid,)).fetchone()[0]
                if status in ("queued", "recompile"):
                    if not self._restore_rerun(conn, tid, "已取消重新开始，保留原来的译文"):
                        conn.execute(
                            "UPDATE translations SET status = 'canceled', finished_at = ? WHERE id = ?",
                            (now(), tid),
                        )
                elif status == "running":
                    kill = tid
        self.hub.notify()
        if kill is not None:
            self.on_cancel_running(kill)

    def delete_job(self, user_id: int, job_id: str) -> None:
        with self.db.transaction() as conn:
            job = self.get_job(user_id, job_id)
            if job is None:
                raise ToolError("job_not_found", "找不到这条记录")
            if job["status"] in ACTIVE:
                raise ToolError("job_active", "进行中的任务请先取消再删除")
            conn.execute("UPDATE jobs SET deleted_at = ? WHERE id = ?", (now(), job_id))

    def rerun_job(self, user_id: int, job_id: str, action: str) -> dict:
        """对已完成的 job 重新编译或完整重翻。

        - ``recompile``：用已保存的块级译文缓存 ``--cache-only`` 重排，不调模型。
        - ``retranslate``：从头完整重翻（调模型；旧缓存留到新一轮成功后才被替换）。

        job 与 translation 原地回到运行态；重新开始失败或被取消时整体回滚到重跑前的
        状态（:meth:`_restore_rerun`），不会因为一次失败的重跑丢掉原来的译文。
        """
        if action not in RERUN_ACTIONS:
            raise ToolError("invalid_request", "action 只能是 recompile 或 retranslate")
        with self.db.transaction() as conn:
            job = self.get_job(user_id, job_id)
            if job is None:
                raise ToolError("job_not_found", "找不到这条记录")
            tid = job["translation_id"]
            if job["status"] in ACTIVE:
                raise ToolError("job_active", "任务还在进行中，不能重新开始")
            if job["status"] not in FINISHED:
                raise ToolError("job_not_rerunnable", "只有已完成或部分完成的任务可以重新开始")
            # 其它用户可能挂着同一 translation 的 job：在跑就拒绝。
            if conn.execute(_JOBS_SIBLING, (tid, job_id, *ACTIVE)).fetchone() is not None:
                raise ToolError("job_active", "这篇论文正在被其他用户处理，请稍后再试")
            if action == "recompile" and not self.paths.translate_cache(tid).is_file():
                raise ToolError("cache_missing", "这篇论文没有已保存的译文缓存，请完整重新翻译")
            if action == "retranslate":
                quota = conn.execute("SELECT daily_quota FROM users WHERE id = ?", (user_id,)).fetchone()
                if quota is not None and self.quota_used(user_id) >= quota[0]:
                    raise ToolError("quota_exceeded", "今日额度已用完，明天 0 点恢复")
            at = now()
            new_status = "recompile" if action == "recompile" else "queued"
            snapshot = {
                "tr": dict(conn.execute(
                    "SELECT status, stats, result_json, translated_sha, finished_at "
                    "FROM translations WHERE id = ?", (tid,),
                ).fetchone()),
                "job": {"id": job_id, "cache_hit": job["cache_hit"], "rerun": job["rerun"]},
            }
            conn.execute(
                "UPDATE translations SET status = ?, queued_at = ?, started_at = NULL, "
                "finished_at = NULL, rerun_action = ?, rerun_prev = ? WHERE id = ?",
                (new_status, at, action, json.dumps(snapshot), tid),
            )
            conn.execute(
                "UPDATE jobs SET status = ?, rerun = 1, started_at = ?, finished_at = NULL, "
                "cache_hit = 0 WHERE id = ?",
                (new_status, at, job_id),
            )
            # 先 status 后 milestone：前端在 status 事件处丢掉上一轮的进度，里程碑属于新一轮。
            self._append(conn, job_id, "status", {"status": new_status}, "job")
            text = "用已保存的译文重新编译" if action == "recompile" else "从头完整重新翻译"
            self._append(conn, job_id, "milestone", {"text": text}, "job")
        self.hub.notify()
        self.on_enqueue()
        return self.job_view(self.get_job(user_id, job_id))

    def _restore_rerun(self, conn: sqlite3.Connection, tid: str, note: str) -> bool:
        """把重跑中途失败或被取消的 translation 回滚到重跑前；没有重跑快照返回 ``False``。"""
        row = conn.execute("SELECT rerun_prev FROM translations WHERE id = ?", (tid,)).fetchone()
        if row is None or not row[0]:
            return False
        prev = json.loads(row[0])
        tr, owner = prev["tr"], prev["job"]
        at = now()
        waiting = [r["id"] for r in conn.execute(_JOBS_OF_TRANSLATION, (tid, *ACTIVE)).fetchall()]
        conn.execute(
            "UPDATE translations SET status = ?, stats = ?, result_json = ?, translated_sha = ?, "
            "finished_at = ?, rerun_action = NULL, rerun_prev = NULL WHERE id = ?",
            (tr["status"], tr["stats"], tr["result_json"], tr["translated_sha"], tr["finished_at"], tid),
        )
        conn.execute(
            "UPDATE jobs SET cache_hit = ?, rerun = ? WHERE id = ?",
            (owner["cache_hit"], owner["rerun"], owner["id"]),
        )
        for job_id in dict.fromkeys([owner["id"], *waiting]):
            conn.execute(
                "UPDATE jobs SET status = ?, finished_at = ? WHERE id = ?", (tr["status"], at, job_id)
            )
            self._append(conn, job_id, "milestone", {"text": note}, "tr")
            self._append(conn, job_id, "status", {"status": tr["status"]}, "tr")
        return True

    # ---- runner 使用 --------------------------------------------------------- #
    def recover(self) -> list[str]:
        """启动时：上次没跑完的 translation 放回队首，名下 job 回到排队。"""
        with self.db.transaction() as conn:
            running = [r["id"] for r in conn.execute(
                "SELECT id FROM translations WHERE status = 'running' ORDER BY started_at"
            ).fetchall()]
            first = conn.execute("SELECT MIN(queued_at) FROM translations").fetchone()[0] or now()
            for offset, tid in enumerate(running):
                conn.execute(
                    "UPDATE translations SET status = 'queued', queued_at = ? WHERE id = ?",
                    (first - len(running) + offset, tid),
                )
                for job in conn.execute(
                    "SELECT id FROM jobs WHERE translation_id = ? AND status = 'running'", (tid,)
                ).fetchall():
                    conn.execute("UPDATE jobs SET status = 'queued' WHERE id = ?", (job["id"],))
                    self._append(conn, job["id"], "milestone", {"text": "服务已重启，任务将重新开始"}, "tr")
                    self._append(conn, job["id"], "status", {"status": "queued"}, "tr")
        return running

    def claim_next(self) -> sqlite3.Row | None:
        """取最早排队的 translation 标为 running；没有则 ``None``。"""
        with self.db.transaction() as conn:
            tr = conn.execute(
                "SELECT id FROM translations WHERE status IN ('queued', 'recompile') "
                "ORDER BY queued_at LIMIT 1"
            ).fetchone()
            if tr is None:
                return None
            at = now()
            conn.execute(
                "UPDATE translations SET status = 'running', attempt = attempt + 1, started_at = ? "
                "WHERE id = ?",
                (at, tr["id"]),
            )
            conn.execute(
                "UPDATE jobs SET status = 'running', started_at = ? WHERE translation_id = ? "
                "AND status IN ('queued', 'recompile')",
                (at, tr["id"]),
            )
            row = conn.execute(
                "SELECT t.*, s.pages FROM translations t JOIN sources s ON s.sha256 = t.source_sha "
                "WHERE t.id = ?",
                (tr["id"],),
            ).fetchone()
        return row

    def has_waiting_jobs(self, tid: str) -> bool:
        return bool(self.db.one(
            _WAITING_COUNT, (tid, *ACTIVE)
        )[0])

    def finish(
        self,
        tid: str,
        status: str,
        *,
        events: list[tuple[str, dict]],
        stats: dict | None = None,
        result: dict | None = None,
        translated_sha: str | None = None,
    ) -> None:
        """translation 终态（或 ``queued`` 重新排队）：先发收尾事件，再同步 job 状态。

        重跑（重新编译/重翻）以 ``failed``/``canceled`` 收场时不落终态，回滚到重跑前。
        """
        at = now()
        final = status != "queued"
        with self.db.transaction() as conn:
            waiting = conn.execute(_JOBS_OF_TRANSLATION, (tid, *ACTIVE)).fetchall()
            if status in ("failed", "canceled"):
                for job in waiting:
                    for kind, payload in events:
                        self._append(conn, job["id"], kind, payload, "tr")
                note = "重新处理没有完成，已保留原来的译文" if status == "failed" else "已取消，保留原来的译文"
                if self._restore_rerun(conn, tid, note):
                    self.hub.notify()
                    return
                events = []  # 已按上面追加过，下面只同步状态
            conn.execute(
                "UPDATE translations SET status = ?, stats = ?, result_json = ?, translated_sha = ?, "
                "finished_at = ?, queued_at = CASE WHEN ? THEN queued_at ELSE ? END WHERE id = ?",
                (
                    status,
                    json.dumps(stats, ensure_ascii=False) if stats is not None else None,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    translated_sha,
                    at if final else None,
                    final,
                    at,
                    tid,
                ),
            )
            if final:
                conn.execute(
                    "UPDATE translations SET rerun_action = NULL, rerun_prev = NULL WHERE id = ?", (tid,)
                )
            for job in waiting:
                for kind, payload in events:
                    self._append(conn, job["id"], kind, payload, "tr")
                self._append(conn, job["id"], "status", {"status": status}, "tr")
                conn.execute(
                    "UPDATE jobs SET status = ?, finished_at = ? WHERE id = ?",
                    (status, at if final else None, job["id"]),
                )
        self.hub.notify()
        if not final:
            self.on_enqueue()
