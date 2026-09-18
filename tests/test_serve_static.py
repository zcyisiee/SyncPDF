"""``bdt serve`` 的前端静态伺服（W15，:mod:`babeldoc_tools.serve.static`）。

覆盖三件事：

1. **没构建过就静默跳过**：dist 不存在 → 不挂回退路由，未知路径照旧是 JSON 404；
2. **构建产物按哈希/非哈希分档缓存**：``assets/*`` 长缓存 + immutable，``index.html`` 与
   ``pdfjs/*`` 一律 ``no-cache``；SPA 深链回退到 index；
3. **接口不被前端接管**：``/api``、``/docs``、``/openapi.json`` 语义不变；dist 内的符号
   链接不能穿到 dist 之外（与上传端点同一条「符号链接不穿透」规则）。

既有的 ``/api`` 语义（health、OpenAPI、错误信封）由 ``tests/test_serve_app.py`` 守着 ——
本文件只在「真的把 dist 挂上」的条件下复核它们没被回退路由抢走。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="serve 需要 web extra: fastapi")

from babeldoc_tools.serve import static as static_mod  # noqa: E402
from babeldoc_tools.serve.app import create_app  # noqa: E402
from babeldoc_tools.serve.schemas import API_PREFIX  # noqa: E402
from babeldoc_tools.serve.static import IMMUTABLE_CACHE  # noqa: E402
from babeldoc_tools.serve.static import NO_CACHE  # noqa: E402
from babeldoc_tools.serve.store import DocumentStore  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

HEALTH = f"{API_PREFIX}/health"
INDEX_HTML = b'<!doctype html><html><body><div id="root"></div></body></html>\n'
ASSET_JS = b"console.log('w15 asset');\n"
WORKER_JS = b"self.onmessage = () => {};\n"


def _make_workdir(parent: Path, did: str) -> Path:
    workdir = parent / did
    (workdir / "agent").mkdir(parents=True)
    (workdir / "agent" / "run_state.json").write_text("{}", encoding="utf-8")
    return workdir


@pytest.fixture
def root(tmp_path: Path) -> Path:
    base = tmp_path / "root"
    base.mkdir()
    _make_workdir(base, "alpha")
    return base


def _write_dist(tmp_path: Path) -> Path:
    """一份最小但形状真实的 ``pnpm build`` 产物（index + 哈希资源 + pdfjs 固定名资源）。"""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "pdfjs").mkdir(parents=True)
    (dist / static_mod.INDEX_FILE).write_bytes(INDEX_HTML)
    (dist / "assets" / "index-abc123.js").write_bytes(ASSET_JS)
    (dist / "pdfjs" / "worker.js").write_bytes(WORKER_JS)
    return dist


@pytest.fixture
def mounted_client(root: Path, tmp_path: Path, monkeypatch):
    """把 ``web/dist`` 换成 tmp 里的构建产物（仓库里有没有 dist 都不影响本文件）。"""
    dist = _write_dist(tmp_path)
    monkeypatch.setattr(static_mod, "frontend_dist", lambda: dist)
    with TestClient(create_app(DocumentStore.for_root(root))) as client:
        yield client


@pytest.fixture
def bare_client(root: Path, monkeypatch):
    """没构建过的环境：dist 不存在 → 不挂回退路由。"""
    monkeypatch.setattr(static_mod, "frontend_dist", lambda: None)
    with TestClient(create_app(DocumentStore.for_root(root))) as client:
        yield client


# --------------------------------------------------------------------------- #
# 没构建过：静默跳过
# --------------------------------------------------------------------------- #
def test_no_dist_keeps_api_only(bare_client):
    """dist 不存在 → 根路径仍是 JSON 404（不挂 SPA 回退），接口照常。"""
    response = bare_client.get("/")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert bare_client.get(HEALTH).status_code == 200


def test_dist_without_index_is_skipped(root: Path, tmp_path: Path, monkeypatch):
    """dist 目录在但缺 index.html（构建半截）→ 同样不挂，不在半成品上伺服。"""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "assets").mkdir()
    monkeypatch.setattr(static_mod, "frontend_dist", lambda: dist)
    with TestClient(create_app(DocumentStore.for_root(root))) as client:
        assert client.get("/").status_code == 404


def test_frontend_dist_candidates_are_static_dist_only():
    """``frontend_dist()`` 只认 ``web/dist``：真实仓库要么没有该目录，要么里面有 index。"""
    dist = static_mod.frontend_dist()
    if dist is not None:
        assert dist.parts[-2:] == ("web", "dist")
        assert (dist / static_mod.INDEX_FILE).is_file()


# --------------------------------------------------------------------------- #
# 构建产物：入口 / 哈希资源 / 固定名资源 / SPA 回退
# --------------------------------------------------------------------------- #
def test_index_is_served_with_no_cache(mounted_client):
    response = mounted_client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == NO_CACHE
    assert response.content == INDEX_HTML


def test_hashed_asset_is_immutable(mounted_client):
    response = mounted_client.get("/assets/index-abc123.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == IMMUTABLE_CACHE
    assert response.content == ASSET_JS


def test_fixed_name_asset_is_no_cache(mounted_client):
    """``pdfjs/*`` 是固定名（没有内容哈希）→ 不许长缓存，否则新构建永远刷不掉。"""
    response = mounted_client.get("/pdfjs/worker.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == NO_CACHE
    assert response.content == WORKER_JS


def test_spa_deep_link_falls_back_to_index(mounted_client):
    """前端 hash 路由的深链（``/d/<did>/translate``）在浏览器里解析 → 服务端给入口。"""
    response = mounted_client.get("/d/alpha/translate")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == NO_CACHE
    assert response.content == INDEX_HTML


def test_head_works_for_index(mounted_client):
    response = mounted_client.head("/")
    assert response.status_code == 200
    assert response.headers["cache-control"] == NO_CACHE
    assert response.content == b""


def test_directory_like_path_falls_back_to_index(mounted_client):
    """/assets（目录）不列目录、也不 404 —— 交给前端路由。"""
    response = mounted_client.get("/assets")
    assert response.status_code == 200
    assert response.content == INDEX_HTML


def test_symlink_out_of_dist_is_not_served(root: Path, tmp_path: Path, monkeypatch):
    """dist 里指向外部的符号链接不穿透（与上传端点同一条规则），回退给入口 HTML。"""
    dist = _write_dist(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("SECRET\n", encoding="utf-8")
    (dist / "escape.txt").symlink_to(outside)
    monkeypatch.setattr(static_mod, "frontend_dist", lambda: dist)

    with TestClient(create_app(DocumentStore.for_root(root))) as client:
        response = client.get("/escape.txt")
    assert response.content == INDEX_HTML
    assert b"SECRET" not in response.content


# --------------------------------------------------------------------------- #
# 接口不被前端接管
# --------------------------------------------------------------------------- #
def test_unknown_api_path_still_json_404(mounted_client):
    response = mounted_client.get(f"{API_PREFIX}/nope")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "not_found"


def test_root_level_api_path_still_json_404(mounted_client):
    """``/api`` 本身也不落进 SPA 回退（前缀判定，不是只看精确路径）。"""
    response = mounted_client.get("/api")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "not_found"


def test_openapi_and_docs_are_not_hijacked(mounted_client):
    schema = mounted_client.get("/openapi.json")
    assert schema.status_code == 200
    assert HEALTH in schema.json()["paths"]
    # 回退路由不进 OpenAPI（不是接口）
    assert "/{path}" not in schema.json()["paths"]

    docs = mounted_client.get("/docs")
    assert docs.status_code == 200
    assert docs.content != INDEX_HTML


def test_health_is_not_hijacked(mounted_client):
    response = mounted_client.get(HEALTH)
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


# --------------------------------------------------------------------------- #
# 单元：路径解析与缓存档位
# --------------------------------------------------------------------------- #
def test_resolve_within_rejects_escape_and_symlink(tmp_path: Path):
    """越界路径、符号链接穿透、目录一律不伺服（返回 None）。"""
    dist = _write_dist(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("SECRET\n", encoding="utf-8")
    (dist / "escape.txt").symlink_to(outside)

    root = dist.resolve()
    assert static_mod._resolve_within(root, "../outside.txt") is None
    assert static_mod._resolve_within(root, "escape.txt") is None
    assert static_mod._resolve_within(root, "assets") is None
    assert static_mod._resolve_within(root, "") is None
    assert static_mod._resolve_within(root, "pdfjs/worker.js") == (
        root / "pdfjs" / "worker.js"
    )


def test_cache_control_tiers():
    assert static_mod._cache_control("assets/index-abc123.js") == IMMUTABLE_CACHE
    assert static_mod._cache_control("index.html") == NO_CACHE
    assert static_mod._cache_control("pdfjs/worker.js") == NO_CACHE
    # 名字里带 assets 但不是顶层 assets/ 目录 → 不算哈希资源
    assert static_mod._cache_control("pdfjs/assets-notes.js") == NO_CACHE
