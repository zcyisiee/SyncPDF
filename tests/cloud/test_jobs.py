"""上传校验、配额、共享缓存与同键合并、排队、取消、软删除与重启恢复。"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from babeldoc_tools.cloud import uploads
from babeldoc_tools.cloud.db import Database
from conftest import Cloud
from conftest import make_pdf
from conftest import parse_sse
from conftest import wait_for

REPO = Path(__file__).resolve().parents[2]


def _events(client, job_id: str, after: int = 0) -> list[dict]:
    headers = {"Last-Event-ID": str(after)} if after else {}
    with client.stream("GET", f"/api/jobs/{job_id}/events", headers=headers) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        return parse_sse(response.iter_lines())


def _texts(events: list[dict]) -> list[str]:
    return [e["data"]["text"] for e in events if "text" in e.get("data", {})]


def test_upload_rejects_non_pdf_scans_and_oversize(cloud, monkeypatch):
    client = cloud.client()
    fake = cloud.tmp / "notes.pdf"
    fake.write_bytes(b"hello, not a pdf")
    assert cloud.upload(client, fake).json()["error"]["code"] == "not_pdf"

    scan = cloud.pdf("scan.pdf", text="")  # 只有空白页：没有文字层
    response = cloud.upload(client, scan)
    assert response.status_code == 422 and response.json()["error"]["code"] == "no_text_layer"

    with monkeypatch.context() as patch:
        patch.setattr(uploads, "MAX_PAGES", 1)
        response = cloud.upload(client, cloud.pdf("long.pdf", pages=2))
        assert response.status_code == 422 and response.json()["error"]["code"] == "too_many_pages"
    with monkeypatch.context() as patch:
        patch.setattr(uploads, "MAX_BYTES", 100)
        response = cloud.upload(client, cloud.pdf("big.pdf", pages=1))
        assert response.status_code == 413 and response.json()["error"]["code"] == "file_too_large"

    response = cloud.upload(client, cloud.pdf("ok.pdf"), thinking="max")
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_thinking"

    # 被拒的上传不留临时文件，也不进原文库
    assert list(cloud.paths.tmp.iterdir()) == []
    assert list(cloud.paths.sources.iterdir()) == []
    assert client.get("/api/jobs").json() == {"items": []}


def test_quota_counts_translations_but_not_cache_hits_cancels_or_failures(cloud, monkeypatch):
    client = cloud.client(quota=2)
    paper = cloud.pdf("a.pdf")
    first = cloud.upload(client, paper).json()
    cloud.runner.run_once()
    assert client.get("/api/me").json()["used"] == 1

    # 同一篇、同档位：缓存命中，不占额度
    hit = cloud.upload(client, paper, name="a-again.pdf").json()
    assert hit["cache_hit"] and hit["status"] == "done"
    # 取消的不计
    canceled = cloud.upload(client, cloud.pdf("b.pdf")).json()
    client.post(f"/api/jobs/{canceled['id']}/cancel")
    # 失败的不计
    monkeypatch.setenv("FAKE_MODE", "fail")
    failed = cloud.upload(client, cloud.pdf("c.pdf")).json()
    cloud.runner.run_once()
    assert client.get(f"/api/jobs/{failed['id']}").json()["status"] == "failed"
    me = client.get("/api/me").json()
    assert (me["used"], me["remaining"]) == (1, 1)

    cloud.upload(client, cloud.pdf("d.pdf"))
    response = cloud.upload(client, cloud.pdf("e.pdf"))
    assert response.status_code == 429 and response.json()["error"]["code"] == "quota_exceeded"
    # 额度用完仍可命中缓存
    assert cloud.upload(client, paper, name="third.pdf").json()["cache_hit"]
    assert first["id"] != hit["id"]


def test_same_key_is_translated_once_and_shared_across_users(cloud):
    alice, bob, carol = cloud.client("alice"), cloud.client("bob"), cloud.client("carol")
    paper = cloud.pdf("paper.pdf")
    a = cloud.upload(alice, paper).json()
    b = cloud.upload(bob, paper, name="my-copy.pdf").json()  # 还在排队：挂到同一个 translation
    assert a["status"] == b["status"] == "queued" and not b["cache_hit"]
    assert cloud.service.db.one("SELECT COUNT(*) FROM translations")[0] == 1

    assert cloud.runner.run_once() and not cloud.runner.run_once()
    assert alice.get(f"/api/jobs/{a['id']}").json()["status"] == "done"
    assert bob.get(f"/api/jobs/{b['id']}").json()["status"] == "done"
    assert "第一页译文已就绪" in _texts(_events(bob, b["id"]))

    c = cloud.upload(carol, paper).json()
    assert c["status"] == "done" and c["cache_hit"] and c["final_rev"]
    events = _events(carol, c["id"])
    assert [e["event"] for e in events] == ["milestone", "hit", "milestone", "status", "end"]
    assert carol.get(f"/api/jobs/{c['id']}/download").content == alice.get(f"/api/jobs/{a['id']}/download").content

    # 换思考强度才重新翻译
    d = cloud.upload(carol, paper, thinking="high").json()
    assert d["status"] == "queued" and not d["cache_hit"]
    assert cloud.service.db.one("SELECT COUNT(*) FROM translations")[0] == 2
    # 引擎二进制变了（缓存键里的 engine_sha）也不复用旧译文
    cloud.service.engine_sha = "another-build"
    assert cloud.upload(carol, paper, name="rebuilt.pdf").json()["status"] == "queued"


def test_queue_is_fifo_with_ahead_count_and_eta(cloud):
    client = cloud.client(quota=10)
    jobs = [cloud.upload(client, cloud.pdf(f"q{i}.pdf")).json() for i in range(3)]
    assert [j["queue"]["ahead"] for j in jobs] == [0, 1, 2]
    assert jobs[2]["queue"]["eta_seconds"] == 2 * 300  # 没有历史耗时：每篇按 5 分钟

    cloud.runner.run_once()
    # 最近完成的平均耗时决定 ETA
    cloud.service.db.one("UPDATE translations SET started_at = finished_at - 120 WHERE status = 'done'")
    view = client.get(f"/api/jobs/{jobs[2]['id']}").json()
    assert view["queue"] == {"ahead": 1, "eta_seconds": 120}
    first = client.get(f"/api/jobs/{jobs[0]['id']}").json()
    assert first["status"] == "done" and first["queue"] is None
    assert cloud.service.claim_next()["source_sha"] == cloud.service.db.one(
        "SELECT t.source_sha FROM jobs j JOIN translations t ON t.id = j.translation_id WHERE j.id = ?",
        (jobs[1]["id"],),
    )[0]


def test_sse_pushes_queue_changes_and_cancel_ends_stream(cloud):
    client = cloud.client()
    job = cloud.upload(client, cloud.pdf("s.pdf")).json()
    with cloud.live(client) as live, live.stream("GET", f"/api/jobs/{job['id']}/events") as response:
        lines = response.iter_lines()
        seen = []
        for line in lines:
            seen.append(line)
            if line.startswith("data: ") and seen[-2] == "event: queue":
                break
        assert '"ahead": 0' in seen[-1]
        assert client.post(f"/api/jobs/{job['id']}/cancel").json()["status"] == "canceled"
        rest = parse_sse(lines)
    assert [e["event"] for e in rest][-3:] == ["milestone", "status", "end"]
    assert rest[-2]["data"]["status"] == "canceled"


def test_sse_resumes_after_last_event_id(cloud):
    client = cloud.client()
    job = cloud.upload(client, cloud.pdf("r.pdf", pages=3)).json()
    cloud.runner.run_once()
    full = _events(client, job["id"])
    ids = [e["id"] for e in full if "id" in e]
    assert ids == list(range(1, len(ids) + 1))
    resumed = _events(client, job["id"], after=ids[4])
    assert [e["id"] for e in resumed if "id" in e] == ids[5:]
    assert resumed[-1]["event"] == "end"
    assert _texts(full)[-2:] == ["全部完成", "可以下载"]


def test_cancel_queued_job_marks_translation_canceled_and_requeues_on_retry(cloud):
    client = cloud.client()
    paper = cloud.pdf("c.pdf")
    job = cloud.upload(client, paper).json()
    assert client.post(f"/api/jobs/{job['id']}/cancel").json()["status"] == "canceled"
    assert cloud.service.db.one("SELECT status FROM translations")[0] == "canceled"
    assert client.post(f"/api/jobs/{job['id']}/cancel").status_code == 409
    assert not cloud.runner.run_once()

    again = cloud.upload(client, paper).json()
    assert again["status"] == "queued"
    assert cloud.service.db.one("SELECT COUNT(*) FROM translations")[0] == 1  # 同键复用原 translation
    assert cloud.runner.run_once()
    assert client.get(f"/api/jobs/{again['id']}").json()["status"] == "done"
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "canceled"


@pytest.fixture
def gated(cloud, monkeypatch):
    gate = cloud.tmp / "gate"
    monkeypatch.setenv("FAKE_GATE", str(gate))
    return gate


def _run_in_background(cloud: Cloud) -> threading.Thread:
    thread = threading.Thread(target=cloud.runner.run_once)
    thread.start()
    return thread


def test_cancel_running_kills_engine_only_when_nobody_else_waits(cloud, gated):
    alice, bob = cloud.client("alice"), cloud.client("bob")
    paper = cloud.pdf("run.pdf")
    a = cloud.upload(alice, paper).json()
    b = cloud.upload(bob, paper).json()
    nxt = cloud.upload(alice, cloud.pdf("next.pdf")).json()
    thread = _run_in_background(cloud)
    wait_for(lambda: any(e["kind"] == "page" for e in cloud.service.events_after(a["id"], 0)))
    assert alice.get(f"/api/jobs/{a['id']}").json()["status"] == "running"

    # alice 取消：bob 还在等，引擎继续
    alice.post(f"/api/jobs/{a['id']}/cancel")
    assert cloud.service.db.one("SELECT status FROM translations WHERE id = (SELECT translation_id FROM jobs WHERE id = ?)", (a["id"],))[0] == "running"
    assert thread.is_alive()
    # bob 也取消：进程组被终止，translation 标为 canceled
    bob.post(f"/api/jobs/{b['id']}/cancel")
    thread.join(timeout=15)
    assert not thread.is_alive()
    row = cloud.service.db.one("SELECT status, translated_sha FROM translations WHERE id = (SELECT translation_id FROM jobs WHERE id = ?)", (b["id"],))
    assert tuple(row) == ("canceled", None)
    assert list(cloud.paths.work.iterdir()) == []
    assert bob.get(f"/api/jobs/{b['id']}").json()["status"] == "canceled"

    # 下一篇照常开始
    gated.touch()
    assert cloud.runner.run_once()
    assert alice.get(f"/api/jobs/{nxt['id']}").json()["status"] == "done"


def test_restart_puts_running_translation_back_at_queue_front(cloud, fake_engine):
    client = cloud.client(quota=10)
    first = cloud.upload(client, cloud.pdf("first.pdf")).json()
    later = cloud.upload(client, cloud.pdf("later.pdf")).json()
    # 模拟：进程在运行中被杀，留下 running 状态与半截 workdir
    tr = cloud.service.claim_next()
    (cloud.paths.work / tr["id"]).mkdir()
    (cloud.paths.work / tr["id"] / "translated.pdf").write_bytes(b"%PDF-half")
    cloud.close()

    restarted = Cloud(cloud.root, fake_engine, cloud.tmp)
    try:
        restarted.runner.recover()
        assert list(restarted.paths.work.iterdir()) == []
        cookie = client.cookies
        again = restarted.anonymous()
        again.cookies = cookie
        view = again.get(f"/api/jobs/{first['id']}").json()
        assert view["status"] == "queued" and view["queue"]["ahead"] == 0
        assert again.get(f"/api/jobs/{later['id']}").json()["queue"]["ahead"] == 1
        texts = [e.get("text") for e in restarted.service.events_after(first["id"], 0)]
        assert "服务已重启，任务将重新开始" in texts

        assert restarted.runner.run_once()
        done = again.get(f"/api/jobs/{first['id']}").json()
        assert done["status"] == "done"
        attempt = restarted.service.db.one("SELECT attempt FROM translations WHERE id = ?", (tr["id"],))[0]
        assert attempt == 2
        page_revs = [e["rev"] for e in restarted.service.events_after(first["id"], 0) if e["kind"] == "page"]
        assert page_revs[-1].startswith("2.")  # 新一轮的预览 rev 不会撞上旧缓存
    finally:
        restarted.close()


def _serve(root, engine, log, env):
    """真实 ``bdt cloud serve`` 子进程；返回 ``(进程, API 地址)``。"""
    process = subprocess.Popen(  # noqa: S603 - argv 由测试构造
        [sys.executable, "-m", "babeldoc_tools", "cloud", "serve", "--root", str(root), "--port", "0",
         "--engine", str(engine), "--translator", "fake:echo"],
        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=log, text=True,
    )
    return process, json.loads(process.stdout.readline())["data"]["api"]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_service_stop_mid_run_requeues_instead_of_failing(tmp_path, fake_engine):
    """回归：停服（systemd 只给服务进程发 SIGTERM）时，运行中的翻译不能记成失败。

    有 SSE 连接挂着时服务也要及时退出；由服务自己终止引擎并把翻译留给下次启动重排。
    """
    import httpx

    root, gate, pid_file = tmp_path / "root", tmp_path / "gate", tmp_path / "engine.pid"
    env = {**os.environ, "FAKE_GATE": str(gate), "FAKE_PID": str(pid_file), "FAKE_PAGES": "2"}
    code = json.loads(subprocess.run(  # noqa: S603 - argv 由测试构造
        [sys.executable, "-m", "babeldoc_tools", "cloud", "invite", "--root", str(root), "--name", "t"],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout)["data"]["code"]
    pdf = make_pdf(tmp_path / "paper.pdf")
    with (tmp_path / "serve.log").open("w") as log:
        process, api = _serve(root, fake_engine, log, env)
        try:
            client = httpx.Client(base_url=api.removesuffix("/api"), timeout=10)
            assert client.post("/api/login", json={"code": code}).status_code == 200
            with pdf.open("rb") as handle:
                job = client.post("/api/jobs", files={"file": ("paper.pdf", handle)},
                                  data={"model": "deepseek/deepseek-flash", "thinking": "low"}).json()
            wait_for(lambda: pid_file.exists() and client.get(f"/api/jobs/{job['id']}").json()["status"] == "running")
            engine_pid = int(pid_file.read_text())
            # 有人正看着进度：SSE 长连接不能拖住停服
            watcher = threading.Thread(target=lambda: client.get(f"/api/jobs/{job['id']}/events", timeout=60), daemon=True)
            watcher.start()
            time.sleep(0.3)
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=15)
            wait_for(lambda: not _alive(engine_pid), timeout=10)
        finally:
            if process.poll() is None:
                process.kill()

        gate.touch()
        process, api = _serve(root, fake_engine, log, env)
        try:
            client = httpx.Client(base_url=api.removesuffix("/api"), timeout=10)
            assert client.post("/api/login", json={"code": code}).status_code == 200
            wait_for(lambda: client.get(f"/api/jobs/{job['id']}").json()["status"] not in ("queued", "running"))
            assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "done"
        finally:
            process.terminate()
            process.wait(timeout=15)
    db = Database(root / "app.db")
    try:
        rows = db.all("SELECT kind, payload FROM job_events WHERE job_id = ? ORDER BY seq", (job["id"],))
    finally:
        db.close()
    statuses = [json.loads(r["payload"])["status"] for r in rows if r["kind"] == "status"]
    assert "failed" not in statuses
    assert "服务已重启，任务将重新开始" in [json.loads(r["payload"]).get("text") for r in rows]


def test_soft_delete_hides_only_own_record(cloud):
    alice, bob = cloud.client("alice"), cloud.client("bob")
    paper = cloud.pdf("shared.pdf")
    a = cloud.upload(alice, paper).json()
    cloud.runner.run_once()
    b = cloud.upload(bob, paper).json()
    assert b["cache_hit"]

    running = cloud.upload(alice, cloud.pdf("busy.pdf")).json()
    assert alice.delete(f"/api/jobs/{running['id']}").json()["error"]["code"] == "job_active"

    assert alice.delete(f"/api/jobs/{a['id']}").status_code == 204
    assert alice.get(f"/api/jobs/{a['id']}").status_code == 404
    assert [item["id"] for item in alice.get("/api/jobs").json()["items"]] == [running["id"]]
    # bob 的记录与文件不受影响
    assert bob.get(f"/api/jobs/{b['id']}").json()["status"] == "done"
    assert bob.get(f"/api/jobs/{b['id']}/download").status_code == 200
