import os
import pickle
import time
from pathlib import Path

import pymupdf
from babeldoc_tools.serve.block_compile import hydrate_parse
from babeldoc_tools.serve.cleanup import cleanup
from babeldoc_tools.serve.database import MetadataDB
from babeldoc_tools.serve.migrate import migrate_root


def test_migrate_imports_source_and_blocks_but_not_debug_history(tmp_path: Path):
    root = tmp_path / "root"
    workdir = root / "paper"
    workdir.mkdir(parents=True)
    import pymupdf

    with pymupdf.open() as pdf:
        pdf.new_page()
        pdf.save(workdir / "source.pdf")
    (workdir / "debug").mkdir()
    (workdir / "debug" / "old.log").write_text("old", encoding="utf-8")
    result = migrate_root(root)
    assert result["migrated"] == ["paper"]
    database = MetadataDB(root)
    row = database.connection.execute(
        "SELECT pdf_sha256 FROM documents WHERE id='paper'"
    ).fetchone()
    assert row is not None
    assert (workdir / "debug" / "old.log").exists()
    assert len(list((root / "assets").rglob("*.pdf"))) == 1
    database.close()


def test_migrated_snapshot_restores_without_original_parser_files(tmp_path):
    root = tmp_path / "root"
    workdir = root / "paper"
    (workdir / "agent").mkdir(parents=True)
    with pymupdf.open() as pdf:
        pdf.new_page()
        pdf.save(workdir / "source.pdf")
    original = (workdir / "source.pdf").read_bytes()
    (workdir / "prepared.pdf").write_bytes(original)
    state = {
        "pdf_path": str(workdir / "source.pdf"),
        "temp_pdf_path": str(workdir / "prepared.pdf"),
    }
    (workdir / "agent/state.pkl").write_bytes(pickle.dumps(state))
    result = migrate_root(root)
    assert result["migrated"] == ["paper"]
    (workdir / "agent/state.pkl").unlink()
    (workdir / "prepared.pdf").unlink()
    (workdir / "source.pdf").unlink()
    restored = hydrate_parse(workdir, tmp_path / "restored")
    assert (restored / "prepared.pdf").read_bytes() == original
    restored_state = pickle.loads((restored / "agent/state.pkl").read_bytes())  # noqa: S301
    assert restored_state["pdf_path"] == "source.pdf"
    assert restored_state["temp_pdf_path"] == "prepared.pdf"
    assert str(workdir) not in repr(restored_state)


def test_cleanup_retains_assets_and_fresh_files_and_symlink_targets(tmp_path):
    for directory in ("tmp", "cache", "assets"):
        (tmp_path / directory).mkdir()
    old = time.time() - 90000
    asset = tmp_path / "assets/retained.pdf"
    asset.write_bytes(b"registered")
    os.utime(asset, (old, old))
    stale = tmp_path / "cache/old"
    stale.write_bytes(b"expired")
    os.utime(stale, (old, old))
    fresh = tmp_path / "tmp/new"
    fresh.write_bytes(b"in progress")
    (tmp_path / "tmp/shortcut").symlink_to(asset)
    result = cleanup(tmp_path)
    assert result["removed_files"] == 1
    assert not stale.exists()
    assert asset.read_bytes() == b"registered"
    assert fresh.exists()
