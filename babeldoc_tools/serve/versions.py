"""版本归档：编译产物的历史版本存档与读取（``docs/reference/http-api.md``，W12）。

每次**成功**的编译发布（:func:`babeldoc_tools.serve.compile.settle_compile` 的成功分支）
都把那一份 PDF 归档成一个版本：``<workdir>/.bdt-serve/versions/<revision>.pdf``
（同一个 revision 的产物只存一份），并在 ``<workdir>/.bdt-serve/versions.json`` 追加一行::

    {"version": 1, "items": [
      {"revision": 2, "created_at": "...", "trigger": "debounce",
       "artifact_name": "paper.mono.pdf", "bytes": 123456, "sha256_head": "…",
       "quality": {"check_verdict": "pass", "pipeline_ok": true}}]}

``revision`` 升序（清单是"当时发布顺序"的事实记录，倒序留给端点）。``trigger`` ∈
``debounce``（草稿保存后的服务端防抖编译）/ ``manual``（显式
``POST /jobs {action:"compile"}``）；``quality`` 是**发布时刻**的详情质量结论
（只记录不门禁：``needs_fix`` 的版本照样可下载，前端黄标）。``sha256_head`` 是版本的
**整文件** sha256（字段名沿用 brief 的冻结形状；理由见 :data:`SHA256_CHUNK_BYTES`）。

写入语义（原子性核对）：W09 的发布是 ``source.replace(output/<name>)`` —— 那一刻
``output/<name>`` 已经是完整的新产物（``os.replace`` 原子替换）。归档**在发布之后**
给同一份字节再加一个名字：优先 ``os.link``（硬链接，零拷贝、不移动文件、不重新读字节，
因此不影响发布的原子性；两个路径必然在同一个 workdir 里，同设备），不支持硬链接时
退回 ``shutil.copy2``。之后的发布用同名 ``os.replace`` 换掉 ``output/<name>`` 的 inode，
已归档的旧版本文件仍是当时那一份字节。

两条边界（红线）：

- **归档目录不进产物白名单**：``.bdt-serve/`` 是服务自己的状态目录，``GET /artifacts``
  的清单里永远没有它；版本 PDF 只能经 §3.7 的专用端点读（:func:`read_version_pdf`
  只认清单里有、且在 ``versions/`` 目录里的数字名文件）。
- **保留最近 50 版**：超出删最旧（文件 + 清单行）。被删的永远是最旧的，因此**不会**
  删掉当前 ``compile.revision`` 指向的那一版（当前 revision 是最大值）。

``compile.json`` 的 ``artifact`` 仍指向 ``output/`` 的最新发布（W09/W10 的下载主路径
一个字都不改），本模块只提供历史。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from babeldoc_tools.common import ToolError
from babeldoc_tools.serve.jobs import utc_now
from babeldoc_tools.serve.schemas import VersionItem
from babeldoc_tools.serve.schemas import VersionQuality
from babeldoc_tools.serve.store import STATE_DIR
from babeldoc_tools.serve.workdir import WorkdirReader

__all__ = [
    "MAX_VERSIONS",
    "SHA256_CHUNK_BYTES",
    "TRIGGER_DEBOUNCE",
    "TRIGGER_MANUAL",
    "TRIGGERS",
    "VERSIONS_DIR",
    "VERSIONS_FILE",
    "VERSIONS_VERSION",
    "VersionFile",
    "archive",
    "download_name",
    "list_versions",
    "manifest_path",
    "quality_snapshot",
    "read_manifest",
    "read_version_pdf",
    "sha256_file",
    "version_file",
    "versions_dir",
]

#: ``<workdir>/.bdt-serve/versions.json``（版本清单；服务端私有状态，不进 ``agent/``）。
VERSIONS_FILE = "versions.json"
#: 版本 PDF 的落地目录名（``<workdir>/.bdt-serve/versions/``）。
VERSIONS_DIR = "versions"
#: 清单结构版本（形状变了才 +1，与 api.md §3.7 解耦）。
VERSIONS_VERSION = 1
#: 保留的版本数上限（超出删最旧：文件 + 清单行）。
MAX_VERSIONS = 50
#: ``sha256_head`` 字段名沿用 brief 的冻结形状，但取的是**整个文件**的 sha256：
#: 实测（W12 真编译冒烟）同一文档两版的**前 19MB 完全相同**（差异从 19,250,981 字节处开始），
#: 只哈希头部根本区分不了版本，也就没法用它核对"下载到的就是当时那一份"。
#: 全文件哈希 65MB 只花 ~40ms（分块读，不把产物一次吃进内存），比一次真 build（~3 分钟）
#: 不值一提。
SHA256_CHUNK_BYTES = 1024 * 1024
#: 触发原因的两个取值（api.md §3.7）。词表在这里，响应模型 / job 记录用同一对字符串。
TRIGGER_DEBOUNCE = "debounce"
TRIGGER_MANUAL = "manual"
#: 合法取值集合（:func:`archive` 的入参校验）。
TRIGGERS = (TRIGGER_DEBOUNCE, TRIGGER_MANUAL)

#: 版本号路径段：只接受十进制数字（拒绝 ``-1`` / ``1.0`` / ``abc`` / 空）。
_REVISION_RE = re.compile(r"^[0-9]{1,10}$")

#: 清单写锁：清单是"读-改-写"，同一 workdir 的并发写会互相覆盖。写很小、每个版本一次，
#: 用一把模块级锁足够（读路径不加锁，靠 ``os.replace`` 的原子替换读到完整文件）。
_manifest_lock = threading.Lock()


def manifest_path(workdir: Path | str) -> Path:
    """``<workdir>/.bdt-serve/versions.json``。"""
    return Path(workdir) / STATE_DIR / VERSIONS_FILE


def versions_dir(workdir: Path | str) -> Path:
    """``<workdir>/.bdt-serve/versions/``（版本 PDF 的落地目录）。"""
    return Path(workdir) / STATE_DIR / VERSIONS_DIR


def version_file(workdir: Path | str, revision: int) -> Path:
    """某一版的归档文件路径（名字全由服务端拼：``<revision>.pdf``）。"""
    return versions_dir(workdir) / f"{revision}.pdf"


def sha256_file(path: Path | str, *, chunk: int = SHA256_CHUNK_BYTES) -> str:
    """整个文件的 sha256（十六进制小写）；分块读，不把几十 MB 的产物一次吃进内存。

    这是清单里 ``sha256_head`` 的取值（字段名沿用 brief 的冻结形状，语义见模块 docstring
    与 :data:`SHA256_CHUNK_BYTES` 的注释）："下载到的字节就是当时发布的那一份"必须能核对得动，
    而只哈希头部区分不了同一文档的不同版本。
    """
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def quality_snapshot(workdir: Path | str) -> dict[str, Any]:
    """发布时刻的 ``quality`` 快照（**只记录不门禁**：``needs_fix`` 照样可下载）。

    口径 = 详情端点当时会报的 ``quality``（:func:`babeldoc_tools.serve.views.quality_status`
    的同一实现）。这里**函数内** import ``views``：``views`` 在模块顶层 import
    :func:`babeldoc_tools.serve.compile.compile_status`，与 ``compile → versions``
    构成模块级循环（见 :mod:`babeldoc_tools.serve.compile` 的同类注释）。
    """
    from babeldoc_tools.serve import views  # noqa: PLC0415 - 打破 views ↔ compile 循环

    status = views.quality_status(WorkdirReader(workdir))
    return {"check_verdict": status.check.verdict, "pipeline_ok": status.pipeline_ok}


# --------------------------------------------------------------------------- #
# 清单读写
# --------------------------------------------------------------------------- #
def read_manifest(workdir: Path | str) -> dict[str, Any]:
    """读版本清单；缺失 / 坏 JSON / 形状不符 → 空清单（不谎报历史）。

    返回的 ``items`` 只保留是对象的行（形状是否**合法**由
    :func:`list_versions` 逐行判定）。
    """
    empty: dict[str, Any] = {"version": VERSIONS_VERSION, "items": []}
    try:
        payload = json.loads(manifest_path(workdir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return empty
    return {
        "version": VERSIONS_VERSION,
        "items": [item for item in payload["items"] if isinstance(item, dict)],
    }


def list_versions(workdir: Path | str) -> list[VersionItem]:
    """清单里**形状合法**的行，``revision`` 升序（一条坏行不该让整个列表读不出来）。"""
    parsed = [_parse_item(item) for item in read_manifest(workdir)["items"]]
    live = [item for item in parsed if item is not None]
    return sorted(live, key=lambda item: item.revision)


def _parse_item(raw: dict[str, Any]) -> VersionItem | None:
    """清单里的一行 → :class:`VersionItem`；形状不合法 / ``trigger`` 不认识 → ``None``。"""
    try:
        return VersionItem.model_validate(raw)
    except ValidationError:
        return None


def _write_manifest(workdir: Path | str, rows: list[dict[str, Any]]) -> None:
    """原子写清单（同目录 tmp + ``os.replace``）。"""
    path = manifest_path(workdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(
        json.dumps(
            {"version": VERSIONS_VERSION, "items": rows}, ensure_ascii=False, indent=2
        )
        + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def _as_revision(raw: dict[str, Any]) -> int | None:
    """清单行的 ``revision``；不是非负整数 → ``None``（该行写回时丢掉）。"""
    value = raw.get("revision")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


# --------------------------------------------------------------------------- #
# 归档（发布成功后调用）
# --------------------------------------------------------------------------- #
def archive(
    workdir: Path | str,
    revision: int,
    trigger: str,
    quality: dict[str, Any] | None = None,
    *,
    published: Path | str,
) -> VersionItem:
    """把刚发布的那份 PDF 归档成 ``revision`` 版；返回写进清单的那一行。

    - ``published`` 是**真 workdir 里**刚发布完的产物（``output/<name>``，字节已是最终态）；
    - ``trigger`` ∈ :data:`TRIGGER_DEBOUNCE` / :data:`TRIGGER_MANUAL`；
    - ``quality`` 缺省时现取（:func:`quality_snapshot`），调用方也可传快照进来（测试/复用）。

    防御性：同一个 ``revision`` 不该被发布两次（``revision`` 单调，草稿不变时再点一次
    手动编译才会同 r 再发一次），真出现时**覆盖文件并更新那一行**（清单按 ``revision``
    去重，最多一行）。归档失败由调用方兜（见 ``compile._archive_version``）：发布已经
    发生，归档是附带的账，不能反过来改编译结论。
    """
    if trigger not in TRIGGERS:
        raise ValueError(f"未知 trigger：{trigger!r}")
    number = _require_revision(revision)
    source = Path(published)
    entry = VersionItem(
        revision=number,
        created_at=utc_now(),
        trigger=trigger,
        artifact_name=source.name,
        bytes=source.stat().st_size,
        sha256_head=sha256_file(source),
        quality=VersionQuality.model_validate(
            quality if quality is not None else quality_snapshot(workdir)
        ),
    )
    target = version_file(workdir, number)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.exists():
        target.unlink()
    try:
        os.link(source, target)
    except OSError:
        # 同一 workdir 内必然同设备，硬链接理论上不会失败；兜底走拷贝（慢但正确）。
        shutil.copy2(source, target)

    with _manifest_lock:
        rows = [
            row
            for row in read_manifest(workdir)["items"]
            if _as_revision(row) not in (None, number)
        ]
        rows.append(entry.model_dump())
        rows.sort(key=lambda row: _as_revision(row) or 0)
        # 超出上限只删最旧的；当前 revision 是最大值，因此永远不会被淘汰。
        dropped = rows[:-MAX_VERSIONS] if len(rows) > MAX_VERSIONS else []
        _write_manifest(workdir, rows[len(dropped) :])
        for row in dropped:
            with contextlib.suppress(OSError):
                version_file(workdir, _as_revision(row) or 0).unlink()
    return entry


# --------------------------------------------------------------------------- #
# 读取（§3.7 的下载端点）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class VersionFile:
    """一份历史版本的落盘事实：清单行 + 解析后的绝对路径。"""

    #: 清单里的那一行（端点据此给 ``Content-Disposition`` 的文件名）。
    item: VersionItem
    #: 解析（``Path.resolve()``）后的绝对路径，仍在 ``versions/`` 目录内。
    path: Path


def read_version_pdf(workdir: Path | str, revision: str | int) -> VersionFile:
    """``versions/<r>.pdf``：清单行 + 绝对路径；不满足一律 404 ``version_not_found``。

    只认**清单里有、并且确实落在 ``versions/`` 目录里的数字名文件**：非数字 / 负数 /
    清单里没有 / 文件缺失 / 符号链接越界都是同一个 404（不泄露存在性，与
    :mod:`babeldoc_tools.serve.artifacts` 同一口径）。
    """
    workdir = Path(workdir)
    number = _parse_revision(revision)
    if number is None:
        raise _not_found(revision)
    item = next((row for row in list_versions(workdir) if row.revision == number), None)
    if item is None:
        raise _not_found(revision)
    try:
        resolved = version_file(workdir, number).resolve()
        inside = resolved.parent == versions_dir(workdir).resolve()
        exists = resolved.is_file()
    except OSError:  # 破符号链接 / 读到一半被删
        raise _not_found(revision) from None
    if not inside or not exists:
        raise _not_found(revision)
    return VersionFile(item=item, path=resolved)


def download_name(item: VersionItem) -> str:
    """服务器给出的落盘名：``<原产物名去 .pdf>.r<r>.pdf``（与前端 W10 的改名同一形状）。"""
    stem = item.artifact_name
    if stem.lower().endswith(".pdf"):
        stem = stem[: -len(".pdf")]
    return f"{stem}.r{item.revision}.pdf"


def _require_revision(revision: Any) -> int:
    """归档路径的 revision 校验（服务端内部值：必须是非负整数）。"""
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError(f"revision 必须是非负整数：{revision!r}")
    return revision


def _parse_revision(raw: str | int) -> int | None:
    """路径段 → 版本号；不是十进制非负整数 → ``None``（调用方报 404）。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    if isinstance(raw, str) and _REVISION_RE.match(raw):
        return int(raw)
    return None


def _not_found(raw: Any) -> ToolError:
    """越界 / 不在清单 / 不存在用同一个错误（不泄露存在性）。"""
    return ToolError(
        "version_not_found",
        f"版本不在归档里或不存在：{raw}",
        revision=str(raw),
    )
