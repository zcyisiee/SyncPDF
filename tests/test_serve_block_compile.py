"""Local compiler acceptance: real PDF composition, bounded renderer seam."""

import json
from types import SimpleNamespace

import pymupdf
import pytest
from babeldoc_tools.common import ToolError
from babeldoc_tools.serve.block_compile import BlockCompiler
from babeldoc_tools.serve.block_compile import valid_box
from babeldoc_tools.serve.jobs import JobRecord
from babeldoc_tools.serve.jobs import utc_now
from babeldoc_tools.serve.store import DocumentStore


@pytest.fixture
def local(tmp_path, monkeypatch):
    workdir = tmp_path / "paper"
    (workdir / "output").mkdir(parents=True)
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=400, height=400)
        page.insert_text((30, 60), "Old block one")
        page.insert_text((30, 180), "Untouched block two")
        pdf.save(workdir / "output/paper.mono.pdf")
    store = DocumentStore.for_root(tmp_path)
    compiler = BlockCompiler(store, None)
    rows = [
        SimpleNamespace(
            id="P1",
            page=1,
            target="New block one",
            geometry={"src_box": [20, 320, 200, 370]},
        ),
        SimpleNamespace(
            id="P2",
            page=1,
            target="New block two",
            geometry={"src_box": [20, 190, 200, 250]},
        ),
    ]
    monkeypatch.setattr(compiler, "_rows", lambda _did: rows)
    calls = []

    def render(_workdir, pid, target, box, temporary, _cache):
        calls.append(pid)
        path = temporary / "stamp.pdf"
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=box[2] - box[0], height=box[3] - box[1])
            page.insert_text((5, 20), target)
            pdf.save(path)
        return SimpleNamespace(ok=True, pdf_path=str(path), font_size=11), False

    monkeypatch.setattr("babeldoc_tools.serve.block_compile.render_request", render)
    return compiler, calls, rows


def record(pid="P1"):
    return JobRecord(
        job_id="j-local",
        did="paper",
        action="compile",
        status="running",
        created_at=utc_now(),
        paragraph_id=pid,
        revision=0,
    )


def test_only_one_stamp_and_other_text_survives(local):
    compiler, calls, _ = local
    result = compiler.compile(record())
    assert calls == ["P1"]
    from babeldoc_tools.serve.asset_store import AssetStore

    assets = AssetStore(compiler.store.store_base, compiler.store.database)
    with pymupdf.open(assets.resolve(result["asset"])) as pdf:
        text = pdf[0].get_text()
        assert "New block one" in text
        assert "Old block one" not in text
        assert "Untouched block two" in text
    compiler.compile(record("P2"))
    assert calls == ["P1", "P2"]


def test_failed_stamp_preserves_page_and_previous_patch(local, monkeypatch):
    compiler, _, _ = local
    compiler.compile(record())
    database = compiler.store.database
    before = database.connection.execute("SELECT payload FROM local_pages").fetchone()[
        0
    ]

    def fail(*_args):
        raise ToolError("compile_failed", "failed stamp")

    monkeypatch.setattr("babeldoc_tools.serve.block_compile.render_request", fail)
    with pytest.raises(ToolError, match="failed stamp"):
        compiler.compile(record())
    assert (
        database.connection.execute("SELECT payload FROM local_pages").fetchone()[0]
        == before
    )
    assert list((compiler.store.store_base / "tmp").iterdir()) == []


def test_revision_change_during_render_does_not_publish(local, monkeypatch):
    compiler, _, _ = local
    from babeldoc_tools.serve import block_compile

    render = block_compile.render_request

    def changed(*args):
        result = render(*args)
        compiler.store.database.save_draft(
            "paper", {"revision": 1, "paragraphs": {}}, expected_revision=0
        )
        return result

    monkeypatch.setattr(block_compile, "render_request", changed)
    with pytest.raises(ToolError) as error:
        compiler.compile(record())
    assert error.value.code == "stale_job"
    assert (
        compiler.store.database.connection.execute(
            "SELECT COUNT(*) FROM local_pages"
        ).fetchone()[0]
        == 0
    )


def test_block_endpoint_lifecycle_and_asset_download(local, monkeypatch):
    import time

    from babeldoc_tools.serve.app import create_app
    from fastapi.testclient import TestClient

    compiler, calls, rows = local
    monkeypatch.setattr(BlockCompiler, "_rows", lambda _self, _did: rows)
    with TestClient(create_app(compiler.store)) as client:
        response = client.post(
            "/api/v1/documents/paper/blocks/P1/compile", json={"base_revision": 0}
        )
        assert response.status_code == 202, response.text
        jid = response.json()["job_id"]
        for _ in range(200):
            job = client.get(f"/api/v1/jobs/{jid}").json()
            if job["status"] not in ("queued", "running"):
                break
            time.sleep(0.01)
        assert job["status"] == "succeeded", job
        assert calls == ["P1"]
        preview = compiler.store.database.connection.execute(
            "SELECT asset_sha256 FROM local_previews"
        ).fetchone()[0]
        downloaded = client.get(f"/api/v1/documents/paper/assets/{preview}")
        assert downloaded.status_code == 200
        assert downloaded.content.startswith(b"%PDF")
        denied = client.get("/api/v1/documents/paper/assets/" + "a" * 64)
        assert denied.status_code == 404


@pytest.mark.parametrize(
    "box", [[0, 0, float("nan"), 10], [0, 0, 401, 10], [1, 1, 0, 0]]
)
def test_invalid_bbox(box):
    with pytest.raises(ToolError) as error:
        valid_box(box, 400, 400)
    assert error.value.code == "bbox_invalid"


def test_export_only_compiles_dirty_block_and_reuses_clean_patch(local):
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


def test_failed_dirty_export_keeps_previous_revision(local, monkeypatch):
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
