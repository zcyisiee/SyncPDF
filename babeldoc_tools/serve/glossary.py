"""全局词表存储（``docs/reference/http-api.md``，W13）。

**一个全局词表**（不做多词表管理）：``<store_base>/.bdt-serve/glossary.csv``
（``DocumentStore.store_base`` 见 :mod:`babeldoc_tools.serve.store`）。它**不属于任何
workdir** —— 同一个 serve 下的所有文档共用这一份，翻译时由服务端把它以
``--glossaries <path>`` 注入子进程的 argv（客户端永远只给 ``use_glossary`` 布尔）。

CSV 编解码/校验与 CLI 共用 :mod:`babeldoc_tools.glossary`（**不写第二份解析器**）：
列 ``source,target,note``，同 source 后者覆盖前者、按 source 排序。这里只负责**落盘**：
同目录 tmp + ``os.replace`` 原子写 + 进程内写锁。

坏文件（手工改坏/半截写入）按"空词表"读：注入少几个术语比让整个服务起不来安全
（与 :mod:`babeldoc_tools.serve.profiles` 对坏 ``profiles.json`` 的口径一致）。
"""

from __future__ import annotations

import threading
from pathlib import Path

from babeldoc_tools.common import ToolError
from babeldoc_tools.glossary import GlossaryEntry
from babeldoc_tools.glossary import normalize_entries
from babeldoc_tools.glossary import parse_csv
from babeldoc_tools.glossary import render_csv
from babeldoc_tools.serve.store import STATE_DIR

__all__ = ["GLOSSARY_FILE", "GlossaryStore"]

#: 词表文件名（放在 ``<store_base>/<STATE_DIR>/`` 下）。
GLOSSARY_FILE = "glossary.csv"

#: 写锁（模块级：一个进程里所有 :class:`GlossaryStore` 共用；同 :mod:`babeldoc_tools.serve.versions`）。
#: HTTP 处理函数在线程池里跑，整表替换必须串行，否则并发 PUT 会互相盖掉。
_write_lock = threading.Lock()


class GlossaryStore:
    """全局词表的读写（路径 + 原子写 + 锁；不可变，一个 ``bdt serve`` 一个实例）。"""

    def __init__(self, store_base: Path | str) -> None:
        self.store_base = Path(store_base)

    @property
    def path(self) -> Path:
        """``<store_base>/.bdt-serve/glossary.csv``。"""
        return self.store_base / STATE_DIR / GLOSSARY_FILE

    # ---------------------------------------------------------------- 读
    def read(self) -> list[GlossaryEntry]:
        """当前词表（已规范化）。文件不存在 / 读不动 / 解析失败 → ``[]``（不抛）。"""
        path = self.path
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return []
        try:
            return normalize_entries(parse_csv(text))
        except ToolError:
            # 坏 CSV（表头缺列/字段非法）当空词表：注入少几条比让每个 PUT 都 500 强。
            return []

    def count(self) -> int:
        """条目数（``GET /glossary`` 的 ``count``）。"""
        return len(self.read())

    def injection_path(self) -> str | None:
        """翻译 job 该注入的词表**文件路径**；词表为空 → ``None``（不注入）。

        空词表不注入是硬规则：``--glossaries <空表>`` 只会给提示词塞一段没有条目的
        约束说明，毫无收益还多一处不一致的可能。
        """
        path = self.path
        if not path.is_file():
            return None
        return str(path) if self.read() else None

    # ---------------------------------------------------------------- 写
    def replace(self, entries) -> list[GlossaryEntry]:
        """整表替换（校验 → 规范化 → 原子写），返回落盘后的条目。

        校验失败（空 source/target、超长、超条数上限）→ ``ToolError(glossary_invalid)``，
        **盘上一字不改**（先规范化再写）。空表也照写：文件变成只有表头的 CSV，
        于是 :meth:`injection_path` 返回 ``None``（语义 = 没有词表）。
        """
        normalized = normalize_entries(entries)
        payload = render_csv(normalized)
        path = self.path
        with _write_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(path)
        return normalized

    def clear(self) -> None:
        """清空词表（删文件；幂等）。"""
        path = self.path
        with _write_lock:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
