"""SSE 的虚拟 kind ``job_update``：job 状态变化通知（W14，api.md §1.4）。

这一层**不落盘**：真相在 ``.bdt-serve/jobs/<jid>.json``（快照）与 ``jobs.jsonl``
（生命周期事件），``job_update`` 只是"刚变了"的提示。测试分四段：

1. 帧形状（``event``/``id``/``data`` 三行，data 只有 5 个字段）；
2. hub 语义：无订阅者即丢弃（不积压）、订阅按 did 过滤、多订阅者各得一份、
   收件箱满了丢最旧；
3. ``JobRegistry`` 钩子：状态变化必通知（create/mark_started/mark_finished）、
   监听器抛异常不影响状态机；
4. 与 SSE 流的合并：job_update 和 run 事件同一条流、生成器收尾退订、job 命名空间
   不被当成续传游标。

job 能不能真跑不归本任务管：最后那条装配用例用 ``--from report``（只读产物，不联网、
不编译、不调模型），workdir 与 ``test_serve_jobs.py`` 同一最小口径。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="serve 需要 web extra: fastapi")

from babeldoc_tools.serve.app import create_app  # noqa: E402
from babeldoc_tools.serve.jobs import EVENT_QUEUED  # noqa: E402
from babeldoc_tools.serve.jobs import JobRecord  # noqa: E402
from babeldoc_tools.serve.jobs import JobRegistry  # noqa: E402
from babeldoc_tools.serve.routers.events import HEARTBEAT_FRAME  # noqa: E402
from babeldoc_tools.serve.routers.events import JOB_UPDATE_BUFFER  # noqa: E402
from babeldoc_tools.serve.routers.events import JOB_UPDATE_KIND  # noqa: E402
from babeldoc_tools.serve.routers.events import JobUpdateHub  # noqa: E402
from babeldoc_tools.serve.routers.events import event_stream  # noqa: E402
from babeldoc_tools.serve.routers.events import job_update_frame  # noqa: E402
from babeldoc_tools.serve.routers.events import parse_last_event_id  # noqa: E402
from babeldoc_tools.serve.schemas import API_PREFIX  # noqa: E402
from babeldoc_tools.serve.store import STATE_DIR  # noqa: E402
from babeldoc_tools.serve.store import DocumentStore  # noqa: E402
from babeldoc_tools.serve.workdir import list_run_ids  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

DID = "paper"
OTHER = "other"
RUN_NEW = "20260917T110000Z-0000b2"
RUN_ID_RE_SHAPED_JOB = "j_01M2RDB312K20Q280DHCTX7N19"


def make_record(
    *,
    job_id: str = RUN_ID_RE_SHAPED_JOB,
    did: str = DID,
    action: str = "run",
    status: str = "running",
    from_stage: str | None = "translate",
    error_code: str | None = None,
) -> JobRecord:
    """一个 job 快照（形状与 ``GET /jobs/{jid}`` 相同；本文件只关心通知用到的字段）。"""
    return JobRecord(
        job_id=job_id,
        did=did,
        action=action,
        status=status,
        created_at="2026-09-17T19:26:20.194Z",
        from_stage=from_stage,
        profile="echo-t",
        error_code=error_code,
    )


def _frame_payload(frame: str) -> dict:
    """帧 → data 行的 JSON（顺手断言三行结构没被人改形状）。"""
    lines = frame.split("\n")
    assert lines[0] == f"event: {JOB_UPDATE_KIND}"
    assert lines[2].startswith("data: ")
    assert lines[3:] == ["", ""]
    return json.loads(lines[2][len("data: ") :])


# --------------------------------------------------------------------------- #
# 1) 帧形状
# --------------------------------------------------------------------------- #
def test_job_update_frame_shape_is_frozen():
    """`event: job_update` + `id: <job_id>:<n>` + data 里恰好 5 个字段。"""
    record = make_record()
    frame = job_update_frame(record, 2)
    assert frame.split("\n")[1] == f"id: {RUN_ID_RE_SHAPED_JOB}:2"

    payload = _frame_payload(frame)
    assert payload["kind"] == JOB_UPDATE_KIND
    assert payload["data"] == {
        "job_id": RUN_ID_RE_SHAPED_JOB,
        "action": "run",
        "status": "running",
        "from_stage": "translate",
        "error_code": None,
    }
    # 命令/信封/pid/路径都不进 SSE（sanitize 的另一半：这里干脆不带）
    assert set(payload) == {"kind", "data"}
    assert set(payload["data"]) == {
        "job_id",
        "action",
        "status",
        "from_stage",
        "error_code",
    }


def test_job_update_frame_carries_error_code_on_failure():
    frame = job_update_frame(
        make_record(status="failed", error_code="translator_failed"), 3
    )
    payload = _frame_payload(frame)
    assert payload["data"]["status"] == "failed"
    assert payload["data"]["error_code"] == "translator_failed"


# --------------------------------------------------------------------------- #
# 2) hub 语义
# --------------------------------------------------------------------------- #
def test_publish_without_subscribers_drops_and_does_not_accumulate():
    """没有订阅者时 publish 什么都不留：后来订阅的人**不会**收到历史（不积压）。"""
    hub = JobUpdateHub()
    record = make_record()
    for _ in range(3):
        hub.publish(record)
    assert hub.subscribers == 0

    subscription = hub.subscribe(DID)
    assert subscription.drain() == []
    # 计次仍在推进（id 是"第 n 次状态变化"，不是"第 n 次投递"）
    hub.publish(record)
    assert subscription.drain()[0].split("\n")[1] == f"id: {record.job_id}:4"


def test_subscription_filters_by_did():
    """job 是全局的、流是每文档的：别的文档的状态变化不进这个收件箱。"""
    hub = JobUpdateHub()
    mine = hub.subscribe(DID)
    theirs = hub.subscribe(OTHER)
    hub.publish(make_record(status="queued"))
    hub.publish(make_record(job_id="j_other", did=OTHER, status="queued"))

    my_frames = mine.drain()
    their_frames = theirs.drain()
    assert len(my_frames) == 1
    assert len(their_frames) == 1
    # 同 did 的那条只进了我的收件箱，反之亦然（各自的 id 指向各自的 job）
    assert my_frames[0].split("\n")[1] == f"id: {RUN_ID_RE_SHAPED_JOB}:1"
    assert their_frames[0].split("\n")[1] == "id: j_other:1"


def test_multiple_subscribers_each_get_the_same_frame():
    hub = JobUpdateHub()
    first = hub.subscribe(DID)
    second = hub.subscribe(DID)
    hub.publish(make_record(status="queued"))
    assert hub.subscribers == 2
    assert first.drain() == second.drain()
    assert len(first.drain()) == 0  # drain 是取走


def test_inbox_is_bounded_and_drops_the_oldest():
    """收件箱满了丢最旧（通知层宁丢不积压）：3 条只留 2 条，最新的那条在。"""
    hub = JobUpdateHub(buffer=2)
    subscription = hub.subscribe(DID)
    for status in ("queued", "running", "succeeded"):
        hub.publish(make_record(status=status))
    frames = subscription.drain()
    assert len(frames) == 2
    assert [
        json.loads(frame.split("\n")[2][len("data: ") :])["data"]["status"]
        for frame in frames
    ] == [
        "running",
        "succeeded",
    ]
    assert JOB_UPDATE_BUFFER == 64  # 生产上限是 64（测试用 2 才看得见淘汰）


def test_unsubscribe_is_idempotent_and_stops_delivery():
    hub = JobUpdateHub()
    subscription = hub.subscribe(DID)
    hub.unsubscribe(subscription)
    hub.unsubscribe(subscription)  # 幂等
    assert hub.subscribers == 0
    hub.publish(make_record())
    assert subscription.drain() == []


# --------------------------------------------------------------------------- #
# 3) JobRegistry 钩子
# --------------------------------------------------------------------------- #
def _registry(tmp_path: Path) -> JobRegistry:
    return JobRegistry(tmp_path)


def test_registry_notifies_on_every_status_change(tmp_path: Path):
    """create/mark_started/mark_finished 各通知一次；mark_cancel_requested 不改状态不通知。"""
    registry = _registry(tmp_path)
    seen: list[tuple[str, str]] = []
    registry.add_listener(lambda record: seen.append((record.job_id, record.status)))

    record = registry.create(
        did=DID, action="run", from_stage="translate", profile="echo-t"
    )
    assert seen == [(record.job_id, "queued")]
    registry.mark_cancel_requested(record)
    assert len(seen) == 1
    registry.mark_started(record, pid=1, pgid=1, spawn_marker="m")
    registry.mark_finished(record, status="succeeded", exit_code=0)
    assert [status for _, status in seen] == ["queued", "running", "succeeded"]


def test_registry_notifies_for_restart_recovery(tmp_path: Path):
    """恢复路径（``_interrupt``）也经 ``_write`` → 也通知（谁订阅谁看得见）。"""
    base = tmp_path
    first = JobRegistry(base)
    record = first.create(
        did=DID, action="run", from_stage="translate", profile="echo-t"
    )
    del record  # 快照已在盘上：模拟"上一次运行留下的 queued"

    seen: list[str] = []
    second = JobRegistry(base)
    second.add_listener(lambda item: seen.append(item.status))
    second.load()
    assert seen == ["interrupted"]


def test_listener_exception_does_not_break_the_state_machine(tmp_path: Path):
    """写坏的监听器不能把 job 流转带沟里：快照与 jobs.jsonl 照常落盘。"""
    registry = _registry(tmp_path)
    seen: list[str] = []

    def boom(_record: JobRecord) -> None:
        raise RuntimeError("listener 坏了")

    registry.add_listener(boom)
    registry.add_listener(lambda record: seen.append(record.status))

    record = registry.create(
        did=DID, action="run", from_stage="translate", profile="echo-t"
    )
    registry.mark_finished(record, status="failed", error_code="boom")
    assert seen == ["queued", "failed"]  # 后面的监听器仍然收到
    snapshot = json.loads(
        (tmp_path / STATE_DIR / "jobs" / f"{record.job_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert snapshot["status"] == "failed"
    events = (
        (tmp_path / STATE_DIR / "jobs.jsonl").read_text(encoding="utf-8").splitlines()
    )
    assert [json.loads(line)["event"] for line in events] == [
        EVENT_QUEUED,
        "job_failed",
    ]


# --------------------------------------------------------------------------- #
# 4) 与 SSE 流合并
# --------------------------------------------------------------------------- #
@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """一个已结束的 run：5 条事件（SSE 的 run 侧不需要真归档语义，只验证顺序）。"""
    directory = tmp_path / "debug" / "runs" / RUN_NEW
    directory.mkdir(parents=True)
    (directory / "events.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    "seq": seq,
                    "at": f"2026-09-17T10:00:0{seq}+00:00",
                    "stage": "translate",
                    "kind": "stage_finished",
                    "data": {"n": seq},
                },
                ensure_ascii=False,
            )
            + "\n"
            for seq in range(1, 6)
        ),
        encoding="utf-8",
    )
    return directory


def _drain(gen, count: int) -> list[str]:
    async def run() -> list[str]:
        frames = [await anext(gen) for _ in range(count)]
        await gen.aclose()
        return frames

    return asyncio.run(run())


def test_stream_interleaves_job_update_after_run_events(run_dir: Path):
    """同一条流：先把存量 run 事件推完，随后把 job_update 接在后面。"""
    hub = JobUpdateHub()
    frames_out: list[str] = []

    async def run() -> None:
        gen = event_stream(
            run_dir,
            RUN_NEW,
            0,
            poll_seconds=0.01,
            heartbeat_seconds=5.0,
            updates=hub,
            did=DID,
        )
        for _ in range(5):
            frames_out.append(await anext(gen))
        hub.publish(make_record(status="succeeded"))
        frames_out.append(await anext(gen))  # 下一轮就吐出来（不必等心跳/轮询周期）
        await gen.aclose()

    asyncio.run(run())
    assert [frame.split("\n")[1] for frame in frames_out[:5]] == [
        f"id: {RUN_NEW}:{seq}" for seq in range(1, 6)
    ]
    update = frames_out[5]
    assert update.split("\n")[0] == f"event: {JOB_UPDATE_KIND}"
    assert update.split("\n")[1] == f"id: {RUN_ID_RE_SHAPED_JOB}:1"


def test_stream_pushes_job_update_within_one_poll_interval(run_dir: Path):
    """job_update 在一个 poll 间隔内出流（生产 0.5s，前端兜底轮询是 5s）。

    先拿一帧心跳 —— 它同时证明订阅已经建立（没有订阅者时 publish 直接丢弃，见上一条）；
    之后的 publish 必须在下一次轮询里被推出去。
    """
    hub = JobUpdateHub()

    async def run() -> tuple[str, str, float]:
        gen = event_stream(
            run_dir,
            RUN_NEW,
            5,
            poll_seconds=0.05,
            heartbeat_seconds=0.01,
            updates=hub,
            did=DID,
        )
        first = await anext(gen)
        assert hub.subscribers == 1
        started = time.monotonic()
        hub.publish(make_record(status="running"))
        second = await anext(gen)
        delay = time.monotonic() - started
        await gen.aclose()
        return first, second, delay

    first, second, delay = asyncio.run(run())
    assert first == HEARTBEAT_FRAME
    assert second.split("\n")[0] == f"event: {JOB_UPDATE_KIND}"
    assert second.split("\n")[1] == f"id: {RUN_ID_RE_SHAPED_JOB}:1"
    assert delay < 0.5, f"推送等了 {delay:.3f}s（应在 poll 间隔内）"


def test_stream_unsubscribes_when_consumer_closes(run_dir: Path):
    """客户端断开（生成器关闭）→ 退订：hub 不会给死掉的连接攒帧。"""
    hub = JobUpdateHub()

    async def run() -> int:
        gen = event_stream(
            run_dir,
            RUN_NEW,
            5,
            poll_seconds=0.01,
            heartbeat_seconds=5.0,
            updates=hub,
            did=DID,
        )
        assert await anext(gen) == HEARTBEAT_FRAME
        assert hub.subscribers == 1
        await gen.aclose()
        return hub.subscribers

    assert asyncio.run(run()) == 0
    hub.publish(make_record())
    assert hub.subscribers == 0  # 没有订阅者 → 丢掉，不积压


def test_stream_without_hub_keeps_w03_framing(run_dir: Path):
    """不给 hub 时行为与 W03 一致：只有 run 事件 + 心跳（job_update 无从出现）。"""
    frames = _drain(
        event_stream(run_dir, RUN_NEW, 5, poll_seconds=0.01, heartbeat_seconds=0.01), 2
    )
    assert frames == [HEARTBEAT_FRAME, HEARTBEAT_FRAME]


def test_job_namespace_is_not_a_resume_cursor():
    """``<job_id>:<n>`` 不是 ``<run_id>:<seq>``：解析器拒绝它，重连不会错位续传。"""
    assert parse_last_event_id(f"{RUN_ID_RE_SHAPED_JOB}:3") is None
    assert parse_last_event_id(f"{RUN_NEW}:3") == (RUN_NEW, 3)
    # 反过来：job_update 的 id 就算被前端交给 Last-Event-ID，也只会退化成"从头重放"
    # （run 里面没有 seq 能对上 job 的计次），不会把某个 run 的游标顶走。


# --------------------------------------------------------------------------- #
# 5) app 装配：真 job 的 queued → running → succeeded 都会过 hub
# --------------------------------------------------------------------------- #
def _make_workdir(root: Path, did: str) -> None:
    """最小 parse 产物（``--from report`` 只读产物 + 写 FINAL_REPORT.md）。"""
    agent = root / did / "agent"
    agent.mkdir(parents=True)
    (agent / "document.md").write_text(
        "<!--P01-001-->\nHello world.\n", encoding="utf-8"
    )
    (agent / "run_state.json").write_text("{}\n", encoding="utf-8")
    (root / ".bdt-serve").mkdir(parents=True, exist_ok=True)
    (root / ".bdt-serve" / "profiles.json").write_text(
        json.dumps({"stub": {"translator": "/usr/bin/true"}}), encoding="utf-8"
    )


def test_app_publishes_every_job_transition_to_the_hub(tmp_path: Path):
    """``create_app`` 把 ``JobRegistry`` 的监听器接上了：真 job 的三次状态变化都收到。"""
    root = tmp_path / "root"
    _make_workdir(root, DID)
    with TestClient(create_app(DocumentStore.for_root(root))) as client:
        hub = client.app.state.job_updates
        subscription = hub.subscribe(DID)
        response = client.post(
            f"{API_PREFIX}/documents/{DID}/jobs",
            json={"action": "run", "from": "report", "profile": "stub"},
        )
        assert response.status_code == 202, response.text
        job_id = response.json()["job_id"]

        deadline = time.monotonic() + 60.0
        record = client.get(f"{API_PREFIX}/jobs/{job_id}").json()
        while (
            record["status"] not in ("succeeded", "failed", "canceled")
            and time.monotonic() < deadline
        ):
            time.sleep(0.2)
            record = client.get(f"{API_PREFIX}/jobs/{job_id}").json()
        assert record["status"] == "succeeded", record

        frames = subscription.drain()
        updates = [
            (
                json.loads(frame.split("\n")[2][len("data: ") :])["data"],
                frame.split("\n")[1],
            )
            for frame in frames
        ]
        assert [data["status"] for data, _ in updates] == [
            "queued",
            "running",
            "succeeded",
        ]
        assert [id_line for _, id_line in updates] == [
            f"id: {job_id}:1",
            f"id: {job_id}:2",
            f"id: {job_id}:3",
        ]
        assert {data["action"] for data, _ in updates} == {"run"}
        assert updates[0][0]["from_stage"] == "report"
        assert [data["error_code"] for data, _ in updates] == [None, None, None]
        # 真 job 会建 run 归档（事件流那边才有 run 侧可读）；顺序无关紧要，只确认没被本改动影响
        assert list_run_ids(root / DID) != []
