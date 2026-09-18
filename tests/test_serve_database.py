"""Durable metadata, revision fencing and content identity regressions."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from babeldoc_tools.common import ToolError
from babeldoc_tools.serve.asset_store import AssetStore
from babeldoc_tools.serve.database import MetadataDB


def test_schema_wal_and_reopen(tmp_path):
    database = MetadataDB(tmp_path)
    assert database.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    database.register_document("doc", "abc", 4)
    database.close()
    database = MetadataDB(tmp_path)
    assert database.document_by_hash("abc")["id"] == "doc"
    assert database.connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1
    database.close()


def test_two_connections_cannot_overwrite_same_revision(tmp_path):
    first, second = MetadataDB(tmp_path), MetadataDB(tmp_path)
    initial = {"revision": 0, "paragraphs": {}}
    first.save_draft("doc", initial, expected_revision=0)

    def edit(database, text):
        try:
            database.save_draft("doc", {"revision": 1, "paragraphs": {"P1": {"target": text}}}, expected_revision=0)
            return "saved"
        except ToolError as error:
            return error.code

    with ThreadPoolExecutor(2) as pool:
        tasks = [pool.submit(edit, first, "A"), pool.submit(edit, second, "B")]
        assert sorted(task.result() for task in tasks) == ["revision_conflict", "saved"]
    assert first.draft("doc")["revision"] == 1
    assert first.connection.execute("SELECT COUNT(*) FROM block_edits").fetchone()[0] == 1
    first.close()
    second.close()


def test_events_resume_across_jobs_and_restart(tmp_path):
    database = MetadataDB(tmp_path)
    database.append_event("j1", "doc", "translation_block_completed", {"block": "P1"})
    cursor = database.events("doc")[-1]["id"]
    database.append_event("j2", "doc", "job_queued", {})
    database.append_event("j3", "other", "job_queued", {})
    database.close()
    database = MetadataDB(tmp_path)
    remaining = database.events("doc", cursor)
    assert [event["job_id"] for event in remaining] == ["j2"]
    assert database.events("doc", remaining[-1]["id"]) == []
    database.close()


def test_asset_dedup_missing_and_path_escape(tmp_path):
    root = tmp_path / "root"
    database = MetadataDB(root)
    assets = AssetStore(root, database)
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-asset")
    digest = assets.put(source, kind="source")
    assert assets.put(source, kind="source") == digest
    assert len(list((root / "assets").rglob("*.pdf"))) == 1
    stored = assets.resolve(digest)
    assert stored.read_bytes() == source.read_bytes()
    stored.unlink()
    with pytest.raises(ToolError, match="unavailable"):
        assets.resolve(digest)
    with database.connection:
        database.connection.execute("UPDATE assets SET relative_path='../source.pdf' WHERE sha256=?", (digest,))
    with pytest.raises(ToolError, match="unavailable"):
        assets.resolve(digest)
    database.close()
