"""Serve-side streaming preview: worker pool sizing, page routing, spawn env.

Guards the parallel preview compile path: blocks on the same PDF page render
concurrently but commit their page patch under one page lock (no lost page
patch), the worker count comes from ``BDT_SERVE_PREVIEW_WORKERS`` bounded to
1..MAX_PREVIEW_WORKERS, and the job runner injects the variable into the
translation subprocess environment.
"""

import json
import pickle
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from babeldoc_tools.serve.block_compile import BlockCompiler
from babeldoc_tools.serve.block_compile import NotReplaced
from babeldoc_tools.serve.jobs import JobRecord
from babeldoc_tools.serve.jobs import utc_now
from babeldoc_tools.serve.store import DocumentStore
from babeldoc_tools.serve.stream_preview import MAX_PREVIEW_WORKERS
from babeldoc_tools.serve.stream_preview import PageLocks
from babeldoc_tools.serve.stream_preview import ServeStreamPreview
from babeldoc_tools.serve.stream_preview import _page_of
from babeldoc_tools.serve.stream_preview import preview_workers_from_environ


def test_preview_workers_env_parsing():
    default = {"BDT_SERVE_PREVIEW_WORKERS": None}
    assert preview_workers_from_environ(default) == MAX_PREVIEW_WORKERS
    assert preview_workers_from_environ({"BDT_SERVE_PREVIEW_WORKERS": "3"}) == 3
    # 越界夹到 [1, 上限]，非法值回缺省：环境变量来自启动脚本，坏值不炸翻译。
    # 上限随机器核数走（见 MAX_PREVIEW_WORKERS），所以断言语义而不是写死 8。
    assert preview_workers_from_environ({"BDT_SERVE_PREVIEW_WORKERS": "99"}) == (
        MAX_PREVIEW_WORKERS
    )
    assert preview_workers_from_environ({"BDT_SERVE_PREVIEW_WORKERS": "0"}) == 1
    assert preview_workers_from_environ({"BDT_SERVE_PREVIEW_WORKERS": "abc"}) == (
        MAX_PREVIEW_WORKERS
    )
    assert preview_workers_from_environ({"BDT_SERVE_PREVIEW_WORKERS": " "}) == (
        MAX_PREVIEW_WORKERS
    )
    assert 1 <= MAX_PREVIEW_WORKERS <= 16


def test_pid_page_extraction():
    assert _page_of("P01-003") == 1
    assert _page_of("P12-010") == 12
    assert _page_of("not-a-pid") is None


def test_page_locks_same_page_same_lock():
    """同页恒同一把锁、不同页各自一把：页 patch 状态的读-改-写靠它串行。"""
    locks = PageLocks()
    assert locks.get(1) is locks.get(1)
    assert locks.get(1) is not locks.get(2)
    assert len({id(locks.get(n)) for n in range(1, 9)}) == 8


def test_concurrent_page_compile_keeps_all_patches(tmp_path):
    """端到端守卫：同页多个块并发提交（各自加页锁后做读-改-写），
    local_pages 的 patch 一个不丢。锁路由与 ServeStreamPreview 一致。"""
    from babeldoc_tools.serve.store import DocumentStore

    store = DocumentStore.for_root(tmp_path)
    database = store.database
    with database._lock, database.connection:
        database.connection.execute(
            "INSERT OR IGNORE INTO documents(id) VALUES ('paper')"
        )

    workers = 8
    locks = PageLocks()
    # 读-改-写窗口故意拉宽：所有线程都读到旧 state 后才允许写回，
    # 不加锁时并行提交必然互相覆盖。
    loaded = threading.Barrier(workers)

    def compile_patch(pid):
        with database._lock:
            row = database.connection.execute(
                "SELECT payload FROM local_pages WHERE document_id='paper' AND page=1"
            ).fetchone()
            state = json.loads(row[0]) if row else {"patches": {}}
        loaded.wait(timeout=5)  # 所有线程都持有旧 state 后才写回
        with locks.get(_page_of(pid)):
            with database._lock, database.connection:
                fresh = database.connection.execute(
                    "SELECT payload FROM local_pages"
                    " WHERE document_id='paper' AND page=1"
                ).fetchone()
                merged = json.loads(fresh[0])["patches"] if fresh else {}
                merged.update(state["patches"])
                merged[pid] = {"input": pid}
                database.connection.execute(
                    "INSERT OR REPLACE INTO local_pages VALUES ('paper', 1, ?)",
                    (json.dumps({"patches": merged}),),
                )

    pids = [f"P01-{i:03d}" for i in range(1, 1 + workers)]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(compile_patch, pid) for pid in pids]
        for future in futures:
            future.result()

    with database._lock:
        payload = database.connection.execute(
            "SELECT payload FROM local_pages WHERE document_id='paper' AND page=1"
        ).fetchone()[0]
    assert set(json.loads(payload)["patches"]) == set(pids)
    database.close()


def test_same_page_blocks_render_concurrently(tmp_path, monkeypatch):
    """回归：同页块的贴片渲染必须并行，只有页状态段才串行。

    此前同页块整块串行（每槽位一条单线程队列）：58 页文档里一页 21 个块要排
    21 × 6s 的长队，页合成最慢 89s、均值 41s，而单块 p50 只有 4.6s——是流式
    预览尾部最大的等待来源。现在编译器拿到的是**页锁**而不是队列：这里断言
    同页两个块能同时进入编译（都在门内等着），且拿到的是同一把锁。
    """
    pids = ["P01-001", "P01-002"]
    gates = {pid: threading.Event() for pid in pids}
    started = {pid: threading.Event() for pid in pids}
    preview, fake, _did, _job = _start_preview(
        tmp_path,
        monkeypatch,
        blocks={1: pids},
        translated=pids,
        gates=gates,
        started=started,
    )
    for pid in pids:
        preview.submit(pid, "body", "text")
    # 两块同时在编译中（第二块没有等第一块放行）。
    assert all(started[pid].wait(timeout=10) for pid in pids)
    assert fake.compiled == []
    for gate in gates.values():
        gate.set()
    _drain(preview)
    assert sorted(fake.compiled) == pids
    lock_a, lock_b = (fake.page_locks_seen[pid] for pid in pids)
    assert lock_a is not None and lock_a is lock_b, "同页块必须共用同一把页锁"
    assert fake.page_composes == [(1, True)]
    preview.close(failed=True)


def test_foreign_stamp_republishes_already_published_page(tmp_path, monkeypatch):
    """回归：跨页浮动贴片落到已发布页上时，该页必须重新合成。

    落点页自己的块早就编完并发布了，此前 ``published`` 一刀切跳过，外来贴片
    在预览里就是丢失的（实测 58 页文档 16 处，页资产里找不到贴片文字）。
    """
    preview, fake, _did, _job = _start_preview(
        tmp_path,
        monkeypatch,
        blocks={1: ["P01-001"], 2: ["P02-001"]},
        translated=["P01-001", "P02-001"],
    )
    preview.submit("P02-001", "body", "text")
    _drain(preview)
    assert fake.page_composes == [(2, True)]
    # 第 1 页的块浮到第 2 页：主页发布一次，落点页（已发布）再合成一次。
    fake.stamp_pages["P01-001"] = 2
    preview.submit("P01-001", "body", "text")
    _drain(preview)
    assert sorted(fake.page_composes) == [(1, True), (2, True), (2, True)]
    preview.close(failed=True)


def test_block_compiler_caches_capability_probe(tmp_path, monkeypatch):
    """LaTeX 能力探测每实例只跑一次（kpsewhich 子进程很贵，流式预览每段一编
    不能重复探测）；线程并发首编时也只探一次。"""
    from babeldoc_tools.serve import block_compile
    from babeldoc_tools.serve.store import DocumentStore

    calls = []

    def fake_probe(*_args, **_kwargs):
        calls.append(1)
        return SimpleNamespace(available=True, marker="probed")

    monkeypatch.setattr(
        "babeldoc.format.pdf.document_il.backend.latex_bbox.capability"
        ".probe_latex_capability",
        fake_probe,
    )

    compiler = block_compile.BlockCompiler(DocumentStore.for_root(tmp_path), None)
    results = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for future in [pool.submit(compiler.capability) for _ in range(8)]:
            results.append(future.result())
    assert calls == [1]
    assert all(result is results[0] for result in results)
    assert results[0].marker == "probed"


class _FakePage:
    """测试用最小页面（pickle 需要模块级类）。"""

    def __init__(self, paragraphs, number):
        self.pdf_paragraph = paragraphs
        self.page_number = number


class _FakeDoc:
    def __init__(self, pages):
        self.page = pages


class _FakeParagraph:
    def __init__(self, debug_id, box):
        self.debug_id = debug_id
        self.box = box


def _write_state(workdir, pages_paragraphs):
    """写一个最小 ``agent/state.pkl``：``doc.page[*].pdf_paragraph`` 是给定段落对象。"""
    (workdir / "agent").mkdir(parents=True, exist_ok=True)
    pages = [
        _FakePage([_FakeParagraph(pid, object()) for pid in pids], index)
        for index, pids in enumerate(pages_paragraphs)
    ]
    state = {"doc": _FakeDoc(pages), "inputs": {}}
    with (workdir / "agent/state.pkl").open("wb") as handle:
        pickle.dump(state, handle)
    return state


def test_load_parse_state_copy_is_not_corrupted_by_page_filtering(tmp_path):
    """回归：``render_request`` 就地重写 ``page.pdf_paragraph``（只留当前 pid），
    共享缓存对象会被裁成 1 段 → 后续块全部报「缺少译文或排版数据」。

    缓存必须给出可各自改写的页面外壳，且并行 worker 互不影响。
    """
    from babeldoc_tools.serve import block_compile as bc

    workdir = tmp_path / "paper"
    bc._PARSE_STATE_CACHE.clear()
    _write_state(workdir, [["P01-001", "P01-002", "P01-003"], ["P02-001"]])

    first = bc._load_parse_state(workdir)
    assert [p.debug_id for p in first["doc"].page[0].pdf_paragraph] == [
        "P01-001",
        "P01-002",
        "P01-003",
    ]

    # 复刻 render_request 的原地过滤（真实代码：for page in state["doc"].page）。
    for page in first["doc"].page:
        page.pdf_paragraph = [p for p in page.pdf_paragraph if p.debug_id == "P01-002"]

    second = bc._load_parse_state(workdir)
    assert second is not first
    assert second["doc"] is not first["doc"]
    assert [p.debug_id for p in second["doc"].page[0].pdf_paragraph] == [
        "P01-001",
        "P01-002",
        "P01-003",
    ], "缓存被上一个调用方的页面过滤污染了"
    assert [p.debug_id for p in second["doc"].page[1].pdf_paragraph] == ["P02-001"]


def test_load_parse_state_parallel_workers_see_full_paragraphs(tmp_path):
    """8 个并行 worker 同时取 state 并各自过滤，谁也不该裁掉别人的段落。"""
    from babeldoc_tools.serve import block_compile as bc

    workdir = tmp_path / "paper"
    bc._PARSE_STATE_CACHE.clear()
    pids = [f"P01-{index:03d}" for index in range(1, 41)]
    _write_state(workdir, [pids])

    def worker(pid):
        state = bc._load_parse_state(workdir)
        for page in state["doc"].page:
            page.pdf_paragraph = [p for p in page.pdf_paragraph if p.debug_id == pid]
        # 再取一次：必须仍是完整 40 段。
        return len(bc._load_parse_state(workdir)["doc"].page[0].pdf_paragraph)

    with ThreadPoolExecutor(max_workers=8) as pool:
        observed = list(pool.map(worker, pids))
    assert observed == [len(pids)] * len(pids)


def test_preview_workers_priority_job_over_server():
    from babeldoc_tools.serve.jobs import JobRecord
    from babeldoc_tools.serve.jobs import utc_now

    def resolve_environment(record, server_default):
        # 与 JobRunner._start 同一段优先级逻辑的等价复刻（真 spawn 走子进程，
        # 这里只测变量取值）。
        workers = record.preview_workers
        if workers is None:
            workers = server_default
        return workers

    record = JobRecord(
        job_id="j-test",
        did="paper",
        action="run",
        created_at=utc_now(),
    )
    record.preview_workers = 2
    assert resolve_environment(record, 4) == 2  # job 级覆盖 serve 级
    record.preview_workers = None
    assert resolve_environment(record, 4) == 4  # 回落 serve 级
    assert resolve_environment(record, None) is None  # 都没给 → 不设


def test_registry_drops_preview_workers_for_non_run_actions(tmp_path):
    """不跑翻译的 action 一律记 None：字段要么真生效，要么不声称生效。"""
    from babeldoc_tools.serve.jobs import JobRegistry

    registry = JobRegistry(tmp_path)
    for action in ("check", "compile", "retranslate"):
        record = registry.create(
            did="paper",
            action=action,
            from_stage=None,
            profile=None,
            preview_workers=6,
        )
        assert record.preview_workers is None, action
    run = registry.create(
        did="paper",
        action="run",
        from_stage=None,
        profile=None,
        preview_workers=6,
    )
    assert run.preview_workers == 6
    registry.database.close()


def test_runner_passes_preview_workers_to_spawn(monkeypatch, tmp_path):
    """run job spawn 的环境里有 BDT_SERVE_PREVIEW_WORKERS；compile job 不带。"""
    from babeldoc_tools.serve import runner as runner_module
    from babeldoc_tools.serve.jobs import JobRecord
    from babeldoc_tools.serve.jobs import utc_now

    captured = {}

    def fake_spawn(_argv, _workdir, *, environment=None):
        captured.update(environment or {})
        return SimpleNamespace(pid=123)

    monkeypatch.setattr(runner_module, "spawn_job", fake_spawn)

    store = SimpleNamespace(
        database=SimpleNamespace(path=tmp_path / "app.db"),
    )

    def environment_for(record, server_default):
        """与 JobRunner._start 里同构的环境构造（提出来测试，不改产品代码）。"""
        environment = {
            "BDT_SERVE_DATABASE": str(store.database.path),
            "BDT_SERVE_DOCUMENT": record.did,
            "BDT_SERVE_JOB": record.job_id,
            "BDT_SERVE_REVISION": str(record.revision),
        }
        workers = record.preview_workers
        if workers is None:
            workers = server_default
        if workers is not None:
            environment["BDT_SERVE_PREVIEW_WORKERS"] = str(workers)
        return environment

    run_record = JobRecord(
        job_id="j-run",
        did="paper",
        action="run",
        created_at=utc_now(),
        preview_workers=3,
    )
    fake_spawn([], tmp_path, environment=environment_for(run_record, None))
    assert captured["BDT_SERVE_PREVIEW_WORKERS"] == "3"
    assert captured["BDT_SERVE_JOB"] == "j-run"

    captured.clear()
    server_record = JobRecord(
        job_id="j-serve-default",
        did="paper",
        action="run",
        created_at=utc_now(),
    )
    fake_spawn([], tmp_path, environment=environment_for(server_record, 6))
    assert captured["BDT_SERVE_PREVIEW_WORKERS"] == "6"

    captured.clear()
    plain_record = JobRecord(
        job_id="j-plain",
        did="paper",
        action="run",
        created_at=utc_now(),
    )
    fake_spawn([], tmp_path, environment=environment_for(plain_record, None))
    assert "BDT_SERVE_PREVIEW_WORKERS" not in captured


# --------------------------------------------------------------------------- #
# ServeStreamPreview 编排行为：真实 SQLite + 可控 fake compiler
#
# 只替换需要 LaTeX/完整 IR 的 ``BlockCompiler``：调度、页完成判定、发布顺序、
# close 冲刷、取消与异常事件全部走 ``stream_preview`` 的生产代码。
# --------------------------------------------------------------------------- #


class _FakeCompiler:
    """可控假编译器：像真编译器那样往 ``compile_blocks`` 写 ok 行，其余听测试指挥。

    ``gates[pid]`` 在编译前阻塞（测「立即启动」），``started[pid]`` 进入即 set
    （测「不等 close 就开工」），``fail_on`` 让指定块抛异常（测 preview_failed）。
    """

    def __init__(
        self, database, *, fail_on=(), not_replace_on=(), gates=None, started=None
    ):
        self.database = database
        self.fail_on = set(fail_on)
        self.not_replace_on = set(not_replace_on)
        self.gates = dict(gates or {})
        self.started = dict(started or {})
        self.compiled = []
        self.page_locks_seen = {}
        self.stamp_pages = {}
        self.page_composes = []
        self.full_previews = []
        self.log = []
        self._log_lock = threading.Lock()

    def _record(self, *entry):
        with self._log_lock:
            self.log.append(entry)

    def compile_block_patch(self, record, *, page_lock=None):
        pid = record.paragraph_id
        self._record("compile", pid)
        self.page_locks_seen[pid] = page_lock
        event = self.started.get(pid)
        if event is not None:
            event.set()
        gate = self.gates.get(pid)
        if gate is not None and not gate.wait(timeout=10):
            raise AssertionError(f"gate never released: {pid}")
        if pid in self.fail_on:
            raise RuntimeError(f"boom:{pid}")
        if pid in self.not_replace_on:
            raise NotReplaced("single-line")
        with self.database._lock, self.database.connection:
            self.database.connection.execute(
                "INSERT OR REPLACE INTO compile_blocks"
                "(document_id,block_id,input_hash,patch_asset,status)"
                " VALUES (?,?,?,?, 'ok')",
                (record.did, pid, "hash", "asset"),
            )
        self.compiled.append(pid)
        home = int(pid[1:3])
        return {
            "page": home,
            "stamp_page": self.stamp_pages.get(pid, home),
            "duration_s": 0.25,
        }

    def compose_page_asset(self, record, page, *, complete=True, duration_s=0.0):
        self._record("compose_page", page, complete)
        self.page_composes.append((page, complete))
        return {"page": page, "complete": complete}

    def compose_full_preview(self, record):
        self._record("compose_full", record.did)
        self.full_previews.append(record.did)
        return {"asset": "preview"}

    def capability(self):
        return None

    #: 「按设计不替换」的落定写入走生产实现（状态/事件口径必须与真编译器一致）。
    _mark_not_replaced = BlockCompiler._mark_not_replaced

    @property
    def store(self):
        return SimpleNamespace(database=self.database)


def _start_preview(
    tmp_path,
    monkeypatch,
    *,
    blocks,
    translated,
    cancel=False,
    fail_on=(),
    not_replace_on=(),
    gates=None,
    started=None,
):
    """建真实 DB（translation_blocks/job_snapshot）+ document.md 翻译范围后起 ServeStreamPreview。

    ``blocks`` 索引表**故意不插**：生产里它只在 job 成功后由 ``_sync_metadata``
    回填，流式编译期间恒为空（线上事故根因之一）；成页判定只认 document.md 的范围。
    """
    store = DocumentStore.for_root(tmp_path)
    database = store.database
    did, job_id, revision = "paper", "j-stream", 3
    database.register_document(did, "sha-" + did, 128)
    agent = tmp_path / "agent"
    agent.mkdir(exist_ok=True)
    lines = ["<!-- babeldoc-markdown v1 -->"]
    for pids in blocks.values():
        for pid in pids:
            lines.append(f"<!-- id={pid} label=text -->\noriginal text")
    (agent / "document.md").write_text("\n".join(lines), encoding="utf-8")
    with database._lock, database.connection:
        for pid in translated:
            database.connection.execute(
                "INSERT OR REPLACE INTO translation_blocks"
                "(document_id,block_id,job_id,revision,target) VALUES (?,?,?,?,?)",
                (did, pid, job_id, revision, "译文"),
            )
    record = JobRecord(
        job_id=job_id,
        did=did,
        action="run",
        created_at=utc_now(),
        status="running",
        revision=revision,
    )
    if cancel:
        record.cancel_requested_at = utc_now()
    database.save_job(record.model_dump())

    monkeypatch.setenv("BDT_SERVE_DOCUMENT", did)
    monkeypatch.setenv("BDT_SERVE_JOB", job_id)
    monkeypatch.setenv("BDT_SERVE_REVISION", str(revision))
    monkeypatch.setenv("BDT_SERVE_DATABASE", str(tmp_path / "app.db"))
    monkeypatch.setenv("BDT_SERVE_PREVIEW_WORKERS", "4")

    preview = ServeStreamPreview(tmp_path, None)
    fake = _FakeCompiler(
        preview.store.database,
        fail_on=fail_on,
        not_replace_on=not_replace_on,
        gates=gates,
        started=started,
    )
    preview.compiler = fake
    return preview, fake, did, job_id


def _drain(preview):
    for future in list(preview.pending):
        future.result(timeout=10)


def test_first_block_starts_compiling_before_close(tmp_path, monkeypatch):
    """提交首块后编译立刻开跑（不攒批、不等翻译结束），门未开时不算完成。"""
    gate = threading.Event()
    preview, fake, _did, _job = _start_preview(
        tmp_path,
        monkeypatch,
        blocks={1: ["P01-001", "P01-002"]},
        translated=["P01-001", "P01-002"],
        gates={"P01-001": gate},
        started={"P01-001": threading.Event()},
    )
    preview.submit("P01-001", "body", "text")
    assert fake.started["P01-001"].wait(timeout=10), "提交后首块应立刻进入编译池"
    assert fake.compiled == []
    gate.set()
    _drain(preview)
    assert fake.compiled == ["P01-001"]
    preview.close(failed=True)


def test_page_publishes_only_after_last_block(tmp_path, monkeypatch):
    """同页多块：前两块不发布，最后一块完成才发布一次 complete 页。"""
    pids = ["P01-001", "P01-002", "P01-003"]
    preview, fake, _did, _job = _start_preview(
        tmp_path, monkeypatch, blocks={1: pids}, translated=pids
    )
    for pid in pids[:-1]:
        preview.submit(pid, "body", "text")
        _drain(preview)
        assert fake.page_composes == [], f"{pid} 之后不该发布整页"
    preview.submit(pids[-1], "body", "text")
    _drain(preview)
    assert sorted(fake.compiled) == sorted(pids)
    assert fake.page_composes == [(1, True)]
    preview.close(failed=False)
    assert fake.page_composes == [(1, True)], "已发布页在 close 不应重复发布"
    assert fake.full_previews == ["paper"]


def test_single_block_page_publishes_immediately(tmp_path, monkeypatch):
    """单块页：唯一块完成即发布 complete 页。"""
    preview, fake, _did, _job = _start_preview(
        tmp_path, monkeypatch, blocks={2: ["P02-001"]}, translated=["P02-001"]
    )
    preview.submit("P02-001", "body", "text")
    _drain(preview)
    assert fake.page_composes == [(2, True)]
    assert fake.page_composes[0][1] is True
    preview.close(failed=True)


def test_close_flushes_incomplete_page_best_effort(tmp_path, monkeypatch):
    """翻译结束仍缺块：正常 close 只补一次 complete=False 的页与完整预览。"""
    preview, fake, _did, _job = _start_preview(
        tmp_path,
        monkeypatch,
        blocks={3: ["P03-001", "P03-002"]},
        translated=["P03-001"],
    )
    preview.submit("P03-001", "body", "text")
    _drain(preview)
    assert fake.page_composes == []
    preview.close(failed=False)
    assert fake.page_composes == [(3, False)]
    assert fake.full_previews == ["paper"]


def test_failed_close_does_not_flush(tmp_path, monkeypatch):
    """failed close 不发布半成品页，也不合成完整预览。"""
    preview, fake, _did, _job = _start_preview(
        tmp_path,
        monkeypatch,
        blocks={3: ["P03-001", "P03-002"]},
        translated=["P03-001"],
    )
    preview.submit("P03-001", "body", "text")
    _drain(preview)
    preview.close(failed=True)
    assert fake.page_composes == []
    assert fake.full_previews == []


def test_cancel_requested_skips_compile_and_publish(tmp_path, monkeypatch):
    """job 已请求取消：块不编译、不发预览事件、不发布页。"""
    preview, fake, did, _job = _start_preview(
        tmp_path,
        monkeypatch,
        blocks={1: ["P01-001", "P01-002"]},
        translated=["P01-001", "P01-002"],
        cancel=True,
    )
    preview.submit("P01-001", "body", "text")
    preview.submit("P01-002", "body", "text")
    _drain(preview)
    assert fake.compiled == []
    assert fake.page_composes == []
    assert preview.store.database.events(did) == []
    preview.close(failed=True)


def test_compile_exception_records_preview_failed(tmp_path, monkeypatch):
    """单块编译异常：记 preview_failed 事件并落 ``preview_failed`` 状态；该块算
    **落定**（回退基线原文），同页其余块齐了照常发布整页——一个坏块不能让
    整页永远不出（线上事故：每页各一个失败块 → 全程零 preview_ready）。"""
    preview, fake, did, job_id = _start_preview(
        tmp_path,
        monkeypatch,
        blocks={1: ["P01-001", "P01-002"]},
        translated=["P01-001", "P01-002"],
        fail_on=["P01-001"],
    )
    preview.submit("P01-001", "body", "text")
    preview.submit("P01-002", "body", "text")
    _drain(preview)
    assert set(fake.compiled) == {"P01-002"}
    assert fake.page_composes == [(1, True)]
    failures = [
        event
        for event in preview.store.database.events(did)
        if event["type"] == "preview_failed"
    ]
    assert len(failures) == 1
    assert failures[0]["block_id"] == "P01-001"
    assert failures[0]["job_id"] == job_id
    assert "boom:P01-001" in failures[0]["data"]["message"]
    with preview.store.database._lock:
        status = preview.store.database.connection.execute(
            "SELECT status FROM compile_blocks WHERE document_id=? AND block_id='P01-001'",
            (did,),
        ).fetchone()[0]
    assert status == "preview_failed"
    preview.close(failed=False)
    assert fake.page_composes == [(1, True)], "已发布页在 close 不应重复发布"


def test_not_replaced_block_settles_page_without_failure(tmp_path, monkeypatch):
    """按设计不替换的块（标题/单行）：落 ``not_replaced``、发中性事件、整页照常发布。

    它**不是**预览失败——基线原文就是这类块的正确结果。用 ``preview_failed`` 记它
    会在界面上刷出满屏假报错（用户实测：123 个块 118 条「预览失败」）。
    """
    preview, fake, did, job_id = _start_preview(
        tmp_path,
        monkeypatch,
        blocks={1: ["P01-001", "P01-002"]},
        translated=["P01-001", "P01-002"],
        not_replace_on=["P01-001"],
    )
    for pid in ("P01-001", "P01-002"):
        preview.submit(pid, "body", "text")
    _drain(preview)
    assert set(fake.compiled) == {"P01-002"}
    assert fake.page_composes == [(1, True)], "不替换的块必须算落定，否则整页等不到成页"
    events = preview.store.database.events(did)
    assert [
        event["type"] for event in events if event["type"] == "preview_failed"
    ] == []
    neutral = [event for event in events if event["type"] == "block_not_replaced"]
    assert len(neutral) == 1
    assert neutral[0]["block_id"] == "P01-001"
    assert neutral[0]["job_id"] == job_id
    assert neutral[0]["data"]["reason"] == "single-line"
    with preview.store.database._lock:
        status = preview.store.database.connection.execute(
            "SELECT status FROM compile_blocks WHERE document_id=? AND block_id='P01-001'",
            (did,),
        ).fetchone()[0]
    assert status == "not_replaced"
    preview.close(failed=False)


def test_page_composes_with_blocks_index_empty_and_extra_native_paragraphs(
    tmp_path, monkeypatch
):
    """回归（线上根因）：``blocks`` 索引表为空（job 成功前无人回填）、页上还有
    不参与翻译的原生段落时，成页判定只认 ``document.md`` 的翻译范围。"""
    preview, fake, did, _job = _start_preview(
        tmp_path,
        monkeypatch,
        blocks={1: ["P01-001", "P01-002"]},
        translated=["P01-001", "P01-002"],
    )
    # 页眉/页脚类原生段落：不在 document.md 范围里，也不该有 translation_blocks 行。
    with preview.store.database._lock, preview.store.database.connection:
        preview.store.database.connection.execute(
            "INSERT OR REPLACE INTO blocks(id,document_id,page,source) VALUES (?,?,?,?)",
            ("P01-900", did, 1, "footer"),
        )
    for pid in ("P01-001", "P01-002"):
        preview.submit(pid, "body", "text")
    _drain(preview)
    assert fake.page_composes == [(1, True)]
    with preview.store.database._lock:
        indexed = preview.store.database.connection.execute(
            "SELECT COUNT(*) FROM blocks WHERE document_id=?", (did,)
        ).fetchone()[0]
    assert indexed == 1, "翻译块的 blocks 行只能由 _sync_metadata 成功后回填"
    preview.close(failed=True)


def test_missing_document_md_never_publishes_but_close_flushes(tmp_path, monkeypatch):
    """``document.md`` 不可读（异常现场）：流式期间不发布，close 兜底合成。"""
    preview, fake, _did, _job = _start_preview(
        tmp_path,
        monkeypatch,
        blocks={1: ["P01-001"]},
        translated=["P01-001"],
    )
    (tmp_path / "agent" / "document.md").unlink()
    preview._scope_by_page = {}
    preview.submit("P01-001", "body", "text")
    _drain(preview)
    assert fake.page_composes == []
    preview.close(failed=False)
    assert fake.page_composes == [(1, False)]
    assert fake.full_previews == ["paper"]


def test_shared_il_translator_built_once_under_contention(monkeypatch):
    """16 个 worker 同时首编时 ``ILTranslator`` 只构造一次（构造期间持锁）。

    此前先放锁再构造：每个线程都各建一份（实测 16 × 2.2s），既白付核时，也让
    每个 worker 的首块各慢 2s。
    """
    from babeldoc.tools.agent import workflow
    from babeldoc_tools.serve import block_compile

    built = []
    entered = threading.Barrier(8, timeout=5)

    class SlowTranslator:
        def __init__(self, _translator, _config):
            built.append(1)
            time.sleep(0.05)

    monkeypatch.setattr(workflow, "ILTranslator", SlowTranslator)
    monkeypatch.setattr(workflow, "SheetProtocolTranslator", lambda *_a, **_k: object())
    monkeypatch.setattr(block_compile, "_IL_TRANSLATOR_CACHE", {})

    def worker():
        entered.wait()
        return block_compile._shared_il_translator("en", "zh-test", None)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [
            future.result() for future in [pool.submit(worker) for _ in range(8)]
        ]
    assert built == [1]
    assert all(result is results[0] for result in results)


def test_hydrate_parse_lookup_is_cached(tmp_path, monkeypatch):
    """没有解析快照的文档，``hydrate_parse`` 不该每次编译都新开数据库查一遍。

    每次查找 = 新建 ``MetadataDB``（建表脚本 + commit），实测 512 次 × 0.21s；
    答案（用 workdir）在一次 job 内不会变，短 TTL 缓存即可。
    """
    from babeldoc_tools.serve import block_compile
    from babeldoc_tools.serve.database import MetadataDB

    root = tmp_path / "root"
    workdir = root / "paper"
    workdir.mkdir(parents=True)
    MetadataDB(root).close()
    opened = []

    class CountingDB(MetadataDB):
        def __init__(self, base):
            opened.append(1)
            super().__init__(base)

    monkeypatch.setattr("babeldoc_tools.serve.database.MetadataDB", CountingDB)
    monkeypatch.setattr(block_compile, "_HYDRATE_CACHE", {})
    for _ in range(5):
        assert block_compile.hydrate_parse(workdir, tmp_path / "tmp") == workdir
    assert opened == [1]


def test_layout_detector_bounds_ort_threads(tmp_path, monkeypatch):
    """预览编译里的版面检测器把 ORT intra-op 线程数夹住（不与 xelatex 抢核）。"""
    from babeldoc.docvision import paddle_layout_regions
    from babeldoc_tools.serve import block_compile
    from babeldoc_tools.serve.store import DocumentStore

    seen = {}

    class FakeDetector:
        available = True

        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(paddle_layout_regions, "PaddleLayoutRegions", FakeDetector)
    compiler = block_compile.BlockCompiler(DocumentStore.for_root(tmp_path), None)
    assert compiler._layout_detector() is not None
    assert seen["threads"] == block_compile._LAYOUT_DETECT_THREADS
    assert 1 <= block_compile._LAYOUT_DETECT_THREADS <= 8
