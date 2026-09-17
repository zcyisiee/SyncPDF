"""``bdt serve`` 的文档 resolver：单段 did、目录逃逸、符号链接、workdir 限制。

安全边界（见 ``babeldoc_tools/serve/store.py``）在 W01 就被钉死：Web 层读文档
产物只能走 :meth:`DocumentStore.resolve`。这些测试不启动 HTTP 服务、不读 PDF。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from babeldoc_tools.common import ToolError
from babeldoc_tools.serve.store import DocumentStore


def _make_workdir(parent: Path, did: str) -> Path:
    """造一个最小 workdir：``<parent>/<did>/agent/``（= bdt 的 workdir 形状）。"""
    workdir = parent / did
    (workdir / "agent").mkdir(parents=True)
    (workdir / "agent" / "run_state.json").write_text("{}", encoding="utf-8")
    return workdir


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """含两个文档 + 一个文件 + 一个隐藏目录的根目录。"""
    base = tmp_path / "root"
    base.mkdir()
    _make_workdir(base, "alpha")
    _make_workdir(base, "beta")
    (base / "loose.pdf").write_text("not a document", encoding="utf-8")
    (base / ".hidden").mkdir()
    return base


# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #
def test_list_dids_only_visible_documents(root):
    """文件、隐藏目录都不是文档；did 排序稳定。"""
    store = DocumentStore.for_root(root)
    assert store.list_dids() == ["alpha", "beta"]
    assert store.mode == "root"


def test_root_is_stored_resolved(root):
    """``root`` 存的是 resolve 后的绝对路径（macOS ``/tmp`` 是符号链接）。"""
    store = DocumentStore.for_root(root)
    assert store.root == root.resolve()
    assert store.root.is_absolute()


def test_symlinked_root_path_is_resolved(root, tmp_path):
    """``--root`` 本身是符号链接时按其真实目录枚举（不误判为逃逸）。"""
    link = tmp_path / "root-link"
    link.symlink_to(root, target_is_directory=True)
    store = DocumentStore.for_root(link)
    assert store.list_dids() == ["alpha", "beta"]
    assert store.resolve("alpha") == root.resolve() / "alpha"


def test_workdir_mode_lists_only_that_document(root):
    """``--workdir`` 只公开那一个文档。"""
    store = DocumentStore.for_workdir(root / "alpha")
    assert store.mode == "workdir"
    assert store.list_dids() == ["alpha"]


# --------------------------------------------------------------------------- #
# 合法 did
# --------------------------------------------------------------------------- #
def test_resolve_returns_absolute_workdir(root):
    store = DocumentStore.for_root(root)
    assert store.resolve("alpha") == (root / "alpha").resolve()
    assert store.resolve("beta").is_dir()


def test_resolve_allows_symlink_inside_root(root):
    """指向根目录**内部**的符号链接不越界，按目标目录解析。"""
    _make_workdir(root, "gamma")
    (root / "gamma-link").symlink_to(root / "gamma", target_is_directory=True)
    store = DocumentStore.for_root(root)
    assert store.resolve("gamma-link") == (root / "gamma").resolve()
    assert "gamma-link" in store.list_dids()


# --------------------------------------------------------------------------- #
# 非法 did / 越界
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "did",
    [
        "",
        ".",
        "..",
        ".hidden",
        "a/b",
        "../b",
        "alpha/../beta",
        "alpha\x00",
        "\\windows",
    ],
)
def test_invalid_did_rejected(root, did):
    """空串 / 相对段 / 分隔符 / NUL / 隐藏名 → ``invalid_document_id``。"""
    store = DocumentStore.for_root(root)
    with pytest.raises(ToolError) as excinfo:
        store.resolve(did)
    assert excinfo.value.code == "invalid_document_id"


def test_directory_escape_via_parent_segments(root):
    """``../`` 形式的目录逃逸在 did 校验阶段就被拒。"""
    store = DocumentStore.for_root(root)
    for did in ("../outside", "../../etc", "alpha/../../outside"):
        with pytest.raises(ToolError) as excinfo:
            store.resolve(did)
        assert excinfo.value.code in {"invalid_document_id", "document_not_found"}


def test_symlink_escape_rejected(root, tmp_path):
    """根目录内的符号链接指向根目录外 → ``path_escape``，且不读目标内容。"""
    outside = tmp_path / "outside"
    _make_workdir(outside, "secret")
    (root / "leak").symlink_to(outside, target_is_directory=True)

    store = DocumentStore.for_root(root)
    with pytest.raises(ToolError) as excinfo:
        store.resolve("leak")
    assert excinfo.value.code == "path_escape"
    assert str(outside.resolve()) in str(excinfo.value.extra["resolved"])
    # 越界目录不出现在枚举里
    assert "leak" not in store.list_dids()


def test_symlink_to_system_dir_rejected(root):
    """指向系统目录的符号链接同样被拒（不暴露 /etc 等外部文件）。"""
    system = Path("/etc")
    if not system.is_dir():  # pragma: no cover - POSIX 上恒存在
        pytest.skip("/etc 不存在")
    (root / "sys").symlink_to(system, target_is_directory=True)
    store = DocumentStore.for_root(root)
    with pytest.raises(ToolError) as excinfo:
        store.resolve("sys")
    assert excinfo.value.code == "path_escape"
    assert "sys" not in store.list_dids()


def test_self_symlink_rejected(root):
    """``<root>/self -> <root>`` 解析回根目录本身 → 拒绝（不是文档）。"""
    (root / "self").symlink_to(root, target_is_directory=True)
    store = DocumentStore.for_root(root)
    with pytest.raises(ToolError) as excinfo:
        store.resolve("self")
    assert excinfo.value.code == "path_escape"


# --------------------------------------------------------------------------- #
# 缺失文档 / workdir 限制
# --------------------------------------------------------------------------- #
def test_missing_document_is_not_found(root):
    store = DocumentStore.for_root(root)
    with pytest.raises(ToolError) as excinfo:
        store.resolve("nope")
    assert excinfo.value.code == "document_not_found"


def test_file_is_not_a_document(root):
    store = DocumentStore.for_root(root)
    with pytest.raises(ToolError) as excinfo:
        store.resolve("loose.pdf")
    assert excinfo.value.code == "document_not_found"


def test_workdir_mode_hides_siblings(root):
    """兄弟目录既不在枚举里，也不能被解析（错误码与"不存在"一致，不泄露存在性）。"""
    store = DocumentStore.for_workdir(root / "alpha")
    assert store.list_dids() == ["alpha"]
    with pytest.raises(ToolError) as excinfo:
        store.resolve("beta")
    assert excinfo.value.code == "document_not_found"
    assert (root / "beta").is_dir()  # 兄弟目录确实存在，只是不可见


# --------------------------------------------------------------------------- #
# 根目录本身
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", ["missing", "file"])
def test_invalid_root_rejected(tmp_path, kind):
    path = tmp_path / "nope"
    if kind == "file":
        path.write_text("x", encoding="utf-8")
    with pytest.raises(ToolError) as excinfo:
        DocumentStore.for_root(path)
    assert excinfo.value.code == "invalid_root"
    assert excinfo.value.extra["root"] == str(path.resolve())


def test_invalid_workdir_rejected(tmp_path):
    with pytest.raises(ToolError) as excinfo:
        DocumentStore.for_workdir(tmp_path / "nope")
    assert excinfo.value.code == "invalid_root"


def test_filesystem_root_rejected_as_workdir():
    """``--workdir /`` 没有可用的文档名 → 明确报错，不当成文档。"""
    with pytest.raises(ToolError) as excinfo:
        DocumentStore.for_workdir(Path("/"))
    assert excinfo.value.code == "invalid_root"


def test_root_removed_after_startup_reports_root_missing(root):
    """根目录运行期被删 → ``root_missing``（health 据此报 503，不谎报 ok）。"""
    store = DocumentStore.for_root(root)
    assert store.list_dids() == ["alpha", "beta"]
    shutil.rmtree(root)
    for call in (store.list_dids, lambda: store.resolve("alpha")):
        with pytest.raises(ToolError) as excinfo:
            call()
        assert excinfo.value.code == "root_missing"


def test_nested_symlink_escape_rejected(root, tmp_path):
    """链式符号链接指向外部 → 仍拒绝（``resolve`` 会展开整条链）。"""
    outside = tmp_path / "outside2"
    _make_workdir(outside, "secret")
    (root / "hop").symlink_to(outside, target_is_directory=True)
    (root / "hop2").symlink_to(root / "hop", target_is_directory=True)

    store = DocumentStore.for_root(root)
    for did in ("hop", "hop2"):
        with pytest.raises(ToolError) as excinfo:
            store.resolve(did)
        assert excinfo.value.code == "path_escape"
    assert store.list_dids() == ["alpha", "beta"]
