"""``bdt serve`` 事件端点：分页游标 + SSE 实时流（api.md §1.3 / §1.4）。

测试自包含（代码造最小 workdir，不读 ``tmp/`` 下真实产物、不起 uvicorn）：手写
``events.jsonl`` 覆盖半行、损坏行、多 run、没有 run 的文档；SSE 只单测
``event_stream`` 生成器与 ``sse_frame`` 帧格式（``TestClient`` 会把响应体整段
收完，异步流会挂死，所以 HTTP 层只测错误路径与 OpenAPI 声明），人工 SSE 验证
见 brief 的 curl -N 步骤。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="serve 需要 web extra: fastapi")

from babeldoc_tools.serve.app import create_app  # noqa: E402
from babeldoc_tools.serve.routers.events import EVENTS_DEFAULT_LIMIT  # noqa: E402
from babeldoc_tools.serve.routers.events import EVENTS_LIMIT_MAX  # noqa: E402
from babeldoc_tools.serve.routers.events import HEARTBEAT_FRAME  # noqa: E402
from babeldoc_tools.serve.routers.events import event_stream  # noqa: E402
from babeldoc_tools.serve.routers.events import parse_last_event_id  # noqa: E402
from babeldoc_tools.serve.routers.events import sse_frame  # noqa: E402
from babeldoc_tools.serve.schemas import API_PREFIX  # noqa: E402
from babeldoc_tools.serve.store import DocumentStore  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# 写端点白名单是唯一来源：W07 只放行 job 的两条 POST，W08 加上传与 profile 写入
# （见那个模块的 ALLOWED_WRITE_ROUTES）。
from test_serve_app import assert_no_unexpected_write_routes  # noqa: E402

DID = "paper"
BARE = "bare"
SPARSE = "sparse"
JUNK = "junk"
RUN_NEW = "20260917T110000Z-0000b2"
RUN_OLD = "20260917T100000Z-0000a1"

DOCUMENTS = f"{API_PREFIX}/documents"
EVENTS = f"{DOCUMENTS}/{DID}/events"
STREAM = f"{EVENTS}/stream"
#: OpenAPI 里路径是模板（不是具体 did）
EVENTS_TEMPLATE = f"{DOCUMENTS}/{{did}}/events"
STREAM_TEMPLATE = f"{EVENTS_TEMPLATE}/stream"


def _event(seq: int, stage: str, kind: str, data: dict | None = None) -> dict:
    """一条真实形状的事件（``{seq, at, stage, kind, data}``，at 是带 +00:00 的 UTC）。"""
    return {
        "seq": seq,
        "at": f"2026-09-17T10:00:0{seq}+00:00",
        "stage": stage,
        "kind": kind,
        "data": {"n": seq} if data is None else data,
    }


#: 最新 run：5 条连续事件，覆盖两个阶段 / 两种 kind（过滤与分页都要能推进游标）。
#:
#: **注意：`paragraph.done` / `segment.done` / `batch.done` 是本用例专用的夹具 kind —— 真实
#: `bdt run` 的 translate 阶段不会产生它们**（W14 实测：整篇翻译是一次 `translator.whole`
#: 子进程调用，translate 阶段真实 kind 只有 stage_started / artifact_bundle / call_started /
#: call_finished / text_version / missing_ids / stage_finished / stage_error）。这里用它们只是
#: 因为“某阶段有多个不同 kind”能同时测过滤与分页游标；不要把它们当“已有段落级事件”的依据。
_NEW_EVENTS = [
    _event(1, "parse", "stage.started"),
    _event(2, "parse", "paragraph.done", {"pages": 18}),
    _event(3, "translate", "segment.done", {"text": "你好，世界。", "n": 1}),
    _event(4, "translate", "segment.done", {"text": "第二段。", "n": 2}),
    _event(5, "translate", "batch.done", {"returncode": 0}),
]

#: 旧 run：只有 2 条（?run_id= 指定老 run 时必须读它，而不是最新那个）。
_OLD_EVENTS = [
    _event(1, "parse", "stage.started"),
    _event(2, "translate", "segment.done", {"text": "旧 run。", "n": 1}),
]


def _write_events(run_dir: Path, events: list[dict], *, extra: str = "") -> Path:
    """写 ``events.jsonl``；``extra`` 追加原始文本（造半行/损坏行用）。"""
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "events.jsonl"
    path.write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)
        + extra,
        encoding="utf-8",
    )
    return path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """一个完整文档（两个 run）+ 没有 run 的文档 + run 还没有 events.jsonl 的文档。"""
    base = tmp_path / "root"
    base.mkdir()
    workdir = base / DID
    (workdir / "agent").mkdir(parents=True)
    _write_events(workdir / "debug" / "runs" / RUN_OLD, _OLD_EVENTS)
    _write_events(workdir / "debug" / "runs" / RUN_NEW, _NEW_EVENTS)

    # 目录名不像 run_id 的目录不是 run（不参与默认选择，也不让它变成"有 run"）
    _write_events(workdir / "debug" / "runs" / "not-a-run", _OLD_EVENTS)

    (base / BARE).mkdir()

    sparse_run = base / SPARSE / "debug" / "runs" / RUN_NEW
    sparse_run.mkdir(parents=True)
    (sparse_run / "manifest.json").write_text("{}\n", encoding="utf-8")

    junk_run = base / JUNK / "debug" / "runs" / "garbage"
    _write_events(junk_run, _OLD_EVENTS)
    return base


@pytest.fixture
def workdir(root: Path) -> Path:
    return root / DID


@pytest.fixture
def client(root: Path):
    with TestClient(create_app(DocumentStore.for_root(root))) as test_client:
        yield test_client


def _page(client: TestClient, **params) -> dict:
    response = client.get(EVENTS, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _seqs(body: dict) -> list[int]:
    return [event["seq"] for event in body["events"]]


# --------------------------------------------------------------------------- #
# 分页
# --------------------------------------------------------------------------- #
def test_events_page_shape_and_default_run(client):
    """默认最新 run；响应就是冻结的四个键（run_id 是 additive 扩展）。"""
    body = _page(client)
    assert set(body) == {"run_id", "events", "next_after_seq", "has_more"}
    assert body["run_id"] == RUN_NEW  # 目录名倒序 = 时间序，取最新
    assert body["events"] == _NEW_EVENTS  # 事件原样透传（data 不裁剪）
    assert body["next_after_seq"] == 5
    assert body["has_more"] is False


def test_events_default_limit_and_cap_are_pinned(client):
    """默认 500 / 上限 2000：超限是 422 validation_error（不静默截断）。"""
    assert (EVENTS_DEFAULT_LIMIT, EVENTS_LIMIT_MAX) == (500, 2000)
    assert client.get(EVENTS, params={"limit": EVENTS_LIMIT_MAX}).status_code == 200
    for bad in (0, -1, EVENTS_LIMIT_MAX + 1):
        response = client.get(EVENTS, params={"limit": bad})
        assert response.status_code == 422, bad
        assert response.json()["error"]["code"] == "validation_error"


def test_events_pagination_has_no_gap_and_no_overlap(client):
    """翻 3 页：seq 连续、无重叠、无丢失，最后一页 has_more=false。"""
    pages = []
    after_seq = 0
    for _ in range(3):
        page = _page(client, after_seq=after_seq, limit=2)
        pages.append(page)
        after_seq = page["next_after_seq"]

    assert [_seqs(page) for page in pages] == [[1, 2], [3, 4], [5]]
    assert [page["next_after_seq"] for page in pages] == [2, 4, 5]
    assert [page["has_more"] for page in pages] == [True, True, False]
    flat = [seq for page in pages for seq in _seqs(page)]
    assert flat == sorted(set(flat))  # 无重复
    assert flat == [event["seq"] for event in _NEW_EVENTS]  # 无丢失
    assert [page["run_id"] for page in pages] == [RUN_NEW] * 3


def test_events_has_more_boundary(client):
    """``has_more`` 只多不少：正好取完为 false，还有剩余为 true。"""
    assert _page(client, limit=5)["has_more"] is False
    tail = _page(client, limit=4)
    assert tail["has_more"] is True
    assert tail["next_after_seq"] == 4  # 游标推进到本页最后一条


def test_events_filter_advances_cursor_even_without_match(client):
    """过滤后本页 0 条也必须推进游标（否则轮询被过滤条件卡死，契约红线）。"""
    empty = _page(client, stage="no-such-stage", limit=2)
    assert empty["events"] == []
    assert empty["next_after_seq"] == 2  # 扫描位置，不是匹配位置
    assert empty["has_more"] is True

    # 继续拿下一页：仍然按扫描位置推进，不会卡在第 2 条
    second = _page(
        client, stage="no-such-stage", limit=2, after_seq=empty["next_after_seq"]
    )
    assert second["events"] == []
    assert second["next_after_seq"] == 4
    third = _page(
        client, stage="no-such-stage", limit=2, after_seq=second["next_after_seq"]
    )
    assert third["next_after_seq"] == 5
    assert third["has_more"] is False


def test_events_filter_by_kind_and_stage(client):
    """两个过滤参数各自生效；命中时游标同样只按扫描位置推进。"""
    segments = _page(client, kind="segment.done")
    assert _seqs(segments) == [3, 4]
    assert segments["next_after_seq"] == 5

    translate = _page(client, stage="translate")
    assert _seqs(translate) == [3, 4, 5]

    both = _page(client, stage="translate", kind="segment.done")
    assert _seqs(both) == [3, 4]

    # 过滤 + 分页：第一页窗口是 [1,2]，没有 segment.done → 空页但游标到 2
    first = _page(client, kind="segment.done", limit=2)
    assert first["events"] == []
    assert first["next_after_seq"] == 2


def test_events_run_id_selects_older_run(client):
    """``?run_id=`` 指定老 run：读它的 events.jsonl，response 里的 run_id 也是它。"""
    body = _page(client, run_id=RUN_OLD)
    assert body["run_id"] == RUN_OLD
    assert body["events"] == _OLD_EVENTS
    assert body["next_after_seq"] == 2


def test_events_after_seq_is_run_local(client):
    """游标只在同一个 run 内有意义：老 run 的 after_seq 按老 run 的 seq 解释。"""
    body = _page(client, run_id=RUN_OLD, after_seq=1)
    assert _seqs(body) == [2]
    assert body["next_after_seq"] == 2


# --------------------------------------------------------------------------- #
# 归档容错（复用 read_events：半行不发布、损坏行跳过）
# --------------------------------------------------------------------------- #
def test_events_half_line_is_not_published(client, workdir):
    """文件尾行没有换行结尾 → 本次不发布；补上换行后才出现。"""
    path = workdir / "debug" / "runs" / RUN_NEW / "events.jsonl"
    half = json.dumps(_event(6, "translate", "batch.done"), ensure_ascii=False)
    path.write_text(path.read_text(encoding="utf-8") + half, encoding="utf-8")

    body = _page(client)
    assert _seqs(body) == [1, 2, 3, 4, 5]
    assert body["next_after_seq"] == 5
    assert body["has_more"] is False

    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    body = _page(client)
    assert _seqs(body) == [1, 2, 3, 4, 5, 6]
    assert body["next_after_seq"] == 6


def test_events_corrupt_line_is_skipped(client, workdir):
    """损坏行跳过，不 500、也不卡住游标（它的 seq 前缀拿不到有效事件）。"""
    run_dir = workdir / "debug" / "runs" / RUN_NEW
    corrupt = '{"seq": 3, "stage": "translate"\n'
    _write_events(
        run_dir,
        _NEW_EVENTS[:2],  # seq 1, 2
        extra=corrupt  # seq 3 是一行坏 JSON
        + "".join(
            json.dumps(event, ensure_ascii=False) + "\n" for event in _NEW_EVENTS[3:]
        ),
    )
    body = _page(client)
    assert _seqs(body) == [1, 2, 4, 5]
    assert body["next_after_seq"] == 5


def test_events_without_integer_seq_is_not_published(client, workdir):
    """没有整数 seq 的事件无法定位游标（每次读都会重来），不发布。"""
    run_dir = workdir / "debug" / "runs" / RUN_NEW
    _write_events(
        run_dir,
        _NEW_EVENTS,
        extra=json.dumps(
            {"at": "2026-09-17T10:00:09+00:00", "stage": "parse", "kind": "torn"},
            ensure_ascii=False,
        )
        + "\n",
    )
    body = _page(client)
    assert _seqs(body) == [1, 2, 3, 4, 5]
    assert body["next_after_seq"] == 5
    # 再翻一页也不会重复吐出那一行（游标语义不依赖它）
    assert _page(client, after_seq=5)["events"] == []


def test_events_run_without_events_file_is_empty_page(client):
    """run 已建但 events.jsonl 还没出现（刚启动）→ 200 + 空页，不是 404。"""
    response = client.get(f"{DOCUMENTS}/{SPARSE}/events")
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "run_id": RUN_NEW,
        "events": [],
        "next_after_seq": 0,
        "has_more": False,
    }


# --------------------------------------------------------------------------- #
# 没有 run / run 不存在
# --------------------------------------------------------------------------- #
def test_events_without_any_run_is_404(client):
    """``debug/runs`` 不存在 → 404 events_unavailable（不拿空数组假成功）。"""
    response = client.get(f"{DOCUMENTS}/{BARE}/events")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "events_unavailable"
    assert BARE in error["message"]


def test_events_ignores_directories_that_are_not_run_ids(client):
    """``debug/runs`` 里的非法目录名不是 run：不选它、也不假装有 run。"""
    assert _page(client)["run_id"] == RUN_NEW  # 有合法 run 时不会被 not-a-run 抢走

    response = client.get(f"{DOCUMENTS}/{JUNK}/events")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "events_unavailable"


def test_events_unknown_run_id_is_404(client):
    """形状合法但不存在 → 404 events_unavailable（与"没有 run"同一个码）。"""
    response = client.get(EVENTS, params={"run_id": "20260917T110000Z-0000ff"})
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "events_unavailable"
    assert error["detail"]["run_id"] == "20260917T110000Z-0000ff"


def test_events_run_id_shape_is_validated(client):
    """run_id 形状不对 → 422 validation_error（契约 §1.2 的参数校验语义）。"""
    for bad in ("nope", "20260917T110000Z", "20260917T110000Z-0000B2", "1; rm -rf /"):
        response = client.get(EVENTS, params={"run_id": bad})
        assert response.status_code == 422, bad
        assert response.json()["error"]["code"] == "validation_error"


def test_events_unknown_document_uses_error_envelope(client):
    response = client.get(f"{DOCUMENTS}/nope/events")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "document_not_found"


# --------------------------------------------------------------------------- #
# SSE：帧格式、续传、心跳
# --------------------------------------------------------------------------- #
def test_sse_frame_has_event_id_and_data_lines():
    """一帧三行 + 空行；id 是 ``<run_id>:<seq>``；data 是事件 JSON 原样。"""
    event = _NEW_EVENTS[2]
    frame = sse_frame(RUN_NEW, event, event["seq"])
    lines = frame.split("\n")
    assert lines[0] == "event: segment.done"
    assert lines[1] == f"id: {RUN_NEW}:3"
    assert lines[2].startswith("data: ")
    assert lines[3:] == ["", ""]
    # data 一行装得下整个事件（换行被 json 转义），且不丢非 ASCII
    assert frame.count("data: ") == 1
    assert json.loads(lines[2][len("data: ") :]) == event
    assert "你好，世界。" in frame


def test_sse_frame_falls_back_to_message_event_name():
    """缺 kind 的事件用 SSE 默认事件名，不吐 ``event: None``。"""
    frame = sse_frame(RUN_NEW, {"seq": 1, "at": "..."}, 1)
    assert frame.splitlines()[0] == "event: message"


def test_last_event_id_parsing():
    assert parse_last_event_id(f"{RUN_NEW}:12") == (RUN_NEW, 12)
    assert parse_last_event_id(f"{RUN_NEW}:0") == (RUN_NEW, 0)
    for bad in (None, "", "12", "garbage:3", f"{RUN_NEW}:", f"{RUN_NEW}:x", "a:b:3"):
        assert parse_last_event_id(bad) is None, bad


def _drain(gen, count: int) -> list[str]:
    """同步取 ``count`` 帧（不依赖 pytest-asyncio）。"""

    async def run() -> list[str]:
        frames = [await anext(gen) for _ in range(count)]
        await gen.aclose()
        return frames

    return asyncio.run(run())


def test_event_stream_emits_backlog_then_heartbeat(workdir: Path):
    """run 已结束：先立刻推全部存量事件，然后按间隔发心跳注释行。"""
    run_dir = workdir / "debug" / "runs" / RUN_NEW
    frames = _drain(
        event_stream(run_dir, RUN_NEW, poll_seconds=0.01, heartbeat_seconds=0.02),
        len(_NEW_EVENTS) + 2,
    )
    backlog = frames[: len(_NEW_EVENTS)]
    for event, frame in zip(_NEW_EVENTS, backlog, strict=True):
        assert frame == sse_frame(RUN_NEW, event, event["seq"])
    assert frames[len(_NEW_EVENTS) :] == [HEARTBEAT_FRAME, HEARTBEAT_FRAME]


def test_event_stream_resumes_after_seq(workdir: Path):
    """``after_seq`` 续传：只推它之后的事件（断线重连不重复吐）。"""
    run_dir = workdir / "debug" / "runs" / RUN_NEW
    frames = _drain(
        event_stream(run_dir, RUN_NEW, 3, poll_seconds=0.01, heartbeat_seconds=5.0),
        2,
    )
    assert [
        json.loads(frame.splitlines()[2][len("data: ") :])["seq"] for frame in frames
    ] == [
        4,
        5,
    ]
    assert all(f"id: {RUN_NEW}:" in frame for frame in frames)


def test_event_stream_picks_up_appended_events(workdir: Path):
    """tail 语义：追加一行事件后（且补上换行）下一轮就读到它。"""
    path = workdir / "debug" / "runs" / RUN_NEW / "events.jsonl"
    path.write_text(
        path.read_text(encoding="utf-8")
        + json.dumps(_event(6, "build", "stage.started"), ensure_ascii=False),
        encoding="utf-8",
    )

    async def run() -> list[str]:
        gen = event_stream(
            path.parent, RUN_NEW, 5, poll_seconds=0.01, heartbeat_seconds=0.01
        )
        first = await anext(gen)  # 半行未发布 → 先给心跳
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        second = await anext(gen)  # 换行补上后同一段文本被发布
        await gen.aclose()
        return [first, second]

    first, second = asyncio.run(run())
    assert first == HEARTBEAT_FRAME
    assert second == sse_frame(RUN_NEW, _event(6, "build", "stage.started"), 6)


def test_event_stream_stops_when_consumer_closes(workdir: Path):
    """消费方关闭生成器（= 客户端断开后 Starlette 取消任务）后不再产出。"""
    run_dir = workdir / "debug" / "runs" / RUN_NEW

    async def run() -> None:
        gen = event_stream(
            run_dir, RUN_NEW, 5, poll_seconds=0.01, heartbeat_seconds=0.01
        )
        assert await anext(gen) == HEARTBEAT_FRAME
        await gen.aclose()
        with pytest.raises(StopAsyncIteration):
            await anext(gen)

    asyncio.run(run())


def test_sse_stream_error_paths_use_json_envelope(client):
    """SSE 端点的错误也走统一 JSON 信封（前端只有一套错误解析）。"""
    response = client.get(f"{DOCUMENTS}/{BARE}/events/stream")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "events_unavailable"

    response = client.get(STREAM, params={"run_id": "20260917T110000Z-0000ff"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "events_unavailable"

    response = client.get(STREAM, params={"run_id": "nope"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


# --------------------------------------------------------------------------- #
# OpenAPI
# --------------------------------------------------------------------------- #
def test_events_openapi_declares_sse_and_get_only(client):
    """SSE 端点声明 text/event-stream + 续传参数；W01 起所有端点仍是只读 GET。"""
    schema = client.get("/openapi.json").json()
    assert set(schema["paths"][EVENTS_TEMPLATE]) == {"get"}
    assert set(schema["paths"][STREAM_TEMPLATE]) == {"get"}

    stream = schema["paths"][STREAM_TEMPLATE]["get"]
    assert "text/event-stream" in stream["responses"]["200"]["content"]
    params = {item["name"]: item["in"] for item in stream["parameters"]}
    assert params == {
        "did": "path",
        "after_seq": "query",
        "run_id": "query",
        "Last-Event-ID": "header",
    }

    page_params = {
        item["name"]: item["in"]
        for item in schema["paths"][EVENTS_TEMPLATE]["get"]["parameters"]
    }
    assert page_params == {
        "did": "path",
        "after_seq": "query",
        "limit": "query",
        "stage": "query",
        "kind": "query",
        "run_id": "query",
    }
    # W01–W03 的只读边界 + W07 的两条 job POST 白名单（helper 是唯一来源）
    assert_no_unexpected_write_routes(schema)
