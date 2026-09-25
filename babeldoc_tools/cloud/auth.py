"""邀请码账户与 cookie 会话：数据库只存 token 的 SHA-256，不存明文。"""

from __future__ import annotations

import collections
import hashlib
import secrets
import sqlite3
import threading
import time

from babeldoc_tools.cloud.db import Database
from babeldoc_tools.cloud.db import now

__all__ = [
    "COOKIE",
    "SESSION_SECONDS",
    "LoginLimiter",
    "create_invite",
    "login",
    "logout",
    "mask_code",
    "user_for_token",
]

COOKIE = "bdt_session"
SESSION_SECONDS = 30 * 24 * 3600
#: 去掉易混淆的 0/O、1/I。
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _normalize(code: str) -> str:
    return code.strip().upper()


def create_invite(db: Database, name: str, daily_quota: int) -> dict:
    """新建一个账户并返回其邀请码（形如 ``YJ-7Q2K-M8TD``）。"""
    for _ in range(20):
        body = "".join(secrets.choice(_ALPHABET) for _ in range(8))
        code = f"YJ-{body[:4]}-{body[4:]}"
        try:
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO users(invite_code, name, daily_quota, created_at) VALUES (?,?,?,?)",
                    (code, name, daily_quota, now()),
                )
        except sqlite3.IntegrityError:
            continue
        return {"code": code, "name": name, "daily_quota": daily_quota}
    raise RuntimeError("无法生成不重复的邀请码")


def mask_code(code: str) -> str:
    head, _, tail = code.rpartition("-")
    return f"{head.split('-')[0]}-****-{tail}" if head else code


def login(db: Database, code: str) -> str | None:
    """邀请码有效 → 新会话 token（明文只返回这一次）；无效 → ``None``。"""
    user = db.one("SELECT id FROM users WHERE invite_code = ?", (_normalize(code),))
    if user is None:
        return None
    token = secrets.token_urlsafe(32)
    with db.transaction() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now(),))
        conn.execute(
            "INSERT INTO sessions(token_hash, user_id, expires_at) VALUES (?,?,?)",
            (_hash(token), user["id"], now() + SESSION_SECONDS),
        )
    return token


def logout(db: Database, token: str) -> None:
    with db.transaction() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash(token),))


def user_for_token(db: Database, token: str | None) -> sqlite3.Row | None:
    if not token:
        return None
    return db.one(
        "SELECT users.* FROM sessions JOIN users ON users.id = sessions.user_id "
        "WHERE sessions.token_hash = ? AND sessions.expires_at > ?",
        (_hash(token), now()),
    )


class LoginLimiter:
    """按来源 IP 限制失败次数：窗口内失败达到上限后，窗口结束前一律拒绝。"""

    def __init__(self, limit: int = 10, window: float = 600.0) -> None:
        self.limit = limit
        self.window = window
        self._failures: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self._lock = threading.Lock()

    def _recent(self, ip: str, at: float) -> collections.deque:
        failures = self._failures[ip]
        while failures and failures[0] <= at - self.window:
            failures.popleft()
        return failures

    def blocked(self, ip: str) -> bool:
        with self._lock:
            return len(self._recent(ip, time.monotonic())) >= self.limit

    def fail(self, ip: str) -> None:
        with self._lock:
            at = time.monotonic()
            self._recent(ip, at).append(at)
