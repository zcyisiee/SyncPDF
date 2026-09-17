"""``bdt serve`` 的响应模型与公共契约词表。

只有**已实现**端点才在这里建模（W01 只有 ``/api/v1/health``）；后续端点的
字段形状冻结在 ``docs/frontend/api.md``，实现时再落到本模块 —— 不预置假 stub。

HTTP 层约定（与 ``bdt`` CLI 的 stdout 信封不同）：

- 成功：直接返回资源 JSON（无 ``{"ok": true}`` 包装）；
- 失败：:class:`ErrorEnvelope`，即 ``{"error": {"code", "message", "detail"?}}``，
  其中 ``detail`` 缺省不出现。
"""

from __future__ import annotations

from typing import Any
from typing import Literal

from pydantic import BaseModel

#: 全部端点前缀（唯一拼写来源：路由、CLI banner、api.md）。
API_PREFIX = "/api/v1"

__all__ = [
    "API_PREFIX",
    "ErrorBody",
    "ErrorEnvelope",
    "HealthResponse",
]


class ErrorBody(BaseModel):
    """机读错误：``code`` 稳定可分支，``message`` 面向人。"""

    code: str
    message: str
    detail: dict[str, Any] | None = None


class ErrorEnvelope(BaseModel):
    """所有非 2xx 响应的统一形状。"""

    error: ErrorBody


class HealthResponse(BaseModel):
    """``GET /api/v1/health``：进程存活 + 当前可见文档数。"""

    status: Literal["ok"] = "ok"
    api: str = API_PREFIX
    #: ``bdt`` 工具层版本（与 ``bdt --version`` 相同）。
    version: str
    #: ``root`` = 枚举子目录；``workdir`` = 只公开 --workdir 那一个文档。
    mode: Literal["root", "workdir"]
    #: 实际被枚举的根目录（绝对路径，已 resolve）。
    root: str
    documents: int
