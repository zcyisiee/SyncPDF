from __future__ import annotations

import os
from pathlib import Path

WORKROOT_ENV = "BABELDOC_EXECUTOR_WORKROOT"
WORKROOT_READY_FILE = ".executor-workroot-ready"


def get_workroot(*, require_ready_file: bool = False) -> Path:
    raw = os.environ.get(WORKROOT_ENV)
    if not raw:
        raise ValueError(f"{WORKROOT_ENV} is required")
    workroot = Path(raw).resolve()
    if not workroot.is_dir():
        raise ValueError("executor workroot must be an existing directory")
    if not os.access(workroot, os.R_OK | os.W_OK):
        raise ValueError("executor workroot must be readable and writable")
    if require_ready_file and not (workroot / WORKROOT_READY_FILE).is_file():
        raise ValueError("executor workroot readiness proof file is missing")
    return workroot


def resolve_inside_workroot(workroot: Path, value: str) -> Path:
    if not value:
        raise ValueError("path must be a non-empty string")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = workroot / candidate
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(workroot)
    except ValueError as exc:
        raise ValueError("path escapes executor workroot") from exc
    return resolved


def resolve_file(workroot: Path, value: str) -> Path:
    path = resolve_inside_workroot(workroot, value)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    return path


def resolve_dir(workroot: Path, value: str, *, create: bool = False) -> Path:
    path = resolve_inside_workroot(workroot, value)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError(f"{value} must resolve to a directory")
    return path


def relative_to_workroot(workroot: Path, value: Path | str | None) -> str | None:
    if value is None:
        return None
    resolved = Path(value).resolve(strict=False)
    try:
        return str(resolved.relative_to(workroot))
    except ValueError as exc:
        raise ValueError("path escapes executor workroot") from exc
