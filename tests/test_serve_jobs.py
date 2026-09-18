"""``bdt serve`` 的 job 核心：持久化调度、受控子进程、取消、重启恢复（W07）。

用**真子进程**跑真 ``bdt run``（不 mock 调度），fixture 只给最小 parse 产物：

- 成功路径：``--from report``（report 阶段只读产物 + 写 FINAL_REPORT.md，不联网、
  不调模型、不编译）；
- "运行中"：``--from translate`` + 睡着的 stub translator（它会再拉一个长命孙进程，
  取消 job 时两者都必须死 —— 进程组取消是本任务的验收核心）；
- 失败路径：stub translator 退出码非 0（信封 error_code 如实透传）。

stub 脚本只做 echo / sleep / exit，不联网；每个用例结束都会收掉残留进程组与
debug 查看器（查看器默认 30 分钟才自退，测试不能等它）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="serve 需要 web extra: fastapi")

from babeldoc_tools.serve import runner as runner_module  # noqa: E402
from babeldoc_tools.serve.app import create_app  # noqa: E402
from babeldoc_tools.serve.jobs import JobRegistry  # noqa: E402
from babeldoc_tools.serve.jobs import new_job_id  # noqa: E402
from babeldoc_tools.serve.jobs import pid_alive  # noqa: E402
from babeldoc_tools.serve.jobs import process_group_id  # noqa: E402
from babeldoc_tools.serve.profiles import Profile  # noqa: E402
from babeldoc_tools.serve.profiles import list_profile_ids  # noqa: E402
from babeldoc_tools.serve.profiles import resolve_profile  # noqa: E402
from babeldoc_tools.serve.routers.jobs import FORBIDDEN_JOB_FIELDS  # noqa: E402
from babeldoc_tools.serve.runner import MAX_ENVELOPE_BYTES  # noqa: E402
from babeldoc_tools.serve.runner import JobRunner  # noqa: E402
from babeldoc_tools.serve.runner import build_job_argv  # noqa: E402
from babeldoc_tools.serve.runner import classify_exit  # noqa: E402
from babeldoc_tools.serve.runner import sanitize_envelope  # noqa: E402
from babeldoc_tools.serve.runner import stdout_envelope  # noqa: E402
from babeldoc_tools.serve.schemas import API_PREFIX as API  # noqa: E402
from babeldoc_tools.serve.store import STATE_DIR  # noqa: E402
from babeldoc_tools.serve.store import DocumentStore  # noqa: E402
from babeldoc_tools.serve.workdir import list_run_ids  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

#: 4 个文档：并发上限 2 的用例要"1 个排队 + 1 个排队者"才验证得了 FIFO。
DOCS = ("alpha", "beta", "gamma", "delta")

#: ``j_`` + 26 字符 Crockford base32（ULID 风格）。
JOB_ID_RE = re.compile(r"^j_[0-9A-HJKMNP-TV-Z]{26}$")

#: 默认 profile：从不真的被调用的占位命令（``from=report`` 用例用不到 translator）。
STUB_COMMAND = "/usr/bin/true"

SLOW_TRANSLATOR = """#!/bin/sh
# W07 测试 stub：读干提示词（stdin），起一个长命孙进程并记下它的 pid，然后自身长睡。
# 取消 job 时这两层都必须死 —— 这就是"杀整个进程组"的验收面。
pidfile="$1"
cat > /dev/null
sleep 300 &
echo $! > "$pidfile"
sleep 300
"""

FAILING_TRANSLATOR = """#!/bin/sh
# 故意失败：退出码 3 → bdt translate 报 translator_failed（信封 error_code 透传）。
cat > /dev/null
echo "stub translator 故意失败" >&2
exit 3
"""


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _make_workdir(root: Path, did: str) -> Path:
    """最小 parse 产物：``agent/document.md`` 够 ``--from translate`` 过前置校验。"""
    agent = root / did / "agent"
    agent.mkdir(parents=True)
    (agent / "document.md").write_text(
        "<!--P01-001-->\nHello world.\n", encoding="utf-8"
    )
    (agent / "run_state.json").write_text("{}\n", encoding="utf-8")
    return root / did


def _write_profiles(root: Path, profiles: dict[str, dict[str, str]]) -> None:
    """写 ``<root>/.bdt-serve/profiles.json``（命令只在这里，HTTP 永远拿不到）。"""
    state = root / STATE_DIR
    state.mkdir(parents=True, exist_ok=True)
    (state / "profiles.json").write_text(
        json.dumps(profiles, ensure_ascii=False), encoding="utf-8"
    )


def _job_events(root: Path) -> list[dict]:
    """读 ``.bdt-serve/jobs.jsonl``（逐行解析，验证生命周期事件）。"""
    path = root / STATE_DIR / "jobs.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _snapshot(root: Path, job_id: str) -> dict:
    path = root / STATE_DIR / "jobs" / f"{job_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _shutdown(root: Path, *, timeout: float = 20.0) -> None:
    """测试收尾：收掉残留 job 的进程组，再停掉 debug 查看器。

    进程组用 SIGKILL 收（不会漏掉孙进程），然后等它们真的从进程表里消失——
    否则测试退出时还有管道写端开着，读线程与事件循环收尾都得白等。
    """
    groups = []
    for snapshot in sorted((root / STATE_DIR / "jobs").glob("j_*.json")):
        record = json.loads(snapshot.read_text(encoding="utf-8"))
        pgid = record.get("pgid")
        if record.get("status") == "running" and pgid and pid_alive(pgid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGKILL)
            groups.append(pgid)
    deadline = time.monotonic() + timeout
    while groups and time.monotonic() < deadline:
        groups = [pgid for pgid in groups if pid_alive(pgid)]
        if groups:
            time.sleep(0.05)
    for workdir in sorted(root.iterdir()):
        if (workdir / "debug" / "viewer.json").is_file():
            subprocess.run(  # noqa: S603 - 固定 argv
                [
                    sys.executable,
                    "-m",
                    "babeldoc_tools",
                    "debug",
                    "--workdir",
                    str(workdir),
                    "--stop",
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
            )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """4 个最小 workdir 的根目录 + 默认 profile（``stub``）。"""
    base = tmp_path / "root"
    for did in DOCS:
        _make_workdir(base, did)
    _write_profiles(base, {"stub": {"translator": STUB_COMMAND}})
    return base


@pytest.fixture
def stubs(tmp_path: Path) -> dict[str, Path]:
    """stub translator 脚本（慢 / 失败）。"""
    scripts = {
        "slow": tmp_path / "slow-translator.sh",
        "failing": tmp_path / "failing-translator.sh",
    }
    scripts["slow"].write_text(SLOW_TRANSLATOR, encoding="utf-8")
    scripts["failing"].write_text(FAILING_TRANSLATOR, encoding="utf-8")
    for path in scripts.values():
        path.chmod(0o755)
    return scripts


@pytest.fixture
def client(root: Path):
    """真路由 + 真调度（一个 app 一个事件循环）。

    收尾在**退出 TestClient 之前**：事件循环关闭会 ``shutdown_default_executor()``
    等执行器线程结束，而测试客户端退出时若还有 job 子进程在跑，就会等它们自然退出
    （stub 睡 300s ⇒ 测试看起来挂死）。所以先杀残留进程组 / 停查看器，再关循环。
    """
    with TestClient(create_app(DocumentStore.for_root(root))) as test_client:
        yield test_client
        _shutdown(root)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def post_job(
    client,
    did: str,
    *,
    action: str = "run",
    from_stage: str | None = None,
    profile: str = "stub",
    pages: str | None = None,
    dual: bool | None = None,
) -> str:
    """POST 一个 job（断言 202），返回 job_id。"""
    body: dict[str, object] = {"action": action, "profile": profile}
    if from_stage is not None:
        body["from"] = from_stage
    if pages is not None:
        body["pages"] = pages
    if dual is not None:
        body["dual"] = dual
    response = client.post(f"{API}/documents/{did}/jobs", json=body)
    assert response.status_code == 202, response.text
    return response.json()["job_id"]


def get_job(client, job_id: str) -> dict:
    response = client.get(f"{API}/jobs/{job_id}")
    assert response.status_code == 200, response.text
    return response.json()


#: 轮询步长（所有等待都用"固定步数"封顶，不裸 while）。
POLL_SECONDS = 0.5


def job_diagnostics(record: dict, root: Path | None = None) -> str:
    """等待超时时打印的诊断：job 全字段 + （给了 root 就带上）``jobs.jsonl`` 末尾几行。

    子进程起不来（例如 ``python -m babeldoc_tools`` import 失败）/监控没收尾这类问题，
    只看"状态不对"是查不出来的；把 pid/pgid/boot_id、error_code、信封和事件尾部摆出来。
    """
    lines = [f"job 快照：{json.dumps(record, ensure_ascii=False)[:2000]}"]
    if root is not None:
        events = root / STATE_DIR / "jobs.jsonl"
        if events.is_file():
            tail = events.read_text(encoding="utf-8").splitlines()[-6:]
            lines.append("jobs.jsonl 末尾：\n" + "\n".join(tail))
            lines.append(
                f"workdir 内容：{sorted(path.name for path in root.iterdir())}"
            )
    return "\n".join(lines)


def wait_status(
    client,
    job_id: str,
    statuses: set[str],
    timeout: float = 60.0,
    root: Path | None = None,
) -> dict:
    """轮询 ``GET /jobs/{jid}`` 直到状态落进 ``statuses``；超时 fail 并 dump 诊断。

    ``timeout`` 换算成固定步数（:data:`POLL_SECONDS` 一步）：**不会**无限等 —— 一个永远
    到不了期望状态的 job 必须以带证据的断言失败收场，而不是让测试挂死。
    """
    steps = max(1, int(timeout / POLL_SECONDS))
    record = get_job(client, job_id)
    for _ in range(steps):
        if record["status"] in statuses:
            return record
        time.sleep(POLL_SECONDS)
        record = get_job(client, job_id)
    raise AssertionError(
        f"job 停在 {record['status']}（期望 {sorted(statuses)}，等了 "
        f"{timeout:.0f}s）：\n{job_diagnostics(record, root)}"
    )


def wait_pidfile(path: Path, timeout: float = 30.0) -> int:
    """等 stub translator 把孙进程 pid 写出来（同样固定步数封顶）。"""
    for _ in range(max(1, int(timeout / POLL_SECONDS))):
        with contextlib.suppress(OSError, ValueError):
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return int(text)
        time.sleep(POLL_SECONDS)
    raise AssertionError(f"孙进程 pid 文件没有出现：{path}")


def slow_profile(root: Path, stubs: dict[str, Path], pidfile: Path) -> None:
    """把 ``slow`` profile 指向"会拉孙进程并长睡"的 stub。"""
    command = f"{shlex.quote(str(stubs['slow']))} {shlex.quote(str(pidfile))}"
    _write_profiles(
        root, {"slow": {"translator": command}, "stub": {"translator": STUB_COMMAND}}
    )


def start_slow_job(client, root: Path, stubs, did: str = "alpha") -> tuple[str, Path]:
    """起一个"运行中"的 job（translate 阶段睡着），返回 ``(job_id, 孙进程 pid 文件)``。"""
    pidfile = root / did / "grandchild.pid"
    slow_profile(root, stubs, pidfile)
    job_id = post_job(client, did, from_stage="translate", profile="slow")
    wait_status(client, job_id, {"running"}, root=root)
    return job_id, pidfile


def runner_for(root: Path) -> JobRunner:
    """不走 HTTP 的 JobRunner（单测取消/恢复的边角状态用）。"""
    return JobRunner(DocumentStore.for_root(root))


# --------------------------------------------------------------------------- #
# 提交：202 契约形状
# --------------------------------------------------------------------------- #
def test_post_job_returns_202_contract_shape(client):
    response = client.post(
        f"{API}/documents/alpha/jobs",
        json={"action": "run", "from": "report", "profile": "stub"},
    )
    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"job_id", "status", "action"}
    assert body["status"] == "queued"
    assert body["action"] == "run"
    assert JOB_ID_RE.match(body["job_id"])
    assert response.headers["location"] == f"{API}/jobs/{body['job_id']}"
    wait_status(client, body["job_id"], {"succeeded"})


def test_post_job_unknown_document_is_404(client):
    response = client.post(
        f"{API}/documents/nope/jobs", json={"action": "run", "profile": "stub"}
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "document_not_found"


# --------------------------------------------------------------------------- #
# 成功路径：全状态流转 + 持久化
# --------------------------------------------------------------------------- #
def test_job_runs_to_succeeded_and_persists_everything(client, root):
    job_id = post_job(client, "alpha", from_stage="report")
    record = wait_status(client, job_id, {"succeeded"})

    assert record["exit_code"] == 0
    assert record["error_code"] is None and record["error_message"] is None
    assert record["created_at"] <= record["started_at"] <= record["finished_at"]
    assert record["finished_at"].endswith("Z") and "T" in record["finished_at"]
    # 终态不再持有进程身份（pid/pgid 只在运行中）
    assert record["pid"] is None and record["pgid"] is None
    assert record["from_stage"] == "report" and record["profile"] == "stub"

    # 信封原文：一行 JSON，ok=true
    envelope = json.loads(record["envelope"])
    assert envelope["ok"] is True and envelope["data"]["from"] == "report"

    # run_id 抓取：子进程 debug recorder 新建的 run 归档
    runs = list_run_ids(root / "alpha")
    assert runs, "子进程没有建 debug/runs 归档（--debug 没传？）"
    assert record["run_id"] == runs[0]

    # 快照文件 + append-only 生命周期事件
    assert _snapshot(root, job_id)["status"] == "succeeded"
    events = [event for event in _job_events(root) if event["job_id"] == job_id]
    assert [event["event"] for event in events] == [
        "job_queued",
        "job_started",
        "job_finished",
    ]
    assert events[0]["at"] <= events[1]["at"] <= events[2]["at"]


def test_envelope_is_truncated_when_child_prints_a_huge_one(client, monkeypatch):
    """子进程 stdout 最后一行是超大信封 → 只存前 4KB（api.md §3.4）。"""
    script = (
        "import json; print(json.dumps({'ok': True, 'data': {'blob': 'x' * 9000}}))"
    )
    monkeypatch.setattr(
        runner_module, "build_job_argv", lambda **_: [sys.executable, "-c", script]
    )
    job_id = post_job(client, "alpha", from_stage="report")
    record = wait_status(client, job_id, {"succeeded"})

    assert record["exit_code"] == 0
    assert len(record["envelope"]) == MAX_ENVELOPE_BYTES
    # 不是 bdt run 起的子进程 → 没有新 run 归档，run_id 如实为 null
    assert record["run_id"] is None


def test_stdout_without_envelope_is_reported_as_unparsed(client, monkeypatch):
    script = "print('log line'); print('not a json envelope')"
    monkeypatch.setattr(
        runner_module, "build_job_argv", lambda **_: [sys.executable, "-c", script]
    )
    job_id = post_job(client, "alpha", from_stage="report")
    record = wait_status(client, job_id, {"failed"})

    assert record["error_code"] == "envelope_unparsed"
    assert record["envelope"] is None
    assert "信封" in record["error_message"]


# --------------------------------------------------------------------------- #
# 同文档串行 / 跨文档并发
# --------------------------------------------------------------------------- #
def test_second_job_for_same_document_is_409_document_busy(client, root, stubs):
    first, _ = start_slow_job(client, root, stubs)
    response = client.post(
        f"{API}/documents/alpha/jobs",
        json={"action": "run", "from": "translate", "profile": "slow"},
    )
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "document_busy"
    assert error["detail"]["job_id"] == first
    assert error["detail"]["status"] in {"running", "queued"}
    # 文档锁不跨文档：别的文档照常能发
    other = post_job(client, "beta", from_stage="report")
    wait_status(client, other, {"succeeded"})
    # 取消第一个后槽位立刻可用
    assert client.post(f"{API}/jobs/{first}/cancel").status_code == 202
    second = post_job(client, "alpha", from_stage="report")
    wait_status(client, second, {"succeeded"})


def test_global_limit_two_running_and_fifo_promotion(client, root, stubs):
    """4 个文档各 1 个 job：2 个 running，2 个 queued；取消一个 → 先排队的先上位。"""
    jobs = {}
    for did in DOCS:
        pidfile = root / did / "grandchild.pid"
        slow_profile(root, stubs, pidfile)
        jobs[did] = post_job(client, did, from_stage="translate", profile="slow")

    wait_status(client, jobs["alpha"], {"running"})
    wait_status(client, jobs["beta"], {"running"})
    for did in ("gamma", "delta"):
        record = get_job(client, jobs[did])
        assert record["status"] == "queued"
        assert record["started_at"] is None and record["pid"] is None

    # 释放一个槽位：排队最久的 gamma 上位，delta 仍在排队（FIFO，不是随机/后进先出）
    assert client.post(f"{API}/jobs/{jobs['alpha']}/cancel").status_code == 202
    wait_status(client, jobs["gamma"], {"running"})
    assert get_job(client, jobs["delta"])["status"] == "queued"

    # 收尾：把还在跑的 job 都取消（fixture 还会兜底杀进程组）
    for did in DOCS:
        client.post(f"{API}/jobs/{jobs[did]}/cancel")


# --------------------------------------------------------------------------- #
# 取消：进程组（验收核心）
# --------------------------------------------------------------------------- #
def test_cancel_running_kills_whole_process_group(client, root, stubs):
    """取消 running：job 的进程组（含 stub translator 的孙进程）必须全死，锁持到退出。"""
    job_id, pidfile = start_slow_job(client, root, stubs)
    record = get_job(client, job_id)
    grandchild = wait_pidfile(pidfile)

    # 孙进程确实在那个 job 的进程组里（否则"杀组"证明不了什么）
    assert record["pgid"] == record["pid"]
    assert process_group_id(grandchild) == record["pgid"]

    response = client.post(f"{API}/jobs/{job_id}/cancel")
    assert response.status_code == 202
    final = response.json()
    assert final["status"] == "canceled"
    assert final["cancel_requested_at"] is not None
    assert final["finished_at"] is not None
    assert final["pid"] is None and final["pgid"] is None

    # 整棵树都没了：孙进程不在，进程组也不存在
    assert not pid_alive(grandchild)
    assert process_group_id(grandchild) is None
    assert not pid_alive(record["pid"])

    events = [
        event["event"] for event in _job_events(root) if event["job_id"] == job_id
    ]
    assert events == ["job_queued", "job_started", "job_canceled"]

    # 文档槽在进程真的退出后才释放：同文档再发一个立刻能跑
    again = post_job(client, "alpha", from_stage="report")
    wait_status(client, again, {"succeeded"})


def test_cancel_is_idempotent_on_terminal_job(client):
    job_id = post_job(client, "alpha", from_stage="report")
    finished = wait_status(client, job_id, {"succeeded"})

    response = client.post(f"{API}/jobs/{job_id}/cancel")
    assert response.status_code == 200  # 已终态 → 幂等 200，不是错误
    assert response.json()["status"] == "succeeded"
    assert response.json()["finished_at"] == finished["finished_at"]  # 没被改写


def test_cancel_queued_job_is_canceled_without_a_process(client, root, stubs):
    """排队 job 直接 canceled（不略过任何子进程，也不占运行槽）。"""
    jobs = {}
    for did in DOCS:
        slow_profile(root, stubs, root / did / "grandchild.pid")
        jobs[did] = post_job(client, did, from_stage="translate", profile="slow")
    wait_status(client, jobs["alpha"], {"running"})
    wait_status(client, jobs["beta"], {"running"})
    assert get_job(client, jobs["gamma"])["status"] == "queued"
    assert get_job(client, jobs["delta"])["status"] == "queued"

    response = client.post(f"{API}/jobs/{jobs['gamma']}/cancel")
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "canceled"
    assert body["pid"] is None and body["exit_code"] is None
    assert body["error_code"] == "canceled"
    assert body["started_at"] is None  # 从没被启动过
    # 取消排队 job 不释放运行槽：alpha/beta 还在跑，delta 继续排队
    assert get_job(client, jobs["alpha"])["status"] == "running"
    assert get_job(client, jobs["delta"])["status"] == "queued"

    # 真释放运行槽：取消 alpha → 仍在排队的 delta 上位（FIFO）
    assert client.post(f"{API}/jobs/{jobs['alpha']}/cancel").status_code == 202
    wait_status(client, jobs["delta"], {"running"})

    # 收尾（不等待慢 translator 自己结束）
    for did in ("beta", "delta"):
        client.post(f"{API}/jobs/{jobs[did]}/cancel")
    assert get_job(client, jobs["gamma"])["status"] == "canceled"


def test_cancel_unknown_job_is_404(client):
    response = client.post(f"{API}/jobs/j_00000000000000000000000000/cancel")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "job_not_found"


def test_cancel_without_handle_never_signals_an_orphan(root):
    """拿不到进程句柄时**绝不** killpg（不凭孤立 pid 发信号）。

    两种情形都不发信号：监控任务刚好收尾（以它落的真实终态为准）与别的进程写的
    running 快照（如实置 ``interrupted``/``process_not_owned``）。
    """
    runner = runner_for(root)
    mine = runner.registry.create(
        did="alpha", action="run", from_stage="report", profile="stub"
    )
    runner.registry.mark_started(
        mine, pid=os.getpid(), pgid=os.getpgid(0), spawn_marker="beef"
    )
    # 同 boot_id 且没有句柄 ⇒ 只可能是监控刚收尾那一瞬：不改状态
    assert asyncio.run(runner.cancel(mine.job_id)).status == "running"

    alien = runner.registry.create(
        did="beta", action="run", from_stage="report", profile="stub"
    )
    runner.registry.mark_started(
        alien, pid=os.getpid(), pgid=os.getpgid(0), spawn_marker="cafe"
    )
    alien.boot_id = "dead-serve-process"  # 模拟别的进程（重启前）写的 running 快照
    runner.registry.save(alien)
    result = asyncio.run(runner.cancel(alien.job_id))
    assert result.status == "interrupted"
    assert result.interrupted_reason == "process_not_owned"
    # pid 指向的是本测试进程（还活着）：没有被信号打死
    assert pid_alive(os.getpid()) is True
    assert process_group_id(os.getpid()) == os.getpgid(0)


# --------------------------------------------------------------------------- #
# 超时 / 失败
# --------------------------------------------------------------------------- #
def test_timeout_kills_process_group_and_reports_timed_out(
    client, root, stubs, monkeypatch
):
    """job 级超时（服务端护栏）走取消路径：杀进程组 + ``timed_out``，不假装成功。"""
    # Allow cold Python/provider imports before the stub can create its child.
    # The stub sleeps for 300 seconds, so this still exercises the timeout kill.
    monkeypatch.setattr(runner_module, "JOB_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(runner_module, "CHECK_TIMEOUT_SECONDS", 5.0)
    job_id, pidfile = start_slow_job(client, root, stubs)
    grandchild = wait_pidfile(pidfile)

    record = wait_status(client, job_id, {"failed"})
    assert record["error_code"] == "timed_out"
    assert "超" in record["error_message"]
    assert not pid_alive(grandchild)
    assert process_group_id(grandchild) is None
    # 超时也是失败终止：文档槽释放，同文档能再发
    again = post_job(client, "alpha", from_stage="report")
    wait_status(client, again, {"succeeded"})


def test_failed_job_passes_envelope_error_code(client, root, stubs):
    _write_profiles(
        root,
        {
            "bad": {"translator": str(stubs["failing"])},
            "stub": {"translator": STUB_COMMAND},
        },
    )
    job_id = post_job(client, "alpha", from_stage="translate", profile="bad")
    record = wait_status(client, job_id, {"failed"})

    assert record["exit_code"] == 1  # bdt run 以失败信封收场
    assert record["error_code"] == "translator_failed"
    envelope = json.loads(record["envelope"])
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "translator_failed"
    # 失败也留 run 归档（前端能看现场）
    assert record["run_id"] in list_run_ids(root / "alpha")


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
def test_get_unknown_job_is_404(client):
    response = client.get(f"{API}/jobs/j_00000000000000000000000000")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "job_not_found"


def test_list_jobs_newest_first_and_status_filter(client):
    job_ids = []
    for _ in range(3):
        job_id = post_job(client, "alpha", from_stage="report")
        wait_status(client, job_id, {"succeeded"})
        job_ids.append(job_id)

    listed = client.get(f"{API}/documents/alpha/jobs").json()
    assert [record["job_id"] for record in listed] == list(reversed(job_ids))
    assert all(record["did"] == "alpha" for record in listed)

    assert len(client.get(f"{API}/documents/alpha/jobs?status=succeeded").json()) == 3
    assert client.get(f"{API}/documents/alpha/jobs?status=failed").json() == []
    assert client.get(f"{API}/documents/alpha/jobs?status=bogus").status_code == 422
    # 别的文档看不到这些 job
    assert client.get(f"{API}/documents/beta/jobs").json() == []


def test_list_jobs_unknown_document_is_404(client):
    assert client.get(f"{API}/documents/nope/jobs").status_code == 404


# --------------------------------------------------------------------------- #
# 请求校验：诚实拒绝
# --------------------------------------------------------------------------- #
def test_forbidden_fields_are_422_and_not_echoed(client):
    """客户端不得自带命令/密钥字段（api.md §3.4 / EXECUTION.md 第 8 条）。"""
    for field in FORBIDDEN_JOB_FIELDS:
        response = client.post(
            f"{API}/documents/alpha/jobs",
            json={"action": "run", "profile": "stub", field: "rm -rf / --api-key sk-x"},
        )
        assert response.status_code == 422, field
        error = response.json()["error"]
        assert error["code"] == "forbidden_field", field
        assert error["detail"]["field"] == field
        assert "rm -rf" not in response.text and "sk-x" not in response.text
    # 一个 job 都没建
    assert client.get(f"{API}/documents/alpha/jobs").json() == []


def test_forbidden_field_wins_over_missing_profile(client):
    """带了命令字段就先报 ``forbidden_field``，不被“缺 profile”的校验盖掉（W08）。"""
    response = client.post(
        f"{API}/documents/alpha/jobs",
        json={"action": "run", "translator": "rm -rf / --api-key sk-x"},
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "forbidden_field"
    assert error["detail"]["field"] == "translator"
    assert "rm -rf" not in response.text and "sk-x" not in response.text
    assert client.get(f"{API}/documents/alpha/jobs").json() == []


def test_unimplemented_actions_are_422_with_the_phase(client):
    """``retranslate`` 仍未实现 → 422 ``action_not_available`` + 归属任务。

    ``compile`` 自 W09 起已实现（它的行为在 ``tests/test_serve_compile.py`` 里测），
    不再走这条分支。
    """
    response = client.post(
        f"{API}/documents/alpha/jobs", json={"action": "retranslate", "profile": "stub"}
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "action_not_available"
    assert error["detail"]["action"] == "retranslate"
    assert error["detail"]["phase"] == "W11"
    assert client.get(f"{API}/documents/alpha/jobs").json() == []


def test_profile_is_required_and_shape_checked(client):
    missing = client.post(f"{API}/documents/alpha/jobs", json={"action": "run"})
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "validation_error"

    bad = client.post(
        f"{API}/documents/alpha/jobs", json={"action": "run", "profile": "Bad Profile!"}
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "validation_error"


def test_unknown_profile_lists_ids_but_never_commands(client, root):
    _write_profiles(
        root,
        {
            "known": {"translator": "secret-cmd --api-key sk-xxx"},
            "stub": {"translator": STUB_COMMAND},
        },
    )
    response = client.post(
        f"{API}/documents/alpha/jobs", json={"action": "run", "profile": "nope"}
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "unknown_profile"
    # 内置的四个 harness 永远可选；文件里的 id 也在，但只给 id、不给命令。
    assert {"known", "stub"} <= set(error["detail"]["available"])
    assert error["detail"]["available"] == sorted(error["detail"]["available"])
    assert "secret-cmd" not in response.text and "sk-xxx" not in response.text


def test_from_must_be_a_known_stage_and_run_only(client):
    bad_stage = client.post(
        f"{API}/documents/alpha/jobs",
        json={"action": "run", "from": "nope", "profile": "stub"},
    )
    assert bad_stage.status_code == 422
    assert bad_stage.json()["error"]["code"] == "validation_error"

    check_with_from = client.post(
        f"{API}/documents/alpha/jobs",
        json={"action": "check", "from": "build", "profile": "stub"},
    )
    assert check_with_from.status_code == 422
    error = check_with_from.json()["error"]
    assert error["code"] == "forbidden_field" and error["detail"]["field"] == "from"

    compile_with_from = client.post(
        f"{API}/documents/alpha/jobs",
        json={"action": "compile", "from": "build"},
    )
    assert compile_with_from.status_code == 422
    assert compile_with_from.json()["error"]["detail"]["field"] == "from"

    scope_on_run = client.post(
        f"{API}/documents/alpha/jobs",
        json={"action": "run", "scope": "pages", "profile": "stub"},
    )
    assert scope_on_run.status_code == 422
    assert scope_on_run.json()["error"]["detail"]["field"] == "scope"

    pages_on_check = client.post(
        f"{API}/documents/alpha/jobs",
        json={"action": "check", "pages": "1-3", "profile": "stub"},
    )
    assert pages_on_check.status_code == 422
    assert pages_on_check.json()["error"]["detail"]["field"] == "pages"

    malformed_pages = client.post(
        f"{API}/documents/alpha/jobs",
        json={"action": "run", "pages": "1-3;rm -rf /", "profile": "stub"},
    )
    assert malformed_pages.status_code == 422
    assert malformed_pages.json()["error"]["code"] == "validation_error"


# --------------------------------------------------------------------------- #
# 重启恢复（JobRegistry.load：boot_id 方案）
# --------------------------------------------------------------------------- #
def test_recovery_marks_queued_as_interrupted_without_rerun(root):
    """重启后 queued 不自动重跑（收费调用要人显式重试）。"""
    before = JobRegistry(root)
    record = before.create(
        did="alpha", action="run", from_stage="report", profile="stub"
    )
    before.save(record)

    restarted = JobRegistry(root)  # 新的 boot_id = 新进程
    recovered = restarted.load()

    assert [item.job_id for item in recovered] == [record.job_id]
    kept = restarted.get(record.job_id)
    assert kept.status == "interrupted"
    assert kept.interrupted_reason == "server_restart"
    assert kept.pid is None and kept.started_at is None
    # 事件日志里没有 job_started：确实没有自动重跑
    events = [event["event"] for event in _job_events(root)]
    assert events == ["job_queued", "job_interrupted"]


def test_recovery_marks_alien_running_as_interrupted_without_signalling(root):
    """活 pid 但不是本进程起的（boot_id 不符）→ 只改状态，绝不发信号。"""
    alien = JobRegistry(root, boot_id="dead-serve-process-1")
    record = alien.create(
        did="alpha", action="run", from_stage="translate", profile="stub"
    )
    alien.mark_started(
        record, pid=os.getpid(), pgid=os.getpgid(0), spawn_marker="deadbeef"
    )

    restarted = JobRegistry(root)
    restarted.load()

    kept = restarted.get(record.job_id)
    assert kept.status == "interrupted"
    assert kept.interrupted_reason == "server_restart"
    # pid 指向的是本测试进程（还活着）：没有被信号打死
    assert pid_alive(os.getpid()) is True
    assert process_group_id(os.getpid()) == os.getpgid(0)


def test_recovery_marks_dead_pid_as_interrupted(root):
    registry = JobRegistry(root)
    record = registry.create(
        did="alpha", action="run", from_stage="translate", profile="stub"
    )
    registry.mark_started(record, pid=999_999_998, pgid=999_999_998, spawn_marker="x")

    restarted = JobRegistry(root)
    restarted.load()
    assert restarted.get(record.job_id).status == "interrupted"


def test_recovery_keeps_running_when_boot_id_and_pid_match(root):
    """同 boot_id + pid 活 + 进程组对得上 → 真的还在跑，保留 running。"""
    registry = JobRegistry(root)
    record = registry.create(
        did="alpha", action="run", from_stage="translate", profile="stub"
    )
    registry.mark_started(
        record, pid=os.getpid(), pgid=os.getpgid(0), spawn_marker="cafebabe"
    )

    same_process = JobRegistry(root, boot_id=registry.boot_id)
    assert same_process.load() == []
    kept = same_process.get(record.job_id)
    assert kept.status == "running"
    assert kept.pid == os.getpid() and kept.spawn_marker == "cafebabe"


def test_recovery_ignores_corrupt_snapshots(root):
    """坏快照跳过：一个坏文件不该让服务起不来。"""
    (root / STATE_DIR / "jobs").mkdir(parents=True, exist_ok=True)
    (root / STATE_DIR / "jobs" / "j_broken.json").write_text(
        "{ not json", encoding="utf-8"
    )
    registry = JobRegistry(root)
    assert registry.load() == []
    assert registry.get("j_broken") is None


# --------------------------------------------------------------------------- #
# profile 解析（文件 + env 覆盖，命令不外泄）
# --------------------------------------------------------------------------- #
def test_profile_resolution_reads_file_and_env_override(root, monkeypatch):
    _write_profiles(
        root,
        {
            "deepseek-flash": {
                "translator": "file-translator",
                "reviewer": "file-reviewer",
            }
        },
    )
    profile = resolve_profile(root, "deepseek-flash")
    assert profile == Profile(
        id="deepseek-flash",
        translator="file-translator",
        reviewer="file-reviewer",
    )
    assert resolve_profile(root, "nope") is None

    monkeypatch.setenv("BDT_PROFILE_DEEPSEEK_FLASH_TRANSLATOR", "env-translator")
    overridden = resolve_profile(root, "deepseek-flash")
    assert overridden.translator == "env-translator"
    assert overridden.reviewer == "file-reviewer"  # 没覆盖的字段仍取文件值

    # 只在 env 里存在的 id 也算已知（测试/临时覆盖用）
    monkeypatch.setenv("BDT_PROFILE_TEMP_ONLY_REVIEWER", "env-reviewer")
    assert resolve_profile(root, "temp-only").reviewer == "env-reviewer"
    assert "temp-only" in list_profile_ids(root)


def test_profile_missing_or_corrupt_file_means_no_profiles(tmp_path):
    from babeldoc_tools.harnesses import BUILTINS

    empty = tmp_path / "empty"
    (empty / STATE_DIR).mkdir(parents=True)
    # 没有文件也没有 env 覆盖时，只剩内置 harness；文件里的 id 一个都不认。
    assert list_profile_ids(empty) == sorted(BUILTINS)
    assert resolve_profile(empty, "stub") is None

    (empty / STATE_DIR / "profiles.json").write_text("{oops", encoding="utf-8")
    assert resolve_profile(empty, "stub") is None


# --------------------------------------------------------------------------- #
# 纯函数：argv 构造 / 信封解析 / 终态判定 / job id
# --------------------------------------------------------------------------- #
def test_build_job_argv_is_built_server_side_only(tmp_path):
    argv = build_job_argv(
        workdir=tmp_path / "wd",
        action="run",
        from_stage="translate",
        pages="1-3,5",
        dual=True,
        profile=Profile(id="p", translator="tr-cmd --flag", reviewer="rv-cmd"),
    )
    assert argv[:5] == [sys.executable, "-m", "babeldoc_tools", "run", "--workdir"]
    assert argv[5] == str(tmp_path / "wd")
    # 子进程要建 debug 归档（前端事件/几何都读它），但不弹浏览器
    assert "--debug" in argv and "--debug-no-open" in argv
    assert argv[argv.index("--from") + 1] == "translate"
    assert argv[argv.index("--translator") + 1] == "tr-cmd --flag"
    assert argv[argv.index("--reviewer") + 1] == "rv-cmd"
    assert argv[argv.index("--pages") + 1] == "1-3,5"
    assert "--dual" in argv


def test_build_job_argv_check_action_uses_check_stage(tmp_path):
    argv = build_job_argv(
        workdir=tmp_path / "wd",
        action="check",
        from_stage="check",
        pages=None,
        dual=False,
        profile=Profile(id="p"),
    )
    assert argv[argv.index("--from") + 1] == "check"
    # profile 没配命令就不留 --translator/--reviewer（空值会变成 CLI 用法错误）
    assert "--translator" not in argv and "--reviewer" not in argv
    assert "--pages" not in argv and "--dual" not in argv


def test_build_job_argv_from_parse_carries_the_source_pdf(tmp_path):
    """``from=parse``（含缺省）必须带位置参数 ``<workdir>/source.pdf``：parse 的输入。"""
    explicit = build_job_argv(
        workdir=tmp_path / "wd",
        action="run",
        from_stage="parse",
        pages=None,
        dual=False,
        profile=Profile(id="p"),
    )
    assert explicit[-1] == str(tmp_path / "wd" / "source.pdf")
    assert explicit[explicit.index("--from") + 1] == "parse"

    # from 缺省 = CLI 的 parse：同样要带（否则子进程直接 missing_pdf）
    default_from = build_job_argv(
        workdir=tmp_path / "wd",
        action="run",
        from_stage=None,
        pages=None,
        dual=False,
        profile=Profile(id="p"),
    )
    assert default_from[-1] == str(tmp_path / "wd" / "source.pdf")
    assert "--from" not in default_from

    # translate 及之后不需要（CLI 允许省略：parse 产物已在 workdir 里）
    later = build_job_argv(
        workdir=tmp_path / "wd",
        action="run",
        from_stage="translate",
        pages=None,
        dual=False,
        profile=Profile(id="p"),
    )
    assert str(tmp_path / "wd" / "source.pdf") not in later
    # check 固定 --from check，也不带 PDF
    check = build_job_argv(
        workdir=tmp_path / "wd",
        action="check",
        from_stage="check",
        pages=None,
        dual=False,
        profile=Profile(id="p"),
    )
    assert str(tmp_path / "wd" / "source.pdf") not in check


def test_stdout_envelope_takes_the_last_json_line():
    assert stdout_envelope("") is None
    assert stdout_envelope("乱写\n还是乱写") is None
    assert stdout_envelope('{"ok": true}')[1] == {"ok": True}
    text, payload = stdout_envelope('log\n{"ok": false, "error": {"code": "x"}}\n')
    assert payload["error"]["code"] == "x"
    assert text == '{"ok": false, "error": {"code": "x"}}'
    # 末尾的日志行不算信封（只认真实 JSON 行）；取的是最后一个 JSON 行
    assert stdout_envelope('{"ok": true}\ntail log')[1] == {"ok": True}


# --------------------------------------------------------------------------- #
# 信封脱敏（W08：W07 遗留风险 #1 —— 信封会把 profile 命令与 debug token 带出去）
# --------------------------------------------------------------------------- #
#: 带假密钥形状的 profile 命令（落盘/回传的文本里绝不允许出现它）。
COMMAND_WITH_FAKE_KEY = "secret-cmd --api-key sk-xxx"
#: 带 token 的 debug 查看器 URL（json 里通常出现在 data.debug.url）。
VIEWER_URL = "http://127.0.0.1:9999/?token=deadbeef"


def _envelope_with_secrets() -> dict:
    return {
        "ok": True,
        "data": {
            "config": {
                "from": "translate",
                "translator": COMMAND_WITH_FAKE_KEY,
                "reviewer": "rv-cmd --token sk-yyy",
            },
            "debug": {
                "run_id": "20260917T120000Z-abcdef",
                "url": VIEWER_URL,
                "manifest": "/wd/debug/runs/x/manifest.json",
            },
        },
    }


def test_sanitize_envelope_replaces_commands_and_drops_debug_url():
    profile = Profile(
        id="echo-t", translator=COMMAND_WITH_FAKE_KEY, reviewer="rv-cmd --token sk-yyy"
    )
    text = sanitize_envelope(json.dumps(_envelope_with_secrets()), profile)

    assert "sk-xxx" not in text and "sk-yyy" not in text and "deadbeef" not in text
    body = json.loads(text)
    assert body["data"]["config"]["translator"] == "<profile:echo-t>"
    assert body["data"]["config"]["reviewer"] == "<profile:echo-t>"
    assert body["data"]["config"]["from"] == "translate"  # 非命令字段原样保留
    assert "url" not in body["data"]["debug"]  # 带 token 的 URL 移除
    assert body["data"]["debug"]["run_id"] == "20260917T120000Z-abcdef"
    assert body["data"]["debug"]["manifest"].endswith("manifest.json")


def test_sanitize_envelope_scrubs_command_echoed_in_error_message():
    """命令不只出现在 data.config：``<cmd> 退出码 1: ...`` 这种 error.message 也要抹掉。"""
    profile = Profile(id="echo-t", translator=COMMAND_WITH_FAKE_KEY)
    text = json.dumps(
        {
            "ok": False,
            "error": {
                "code": "translator_failed",
                "message": f"{COMMAND_WITH_FAKE_KEY} 退出码 1: boom",
            },
        }
    )
    body = json.loads(sanitize_envelope(text, profile))
    assert body["error"]["message"] == "<profile:echo-t> 退出码 1: boom"


def test_sanitize_envelope_scrubs_json_escaped_command_everywhere():
    """命令含引号时 JSON 里是转义形式：转义形式也要命中（任意字段位置）。"""
    command = 'tr-cmd --prompt "hi"'
    profile = Profile(id="p", translator=command)
    text = json.dumps(
        {"ok": True, "data": {"config": {"translator": command}, "note": command}},
        ensure_ascii=False,
    )
    body = json.loads(sanitize_envelope(text, profile))
    assert body["data"]["config"]["translator"] == "<profile:p>"
    assert body["data"]["note"] == "<profile:p>"


def test_sanitize_envelope_fallback_scrubs_truncated_json():
    """解析失败（信封被截断）也要抹掉命令与 token URL：宁可文本难看，不留密钥。"""
    profile = Profile(id="echo-t", translator=COMMAND_WITH_FAKE_KEY)
    text = (
        '{"ok": false, "error": {"message": "'
        + COMMAND_WITH_FAKE_KEY
        + ' 退出码 1", "debug": {"url": "'
        + VIEWER_URL
        + '"'
    )
    out = sanitize_envelope(text, profile)
    assert "sk-xxx" not in out and "deadbeef" not in out
    assert "<profile:echo-t>" in out and "<redacted-url>" in out


def test_sanitize_envelope_caps_length_and_keeps_alien_json_readable():
    profile = Profile(id="p")
    huge = json.dumps({"ok": True, "data": {"blob": "x" * 9000}})
    assert len(sanitize_envelope(huge, profile)) == MAX_ENVELOPE_BYTES
    # 非 dict 的 JSON 也不出错（保留原文，只是截断）
    assert sanitize_envelope("[]", profile) == "[]"
    assert sanitize_envelope("not json at all", profile) == "not json at all"


def test_job_envelope_is_sanitized_before_persisting(client, root, stubs):
    """真 argv/真子进程：信封里的命令（含参数里的假密钥）与 debug token URL 都不落盘。

    走真路径（不 mock argv）：profile 命令带一个假密钥，stub translator 立刻失败，
    于是 bdt run 以 ``translator_failed`` 收尾 —— 那份信封的 ``data.config.translator``
    与 ``error.message`` 都含整条命令、``data.debug.url`` 含 token，正是要脱敏的三处。
    """
    command = f"{stubs['failing']} --api-key sk-xxx"
    _write_profiles(root, {"leaky": {"translator": command}})
    job_id = post_job(client, "alpha", from_stage="translate", profile="leaky")
    record = wait_status(client, job_id, {"failed"}, root=root)

    assert record["error_code"] == "translator_failed"  # 真跑到了 translator
    dumped = json.dumps(record)
    assert "sk-xxx" not in dumped  # argv 指纹里没有命令原文
    assert "token=" not in dumped  # 查看器 URL 整个被移除
    envelope = json.loads(record["envelope"])
    assert envelope["data"]["config"]["translator"] == "<profile:leaky>"
    assert envelope["data"]["config"]["reviewer"] is None
    assert "url" not in envelope["data"]["debug"]
    assert envelope["data"]["debug"]["run_id"]
    assert "sk-xxx" not in envelope["error"]["message"]  # 命令回显也被抹掉
    assert envelope["error"]["code"] == "translator_failed"
    # 盘上的两处（job 快照 + append-only 事件）同样干净
    snapshot_text = (root / STATE_DIR / "jobs" / f"{job_id}.json").read_text(
        encoding="utf-8"
    )
    events_text = (root / STATE_DIR / "jobs.jsonl").read_text(encoding="utf-8")
    for text in (snapshot_text, events_text):
        assert "sk-xxx" not in text and "token=" not in text
    # 命令原文确实存在过（profiles.json 里），只是没进任何 job 产物
    assert "sk-xxx" in (root / STATE_DIR / "profiles.json").read_text(encoding="utf-8")


def test_classify_exit_matrix():
    assert (
        classify_exit(
            action="run",
            cancel_requested=True,
            timed_out=True,
            exit_code=None,
            envelope=None,
        ).status
        == "failed"
    )  # 超时优先于取消
    assert (
        classify_exit(
            action="check",
            cancel_requested=True,
            timed_out=False,
            exit_code=-15,
            envelope={"ok": False},
        ).status
        == "canceled"
    )
    assert (
        classify_exit(
            action="run",
            cancel_requested=False,
            timed_out=False,
            exit_code=1,
            envelope=None,
        ).error_code
        == "envelope_unparsed"
    )
    assert (
        classify_exit(
            action="run",
            cancel_requested=False,
            timed_out=False,
            exit_code=0,
            envelope={"ok": True},
        ).status
        == "succeeded"
    )
    failed = classify_exit(
        action="run",
        cancel_requested=False,
        timed_out=False,
        exit_code=1,
        envelope={"ok": False, "error": {"code": "check_needs_fix", "message": "x"}},
    )
    assert (failed.status, failed.error_code) == ("failed", "check_needs_fix")
    # 退出码非 0 但信封说 ok（例如 check --strict）：不冒充成功
    assert (
        classify_exit(
            action="run",
            cancel_requested=False,
            timed_out=False,
            exit_code=1,
            envelope={"ok": True},
        ).error_code
        == "exit_1"
    )


def test_job_ids_are_monotonic_and_ulid_shaped():
    ids = [new_job_id() for _ in range(5)]
    assert ids == sorted(ids)
    assert len(set(ids)) == 5
    assert all(JOB_ID_RE.match(job_id) for job_id in ids)
    # 同一毫秒内的发号也保持有序（时间戳位被抬高）
    first = new_job_id(now_ms=1_700_000_000_000, entropy=b"\x00" * 10)
    second = new_job_id(now_ms=1_700_000_000_000, entropy=b"\x00" * 10)
    assert first < second
    assert new_job_id(now_ms=1_700_000_001_000) > second
