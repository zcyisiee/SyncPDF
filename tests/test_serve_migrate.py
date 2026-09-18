from pathlib import Path

from babeldoc_tools.serve.database import MetadataDB
from babeldoc_tools.serve.migrate import migrate_root


def test_migrate_imports_source_and_blocks_but_not_debug_history(tmp_path: Path):
    root = tmp_path / "root"
    workdir = root / "paper"
    workdir.mkdir(parents=True)
    (workdir / "source.pdf").write_bytes(b"%PDF-current")
    (workdir / "debug").mkdir()
    (workdir / "debug" / "old.log").write_text("old", encoding="utf-8")
    result = migrate_root(root)
    assert result["migrated"] == ["paper"]
    database = MetadataDB(root)
    row = database.connection.execute("SELECT pdf_sha256 FROM documents WHERE id='paper'").fetchone()
    assert row is not None
    assert (workdir / "debug" / "old.log").exists()
    assert len(list((root / "assets").rglob("*.pdf"))) == 1
    database.close()
