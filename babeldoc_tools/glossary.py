"""术语表：CSV 编解码 + 校验 + 提示词渲染（W13）。**CLI 与 serve 共用这一份**。

术语表是「源词 → 指定译名（可选备注）」的有序对表。对外只有两种形状：

- **CSV**（唯一落盘/交换格式）：列 ``source,target,note``；``note`` 可缺省，表头必须有
  ``source`` 与 ``target``；
- **JSON 条目**：``{"source": str, "target": str, "note": str | None}``
  （``note`` 为 ``None``/空串 = 没有备注，HTTP 响应里就是 ``null``）。

两个使用方：

- ``bdt translate --glossaries <csv>`` / ``bdt run --glossaries <csv>``：翻译阶段读这个
  CSV，把术语约束渲染进提示词（:func:`render_prompt_block`）；
- ``bdt serve`` 的 ``/glossary``：把同一个 CSV 写在 ``<store_base>/.bdt-serve/glossary.csv``，
  翻译 job 时把**文件路径**经 ``--glossaries`` 传给子进程 —— 注入只发生在服务端 argv 层，
  客户端永远只给 ``use_glossary`` 布尔。

校验只在 :func:`normalize_entries` 里做一次，两边共用：source/target 去空白后非空、
长度上限内；**同一个 source 后者覆盖前者**；结果按 source 排序 —— 因此同一次翻译渲染进
提示词的条目顺序与 ``GET /glossary`` 返回的顺序一致、可复现。
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from babeldoc_tools.common import ToolError

__all__ = [
    "CSV_FIELDS",
    "MAX_ENTRIES",
    "MAX_NOTE_CHARS",
    "MAX_SOURCE_CHARS",
    "MAX_TARGET_CHARS",
    "GlossaryEntry",
    "load_entries",
    "normalize_entries",
    "parse_csv",
    "render_csv",
    "render_prompt_block",
]

#: CSV 列顺序（读时只强制前两列，写时恒写三列）。
CSV_FIELDS = ("source", "target", "note")

#: 单条字段的字符上限（校验用；防止一个粘贴事故把提示词撑爆）。
MAX_SOURCE_CHARS = 200
MAX_TARGET_CHARS = 200
MAX_NOTE_CHARS = 200
#: 整表条目上限。
MAX_ENTRIES = 2000

#: 提示词里词表段的标题（渲染块的第一行；空词表时整段不出现）。
PROMPT_HEADING = "## 术语约束（词表）"


@dataclass(frozen=True)
class GlossaryEntry:
    """一条术语对。

    ``note`` 是给人看的备注（如「首字母小写」「按厂商写法」），渲染时括注在译名后，
    不参与匹配。
    """

    source: str
    target: str
    note: str | None = None


def parse_csv(text: str) -> list[GlossaryEntry]:
    """把 CSV 文本解析成条目（**不做**校验/去重/排序，见 :func:`normalize_entries`）。

    表头必须含 ``source``/``target``；缺列或 CSV 结构坏掉 → ``glossary_invalid``。
    """
    try:
        reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
        fields = reader.fieldnames or []
        if "source" not in fields or "target" not in fields:
            raise ToolError(
                "glossary_invalid",
                "词表 CSV 必须含 source/target 两列（可选第三列 note）",
                fields=fields,
            )
        entries = [
            GlossaryEntry(
                source=(row.get("source") or "").strip(),
                target=(row.get("target") or "").strip(),
                note=((row.get("note") or "").strip() or None),
            )
            for row in reader
        ]
    except ToolError:
        raise
    except (csv.Error, UnicodeDecodeError) as exc:
        raise ToolError(
            "glossary_invalid", f"词表 CSV 解析失败：{exc}"
        ) from exc
    return entries


def render_csv(entries: Iterable[GlossaryEntry]) -> str:
    """条目 → CSV 文本（恒写 ``source,target,note`` 表头；``\\n`` 行尾）。"""
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=list(CSV_FIELDS), lineterminator="\n", extrasaction="ignore"
    )
    writer.writeheader()
    for entry in entries:
        writer.writerow(
            {"source": entry.source, "target": entry.target, "note": entry.note or ""}
        )
    return buffer.getvalue()


def normalize_entries(entries: Iterable[GlossaryEntry]) -> list[GlossaryEntry]:
    """校验 + 去重 + 排序，返回可落盘/可注入的规范表。

    规则（serve 的 ``PUT /glossary`` 与 CLI 的 ``--glossaries`` 共用）：

    - source/target 去空白后**必须非空**，且各自不超过长度上限（note 可空、有则限长）；
    - **同 source 后者覆盖前者**（同一次 PUT 里重复出现的 source 以最后一条为准）；
    - 结果按 source 字典序排序；
    - 任何一条不合法 → ``glossary_invalid``（带 ``index``/``field``，便于前端定位行）。
    """
    cleaned: dict[str, GlossaryEntry] = {}
    for index, raw in enumerate(entries):
        source = (raw.source or "").strip()
        target = (raw.target or "").strip()
        note = (raw.note or "").strip() or None
        _require(index, "source", source, MAX_SOURCE_CHARS)
        _require(index, "target", target, MAX_TARGET_CHARS)
        if note is not None and len(note) > MAX_NOTE_CHARS:
            raise ToolError(
                "glossary_invalid",
                f"第 {index + 1} 条 note 超过 {MAX_NOTE_CHARS} 字符",
                index=index,
                field="note",
            )
        cleaned[source] = GlossaryEntry(source=source, target=target, note=note)
    if len(cleaned) > MAX_ENTRIES:
        raise ToolError(
            "glossary_invalid",
            f"词表条目数超过上限 {MAX_ENTRIES}",
            limit=MAX_ENTRIES,
            count=len(cleaned),
        )
    return [cleaned[source] for source in sorted(cleaned)]


def load_entries(path: str | Path) -> list[GlossaryEntry]:
    """读一个词表 CSV（CLI 的 ``--glossaries``）。文件不存在 → ``glossary_missing``。

    读回来仍然过 :func:`normalize_entries`：手工编辑过的 CSV（空行、重复 source）不该
    让提示词里出现空术语或重复项。
    """
    candidate = Path(path)
    if not candidate.is_file():
        raise ToolError(
            "glossary_missing", f"词表文件不存在：{candidate}", path=str(candidate)
        )
    try:
        text = candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ToolError(
            "glossary_invalid", f"词表文件读不了：{candidate}（{exc}）"
        ) from exc
    return normalize_entries(parse_csv(text))


def render_prompt_block(entries: Iterable[GlossaryEntry]) -> str:
    """渲染提示词里的词表段；**空表返回空串**（调用方据此整行省略占位符）。

    每行一条 ``- source → target``（有 note 则紧跟在译名后括注）。返回值以单个换行结尾，
    由 :func:`babeldoc_tools.common.load_prompt` 替换模板里的 ``{glossary}`` 占位行。
    """
    items = list(entries)
    if not items:
        return ""
    lines = [
        PROMPT_HEADING,
        "",
        "下表给出**强制**的「源词 → 指定译名」。译文中出现源词时必须使用指定译名"
        "（含标题、图注、表注）；词表未覆盖的词照常自然翻译；同一术语全篇一致。",
        "",
    ]
    for entry in items:
        note = f"（{entry.note}）" if entry.note else ""
        lines.append(f"- {entry.source} → {entry.target}{note}")
    lines.append("")
    return "\n".join(lines)


def _require(index: int, field: str, value: str, limit: int) -> None:
    """单字段校验：非空 + 长度上限（空值单独给消息，前端能区分"漏填"与"太长"）。"""
    if not value:
        raise ToolError(
            "glossary_invalid",
            f"第 {index + 1} 条的 {field} 不能为空",
            index=index,
            field=field,
        )
    if len(value) > limit:
        raise ToolError(
            "glossary_invalid",
            f"第 {index + 1} 条的 {field} 超过 {limit} 字符",
            index=index,
            field=field,
        )
