"""Content-addressed payloads; callers never resolve arbitrary download paths."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from babeldoc_tools.common import ToolError
from babeldoc_tools.serve.database import MetadataDB
from babeldoc_tools.serve.database import sha256_file


class AssetStore:
    def __init__(self, root: Path, database: MetadataDB):
        self.root = root.resolve()
        self.database = database

    def put(self, source: Path, *, kind: str, extension: str = "pdf") -> str:
        if not extension.isalnum():
            raise ValueError("Invalid asset extension")
        digest, size = sha256_file(source)
        relative = Path("assets") / digest[:2] / f"{digest}.{extension}"
        target = self.root / relative
        if not target.resolve().is_relative_to(self.root):
            raise ToolError("path_escape", "Asset path escapes storage")
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            fd, name = tempfile.mkstemp(dir=target.parent, prefix=".asset-")
            try:
                with os.fdopen(fd, "wb") as output, source.open("rb") as handle:
                    shutil.copyfileobj(handle, output)
                    output.flush()
                    os.fsync(output.fileno())
                Path(name).replace(target)
            finally:
                Path(name).unlink(missing_ok=True)
        with self.database._lock, self.database.connection:
            self.database.connection.execute(
                "INSERT OR IGNORE INTO assets(sha256,relative_path,kind,byte_size) VALUES (?,?,?,?)",
                (digest, relative.as_posix(), kind, size),
            )
        return digest

    def resolve(self, digest: str) -> Path:
        with self.database._lock:
            row = self.database.connection.execute(
                "SELECT relative_path FROM assets WHERE sha256=?", (digest,)
            ).fetchone()
        if row is None:
            raise ToolError("artifact_not_found", "Asset is not registered")
        path = (self.root / row[0]).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ToolError("artifact_not_found", "Asset is unavailable; regenerate it")
        return path
