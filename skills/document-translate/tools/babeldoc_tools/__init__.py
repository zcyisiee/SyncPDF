"""babeldoc_tools：agent 面向的文档翻译工具包（JSON in / JSON out）。

定位：把 BabelDOC 的 agent 工作流（解析 / 翻译 / 审查 / 重建 / 排版微调）
包成**稳定、可机读、无副作用惊喜**的工具，供编排者（主 Agent，或后续的
MCP wrapper）调用。

- 每个工具输入一个 JSON 对象，返回一个 JSON 对象；错误不抛栈，走
  ``{"ok": false, "error": {...}}``。
- 注册表 + 派发器在 :mod:`babeldoc_tools.registry`，CLI 只是薄壳：
  ``python -m babeldoc_tools list | schema <tool> | call <tool> --args-json '{...}'``。
- 工具实现尽量薄：核心逻辑在 ``babeldoc.tools.agent``（同仓可测试），
  这里只做参数校验、编排、落盘与报告。
"""

from __future__ import annotations

import sys
from pathlib import Path

__version__ = "0.1.0"


def find_repo_root(start: Path | None = None) -> Path | None:
    """向上查找包含 ``babeldoc/tools/agent/`` 的仓库根目录。

    包位置：``<repo>/skills/document-translate/tools/babeldoc_tools/``；
    也兼容 pip 安装后（此时向上找不到 marker，返回 None）。
    """
    current = (start or Path(__file__).resolve().parent).resolve()
    for _ in range(6):
        if (current / "babeldoc" / "tools" / "agent" / "__init__.py").is_file():
            return current
        if current.parent == current:
            break
        current = current.parent
    return None


def ensure_repo_importable() -> None:
    """确保能 import babeldoc：未安装时把仓库根目录加进 sys.path。"""
    try:
        import babeldoc  # noqa: F401

        return
    except ImportError:
        pass
    repo_root = find_repo_root()
    if repo_root is not None and str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


ensure_repo_importable()
