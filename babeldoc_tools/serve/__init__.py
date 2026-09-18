"""``bdt serve``：把现有 workdir 产物以只读 HTTP 接口暴露给 Web 前端。

- :mod:`babeldoc_tools.serve.store`：根目录解析与路径白名单（安全边界）。
- :mod:`babeldoc_tools.serve.schemas`：响应模型与 ``/api/v1`` 前缀。
- :mod:`babeldoc_tools.serve.app`：FastAPI app factory（需 web extra）。
- :mod:`babeldoc_tools.serve.cli`：``bdt serve`` 的参数与启动流程（不需 web extra）。

契约单一事实来源是 ``docs/reference/http-api.md``；HTTP 形状以运行中服务的
``/openapi.json`` 为准。本包不新增对外 CLI：入口仍是 ``bdt serve``。
"""

from __future__ import annotations

__all__: list[str] = []
