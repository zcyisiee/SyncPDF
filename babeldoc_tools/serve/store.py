"""文档根目录解析与路径白名单：Web 服务读文件的唯一边界。

布局沿用 PLAN §3：``<root>/<did>/`` 一个目录 = 一个文档 workdir，
``did`` 就是目录名（不再是 projects 维度）。两种模式：

- ``bdt serve --root <dir>``：``<dir>`` 的直接子目录都是候选文档；
- ``bdt serve --workdir <dir>``：只公开这一个文档（``did`` = 目录名），
  兄弟目录即使存在也不可见、不参与枚举，也不出现在任何错误信息里。

安全约定（W01 起就是硬约束）：

- ``did`` 必须是**单段**目录名：拒绝空串、``.`` / ``..``、``/`` / ``\\``、
  NUL、以及以 ``.`` 开头的隐藏名；
- 解析结果必须落在服务根目录**内部**（先 ``Path.resolve()`` 再判包含关系），
  因此符号链接越界（``<root>/x -> /etc``）一律拒绝；
- 只做 ``stat`` / 列目录，从不读取根目录之外的文件。

所有路径校验集中在本模块；路由层只调 :meth:`DocumentStore.resolve`。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from babeldoc_tools.common import ToolError

__all__ = ["STATE_DIR", "DocumentStore"]

#: 服务自己的状态目录名（job 持久化 / profiles），放在 :attr:`DocumentStore.store_base` 下。
#: 以 ``.`` 开头，因此永远不是合法 did：既不会被枚举成文档，也不会被越界解析。
STATE_DIR = ".bdt-serve"

#: 目录名不允许的字符/形式（单段 did 规则）。
_FORBIDDEN_DID = {".", ".."}
_TEST_WORKDIR = re.compile(
    r"^(?:w\d+(?:-|$)|bbox-|pytest-|test-|smoke-|e2e-|debug-|"
    r"(?:.*-)?agy-|bdt-dbg-|acceptance-|serve-smoke(?:-|$)|ccs\d+-|cloudtest-|"
    r"deepseek-|pi-e2e-)",
    re.I,
)


def _validate_did(did: str) -> str:
    """校验单段 did；非法 → ``ToolError(invalid_document_id)``。"""
    if not isinstance(did, str) or not did:
        raise ToolError("invalid_document_id", "did 不能为空", did=did)
    if (
        did in _FORBIDDEN_DID
        or did.startswith(".")
        or "/" in did
        or "\\" in did
        or "\x00" in did
    ):
        raise ToolError(
            "invalid_document_id",
            f"非法文档 id {did!r}：必须是单段目录名，且不以 . 开头",
            did=did,
        )
    return did


class DocumentStore:
    """服务根目录 + 可见文档白名单（不可变；一个 ``bdt serve`` 一个实例）。"""

    def __init__(self, root: Path | str, *, allowed: frozenset[str] | None = None):
        resolved = Path(root).expanduser().resolve()
        if not allowed and not resolved.is_dir():
            raise ToolError(
                "invalid_root",
                f"服务根目录不存在或不是目录：{resolved}",
                root=str(resolved),
            )
        self.root = resolved
        self._database = None
        #: ``None`` = 枚举根目录全部子目录；否则只允许这些 did。
        self.allowed = allowed
        self.mode: str = "workdir" if allowed is not None else "root"

    # ------------------------------------------------------------ factories
    @classmethod
    def for_root(cls, root: Path | str) -> DocumentStore:
        """服务 ``<root>/<did>/`` 下的全部文档。"""
        return cls(root)

    @classmethod
    def for_workdir(cls, workdir: Path | str) -> DocumentStore:
        """只服务 ``workdir`` 这一个（可能很旧的）只读 workdir。"""
        resolved = Path(workdir).expanduser().resolve()
        if not resolved.is_dir() or not resolved.name:
            # not resolved.name：拒绝 "--workdir /"（根目录没有文档名可用作 did）。
            raise ToolError(
                "invalid_root",
                f"--workdir 不存在、不是目录，或是文件系统根目录：{resolved}",
                root=str(resolved),
            )
        return cls(resolved.parent, allowed=frozenset({resolved.name}))

    # ------------------------------------------------------------- 只读属性
    @property
    def store_base(self) -> Path:
        """服务状态目录（``<store_base>/.bdt-serve/``）的宿主目录。

        ``root`` 模式 = 服务根目录；``workdir`` 模式 = 那个 workdir 本身（兄弟目录
        不可见，状态也不该在别人家里）。用它的模块：:mod:`babeldoc_tools.serve.jobs`
        （job 持久化）与 :mod:`babeldoc_tools.serve.profiles`（provider 配置）。
        """
        if self.allowed is not None and len(self.allowed) == 1:
            return self.root / next(iter(self.allowed))
        return self.root

    @property
    def database(self):
        from babeldoc_tools.serve.database import MetadataDB

        if self._database is None:
            self._database = MetadataDB(self.store_base)
        return self._database

    # ------------------------------------------------------------- internals
    def _require_root(self) -> Path:
        """确认根目录仍在（运行期被删 → ``root_missing``，供 health 报 503）。"""
        if not self.root.is_dir():
            raise ToolError(
                "root_missing",
                f"服务根目录已不可用：{self.root}",
                root=str(self.root),
            )
        return self.root

    def _candidates(self) -> list[str]:
        root = self._require_root()
        if self.allowed is not None:
            return sorted(self.allowed)
        return sorted(entry.name for entry in root.iterdir()
                      if entry.name not in {"assets", "cache", "tmp"})

    # ---------------------------------------------------------------- public
    def list_dids(self) -> list[str]:
        """可见文档的 did（排序稳定）；与 :meth:`resolve` 用同一套校验。"""
        candidates: list[tuple[str, Path]] = []
        for did in self._candidates():
            # serve --root tmp 时，测试运行目录不应污染文件库。
            if self.allowed is None and _TEST_WORKDIR.search(did):
                continue
            try:
                path = self.resolve(did)
            except ToolError as exc:
                if exc.code == "root_missing":
                    raise  # 运行期根目录消失：宁可报错，不返回被截断的列表
                continue  # 文件 / 隐藏名 / 符号链接越界 / 已消失 → 不是文档
            candidates.append((did, path))

        # 同一份源 PDF 可能被多次运行复制到不同 workdir；文件库只展示最近的一份。
        latest: dict[str, tuple[str, Path, float]] = {}
        for did, path in candidates:
            pdf = next(
                (p for name in ("source.pdf", "input.pdf")
                 for p in (path / name,) if p.is_file()),
                None,
            )
            if pdf is None:
                outputs = sorted((path / "output").glob("*.pdf"))
                pdf = outputs[-1] if outputs else None
            # 直接把仓库 ``tmp`` 作为 serve 根目录时，状态/日志目录不是文档。
            if pdf is None and self.root.name == "tmp":
                continue
            if pdf is None:
                latest[f"did:{did}"] = (did, path, path.stat().st_mtime)
                continue
            digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
            stamp = max(path.stat().st_mtime, pdf.stat().st_mtime)
            previous = latest.get(digest)
            if previous is None or stamp > previous[2]:
                latest[digest] = (did, path, stamp)
        return sorted(item[0] for item in latest.values())

    def resolve(self, did: str) -> Path:
        """did → workdir 绝对路径；这是读取文档产物的唯一入口。

        拒绝（``ToolError``）：``invalid_document_id`` 非法 id、
        ``document_not_found`` 不存在或不在服务范围、``path_escape`` 解析后
        越出根目录、``root_missing`` 根目录已不可用。
        """
        _validate_did(did)
        root = self._require_root()
        if self.allowed is not None and did not in self.allowed:
            # 不透露兄弟目录是否存在：与「不存在」同一个错误码。
            raise ToolError(
                "document_not_found",
                f"文档不存在：{did}",
                did=did,
            )
        candidate = (root / did).resolve()
        if candidate == root or root not in candidate.parents:
            raise ToolError(
                "path_escape",
                f"文档 {did!r} 解析后不在服务根目录内",
                did=did,
                resolved=str(candidate),
            )
        if not candidate.is_dir():
            raise ToolError("document_not_found", f"文档不存在：{did}", did=did)
        return candidate
