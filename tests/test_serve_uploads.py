"""``bdt serve`` 的 PDF 上传：魔数/大小校验、did 生成与冲突、原子落盘（W08）。

用 FastAPI ``TestClient`` 走真路由（不联网、不起 uvicorn），另有一部分用例直接调
:func:`babeldoc_tools.serve.uploads.save_upload`（纯函数/目录分配用注入的 did 名验证
冲突与竞态路径，不必依赖当前时钟的秒级边界）。

覆盖的边界：``%PDF-`` 魔数、缺文件名、声明大小与真实字节数两条上限路径（每次上传
只读当前分块，不把文件整体装进内存）、did 冲突递增、符号链接越界不写入、失败不留痕
（目录被收掉、根目录里不留空壳文档）。
"""

from __future__ import annotations

import io
import shutil
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="serve 需要 web extra: fastapi")

from babeldoc_tools.common import ToolError  # noqa: E402
from babeldoc_tools.serve import uploads  # noqa: E402
from babeldoc_tools.serve.app import create_app  # noqa: E402
from babeldoc_tools.serve.schemas import API_PREFIX as API  # noqa: E402
from babeldoc_tools.serve.store import DocumentStore  # noqa: E402
from babeldoc_tools.serve.uploads import MAX_UPLOAD_BYTES  # noqa: E402
from babeldoc_tools.serve.uploads import SOURCE_NAME  # noqa: E402
from babeldoc_tools.serve.uploads import new_upload_did  # noqa: E402
from babeldoc_tools.serve.uploads import save_upload  # noqa: E402
from babeldoc_tools.serve.uploads import upload_slug  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.datastructures import UploadFile  # noqa: E402

#: 最小合法 PDF（``%PDF-`` 魔数 + 一个空 xref 的收尾；只用于上传，不用于解析）。
MINIMAL_PDF = b"%PDF-1.7\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _make_workdir(parent: Path, did: str) -> Path:
    workdir = parent / did
    (workdir / "agent").mkdir(parents=True)
    (workdir / "agent" / "run_state.json").write_text("{}", encoding="utf-8")
    return workdir


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """一个已存在的文档（列表里要有旧文档）+ 上传目标根目录。"""
    base = tmp_path / "root"
    base.mkdir()
    _make_workdir(base, "alpha")
    return base


@pytest.fixture
def client(root: Path):
    with TestClient(create_app(DocumentStore.for_root(root))) as test_client:
        yield test_client


def posted(client, filename: str = "sample.pdf", data: bytes = MINIMAL_PDF):
    return client.post(f"{API}/documents", files={"file": (filename, data, "application/pdf")})


def uploaded_dids(root: Path) -> list[str]:
    return sorted(path.name for path in root.iterdir() if path.name.startswith("up-"))


# --------------------------------------------------------------------------- #
# 纯函数：slug / did
# --------------------------------------------------------------------------- #
def test_upload_slug_keeps_only_safe_characters():
    assert upload_slug("sample.pdf") == "sample"
    assert upload_slug("2602.02908v2 (1).pdf") == "2602-02908v2-1"
    assert upload_slug("Attention Is All You Need!.PDF") == "attention-is-all-you-need"
    # 非 ASCII 名字（论文.pdf）没有可用字符 → 回退 pdf（did 仍合法）
    assert upload_slug("论文.pdf") == "pdf"
    # 目录分隔符/穿越不进 slug（浏览器可能送 "a/b.pdf"，也挡 ".."）：只取基名
    assert upload_slug("../../etc/passwd.pdf") == "passwd"
    assert upload_slug("a\\b.pdf") == "b"
    # 截断到 40 字符且不留尾随 '-'
    long_slug = upload_slug("x" * 60 + ".pdf")
    assert long_slug == "x" * 40
    assert upload_slug("--.pdf") == "pdf"


def test_new_upload_did_shape_uses_utc_timestamp():
    now = datetime(2026, 9, 17, 17, 22, 33, tzinfo=timezone.utc)
    did = new_upload_did("Attention Is All You Need.pdf", now=now)
    assert did == "up-attention-is-all-you-need-20260917-172233"
    # 形状是 store 的 did 规则：单段、不以 '.' 开头、无分隔符
    assert "/" not in did and not did.startswith(".")


# --------------------------------------------------------------------------- #
# 路由：成功路径
# --------------------------------------------------------------------------- #
def test_upload_creates_document_and_returns_contract_shape(client, root):
    response = posted(client, "Attention Is All You Need.pdf")
    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"did", "bytes", "source"}
    assert body["bytes"] == len(MINIMAL_PDF)
    assert body["source"] == SOURCE_NAME
    assert body["did"].startswith("up-attention-is-all-you-need-")
    assert response.headers["content-type"].startswith("application/json")

    # 落盘：只有 source.pdf，没有 agent/ 骨架（不造假"已有数据"的文档）
    workdir = root / body["did"]
    assert (workdir / SOURCE_NAME).read_bytes() == MINIMAL_PDF
    assert sorted(path.name for path in workdir.iterdir()) == [SOURCE_NAME]

    # 上传后列表自然出现（计数为 null，因为没有产物）
    listed = client.get(f"{API}/documents").json()
    uploaded = [item for item in listed if item["did"] == body["did"]]
    assert len(uploaded) == 1
    assert uploaded[0]["pages"] is None and uploaded[0]["paragraph_count"] is None

    # 源 PDF 在 W03 产物白名单里 → 预览/下载立刻可用
    artifacts = client.get(f"{API}/documents/{body['did']}/artifacts").json()
    assert [item["name"] for item in artifacts] == [SOURCE_NAME]
    assert artifacts[0]["kind"] == "source"
    assert client.get(f"{API}/documents/{body['did']}/artifacts/{SOURCE_NAME}").status_code == 200


def test_two_uploads_of_the_same_filename_get_distinct_dids(client, root):
    first = posted(client, "same.pdf").json()["did"]
    second = posted(client, "same.pdf").json()["did"]
    assert first != second
    # 同一秒内 → 同名后缀递增；跨到下一秒 → 时间戳不同。两种都不撞目录。
    assert first.startswith("up-same-") and second.startswith("up-same-")
    assert (root / first / SOURCE_NAME).is_file() and (root / second / SOURCE_NAME).is_file()


# --------------------------------------------------------------------------- #
# 校验：魔数 / 文件名 / 大小
# --------------------------------------------------------------------------- #
def test_non_pdf_upload_is_422_and_leaves_nothing(client, root):
    response = posted(client, "fake.pdf", b"<html>not a pdf</html>")
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_pdf"
    assert error["detail"]["reason"] == "not_pdf"
    assert error["detail"]["magic"] == b"<html".hex()
    assert "not a pdf" not in response.text  # 不回显上传内容
    assert uploaded_dids(root) == []


def test_upload_without_filename_is_422(root):
    """路由层：缺文件名 → 422；没有建任何目录。

    Starlette 会把空文件名的 part 当成普通表单字段（路由层看到的是"没有 file"），
    所以两种形状都覆盖：直调上传层（真拿到 filename=None）与走 HTTP（422 校验失败）。
    """
    store = DocumentStore.for_root(root)
    with pytest.raises(ToolError) as excinfo:
        save_upload(store, UploadFile(filename="", file=io.BytesIO(MINIMAL_PDF)))
    assert excinfo.value.code == "invalid_pdf"
    assert excinfo.value.extra["reason"] == "missing_filename"

    with TestClient(create_app(store)) as client:
        response = client.post(
            f"{API}/documents", files={"file": ("", MINIMAL_PDF, "application/pdf")}
        )
    assert response.status_code == 422
    assert uploaded_dids(root) == []


def test_upload_missing_file_field_is_validation_error(client):
    response = client.post(f"{API}/documents", data={"not_a_file": "x"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_declared_size_over_limit_is_413_before_reading(client, root, monkeypatch):
    """上传层已声明大小（Starlette 解析 multipart 时给出）→ 不落盘直接 413。"""
    monkeypatch.setattr(uploads, "MAX_UPLOAD_BYTES", 16)
    response = posted(client, "big.pdf", MINIMAL_PDF)
    assert response.status_code == 413
    error = response.json()["error"]
    assert error["code"] == "file_too_large"
    assert error["detail"] == {"limit_bytes": 16, "size_bytes": len(MINIMAL_PDF)}
    assert uploaded_dids(root) == []


def test_streaming_size_guard_aborts_and_cleans_up(root, monkeypatch):
    """没有声明大小时按真实字节数卡上限：边写边判，超限即中止并收掉刚建的目录。"""
    monkeypatch.setattr(uploads, "MAX_UPLOAD_BYTES", 8)
    monkeypatch.setattr(uploads, "CHUNK_BYTES", 5)
    upload = UploadFile(filename="big.pdf", file=io.BytesIO(MINIMAL_PDF))
    with pytest.raises(ToolError) as excinfo:
        save_upload(DocumentStore.for_root(root), upload)
    assert excinfo.value.code == "file_too_large"
    assert excinfo.value.extra["limit_bytes"] == 8
    assert excinfo.value.extra["size_bytes"] == 10  # 读到第二个分块就知道超了
    assert uploaded_dids(root) == []  # 半成品与目录都收掉了


def test_file_too_large_default_limit_is_200mb():
    assert MAX_UPLOAD_BYTES == 200 * 1024 * 1024


# --------------------------------------------------------------------------- #
# did 分配：冲突递增 / 符号链接越界
# --------------------------------------------------------------------------- #
def test_did_conflict_increments_suffix(root, monkeypatch):
    base = "up-sample-20260917-120000"
    monkeypatch.setattr(uploads, "new_upload_did", lambda *_args, **_kwargs: base)
    (root / base).mkdir()
    (root / base / "occupied.txt").write_text("keep", encoding="utf-8")
    (root / f"{base}-2").mkdir()  # 目录重名
    (root / f"{base}-3").write_text("a file, not a workdir", encoding="utf-8")  # 文件也占名

    store = DocumentStore.for_root(root)
    did = save_upload(store, UploadFile(filename="sample.pdf", file=io.BytesIO(MINIMAL_PDF)))

    assert did == f"{base}-4"
    assert (root / did / SOURCE_NAME).read_bytes() == MINIMAL_PDF
    # 被占用的目录/文件一个都没被动
    assert (root / base / "occupied.txt").read_text(encoding="utf-8") == "keep"
    assert (root / f"{base}-3").read_text(encoding="utf-8") == "a file, not a workdir"


def test_symlinked_candidate_is_skipped_not_written_through(root, monkeypatch, tmp_path):
    """同名符号链接（含指向根目录外）绝不写入：换下一个候选名。"""
    base = "up-sample-20260917-120000"
    monkeypatch.setattr(uploads, "new_upload_did", lambda *_args, **_kwargs: base)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / base).symlink_to(outside, target_is_directory=True)
    (root / f"{base}-2").symlink_to(tmp_path / "dangling", target_is_directory=True)

    did = save_upload(
        DocumentStore.for_root(root), UploadFile(filename="x.pdf", file=io.BytesIO(MINIMAL_PDF))
    )

    assert did == f"{base}-3"
    assert list(outside.iterdir()) == []  # 没有穿过符号链接把字节写出去
    assert (root / did / SOURCE_NAME).is_file()


def test_upload_conflict_reports_409_after_too_many_collisions(root, monkeypatch):
    monkeypatch.setattr(uploads, "new_upload_did", lambda *_args, **_kwargs: "up-same")
    monkeypatch.setattr(uploads, "MAX_DID_ATTEMPTS", 2)
    (root / "up-same").mkdir()
    (root / "up-same-2").mkdir()
    store = DocumentStore.for_root(root)
    with pytest.raises(ToolError) as excinfo:
        save_upload(store, UploadFile(filename="s.pdf", file=io.BytesIO(MINIMAL_PDF)))
    assert excinfo.value.code == "upload_conflict"
    assert excinfo.value.extra["attempts"] == 2


# --------------------------------------------------------------------------- #
# 与 store 的边界：workdir 模式 / 根目录不可用
# --------------------------------------------------------------------------- #
def test_upload_in_workdir_mode_is_rejected_without_writing(tmp_path):
    """``--workdir`` 只公开一个 workdir：在它旁边建的 did 进不了可见范围，如实拒绝。"""
    base = tmp_path / "root"
    workdir = _make_workdir(base, "only")
    store = DocumentStore.for_workdir(workdir)
    with TestClient(create_app(store)) as client:
        response = posted(client)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "upload_not_supported"
    assert error["detail"]["mode"] == "workdir"
    # 兄弟目录与 workdir 里都没有留下任何上传产物
    assert sorted(path.name for path in workdir.iterdir()) == ["agent"]
    assert sorted(path.name for path in base.iterdir()) == ["only"]


def test_upload_when_root_is_gone_is_503(root):
    """运行期根目录消失：如实 503 ``root_missing``（不静默建到别处）。"""
    store = DocumentStore.for_root(root)
    app = create_app(store)
    shutil.rmtree(root)
    with TestClient(app) as client:
        response = posted(client)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "root_missing"


def test_upload_does_not_echo_or_store_client_metadata(client, root):
    """文件名以外的表单字段被忽略（不落盘、不回显），多出来的 multipart 字段不进状态。"""
    response = client.post(
        f"{API}/documents",
        files={"file": ("meta.pdf", MINIMAL_PDF, "application/pdf")},
        data={"translator": "rm -rf / --api-key sk-xxx", "did": "../../etc"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["did"].startswith("up-meta-")
    assert "rm -rf" not in response.text and "sk-xxx" not in response.text
    workdir = root / body["did"]
    assert sorted(path.name for path in workdir.iterdir()) == [SOURCE_NAME]
    # serve 的状态目录里也没有这些字段
    state = root / ".bdt-serve"
    if state.is_dir():
        for path in state.rglob("*"):
            if path.is_file():
                assert "sk-xxx" not in path.read_text(encoding="utf-8", errors="ignore")


def test_upload_creates_no_agent_skeleton(client, root):
    """上传只落 source.pdf：没有 agent/ 骨架，也没有任何服务端状态（不伪造 run_state）。"""
    did = posted(client).json()["did"]
    assert not (root / did / "agent").exists()
    detail = client.get(f"{API}/documents/{did}").json()
    assert detail["available"]["run_state"] is False
    assert detail["available"]["anchors"] is False
    assert detail["config"] is None
    assert set(detail["stage_summary"].values()) == {"not_run"}
    # 没有 job、没有 profiles.json（上传不碰服务端状态目录）
    state = root / ".bdt-serve"
    assert list(state.rglob("j_*.json")) == []
    assert not (state / "profiles.json").exists()
