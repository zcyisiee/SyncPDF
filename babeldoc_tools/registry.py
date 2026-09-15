"""工具调用封装：统一把实现函数的返回/异常转成机读 JSON。

子命令在 :mod:`babeldoc_tools.__main__` 里写死（不再有注册表 / JSON Schema /
工具发现）。本模块只做一件事：

- 成功 → ``{"ok": true, "data": <实现函数返回值>}``
- :class:`babeldoc_tools.common.ToolError` → ``{"ok": false, "error": {"code",
  "message", ...extra}}``
- 其它异常 → ``{"ok": false, "error": {"code": "tool_exception", ...}}``
- 实现函数显式返回 ``{"ok": false, ...}`` → 提升成标准错误结构

约定：实现函数是普通 Python 函数，入参为显式关键字参数（不再是 ``args: dict``）。
"""

from __future__ import annotations

import traceback
from collections.abc import Callable
from typing import Any

from babeldoc_tools.common import ToolError

__all__ = ["ToolError", "error_payload", "invoke"]


def error_payload(code: str, message: str, **extra) -> dict:
    """构造 ``{"ok": false, "error": {...}}``。"""
    error = {"code": code, "message": message}
    error.update(extra)
    return {"ok": False, "error": error}


def invoke(fn: Callable[..., Any], **kwargs) -> dict:
    """调用实现函数并包装成标准 JSON 信封。"""
    try:
        data = fn(**kwargs)
    except ToolError as exc:
        return error_payload(exc.code, exc.message, **exc.extra)
    except Exception as exc:  # noqa: BLE001 - 统一转成机读错误
        return error_payload(
            "tool_exception",
            f"{type(exc).__name__}: {exc}",
            traceback=traceback.format_exc(limit=8).splitlines()[-8:],
        )
    if isinstance(data, dict) and data.get("ok") is False:
        error = data.get("error") or {
            "code": "tool_failed",
            "message": str(data.get("message") or "工具返回 ok=false"),
        }
        return {"ok": False, "error": error}
    return {"ok": True, "data": data}
