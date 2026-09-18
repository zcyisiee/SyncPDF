import asyncio

from babeldoc_tools.serve.database import MetadataDB
from babeldoc_tools.serve.routers.events import persistent_stream
from babeldoc_tools.serve.store import DocumentStore


def test_translation_commits_only_current_active_revision(tmp_path):
    db = MetadataDB(tmp_path)
    db.save_job({"job_id": "j1", "did": "paper", "action": "run", "status": "running"})
    db.save_draft(
        "paper",
        {"revision": 1, "paragraphs": {"P1": {"target": "Manual"}}},
        expected_revision=0,
    )
    assert not db.commit_translation("j1", "paper", 0, "P2", "Stale", {"index": 1})
    assert db.events("paper") == []
    assert db.commit_translation("j1", "paper", 1, "P1", "Generated", {"index": 1})
    assert db.events("paper")[0]["data"]["text"] == "Manual"
    assert (
        db.connection.execute("SELECT COUNT(*) FROM translation_blocks").fetchone()[0]
        == 0
    )
    assert db.commit_translation("j1", "paper", 1, "P2", "Accepted", {"index": 2})
    assert (
        db.connection.execute("SELECT target FROM translation_blocks").fetchone()[0]
        == "Accepted"
    )
    db.save_job({"job_id": "j1", "did": "paper", "action": "run", "status": "canceled"})
    assert not db.commit_translation("j1", "paper", 1, "P3", "Canceled", {})
    db.close()


def test_sse_cursor_survives_restart_and_different_jobs(tmp_path):
    (tmp_path / "paper").mkdir()
    store = DocumentStore.for_root(tmp_path)
    store.database.append_event("j1", "paper", "preview_ready", {"n": 1})
    store.database.append_event("j2", "paper", "preview_ready", {"n": 2})
    cursor = store.database.events("paper")[0]["id"]
    store.database.close()
    restarted = DocumentStore.for_root(tmp_path)

    async def receive():
        stream = persistent_stream(restarted, "paper", cursor)
        try:
            return await anext(stream)
        finally:
            await stream.aclose()

    frame = asyncio.run(receive())
    assert '"n":2' in frame
    assert '"n":1' not in frame
    assert '"job_id":"j2"' in frame
    restarted.database.close()
