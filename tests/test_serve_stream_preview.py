"""Serve-side streaming preview: worker pool sizing, page routing, spawn env.

Guards the parallel preview compile path: blocks on the same PDF page must
serialize (no lost page patch), the worker count comes from
``BDT_SERVE_PREVIEW_WORKERS`` bounded to 1..8, and the job runner injects the
variable into the translation subprocess environment.
"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from babeldoc_tools.serve.stream_preview import MAX_PREVIEW_WORKERS
from babeldoc_tools.serve.stream_preview import _lock_index
from babeldoc_tools.serve.stream_preview import _page_of
from babeldoc_tools.serve.stream_preview import preview_workers_from_environ


def test_preview_workers_env_parsing():
    default = {"BDT_SERVE_PREVIEW_WORKERS": None}
    assert preview_workers_from_environ(default) == MAX_PREVIEW_WORKERS
    assert preview_workers_from_environ({"BDT_SERVE_PREVIEW_WORKERS": "3"}) == 3
    # 越界夹到 [1, 8]，非法值回缺省：环境变量来自启动脚本，坏值不炸翻译。
    assert preview_workers_from_environ({"BDT_SERVE_PREVIEW_WORKERS": "99"}) == 8
    assert preview_workers_from_environ({"BDT_SERVE_PREVIEW_WORKERS": "0"}) == 1
    assert preview_workers_from_environ({"BDT_SERVE_PREVIEW_WORKERS": "abc"}) == 8
    assert preview_workers_from_environ({"BDT_SERVE_PREVIEW_WORKERS": " "}) == 8


def test_pid_page_extraction():
    assert _page_of("P01-003") == 1
    assert _page_of("P12-010") == 12
    assert _page_of("not-a-pid") is None


def test_same_page_blocks_share_one_lock():
    """同页块必须路由到同一个 worker 槽：BlockCompiler.compile 对页状态是
    读-改-写，两个同页块并行会互相覆盖 patch。"""
    assert _lock_index("P01-001", 8) == _lock_index("P01-999", 8)
    assert _lock_index("P01-001", 8) != _lock_index("P02-001", 8)
    # 前 8 页在 8 worker 下互不共槽（页数 ≤ worker 数时全并行）。
    assert len({_lock_index(f"P{n:02d}-001", 8) for n in range(1, 9)}) == 8
    # worker=1 退化为全串行（旧行为）。
    assert len({_lock_index(f"P{n:02d}-001", 1) for n in range(1, 9)}) == 1


def test_concurrent_page_compile_keeps_all_patches(tmp_path):
    """端到端守卫：同页多个块并发提交（各自加页锁后做读-改-写），
    local_pages 的 patch 一个不丢。锁路由与 ServeStreamPreview._compile 一致。"""
    from babeldoc_tools.serve.store import DocumentStore

    store = DocumentStore.for_root(tmp_path)
    database = store.database
    with database._lock, database.connection:
        database.connection.execute(
            "INSERT OR IGNORE INTO documents(id) VALUES ('paper')"
        )

    workers = 8
    locks = [threading.Lock() for _ in range(workers)]
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
        with locks[_lock_index(pid, workers)]:
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
        for future in [
            pool.submit(compiler.capability) for _ in range(8)
        ]:
            results.append(future.result())
    assert calls == [1]
    assert all(result is results[0] for result in results)
    assert results[0].marker == "probed"


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
