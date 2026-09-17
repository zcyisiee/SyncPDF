"""可下载产物白名单与路径判定（``docs/frontend/api.md`` §1.5）。

前端能下载的 workdir 产物只有三处：

- ``output/*.pdf`` —— 编译产物（mono / dual），pdf.js 用 Range 请求按需取字节；
- ``agent/*.md`` / ``agent/*.json`` / ``agent/*.jsonl`` —— 译文与几何/检查产物；
- workdir 根下的 ``FINAL_REPORT.md``（kind=report）与 ``source.pdf``（kind=source）。

白名单不是"这些目录里的随便什么文件"，而是"这些目录里的这些形态"：判定只有
:func:`classify` 一处，清单（:func:`list_artifacts`）与下载（:func:`resolve_artifact`）
共用同一份规则，避免两套规则漂移。因此 ``agent/state.pkl``、``agent/source/``、
``debug/``（run 归档、临时 PDF）以及根下的 run 工作目录既不进清单也不可下载 ——
它们要么无前端用途，要么体积过大。

安全边界（与 :mod:`babeldoc_tools.serve.store` 的 did 校验同一个思路，这里是文件级）：

- 名字必须是**单层目录 + 单层文件名**或根级白名单文件名，所以 ``..``、绝对路径、
  ``a/b/c``、``\\``、NUL 在碰盘之前就被拒；
- 解析（``Path.resolve()``）后的父目录必须仍是 workdir 内的那个白名单目录，
  因此符号链接越界（``output/x.pdf -> /etc/hosts``）与白名单目录本身是指向外部的
  符号链接都被拒；
- 拒绝与非白名单一律报同一个 ``artifact_not_found``（404），不泄露存在性。

本模块只读（``iterdir`` / ``stat``），不读文件内容、不创建任何文件。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath

from babeldoc_tools.common import ToolError

__all__ = [
    "DIR_SUFFIX_KINDS",
    "ROOT_FILE_KINDS",
    "ArtifactEntry",
    "classify",
    "list_artifacts",
    "media_type",
    "resolve_artifact",
]

#: workdir 根下唯一可下载的两个单文件 → kind。
ROOT_FILE_KINDS: dict[str, str] = {
    "FINAL_REPORT.md": "report",
    "source.pdf": "source",
}

#: 白名单子目录 → 扩展名 → kind（**不递归**子目录）。
DIR_SUFFIX_KINDS: dict[str, dict[str, str]] = {
    "agent": {".md": "markdown", ".json": "json", ".jsonl": "json"},
    "output": {".pdf": "pdf"},
}

#: kind → 下载响应的 ``Content-Type``（pdf.js 只依赖 pdf 的 Range，其余由前端按 kind 处理）。
_MEDIA_TYPES: dict[str, str] = {
    "pdf": "application/pdf",
    "source": "application/pdf",
    "markdown": "text/markdown; charset=utf-8",
    "report": "text/markdown; charset=utf-8",
    "json": "application/json",
}


@dataclass(frozen=True)
class ArtifactEntry:
    """清单里的一件产物（事实来自一次 ``stat``；不读内容）。"""

    #: 稳定短名 = workdir 相对路径（如 ``output/paper.mono.pdf``），同时是下载键。
    name: str
    #: ``pdf`` | ``markdown`` | ``json`` | ``report`` | ``source``。
    kind: str
    #: 解析后的绝对路径（白名单内）；只给服务端下载用，不下发。
    path: Path
    size: int
    #: 文件 mtime，UTC ISO8601（毫秒 + ``Z``，api.md §1 的时间约定）。
    mtime: str


def classify(name: str) -> str | None:
    """白名单名字 → kind；不在白名单内 → ``None``（不碰盘）。"""
    if "\\" in name or "\x00" in name:
        return None
    if name in ROOT_FILE_KINDS:
        return ROOT_FILE_KINDS[name]
    parts = name.split("/")
    if len(parts) != 2:
        return None
    dirname, filename = parts
    suffix_kinds = DIR_SUFFIX_KINDS.get(dirname)
    if suffix_kinds is None or not filename or filename.startswith("."):
        return None
    return suffix_kinds.get(PurePosixPath(filename).suffix.lower())


def media_type(kind: str) -> str:
    """kind → ``Content-Type``（未知 kind 不可能进白名单，兜底 octet-stream）。"""
    return _MEDIA_TYPES.get(kind, "application/octet-stream")


def resolve_artifact(workdir: Path | str, name: str) -> ArtifactEntry:
    """白名单名字 → 可下载产物；越界/非白名单/不存在 → ``artifact_not_found``。

    读取文档产物仍以 :meth:`babeldoc_tools.serve.store.DocumentStore.resolve` 给出的
    workdir 为唯一起点；本函数只在这个 workdir 内做文件级的白名单与越界判定。
    """
    kind = classify(name)
    if kind is None:
        raise _not_found(name)
    workdir_root = Path(workdir).resolve()
    # 白名单目录（根级文件视为 workdir 自己）解析后必须仍在 workdir 内。
    base = (
        workdir_root if name in ROOT_FILE_KINDS else workdir_root / name.split("/")[0]
    ).resolve()
    if base != workdir_root and workdir_root not in base.parents:
        raise _not_found(name)
    target = (base / name.split("/")[-1]).resolve()
    if target.parent != base or not target.is_file():
        raise _not_found(name)
    try:
        stat = target.stat()
    except OSError:  # 破符号链接 / 读到一半被删
        raise _not_found(name) from None
    return ArtifactEntry(
        name=name,
        kind=kind,
        path=target,
        size=stat.st_size,
        mtime=_iso_mtime(stat.st_mtime),
    )


def list_artifacts(workdir: Path | str) -> list[ArtifactEntry]:
    """白名单内**实际存在**的产物清单（按 ``name`` 排序，稳定可复现）。

    目录不存在 / 不是目录 / 不可读都只是"没有这类产物"（不报错）；候选名单一律经
    :func:`resolve_artifact` 过一遍，所以越界符号链接与白名单外的名字不会出现在清单里。
    """
    workdir_root = Path(workdir).resolve()
    names = list(ROOT_FILE_KINDS)
    for dirname in DIR_SUFFIX_KINDS:
        try:
            entries = sorted((workdir_root / dirname).iterdir())
        except OSError:  # 目录缺失 / 是文件 / 权限不足
            continue
        names.extend(f"{dirname}/{entry.name}" for entry in entries)

    found: list[ArtifactEntry] = []
    for name in names:
        try:
            found.append(resolve_artifact(workdir_root, name))
        except ToolError:
            continue
    return sorted(found, key=lambda entry: entry.name)


def _not_found(name: str) -> ToolError:
    """越界 / 非白名单 / 不存在用同一个错误（不泄露存在性，也不回显绝对路径）。"""
    return ToolError(
        "artifact_not_found",
        f"产物不在白名单内或不存在：{name}",
        name=name,
    )


def _iso_mtime(epoch_seconds: float) -> str:
    """epoch 秒 → UTC ISO8601（毫秒 + ``Z``）。

    ``stat`` 的 mtime 是真实 UTC 瞬间（带时区），所以直接用 UTC 转换，不像
    ``views._utc_iso`` 那样需要处理 ``run_state`` 的本地墙钟 naive 值。
    """
    moment = datetime.datetime.fromtimestamp(epoch_seconds, datetime.timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")
