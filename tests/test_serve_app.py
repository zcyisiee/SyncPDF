"""``bdt serve`` 的 HTTP 层：health、OpenAPI、统一错误信封、workdir 限制。

用 FastAPI ``TestClient`` 走真实路由（不起 uvicorn、不联网）。缺 web extra 的
环境自动 skip，保证其它测试环境不因可选依赖失败。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="serve 需要 web extra: fastapi")

from babeldoc_tools import __version__  # noqa: E402
from babeldoc_tools.__main__ import _build_parser  # noqa: E402
from babeldoc_tools.serve.app import create_app  # noqa: E402
from babeldoc_tools.serve.schemas import API_PREFIX  # noqa: E402
from babeldoc_tools.serve.store import DocumentStore  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

HEALTH = f"{API_PREFIX}/health"


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
    _make_workdir(base, "beta")
    return base


@pytest.fixture
def client(root: Path):
    with TestClient(create_app(DocumentStore.for_root(root))) as test_client:
        yield test_client


# --------------------------------------------------------------------------- #
# health
# --------------------------------------------------------------------------- #
def test_health_shape(client, root):
    response = client.get(HEALTH)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"status", "api", "version", "mode", "root", "documents"}
    assert body["status"] == "ok"
    assert body["api"] == API_PREFIX
    assert body["version"] == __version__
    assert body["mode"] == "root"
    assert body["root"] == str(root.resolve())
    assert body["documents"] == 2


def test_health_workdir_mode_counts_one(root):
    store = DocumentStore.for_workdir(root / "alpha")
    with TestClient(create_app(store)) as client:
        body = client.get(HEALTH).json()
    assert body["mode"] == "workdir"
    assert body["documents"] == 1
    # 兄弟目录不作为文档暴露
    assert body["root"] == str(root.resolve())


def test_health_reports_root_missing_as_503(client, root):
    """根目录运行期消失 → 503 + ``root_missing``（经 ToolError 处理器，不谎报 ok）。"""
    shutil.rmtree(root)
    response = client.get(HEALTH)
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "root_missing"
    assert str(root.resolve()) in error["message"]
    assert error["detail"]["root"] == str(root.resolve())


# --------------------------------------------------------------------------- #
# OpenAPI
# --------------------------------------------------------------------------- #
def test_openapi_exposes_health(client):
    response = client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"] == "bdt serve"
    assert HEALTH in schema["paths"]
    assert set(schema["paths"][HEALTH]) == {"get"}
    assert schema["paths"][HEALTH]["get"]["responses"]["200"]


def test_docs_page_available(client):
    assert client.get("/docs").status_code == 200


# --------------------------------------------------------------------------- #
# 错误信封
# --------------------------------------------------------------------------- #
def test_unknown_route_uses_error_envelope(client):
    response = client.get(f"{API_PREFIX}/nope")
    assert response.status_code == 404
    body = response.json()
    assert list(body) == ["error"]
    assert set(body["error"]) == {"code", "message"}  # detail 缺省不出现
    assert body["error"]["code"] == "not_found"
    assert f"GET {API_PREFIX}/nope" in body["error"]["message"]


def test_wrong_method_uses_error_envelope(client):
    response = client.post(HEALTH)
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


def test_error_envelope_shape_is_parseable(client):
    """所有错误响应都能按 ``{"error": {code, message}}`` 解析（前端只写一套解析）。"""
    for response in (
        client.get(f"{API_PREFIX}/nope"),
        client.post(HEALTH),
    ):
        error = response.json()["error"]
        assert isinstance(error["code"], str) and error["code"]
        assert isinstance(error["message"], str) and error["message"]


def test_no_cors_headers_for_foreign_origin(client):
    """不注册 CORSMiddleware：任意来源拿不到 ``Access-Control-Allow-*``。"""
    response = client.get(HEALTH, headers={"Origin": "http://evil.example"})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
    assert "access-control-allow-credentials" not in response.headers


def test_preflight_not_enabled(client):
    response = client.options(
        HEALTH,
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert "access-control-allow-origin" not in response.headers


# --------------------------------------------------------------------------- #
# 只读性与 app factory
# --------------------------------------------------------------------------- #
#: W08 之后唯一允许的写端点（``(method, path 模板)``）。**白名单的单一来源**：
#: 其它 serve 测试模块 import :func:`assert_no_unexpected_write_routes` 来断言这条
#: 安全边界，避免白名单在四处漂移。新增写端点必须显式改这里。
ALLOWED_WRITE_ROUTES = frozenset(
    {
        ("post", f"{API_PREFIX}/documents"),
        ("post", f"{API_PREFIX}/documents/{{did}}/jobs"),
        ("post", f"{API_PREFIX}/jobs/{{jid}}/cancel"),
        ("put", f"{API_PREFIX}/profiles"),
        # W09 草稿（§3.3）：只有这两个方法会改盘，GET 仍是只读。
        ("patch", f"{API_PREFIX}/documents/{{did}}/draft"),
        ("delete", f"{API_PREFIX}/documents/{{did}}/draft"),
        # W11 候选（§3.6）：生成（建候选 + 提 job）、采用（写草稿）、拒绝（只改候选状态）。
        ("post", f"{API_PREFIX}/documents/{{did}}/paragraphs/{{pid}}/retranslate"),
        ("post", f"{API_PREFIX}/documents/{{did}}/paragraphs/{{pid}}/candidates/{{cid}}/adopt"),
        ("post", f"{API_PREFIX}/documents/{{did}}/paragraphs/{{pid}}/candidates/{{cid}}/reject"),
    }
)


def assert_no_unexpected_write_routes(schema: dict) -> None:
    """OpenAPI 里除白名单内的写端点外，所有端点必须只有 GET。

    W01–W03 是只读服务；W07 加了两条 POST（提交/取消 job）；W08 加了上传（POST）与
    profile 写入（PUT）；W11 加了候选的三条 POST（生成/采用/拒绝）。这里的断言是
    **安全边界**：多出任何写方法都算越界（而不是"测试过时了"）。
    """
    for path, item in schema["paths"].items():
        for method in item:
            if method == "get":
                continue
            assert (method, path) in ALLOWED_WRITE_ROUTES, (
                f"{method.upper()} {path} 不在写端点白名单里"
            )


def test_openapi_has_only_the_allowlisted_write_routes(client):
    """除 job 的两条 POST 与 W08 的上传/profile 写端点外没有任何写端点（不写假成功 stub）。"""
    schema = client.get("/openapi.json").json()
    assert_no_unexpected_write_routes(schema)
    # 白名单不是空集：两条 job 写端点确实注册在 OpenAPI 里
    for method, path in ALLOWED_WRITE_ROUTES:
        assert method in schema["paths"][path], f"{method.upper()} {path} 没有注册"

def test_factory_does_not_touch_filesystem_beyond_reading(root):
    """create_app 纯工厂：不创建/不修改任何文件。"""
    before = sorted(p.name for p in root.iterdir())
    create_app(DocumentStore.for_root(root))
    assert sorted(p.name for p in root.iterdir()) == before


# --------------------------------------------------------------------------- #
# CLI 接入（bdt serve 是唯一入口）
# --------------------------------------------------------------------------- #
def test_serve_target_is_required_and_exclusive(capsys):
    """``--root`` / ``--workdir`` 二选一且必填（argparse 用法错误 = exit 2）。"""
    parser = _build_parser()
    for argv in (["serve"], ["serve", "--root", "a", "--workdir", "b"]):
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_args(argv)
        assert excinfo.value.code == 2
    assert capsys.readouterr().err


def test_serve_defaults_are_loopback_and_auto_port():
    args = _build_parser().parse_args(["serve", "--root", "tmp"])
    assert args.host == "127.0.0.1"
    assert args.port == 0
    assert args.open is False
    assert args.workdir is None


def test_serve_cli_import_does_not_require_web_extra():
    """``serve`` 的命令行模块不 import fastapi/uvicorn（--help 无需 web extra）。"""
    code = (
        "import sys\n"
        "from babeldoc_tools.serve import cli\n"
        "assert 'fastapi' not in sys.modules, 'fastapi imported'\n"
        "assert 'uvicorn' not in sys.modules, 'uvicorn imported'\n"
        "print('ok')\n"
    )
    out = subprocess.run(  # noqa: S603 - 固定 argv，无外部输入
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


def test_serve_help_without_web_extra():
    """屏蔽 fastapi/uvicorn 后 ``bdt serve --help`` 仍打印帮助并 exit 0。"""
    code = (
        "import sys\n"
        "sys.modules['fastapi'] = None\n"
        "sys.modules['uvicorn'] = None\n"
        "from babeldoc_tools.__main__ import main\n"
        "raise SystemExit(main(['serve', '--help']))\n"
    )
    out = subprocess.run(  # noqa: S603 - 固定 argv，无外部输入
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr
    assert "--root" in out.stdout and "--workdir" in out.stdout


def test_serve_reports_missing_web_extra(tmp_path, monkeypatch, capsys):
    """缺依赖：stdout 一个 ``web_extra_missing`` 信封 + exit 1，错误里给安装命令。"""
    root = tmp_path / "root"
    _make_workdir(root, "alpha")
    args = _build_parser().parse_args(["serve", "--root", str(root)])
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    monkeypatch.setitem(sys.modules, "fastapi", None)

    from babeldoc_tools.serve.cli import run

    assert run(args) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "web_extra_missing"
    assert "web extra" in payload["error"]["message"]


def test_serve_reports_invalid_root(tmp_path, capsys):
    """根目录不存在：启动前就报 ``invalid_root``（不建半个服务）。"""
    args = _build_parser().parse_args(["serve", "--root", str(tmp_path / "nope")])

    from babeldoc_tools.serve.cli import run

    assert run(args) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "invalid_root"


def test_serve_rejects_out_of_range_port(tmp_path, capsys):
    root = tmp_path / "root"
    _make_workdir(root, "alpha")
    args = _build_parser().parse_args(["serve", "--root", str(root), "--port", "70000"])

    from babeldoc_tools.serve.cli import run

    assert run(args) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "invalid_port"
