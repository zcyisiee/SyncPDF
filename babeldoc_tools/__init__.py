"""MAS-facing BabelDOC tools (Python API, JSON schema, and dispatch)."""

import sys
from pathlib import Path

from . import registry
from .registry import dispatch
from .registry import get_schema
from .registry import list_tools
from .registry import tool_names
from .registry import ToolResult

from . import tools as _tools  # noqa: F401 - registration side effect

__version__ = "0.1.0"


def find_repo_root(start: Path | None = None) -> Path | None:
    """Find a checkout containing the legacy ``babeldoc`` package."""

    current = (start or Path(__file__).resolve().parent).resolve()
    for _ in range(8):
        if (current / "babeldoc" / "tools" / "agent" / "__init__.py").is_file():
            return current
        if current.parent == current:
            break
        current = current.parent
    return None


def ensure_repo_importable() -> None:
    root = find_repo_root()
    if root is not None and str(root) not in sys.path:
        sys.path.insert(0, str(root))

__all__ = ["ToolResult", "dispatch", "ensure_repo_importable", "find_repo_root", "get_schema", "list_tools", "registry", "tool_names"]
