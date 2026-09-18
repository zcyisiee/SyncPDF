"""``bdt serve`` 版本归档：发布即归档 / 50 上限 / 读取边界 / 清单端点（api.md §3.7，W12）。

测试自包含（代码造最小 workdir + 造字节，不读 ``tmp/`` 下真产物、不起 uvicorn、不跑真
编译）。编译链路的集成（settle_compile 发布后归档、失败/取消不归档、trigger 值）在
``tests/test_serve_compile.py`` 里用同一套 stub 覆盖 —— 那里的 fixture 才有编译 stub。

四条要证的事实：

1. **归档就是发布的那一份字节**：归档用硬链接（同一 inode），下一次发布 ``os.replace``
   换掉 ``output/`` 的 inode 后，旧版本文件仍是当时那一份；
2. **只增不覆盖**：revision 单调，同 r 再发布（防御性）覆盖文件 + 更新那一行；
3. **保留最近 50 版**：超出删最旧的（文件 + 清单行）；
4. **读取边界**：``.bdt-serve/versions/`` 不在产物白名单里，版本 PDF 只能经专用端点读，
   非数字 / 不在清单 / 文件缺失 / 符号链接越界一律 404 ``version_not_found``。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="serve 需要 web extra: fastapi")

from babeldoc_tools.common import ToolError  # noqa: E402
from babeldoc_tools.serve import versions  # noqa: E402
from babeldoc_tools.serve.app import create_app  # noqa: E402
from babeldoc_tools.serve.schemas import API_PREFIX  # noqa: E402
from babeldoc_tools.serve.store import DocumentStore  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# 写端点白名单是唯一来源（W12 只有两条 GET，不该新增任何写方法）。
from test_serve_app import assert_no_unexpected_write_routes  # noqa: E402

DID = "paper"
PDF_NAME = "paper.mono.pdf"
ARTIFACT = f"{API_PREFIX}/documents/{DID}/artifacts"
VERSIONS = f"{API_PREFIX}/documents/{DID}/versions"
VERSIONS_TEMPLATE = f"{API_PREFIX}/documents/{{did}}/versions"
VERSION_PDF_TEMPLATE = f"{VERSIONS_TEMPLATE}/{{revision}}/pdf"

QUALITY_OK = {"check_verdict": "pass", "pipeline_ok": True}
QUALITY_NEEDS_FIX = {"check_verdict": "needs_fix", "pipeline_ok": False}


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _pdf(tag: str, size: int = 64) -> bytes:
    """可辨识的假 PDF 字节（首行带 tag，便于断言"这版是这一份"）。"""
    return f"%PDF-1.4 {tag}\n".encode() + bytes([(len(tag) + index) % 251 for index in range(size)])


def version_pdf_path(workdir: Path, revision: int) -> Path:
    return workdir / ".bdt-serve" / "versions" / f"{revision}.pdf"


def manifest(workdir: Path) -> dict:
    return json.loads(
        versions.manifest_path(workdir).read_text(encoding="utf-8")
    )


def publish(workdir: Path, revision: int, tag: str, *, trigger=versions.TRIGGER_MANUAL,
            quality=QUALITY_OK) -> versions.VersionItem:
    """走一遍 W09 的发布语义（``os.replace`` 到 ``output/<name>``）再归档。"""
    staging = workdir / "output" / f".staged-{revision}.pdf"
    _write(staging, _pdf(tag))
    # 与 W09 的发布同一语义（`os.replace` 到同名产物）：发布后的 `output/<name>` 已是最终字节。
    staging.replace(workdir / "output" / PDF_NAME)
    return versions.archive(
        workdir, revision, trigger, quality, published=workdir / "output" / PDF_NAME
    )


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """最小 workdir：``output/`` + 一份已发布的 PDF。"""
    root = tmp_path / "root" / DID
    _write(root / "output" / PDF_NAME, _pdf("r1"))
    return root


@pytest.fixture
def client(workdir: Path):
    with TestClient(create_app(DocumentStore.for_root(workdir.parent))) as test_client:
        yield test_client


# --------------------------------------------------------------------------- #
# 归档：事实来自发布的那一份
# --------------------------------------------------------------------------- #
def test_archive_records_the_published_bytes_and_facts(workdir: Path):
    entry = publish(workdir, 1, "r1", trigger=versions.TRIGGER_DEBOUNCE, quality=QUALITY_OK)

    published = (workdir / "output" / PDF_NAME).read_bytes()
    assert entry.revision == 1
    assert entry.trigger == "debounce"
    assert entry.artifact_name == PDF_NAME
    assert entry.bytes == len(published)
    assert entry.quality.check_verdict == "pass" and entry.quality.pipeline_ok is True
    assert entry.created_at.endswith("Z") and "T" in entry.created_at
    # 指纹 = 归档文件**前** 64KiB 的 sha256（这里文件比 64KiB 小 → 全文件）
    assert entry.sha256_head == hashlib.sha256(published).hexdigest()
    assert version_pdf_path(workdir, 1).read_bytes() == published

    rows = manifest(workdir)["items"]
    assert [row["revision"] for row in rows] == [1]
    assert rows[0]["quality"] == {"check_verdict": "pass", "pipeline_ok": True}


def test_archive_links_the_published_file_so_the_next_release_keeps_it(workdir: Path):
    """归档是硬链接（同一 inode）：下一次发布 replace 掉 output/ 后旧版本字节不变。"""
    publish(workdir, 1, "r1")
    output = workdir / "output" / PDF_NAME
    archived = version_pdf_path(workdir, 1)
    assert archived.stat().st_ino == output.stat().st_ino  # 链接而非拷贝
    assert archived.stat().st_nlink == 2

    publish(workdir, 2, "r2")
    assert output.read_bytes().startswith(b"%PDF-1.4 r2")
    assert archived.read_bytes().startswith(b"%PDF-1.4 r1")  # 旧版本仍是当时那一份
    assert version_pdf_path(workdir, 2).read_bytes() == output.read_bytes()


def test_archive_same_revision_overwrites_file_and_row(workdir: Path):
    """revision 单调 → 同 r 不该重发布；真出现时覆盖文件并更新那一行（防御性）。"""
    publish(workdir, 3, "first")
    second = publish(workdir, 3, "second")

    rows = manifest(workdir)["items"]
    assert [row["revision"] for row in rows] == [3]  # 仍只有一行
    assert rows[0]["sha256_head"] == second.sha256_head
    assert version_pdf_path(workdir, 3).read_bytes().startswith(b"%PDF-1.4 second")


def test_archive_keeps_the_newest_fifty_versions(workdir: Path):
    for revision in range(1, versions.MAX_VERSIONS + 6):  # 55 版
        publish(workdir, revision, f"r{revision}")

    rows = manifest(workdir)["items"]
    kept = [row["revision"] for row in rows]
    assert len(kept) == versions.MAX_VERSIONS
    assert kept == list(range(6, versions.MAX_VERSIONS + 6))  # 淘汰最旧的 5 版
    assert kept == sorted(kept)  # 清单升序
    for revision in range(1, 6):
        assert not version_pdf_path(workdir, revision).exists()
    for revision in (6, versions.MAX_VERSIONS + 5):
        assert version_pdf_path(workdir, revision).is_file()


def test_archive_quality_snapshot_is_recorded_not_gated(workdir: Path):
    """质量只记录不门禁：``needs_fix`` 的版本照样归档、照样可下载（黄标由前端给）。"""
    entry = publish(workdir, 1, "r1", quality=QUALITY_NEEDS_FIX)
    assert entry.quality.check_verdict == "needs_fix"
    assert entry.quality.pipeline_ok is False
    assert version_pdf_path(workdir, 1).is_file()


def test_quality_snapshot_matches_the_detail_endpoint(workdir: Path):
    """缺省质量快照取自详情端点的同一实现（``views.quality_status``）。"""
    _write(
        workdir / "agent" / "run_state.json",
        json.dumps(
            {
                "quality": {
                    "check": {"verdict": "needs_fix", "reasons": ["link_missing"]},
                    "reviewer": {"verdict": "pass"},
                }
            }
        ).encode(),
    )
    assert versions.quality_snapshot(workdir) == {
        "check_verdict": "needs_fix",
        "pipeline_ok": False,
    }
    entry = versions.archive(
        workdir, 1, versions.TRIGGER_DEBOUNCE, published=workdir / "output" / PDF_NAME
    )
    with TestClient(create_app(DocumentStore.for_root(workdir.parent))) as client:
        detail = client.get(f"{API_PREFIX}/documents/{DID}").json()
    assert entry.quality.model_dump() == {
        "check_verdict": detail["quality"]["check"]["verdict"],
        "pipeline_ok": detail["quality"]["pipeline_ok"],
    }


def test_sha256_file_hashes_the_whole_file(tmp_path: Path):
    """``sha256_head`` 取**整个文件**的 sha256：只哈希头部区分不了同一文档的不同版本。

    真实教训（W12 真编译冒烟）：同一文档两次编译的产物前 19MB **逐字节相同**（差异从
    19,250,981 字节处开始），所以 64KiB 头部指纹两版一模一样 —— 那样就无法核对
    "下载到的就是当时那一份"。
    """
    prefix = bytes(range(256)) * 256  # 64KiB 相同前缀
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    tail_a = prefix + b"tail-a"
    _write(first, tail_a)
    _write(second, prefix + b"tail-b")

    assert versions.sha256_file(first) == hashlib.sha256(tail_a).hexdigest()
    assert versions.sha256_file(first) != versions.sha256_file(second)


def test_archive_rejects_unknown_trigger_and_negative_revision(workdir: Path):
    published = workdir / "output" / PDF_NAME
    with pytest.raises(ValueError):
        versions.archive(workdir, 1, "cron", published=published)
    with pytest.raises(ValueError):
        versions.archive(workdir, -1, versions.TRIGGER_MANUAL, published=published)


# --------------------------------------------------------------------------- #
# 清单读取：容错 + 排序
# --------------------------------------------------------------------------- #
def test_manifest_read_tolerates_missing_and_broken_files(workdir: Path):
    assert versions.read_manifest(workdir) == {"version": 1, "items": []}
    assert versions.list_versions(workdir) == []

    versions.manifest_path(workdir).parent.mkdir(parents=True, exist_ok=True)
    versions.manifest_path(workdir).write_text("{不是 JSON", encoding="utf-8")
    assert versions.read_manifest(workdir) == {"version": 1, "items": []}

    versions.manifest_path(workdir).write_text(json.dumps({"items": "nope"}), encoding="utf-8")
    assert versions.list_versions(workdir) == []


def test_list_versions_skips_bad_rows_and_sorts_ascending(workdir: Path):
    versions.manifest_path(workdir).parent.mkdir(parents=True, exist_ok=True)
    good = {
        "revision": 2,
        "created_at": "2026-09-17T10:00:00.000Z",
        "trigger": "manual",
        "artifact_name": PDF_NAME,
        "bytes": 1,
        "sha256_head": "0" * 64,
        "quality": QUALITY_OK,
    }
    versions.manifest_path(workdir).write_text(
        json.dumps(
            {
                "version": 1,
                "items": [
                    good | {"revision": 5},
                    {"revision": "three"},  # 形状不合法 → 跳过
                    good | {"revision": 1},
                    good | {"revision": 3, "trigger": "cron"},  # 未知 trigger → 跳过
                ],
            }
        ),
        encoding="utf-8",
    )
    assert [item.revision for item in versions.list_versions(workdir)] == [1, 5]


# --------------------------------------------------------------------------- #
# 读取边界
# --------------------------------------------------------------------------- #
def test_read_version_pdf_rejects_everything_outside_the_manifest(workdir: Path):
    publish(workdir, 1, "r1")
    assert versions.read_version_pdf(workdir, 1).path == version_pdf_path(workdir, 1)

    for raw in ("abc", "1.0", "-1", "1e0", "", "99", "../output/paper.mono.pdf"):
        with pytest.raises(ToolError) as excinfo:
            versions.read_version_pdf(workdir, raw)
        assert excinfo.value.code == "version_not_found"


def test_read_version_pdf_rejects_symlink_escape(workdir: Path):
    """``versions/1.pdf`` 指向 workdir 外 → 与"不存在"同一个 404（不泄露存在性）。"""
    publish(workdir, 1, "r1")
    outside = workdir.parent.parent / "outside.pdf"
    _write(outside, b"%PDF-1.4 outside")
    target = version_pdf_path(workdir, 1)
    target.unlink()
    target.symlink_to(outside)

    with pytest.raises(ToolError) as excinfo:
        versions.read_version_pdf(workdir, 1)
    assert excinfo.value.code == "version_not_found"


def test_read_version_pdf_reports_a_missing_file(workdir: Path):
    publish(workdir, 1, "r1")
    version_pdf_path(workdir, 1).unlink()  # 手删（清单还在）
    with pytest.raises(ToolError) as excinfo:
        versions.read_version_pdf(workdir, 1)
    assert excinfo.value.code == "version_not_found"


# --------------------------------------------------------------------------- #
# 端点（§3.7）
# --------------------------------------------------------------------------- #
def test_versions_endpoint_lists_newest_first_with_compile_context(workdir: Path, client):
    publish(workdir, 1, "r1")
    publish(workdir, 2, "r2", trigger=versions.TRIGGER_DEBOUNCE, quality=QUALITY_NEEDS_FIX)
    state = workdir / ".bdt-serve"
    state.mkdir(parents=True, exist_ok=True)
    (state / "compile.json").write_text(
        json.dumps(
            {
                "status": "ok",
                "revision": 2,
                "artifact": {"name": PDF_NAME, "revision": 2, "size": 1},
            }
        ),
        encoding="utf-8",
    )
    # 草稿比已编译的那一版新 → stale=true（与详情端点同一判据）
    (state / "draft.json").write_text(
        json.dumps({"revision": 3, "updated_at": None, "paragraphs": {}}), encoding="utf-8"
    )

    body = client.get(VERSIONS).json()
    assert body["did"] == DID
    assert body["current_revision"] == 2
    assert body["stale"] is True
    assert [item["revision"] for item in body["items"]] == [2, 1]
    assert body["items"][0]["trigger"] == "debounce"
    assert body["items"][0]["quality"] == {"check_verdict": "needs_fix", "pipeline_ok": False}
    assert body["items"][1]["trigger"] == "manual"
    # 清单文件本身仍是升序（倒序只发生在响应里）
    assert [row["revision"] for row in manifest(workdir)["items"]] == [1, 2]


def test_versions_endpoint_is_empty_for_a_never_compiled_document(client):
    """从没编译成功过 → 空数组 + current_revision=0（不是 404）。"""
    assert client.get(VERSIONS).json() == {
        "did": DID,
        "current_revision": 0,
        "stale": False,
        "items": [],
    }


def test_versions_endpoint_unknown_document_is_404(client):
    response = client.get(f"{API_PREFIX}/documents/nope/versions")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "document_not_found"


def test_version_download_returns_the_archived_bytes(workdir: Path, client):
    publish(workdir, 1, "r1")
    publish(workdir, 2, "r2")

    response = client.get(f"{VERSIONS}/1/pdf")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert (
        response.headers["content-disposition"]
        == 'inline; filename="paper.mono.r1.pdf"'
    )
    assert response.content == version_pdf_path(workdir, 1).read_bytes()
    assert response.content.startswith(b"%PDF-1.4 r1")

    current = client.get(f"{VERSIONS}/2/pdf")
    assert current.content == (workdir / "output" / PDF_NAME).read_bytes()


def test_version_download_bad_revisions_are_404(workdir: Path, client):
    publish(workdir, 1, "r1")
    for raw in ("abc", "1.0", "-1", "99"):
        response = client.get(f"{VERSIONS}/{raw}/pdf")
        assert response.status_code == 404, raw
        assert response.json()["error"]["code"] == "version_not_found"


def test_versions_directory_never_shows_up_in_the_artifact_list(workdir: Path, client):
    """版本文件不在 W03 白名单里：``GET /artifacts`` 清单不得混入 ``.bdt-serve/`` 下的东西。"""
    publish(workdir, 1, "r1")
    versions.manifest_path(workdir)  # 清单与版本文件都已落盘
    names = [item["name"] for item in client.get(ARTIFACT).json()]
    assert names == ["output/paper.mono.pdf"]
    assert not [name for name in names if "versions" in name or name.startswith(".bdt-serve")]


def test_versions_survive_a_restart(workdir: Path):
    publish(workdir, 1, "r1")
    publish(workdir, 2, "r2")

    with TestClient(create_app(DocumentStore.for_root(workdir.parent))) as restarted:
        body = restarted.get(VERSIONS).json()
        assert [item["revision"] for item in body["items"]] == [2, 1]
        assert restarted.get(f"{VERSIONS}/1/pdf").content.startswith(b"%PDF-1.4 r1")


def test_versions_routes_are_get_only(client):
    """W12 只有两条 GET：OpenAPI 的写端点白名单一个都不能多。"""
    schema = client.get("/openapi.json").json()
    assert_no_unexpected_write_routes(schema)
    for template in (VERSIONS_TEMPLATE, VERSION_PDF_TEMPLATE):
        assert set(schema["paths"][template]) == {"get"}
    items_schema = schema["paths"][VERSIONS_TEMPLATE]["get"]["responses"]["200"]
    assert "VersionsResponse" in json.dumps(items_schema, ensure_ascii=False)


def test_archive_failure_does_not_break_the_published_output(workdir: Path):
    """归档目录不可写 → 归档抛错（由 compile 兜底记日志）；发布出去的 PDF 分毫不动。"""
    publish(workdir, 1, "r1")
    state = workdir / ".bdt-serve"
    shutil.rmtree(state / "versions")
    (state / "versions").write_text("占位文件（不是目录）", encoding="utf-8")

    with pytest.raises(OSError):
        publish(workdir, 2, "r2")
    assert (workdir / "output" / PDF_NAME).read_bytes().startswith(b"%PDF-1.4 r2")
    assert [row["revision"] for row in manifest(workdir)["items"]] == [1]
