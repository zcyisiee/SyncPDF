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

__version__ = "0.1.0"
