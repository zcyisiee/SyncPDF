"""邀请码登录、会话、限速与越权访问。"""

from __future__ import annotations

import json
import subprocess
import sys

from babeldoc_tools.cloud import auth


def test_login_sets_session_cookie_and_me_reports_quota(cloud):
    code = cloud.invite("内测用户", quota=3)
    client = cloud.anonymous()
    assert client.get("/api/me").status_code == 401

    response = client.post("/api/login", json={"code": code.lower()})  # 大小写不敏感
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Secure" not in cookie

    me = client.get("/api/me").json()
    assert me == {
        "name": "内测用户",
        "code": f"YJ-****-{code[-4:]}",
        "daily_quota": 3,
        "used": 0,
        "remaining": 3,
    }
    # 数据库只存 token 的哈希
    token = client.cookies.get(auth.COOKIE)
    assert cloud.service.db.one("SELECT COUNT(*) FROM sessions WHERE token_hash = ?", (token,))[0] == 0

    assert client.post("/api/logout").status_code == 200
    client.cookies.set(auth.COOKIE, token)
    assert client.get("/api/me").status_code == 401


def test_failed_logins_are_rate_limited_per_ip(cloud):
    code = cloud.invite()
    client = cloud.anonymous()
    for _ in range(10):
        assert client.post("/api/login", json={"code": "YJ-NOPE-NOPE"}).status_code == 401
    # 达到上限后，正确的邀请码在窗口内也被拒绝；另一个来源 IP 不受影响
    assert client.post("/api/login", json={"code": code}).status_code == 429
    other = client.post("/api/login", json={"code": code}, headers={"X-Real-IP": "10.0.0.9"})
    assert other.status_code == 200


def test_other_users_jobs_are_not_found(cloud):
    alice, bob = cloud.client("alice"), cloud.client("bob")
    job = cloud.upload(alice, cloud.pdf("a.pdf")).json()
    cloud.runner.run_once()

    base = f"/api/jobs/{job['id']}"
    for method, url in (
        ("GET", base),
        ("GET", f"{base}/events"),
        ("GET", f"{base}/pages/1.webp"),
        ("GET", f"{base}/download"),
        ("POST", f"{base}/cancel"),
        ("DELETE", base),
    ):
        response = bob.request(method, url)
        assert response.status_code == 404, (method, url, response.text)
        assert response.json()["error"]["code"] == "job_not_found"
    assert bob.get("/api/jobs").json() == {"items": []}
    assert alice.get(base).json()["status"] == "done"


def test_invite_cli_prints_code(tmp_path):
    out = subprocess.run(  # noqa: S603 - 固定 argv，无外部输入
        [sys.executable, "-m", "babeldoc_tools", "cloud", "invite", "--root", str(tmp_path / "r"),
         "--name", "内测", "--quota", "4"],
        capture_output=True, text=True, check=True,
    )
    payload = json.loads(out.stdout)
    assert payload["ok"] and payload["data"]["daily_quota"] == 4
    assert payload["data"]["code"].startswith("YJ-") and len(payload["data"]["code"]) == 12
