"""Small compatibility helpers shared by callers of the legacy tool layer."""

from __future__ import annotations

import json
from pathlib import Path

from babeldoc_core import JobError


class ToolError(JobError):
    """Legacy spelling for a stable core error."""

    @property
    def extra(self):
        return self.details
AGENT_DIR = "agent"


def agent_dir(workdir: str | Path) -> Path:
    return Path(workdir) / AGENT_DIR


def require_workdir(workdir: str | Path) -> Path:
    path = Path(workdir)
    if not (path / AGENT_DIR).is_dir():
        raise JobError("workdir_missing", f"{path}/agent 不存在：请先跑 parse_document")
    return path


def read_json(path: str | Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(path: str | Path, payload):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return destination
