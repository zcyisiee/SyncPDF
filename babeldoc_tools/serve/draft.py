"""草稿读写：``<workdir>/.bdt-serve/draft.json``（``docs/frontend/api.md`` §3.3，W09）。

草稿是"改译文 / 调排版"的唯一写入口，形状冻结在 §3.3::

    {"revision": 3, "updated_at": "...", "paragraphs": {"P05-002": {...}}}

三条不可妥协的规则：

- **revision 单调不回退**：冷启动（文件不存在/损坏）从 0 开始，每次成功写入 +1，
  删除（``DELETE /draft``）也 +1 —— 前端靠它判断"编译后的草稿又变了"（stale）；
- **乐观并发**：写请求带 ``base_revision``，与当前不符 → ``409 revision_conflict``
  （``detail.current_revision``），两个标签页不会互相覆盖；
- **校验复用排版覆盖的既有逻辑**：段落 id 形状与 layout 键名/范围直接调
  :mod:`babeldoc.tools.agent.layout_overrides` 的 ``_ID_RE`` / ``validate``，
  不在这里复制魔法数字（否则两处范围迟早漂移）。

写盘是**同目录 tmp + ``os.replace``** 的原子替换：读方（详情端点、编译流程）永远
看到完整 JSON。``layout`` 是**覆盖层**：编译时叠加在工作区既有的
``agent/layout_overrides.json`` 之上（未涉及的段落保持原样，见
:mod:`babeldoc_tools.serve.compile`）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

from babeldoc.tools.agent import layout_overrides
from pydantic import BaseModel
from pydantic import ValidationError

from babeldoc_tools.common import ToolError
from babeldoc_tools.serve.jobs import utc_now
from babeldoc_tools.serve.store import STATE_DIR

if TYPE_CHECKING:
    from babeldoc_tools.serve.store import DocumentStore

__all__ = [
    "DRAFT_FILE",
    "DRAFT_VERSION",
    "DraftDoc",
    "DraftParagraph",
    "DraftRegistry",
    "DraftResponse",
    "DraftStore",
    "PARAGRAPH_ID_RE",
    "draft_path",
    "read_draft",
    "validate_changes",
]

#: 草稿文件名（放在 workdir 的 ``.bdt-serve/`` 下，与 job 状态同级，不进 ``agent/``）。
DRAFT_FILE = "draft.json"
#: 草稿文件结构版本（与 api.md §3.3 的形状解耦：形状变了才 +1）。
DRAFT_VERSION = 1

#: 段落 id 形状：与 ``layout_overrides`` 同一个正则对象（不是复制一份）。
PARAGRAPH_ID_RE = layout_overrides._ID_RE  # noqa: SLF001 - 就是这个模块的校验来源

#: 段落条目里允许出现的字段（其余 → 422 ``draft_invalid``，不静默忽略）。
_DRAFT_FIELDS = ("target", "layout")


def draft_path(workdir: Path | str) -> Path:
    """``<workdir>/.bdt-serve/draft.json``。"""
    return Path(workdir) / STATE_DIR / DRAFT_FILE


class DraftParagraph(BaseModel):
    """一段草稿：译文覆盖 + 排版覆盖（两者都可缺）。"""

    #: 该段译文（完整段落文本）；``None`` = 没有覆盖，用 ``translated.jsonl`` 的值。
    target: str | None = None
    #: 段落排版覆盖（键名/范围同 ``layout_overrides``）；``None`` = 没有覆盖。
    layout: dict[str, Any] | None = None
    updated_at: str | None = None


class DraftDoc(BaseModel):
    """草稿文档（落盘形状；``version`` 只在文件里，不下发）。"""

    version: int = DRAFT_VERSION
    revision: int = 0
    updated_at: str | None = None
    paragraphs: dict[str, DraftParagraph] = {}


class DraftResponse(BaseModel):
    """``GET/PATCH/DELETE /documents/{did}/draft`` 的响应体（api.md §3.3 冻结形状）。"""

    revision: int = 0
    updated_at: str | None = None
    paragraphs: dict[str, DraftParagraph] = {}


def read_draft(workdir: Path | str) -> DraftDoc:
    """读草稿；缺失 / 坏 JSON / 形状不符 → 空草稿（``revision=0``，不谎报历史）。

    容错是刻意的：草稿文件坏了不该让详情/编译端点 500 —— 但**不**回退到某个旧的
    内存副本（那会让"读到的 revision"与磁盘不一致，破坏乐观并发）。
    """
    path = draft_path(workdir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DraftDoc()
    if not isinstance(payload, dict):
        return DraftDoc()
    try:
        return DraftDoc.model_validate(payload)
    except ValidationError:
        return DraftDoc()


def validate_changes(paragraphs: Any) -> dict[str, dict[str, Any] | None]:
    """校验 PATCH 的 ``paragraphs``，返回规范化后的 ``{pid: 补丁 | None}``。

    ``None`` 出现在两个层级：条目级（``{"P05-002": null}`` = 整段删除）与字段级
    （``{"layout": null}`` = 删掉该字段）。不合法 → ``ToolError(draft_invalid)``，
    ``detail.errors`` 是逐条人话（字段名带路径）。
    """
    if not isinstance(paragraphs, dict):
        raise ToolError(
            "draft_invalid",
            "paragraphs 必须是对象",
            errors=["paragraphs 必须是对象（{段落 id: 补丁}）"],
        )
    errors: list[str] = []
    out: dict[str, dict[str, Any] | None] = {}
    for raw_pid, entry in paragraphs.items():
        pid = str(raw_pid)
        if not PARAGRAPH_ID_RE.match(pid):
            errors.append(f"paragraphs.{pid}: 非法的段落 id（[A-Za-z0-9._-]{{1,64}}）")
            continue
        if entry is None:
            out[pid] = None
            continue
        if not isinstance(entry, dict):
            errors.append(f"paragraphs.{pid}: 必须是对象或 null（null = 整段删除）")
            continue
        unknown = sorted(set(entry) - set(_DRAFT_FIELDS))
        if unknown:
            errors.append(f"paragraphs.{pid}: 未知字段 {unknown}")
        patch: dict[str, Any] = {}
        if "target" in entry:
            target = entry["target"]
            if target is not None and not isinstance(target, str):
                errors.append(f"paragraphs.{pid}.target: 必须是字符串或 null")
            else:
                patch["target"] = target
        if "layout" in entry:
            layout = entry["layout"]
            if layout is None:
                patch["layout"] = None
            elif not isinstance(layout, dict):
                errors.append(f"paragraphs.{pid}.layout: 必须是对象或 null")
            else:
                # 复用 layout_overrides 的键名/范围校验（含 box 四元数组与列表键）。
                errors.extend(
                    layout_overrides.validate(
                        {
                            "version": layout_overrides.VERSION,
                            "paragraphs": {pid: layout},
                        },
                        allow_none=True,
                    )
                )
                patch["layout"] = layout
        out[pid] = patch
    if errors:
        raise ToolError("draft_invalid", "草稿字段不合法", errors=errors)
    return out


class DraftStore:
    """一个 workdir 的草稿读写（``asyncio.Lock`` 串行化写，读不加锁）。

    per-workdir 而不是全局：两个文档的草稿互不阻塞（EXECUTION.md 纠偏 7 只要求
    同文档串行）。锁只在**写**路径上；读路径靠原子替换保证"永远读到完整文件"。
    """

    def __init__(self, workdir: Path | str) -> None:
        self.workdir = Path(workdir)
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        """草稿文件路径。"""
        return draft_path(self.workdir)

    def read(self) -> DraftDoc:
        """同步读（容错），见 :func:`read_draft`。"""
        return read_draft(self.workdir)

    def _write(self, doc: DraftDoc) -> None:
        """原子写（同目录 tmp + ``os.replace``）。"""
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(
            json.dumps(doc.model_dump(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)

    async def get(self) -> DraftDoc:
        """当前草稿（缺文件 → 空草稿 ``revision=0``）。"""
        async with self._lock:
            return self.read()

    async def patch(self, *, base_revision: int, paragraphs: Any) -> DraftDoc:
        """应用补丁：``base_revision`` 校验 → 合并 → ``revision+1`` → 原子落盘。

        校验在拿锁**之前**做（纯函数，失败不必排队）；``revision_conflict`` 在锁内
        判（读-改-写必须原子，否则两个并发 PATCH 会都通过校验）。
        """
        changes = validate_changes(paragraphs)
        async with self._lock:
            doc = self.read()
            if base_revision != doc.revision:
                raise ToolError(
                    "revision_conflict",
                    f"草稿已被其它会话改动：base_revision={base_revision}，"
                    f"当前 revision={doc.revision}",
                    current_revision=doc.revision,
                    base_revision=base_revision,
                )
            now = utc_now()
            for pid, entry in changes.items():
                if entry is None:
                    doc.paragraphs.pop(pid, None)
                    continue
                para = doc.paragraphs.get(pid) or DraftParagraph()
                if "target" in entry:
                    para.target = entry["target"]
                if "layout" in entry:
                    para.layout = entry["layout"]
                if para.target is None and para.layout is None:
                    doc.paragraphs.pop(pid, None)  # 两个字段都被删 = 整段没覆盖
                    continue
                para.updated_at = now
                doc.paragraphs[pid] = para
            doc.revision += 1
            doc.updated_at = now
            self._write(doc)
            return doc

    async def delete(self) -> DraftDoc:
        """清空全部段落覆盖；``revision`` 继续 +1（**不回退**）。"""
        async with self._lock:
            doc = self.read()
            doc.paragraphs = {}
            doc.revision += 1
            doc.updated_at = utc_now()
            self._write(doc)
            return doc


class DraftRegistry:
    """``did`` → :class:`DraftStore` 缓存（同一个 did 永远拿到同一个锁对象）。"""

    def __init__(self, store: DocumentStore) -> None:
        self._store = store
        self._stores: dict[str, DraftStore] = {}

    def for_did(self, did: str) -> DraftStore:
        """该 did 的草稿存储（did 越界/不存在 → ``ToolError``，与其它端点同码）。"""
        workdir = self._store.resolve(did)
        store = self._stores.get(did)
        if store is None:
            store = DraftStore(workdir)
            self._stores[did] = store
        return store
