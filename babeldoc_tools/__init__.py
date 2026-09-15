"""babeldoc_tools：agent 面向的文档翻译工具包（JSON in / JSON out）。

定位：把 BabelDOC 的 agent 工作流（解析 / 翻译 / 审查 / 重建 / 排版微调）
包成**稳定、可机读、无副作用惊喜**的工具，供编排者（主 Agent，或后续的
MCP wrapper）调用。

- CLI 入口是 ``bdt``（= ``python -m babeldoc_tools``），子命令固定为
  ``parse`` / ``translate`` / ``apply`` / ``build`` / ``check`` /
  ``layout-set`` / ``report``；stdout 恒为单行 JSON，错误走
  ``{"ok": false, "error": {...}}``。
- 统一信封由 :mod:`babeldoc_tools.registry` 的 ``invoke`` 提供。
- 工具实现尽量薄：核心逻辑在 ``babeldoc.tools.agent``（同仓可测试），
  这里只做参数编排、落盘与报告。
"""

from __future__ import annotations

__version__ = "0.1.0"
