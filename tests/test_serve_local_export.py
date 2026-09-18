import json

import pymupdf
import pytest
from babeldoc_tools.common import ToolError
from tests.test_serve_block_compile import local  # noqa: F401
from tests.test_serve_block_compile import record


def test_export_only_compiles_dirty_block_and_reuses_clean_patch(local):  # noqa: F811
    compiler, calls, _ = local
    database = compiler.store.database
    database.save_draft(
        "paper",
        {"revision": 1, "paragraphs": {"P1": {"target": "Edited one"}}},
        expected_revision=0,
    )
    job = record()
    job.revision = 1
    result = compiler.export(job)
    assert calls == ["P1"]
    assert result["compiled_blocks"] == 1
    from babeldoc_tools.serve.asset_store import AssetStore

    assets = AssetStore(compiler.store.store_base, database)
    with pymupdf.open(assets.resolve(result["asset"])) as pdf:
        assert "Edited one" in pdf[0].get_text()
        assert "Untouched block two" in pdf[0].get_text()
    again = compiler.export(job)
    assert again["compiled_blocks"] == 0
    assert calls == ["P1"]


def test_failed_dirty_export_keeps_previous_revision(local, monkeypatch):  # noqa: F811
    compiler, _, _ = local
    database = compiler.store.database
    job = record()
    compiler.export(job)
    previous = tuple(
        database.connection.execute(
            "SELECT asset_sha256,revision FROM exports"
        ).fetchone()
    )
    database.save_draft(
        "paper",
        {"revision": 1, "paragraphs": {"P1": {"target": "Edited"}}},
        expected_revision=0,
    )
    job.revision = 1

    def fail(*_args):
        raise ToolError("compile_failed", "stamp failed")

    monkeypatch.setattr("babeldoc_tools.serve.block_compile.render_request", fail)
    with pytest.raises(ToolError):
        compiler.export(job)
    assert (
        tuple(
            database.connection.execute(
                "SELECT asset_sha256,revision FROM exports"
            ).fetchone()
        )
        == previous
    )
    assert (
        json.loads(
            database.connection.execute("SELECT payload FROM drafts").fetchone()[0]
        )["revision"]
        == 1
    )
