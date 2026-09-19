"""``bdt serve`` 产物端点：白名单清单 + Range/HEAD 下载（api.md §1.5）。

测试自包含（代码造最小 workdir，不读 ``tmp/`` 下真实产物、不起 uvicorn）：覆盖
白名单内外、不递归子目录、``debug/`` 不进清单、Range 206/416、HEAD、编码过的
``..`` 穿越、符号链接越界（文件级与白名单目录级）。
"""

from __future__ import annotations

import datetime
import shutil
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="serve 需要 web extra: fastapi")

from babeldoc_tools.common import ToolError  # noqa: E402
from babeldoc_tools.serve import artifacts  # noqa: E402
from babeldoc_tools.serve.app import create_app  # noqa: E402
from babeldoc_tools.serve.schemas import API_PREFIX  # noqa: E402
from babeldoc_tools.serve.store import DocumentStore  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# 写端点白名单是唯一来源：W07 只放行 job 的两条 POST，W08 加上传与 profile 写入
# （见那个模块的 ALLOWED_WRITE_ROUTES）。
from test_serve_app import assert_no_unexpected_write_routes  # noqa: E402

DID = "paper"
BARE = "bare"
RUN_ID = "20260917T100000Z-0000a1"

DOCUMENTS = f"{API_PREFIX}/documents"
ARTIFACTS = f"{DOCUMENTS}/{DID}/artifacts"
#: OpenAPI 里路径是模板（不是具体 did / name）
ARTIFACTS_TEMPLATE = f"{DOCUMENTS}/{{did}}/artifacts"
ARTIFACT_TEMPLATE = f"{ARTIFACTS_TEMPLATE}/{{name}}"

MONO = b"%PDF-1.4\nmono" + bytes(range(120))
DUAL = b"%PDF-1.4\ndual" + bytes(range(200))
SOURCE = b"%PDF-1.4\nsource" + bytes(range(40))
REPORT = "报告：全部通过\n".encode()
TRANSLATED_MD = "# 译文\n\n你好。\n".encode()
GEOMETRY = b'{"version": 1}\n'

#: 白名单内实际存在的产物（按 name 排序，与端点返回顺序一致）。
EXPECTED_NAMES = [
    "FINAL_REPORT.md",
    "agent/layout_geometry.json",
    "agent/translated.jsonl",
    "agent/translated.md",
    "output/paper.dual.pdf",
    "output/paper.mono.pdf",
    "source.pdf",
]


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """一个完整 workdir（白名单内外都有）+ 一个空 workdir。"""
    base = tmp_path / "root"
    workdir = base / DID
    (workdir / "agent").mkdir(parents=True)

    # 白名单内
    _write(workdir / "FINAL_REPORT.md", REPORT)
    _write(workdir / "source.pdf", SOURCE)
    _write(workdir / "output" / "paper.mono.pdf", MONO)
    _write(workdir / "output" / "paper.dual.pdf", DUAL)
    _write(workdir / "agent" / "translated.md", TRANSLATED_MD)
    _write(workdir / "agent" / "translated.jsonl", b'{"id": "P1", "target": "x"}\n')
    _write(workdir / "agent" / "layout_geometry.json", GEOMETRY)

    # 白名单外：非白名单扩展名 / pickle / 点文件 / 子目录 / debug 归档
    _write(workdir / "output" / "notes.txt", b"notes")
    _write(workdir / "agent" / "state.pkl", b"\x80\x04pickle")
    _write(workdir / "agent" / ".hidden.json", b"{}")
    _write(workdir / "agent" / "source" / "links.json", b"{}")
    _write(workdir / "debug" / "runs" / RUN_ID / "events.jsonl", b'{"seq": 1}\n')
    _write(workdir / "debug" / "runs" / RUN_ID / "artifacts" / "evidence.pdf", MONO)

    (base / BARE).mkdir()
    return base


@pytest.fixture
def workdir(root: Path) -> Path:
    return root / DID


@pytest.fixture
def client(root: Path):
    with TestClient(create_app(DocumentStore.for_root(root))) as test_client:
        yield test_client


def _manifest(client: TestClient) -> list[dict]:
    response = client.get(ARTIFACTS)
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# 清单
# --------------------------------------------------------------------------- #
def test_preview_pages_manifest_etag_and_asset_allowlist(root, client):
    from babeldoc_tools.serve.asset_store import AssetStore
    store = DocumentStore.for_root(root)
    database = store.database
    page_path = root / "page.pdf"
    page_path.write_bytes(b"%PDF-page")
    digest = AssetStore(root, database).put(page_path, kind="page")
    with database._lock, database.connection:
        database.connection.execute("INSERT OR IGNORE INTO papers(id) VALUES (?)", (DID,))
        database.connection.execute("INSERT OR IGNORE INTO documents(id,paper_id) VALUES (?,?)", (DID, DID))
        database.connection.execute("INSERT OR REPLACE INTO local_pages VALUES (?,?,?)", (DID, 1, '{"complete":true,"page_revision":2}'))
        database.connection.execute("INSERT OR REPLACE INTO pages(document_id,page,page_asset,dirty) VALUES (?,?,?,0)", (DID, 1, digest))
    first = client.get(f"{API_PREFIX}/documents/{DID}/preview-pages")
    assert first.status_code == 200
    assert first.json()["pages"][0]["asset"] == digest
    assert first.json()["pages"][0]["complete"] is True
    assert client.get(f"{API_PREFIX}/documents/{DID}/assets/{digest}").status_code == 200
    assert client.get(f"{API_PREFIX}/documents/{DID}/preview-pages", headers={"If-None-Match": first.headers["etag"]}).status_code == 304
    with database._lock, database.connection:
        database.connection.execute("UPDATE local_pages SET payload=? WHERE document_id=? AND page=1", ('{"complete":false,"page_revision":3}', DID))
    second = client.get(f"{API_PREFIX}/documents/{DID}/preview-pages", headers={"If-None-Match": first.headers["etag"]})
    assert second.status_code == 200 and second.json()["pages"][0]["complete"] is False


def test_artifacts_manifest_shape_and_whitelist(client):
    """清单恰好是白名单内存在的产物；name=path=workdir 相对路径；mtime 是 UTC。"""
    items = _manifest(client)
    assert [item["name"] for item in items] == EXPECTED_NAMES
    for item in items:
        assert set(item) == {"name", "path", "kind", "size", "mtime"}
        assert item["path"] == item["name"]  # 相对路径，不下发服务端绝对路径
        assert not item["name"].startswith("/")
        assert item["size"] > 0
        moment = datetime.datetime.fromisoformat(item["mtime"].replace("Z", "+00:00"))
        assert moment.tzinfo is not None and item["mtime"].endswith("Z")

    by_name = {item["name"]: item for item in items}
    assert by_name["source.pdf"]["kind"] == "source"
    assert by_name["FINAL_REPORT.md"]["kind"] == "report"
    assert by_name["output/paper.mono.pdf"]["kind"] == "pdf"
    assert by_name["agent/translated.md"]["kind"] == "markdown"
    assert by_name["agent/translated.jsonl"]["kind"] == "json"
    assert by_name["output/paper.mono.pdf"]["size"] == len(MONO)
    assert by_name["source.pdf"]["size"] == len(SOURCE)


def test_artifacts_manifest_excludes_non_whitelist_and_debug(client):
    """debug/ 归档、pickle、点文件、嵌套子目录、非白名单扩展名都不进清单。"""
    names = {item["name"] for item in _manifest(client)}
    assert names.isdisjoint(
        {
            "output/notes.txt",
            "agent/state.pkl",
            "agent/.hidden.json",
            "agent/source/links.json",
            f"debug/runs/{RUN_ID}/events.jsonl",
            f"debug/runs/{RUN_ID}/artifacts/evidence.pdf",
        }
    )
    assert not any(name.startswith("debug/") for name in names)


def test_artifacts_manifest_empty_workdir_is_empty_array(client):
    """没有任何产物 → 200 + []（清单确实为空，不是缺产物）。"""
    response = client.get(f"{DOCUMENTS}/{BARE}/artifacts")
    assert response.status_code == 200
    assert response.json() == []


def test_artifacts_unknown_document_uses_error_envelope(client):
    for path in (
        f"{DOCUMENTS}/nope/artifacts",
        f"{DOCUMENTS}/nope/artifacts/source.pdf",
    ):
        response = client.get(path)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "document_not_found"


# --------------------------------------------------------------------------- #
# 下载
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("name", "content", "media_type"),
    [
        ("output/paper.mono.pdf", MONO, "application/pdf"),
        ("source.pdf", SOURCE, "application/pdf"),
        ("FINAL_REPORT.md", REPORT, "text/markdown"),
        ("agent/translated.md", TRANSLATED_MD, "text/markdown"),
        ("agent/layout_geometry.json", GEOMETRY, "application/json"),
    ],
)
def test_artifact_download_returns_bytes(client, name, content, media_type):
    response = client.get(f"{ARTIFACTS}/{name}")
    assert response.status_code == 200
    assert response.content == content
    assert response.headers["content-type"].startswith(media_type)
    assert response.headers["accept-ranges"] == "bytes"


def test_artifact_download_range_returns_206(client):
    """pdf.js 依赖：``Range: bytes=a-b`` → 206 + Content-Range（前缀字节一致）。"""
    response = client.get(
        f"{ARTIFACTS}/output/paper.mono.pdf", headers={"Range": "bytes=0-9"}
    )
    assert response.status_code == 206
    assert response.content == MONO[:10]
    assert response.headers["content-range"] == f"bytes 0-9/{len(MONO)}"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-length"] == "10"


def test_artifact_download_open_range_returns_tail(client):
    response = client.get(
        f"{ARTIFACTS}/output/paper.dual.pdf",
        headers={"Range": f"bytes={len(DUAL) - 20}-"},
    )
    assert response.status_code == 206
    assert response.content == DUAL[-20:]
    assert (
        response.headers["content-range"]
        == f"bytes {len(DUAL) - 20}-{len(DUAL) - 1}/{len(DUAL)}"
    )


def test_artifact_download_unsatisfiable_range_is_416(client):
    response = client.get(
        f"{ARTIFACTS}/output/paper.mono.pdf",
        headers={"Range": f"bytes={len(MONO) + 100}-"},
    )
    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{len(MONO)}"


def test_artifact_head_returns_metadata(client):
    """HEAD 等价于 GET 的头部（pdf.js / curl -I 靠它拿长度），不带正文。"""
    response = client.head(f"{ARTIFACTS}/output/paper.mono.pdf")
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-length"] == str(len(MONO))
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-type"].startswith("application/pdf")


def test_artifact_download_rejects_names_outside_whitelist(client):
    """清单外的一切名字都是 404 artifact_not_found（不泄露存在性）。"""
    for name in (
        "output/notes.txt",
        "agent/state.pkl",
        "agent/.hidden.json",
        "agent/source/links.json",
        f"debug/runs/{RUN_ID}/events.jsonl",
        f"debug/runs/{RUN_ID}/artifacts/evidence.pdf",
        "nope.pdf",
        "output/sub/x.pdf",
    ):
        response = client.get(f"{ARTIFACTS}/{name}")
        assert response.status_code == 404, name
        assert response.json()["error"]["code"] == "artifact_not_found", name


def test_artifact_download_rejects_encoded_traversal(client, tmp_path):
    """百分号编码的 ``..``（不会被客户端规范化掉）同样被拒。"""
    (tmp_path / "outside").mkdir(exist_ok=True)
    (tmp_path / "outside" / "secret.pdf").write_bytes(b"SECRET")
    for raw in (
        "%2e%2e%2foutside%2fsecret.pdf",
        "..%2Foutside%2Fsecret.pdf",
        "output%2f..%2f..%2foutside%2fsecret.pdf",
        "%2e%2e%2f..%2foutside%2fsecret.pdf",
    ):
        response = client.get(f"{ARTIFACTS}/{raw}")
        assert response.status_code == 404, raw
        assert response.json()["error"]["code"] == "artifact_not_found", raw
        assert b"SECRET" not in response.content


def test_artifact_download_rejects_symlink_escape(client, workdir, tmp_path):
    """白名单目录里的符号链接指向 workdir 外 → 与"不存在"同一个 404。"""
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    (outside / "secret.pdf").write_bytes(b"SECRET")
    (workdir / "output" / "escape.pdf").symlink_to(outside / "secret.pdf")

    response = client.get(f"{ARTIFACTS}/output/escape.pdf")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "artifact_not_found"
    assert b"SECRET" not in response.content
    assert "output/escape.pdf" not in {item["name"] for item in _manifest(client)}


def test_artifact_rejects_whitelist_dir_symlinked_outside(client, workdir, tmp_path):
    """整个白名单目录是指向 workdir 外的符号链接 → 里面的文件既不可下载也不进清单。"""
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    (outside / "stolen.json").write_bytes(b'{"stolen": true}')
    shutil.rmtree(workdir / "agent")
    (workdir / "agent").symlink_to(outside)

    assert "agent/stolen.json" not in {item["name"] for item in _manifest(client)}
    response = client.get(f"{ARTIFACTS}/agent/stolen.json")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "artifact_not_found"


# --------------------------------------------------------------------------- #
# 白名单规则（单元级：不必经过 HTTP 也能钉住穿越与形态判定）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("source.pdf", "source"),
        ("FINAL_REPORT.md", "report"),
        ("output/paper.mono.pdf", "pdf"),
        ("output/PAPER.PDF", "pdf"),  # 扩展名大小写不敏感
        ("agent/translated.md", "markdown"),
        ("agent/layout_geometry.json", "json"),
        ("agent/translated.jsonl", "json"),
    ],
)
def test_classify_accepts_whitelist_names(name, kind):
    assert artifacts.classify(name) == kind


@pytest.mark.parametrize(
    "name",
    [
        "../secret.pdf",
        "/etc/hosts",
        "agent/../../secret.json",
        "output/..",
        "output/.",
        "agent/",
        "agent/.hidden.json",
        "agent/state.pkl",
        "output/notes.txt",
        "debug/runs/20260917T100000Z-0000a1/events.jsonl",
        "output/sub/x.pdf",
        "source.pdf.bak",
        "REPORT.md",
        "agent\\translated.md",
        "agent/trans\x00lated.md",
        "",
    ],
)
def test_classify_rejects_non_whitelist_names(name):
    assert artifacts.classify(name) is None


def test_resolve_artifact_outside_workdir_is_not_found(workdir):
    """即使调用方直接用绝对/穿越名字调 resolver，也只会拿到 artifact_not_found。"""
    for name in ("../secret.pdf", "/etc/hosts", "agent/../../secret.json"):
        with pytest.raises(ToolError) as excinfo:
            artifacts.resolve_artifact(workdir, name)
        assert excinfo.value.code == "artifact_not_found", name

    assert artifacts.resolve_artifact(workdir, "source.pdf").path.is_file()


def test_media_type_mapping():
    assert artifacts.media_type("pdf") == "application/pdf"
    assert artifacts.media_type("source") == "application/pdf"
    assert artifacts.media_type("json") == "application/json"
    assert artifacts.media_type("markdown").startswith("text/markdown")
    assert artifacts.media_type("report").startswith("text/markdown")


# --------------------------------------------------------------------------- #
# OpenAPI
# --------------------------------------------------------------------------- #
def test_artifacts_openapi_exposes_get_and_hidden_head(client):
    """两个产物端点对 OpenAPI 都只有 GET（HEAD 是同一条 GET 的头部变体）。"""
    schema = client.get("/openapi.json").json()
    assert set(schema["paths"][ARTIFACTS_TEMPLATE]) == {"get"}
    download = schema["paths"][ARTIFACT_TEMPLATE]
    assert set(download) == {"get"}
    assert set(download["get"]["responses"]["200"]["content"]) == {
        "application/pdf",
        "application/json",
        "text/markdown",
    }
    # W01–W03 的只读边界 + W07 的两条 job POST 白名单（helper 是唯一来源）
    assert_no_unexpected_write_routes(schema)
