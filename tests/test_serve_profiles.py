"""``bdt serve`` 的 profiles：GET 只见 id/label、PUT 只收脚本路径引用（W08）。

红线（`api.md` §3.5 / EXECUTION.md 第 8 条）：客户端**永远**不能把命令字符串或密钥写进
profile。因此本文件重点覆盖：

- GET 响应里没有命令字段，``.bdt-serve/profiles.json` 里的 ``sk-xxx`` 之类字样不出现；
- PUT 的 ``translator_script``/``reviewer_script`` 必须是白名单目录内的 ``scripts/<name>``
  引用：shell 元字符 → 422 ``forbidden_field``，``..`` 穿越 / 符号链接越界 / 不存在 →
  422 ``script_path_forbidden``；
- 写盘是原子的、只动目标 id；三字段都空 = 删 profile；缺字段 = 不动。

脚本白名单目录有两个：``<store_base>/scripts/``（本测试自建）与仓库 ``scripts/``
（测试里用 monkeypatch 换成 tmp 目录，避免依赖仓库里的真实脚本内容）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="serve 需要 web extra: fastapi")

from babeldoc_tools.serve import profiles as profiles_module  # noqa: E402
from babeldoc_tools.serve.app import create_app  # noqa: E402
from babeldoc_tools.serve.profiles import PROFILES_FILE  # noqa: E402
from babeldoc_tools.serve.profiles import humanize_profile_id  # noqa: E402
from babeldoc_tools.serve.profiles import resolve_profile  # noqa: E402
from babeldoc_tools.serve.profiles import resolve_script_reference  # noqa: E402
from babeldoc_tools.serve.profiles import save_profile  # noqa: E402
from babeldoc_tools.serve.profiles import script_dirs  # noqa: E402
from babeldoc_tools.serve.schemas import API_PREFIX as API  # noqa: E402
from babeldoc_tools.serve.store import STATE_DIR  # noqa: E402
from babeldoc_tools.serve.store import DocumentStore  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

#: 手工写在 profiles.json 里的命令（带假密钥形状）——response 里绝不允许出现它。
COMMAND_WITH_FAKE_KEY = "secret-translator --api-key sk-xxx"


def _write_profiles_file(root: Path, payload: dict) -> None:
    state = root / STATE_DIR
    state.mkdir(parents=True, exist_ok=True)
    (state / PROFILES_FILE).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _read_profiles_file(root: Path) -> dict:
    path = root / STATE_DIR / PROFILES_FILE
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _make_script(directory: Path, name: str = "echo-t.sh") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / name
    script.write_text("#!/bin/sh\ncat\n", encoding="utf-8")
    script.chmod(0o755)
    return script


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """一个已存在的文档 + 两个 profile（其中一个命令里带假密钥）+ 白名单脚本目录。"""
    base = tmp_path / "root"
    (base / "alpha" / "agent").mkdir(parents=True)
    (base / "alpha" / "agent" / "run_state.json").write_text("{}", encoding="utf-8")
    _write_profiles_file(
        base,
        {
            "echo-t": {"translator": COMMAND_WITH_FAKE_KEY},
            "custom-label": {"label": "自定义名", "reviewer": "review-cmd"},
            "keeps-custom-keys": {"translator": "t", "note": "手工写的备注"},
        },
    )
    return base


@pytest.fixture
def scripts_dir(root: Path) -> Path:
    """``<store_base>/scripts/`` 白名单目录（PUT 的合法引用目标）。"""
    return root / "scripts"


@pytest.fixture
def repo_scripts(tmp_path: Path, monkeypatch) -> Path:
    """把仓库 ``scripts/`` 换成 tmp 目录（不依赖仓库里的真实脚本）。"""
    directory = tmp_path / "repo-scripts"
    directory.mkdir()
    monkeypatch.setattr(profiles_module, "REPO_SCRIPTS_DIR", directory)
    return directory


@pytest.fixture
def client(root: Path, scripts_dir: Path, repo_scripts: Path):  # noqa: ARG001
    # repo_scripts 只用副作用（把仓库 scripts/ 指向 tmp），显式请求保证 PUT 用例隔离。
    _make_script(scripts_dir)
    with TestClient(create_app(DocumentStore.for_root(root))) as test_client:
        yield test_client


def put(client, **body):
    return client.put(f"{API}/profiles", json=body)


# --------------------------------------------------------------------------- #
# GET：只有 id/label，命令永不出现
# --------------------------------------------------------------------------- #
def test_get_profiles_lists_ids_labels_and_flags(client):
    listed = client.get(f"{API}/profiles").json()
    assert [item["id"] for item in listed] == [
        "custom-label",
        "echo-t",
        "keeps-custom-keys",
    ]
    by_id = {item["id"]: item for item in listed}
    for item in listed:
        assert set(item) == {"id", "label", "has_translator", "has_reviewer"}
    assert by_id["echo-t"] == {
        "id": "echo-t",
        "label": "Echo T",  # 没有 label → id 的人性化形式
        "has_translator": True,
        "has_reviewer": False,
    }
    # profiles.json 里显式写的 label 赢过人性化默认
    assert by_id["custom-label"]["label"] == "自定义名"
    assert by_id["custom-label"]["has_reviewer"] is True
    assert by_id["custom-label"]["has_translator"] is False
    assert by_id["keeps-custom-keys"]["has_translator"] is True


def test_get_profiles_never_leaks_commands(client):
    response = client.get(f"{API}/profiles")
    assert response.status_code == 200
    assert "sk-xxx" not in response.text
    assert "secret-translator" not in response.text
    assert "review-cmd" not in response.text


def test_get_profiles_empty_when_no_file(tmp_path):
    base = tmp_path / "empty"
    base.mkdir()
    with TestClient(create_app(DocumentStore.for_root(base))) as client:
        assert client.get(f"{API}/profiles").json() == []


def test_env_only_profile_appears_without_commands(client, monkeypatch):
    monkeypatch.setenv("BDT_PROFILE_TEMP_ONLY_TRANSLATOR", COMMAND_WITH_FAKE_KEY)
    listed = {item["id"]: item for item in client.get(f"{API}/profiles").json()}
    assert listed["temp-only"]["has_translator"] is True
    assert listed["temp-only"]["label"] == "Temp Only"
    assert "sk-xxx" not in json.dumps(listed)


def test_humanize_profile_id_is_the_documented_fallback():
    assert humanize_profile_id("deepseek-flash") == "Deepseek Flash"
    assert humanize_profile_id("echo-t") == "Echo T"
    assert humanize_profile_id("plain") == "Plain"


# --------------------------------------------------------------------------- #
# PUT：合法路径引用
# --------------------------------------------------------------------------- #
def test_put_creates_profile_from_a_whitelisted_script_reference(client, root, scripts_dir):
    response = put(
        client,
        id="new-profile",
        label="New Profile",
        translator_script="scripts/echo-t.sh",
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "id": "new-profile",
        "label": "New Profile",
        "has_translator": True,
        "has_reviewer": False,
    }
    # 落盘的是**解析后的绝对路径**（子进程 cwd = 文档 workdir，相对引用在那里解析不到）
    entry = _read_profiles_file(root)["new-profile"]
    assert entry["label"] == "New Profile"
    assert entry["translator"] == str((scripts_dir / "echo-t.sh").resolve())
    assert entry["translator"] != "scripts/echo-t.sh"
    # 响应里没有命令
    assert "echo-t.sh" not in response.text
    # 真的能被 job 解析到（命令直接进 argv）
    assert resolve_profile(root, "new-profile").translator == entry["translator"]


def test_put_uses_repo_scripts_dir_when_store_base_has_no_such_script(
    client, root, repo_scripts
):
    _make_script(repo_scripts, "agy-translator.sh")
    assert put(client, id="repo-script", translator_script="scripts/agy-translator.sh").status_code == 200
    assert (
        _read_profiles_file(root)["repo-script"]["translator"]
        == str((repo_scripts / "agy-translator.sh").resolve())
    )


def test_store_base_scripts_wins_over_repo_scripts(client, root, scripts_dir, repo_scripts):
    _make_script(scripts_dir, "both.sh")
    _make_script(repo_scripts, "both.sh")
    assert put(client, id="both", translator_script="scripts/both.sh").status_code == 200
    assert _read_profiles_file(root)["both"]["translator"] == str(
        (scripts_dir / "both.sh").resolve()
    )


def test_put_updates_only_the_target_entry(client, root):
    put(client, id="echo-t", label="Echo T 新名")
    stored = _read_profiles_file(root)
    assert stored["echo-t"] == {"translator": COMMAND_WITH_FAKE_KEY, "label": "Echo T 新名"}
    # 其它条目（含手工写的自定义键）原样保留
    assert stored["keeps-custom-keys"] == {"translator": "t", "note": "手工写的备注"}
    assert stored["custom-label"] == {"label": "自定义名", "reviewer": "review-cmd"}


def test_put_reviewer_and_label_roundtrip(client, root, scripts_dir):
    _make_script(scripts_dir, "reviewer.sh")
    response = put(
        client,
        id="echo-t",
        reviewer_script="scripts/reviewer.sh",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["has_reviewer"] is True and body["has_translator"] is True
    stored = _read_profiles_file(root)
    assert stored["echo-t"]["translator"] == COMMAND_WITH_FAKE_KEY  # 缺席 = 不动
    assert stored["echo-t"]["reviewer"] == str((scripts_dir / "reviewer.sh").resolve())


def test_put_label_empty_string_clears_it(client, root):
    put(client, id="custom-label", label="")
    assert "label" not in _read_profiles_file(root)["custom-label"]
    listed = {item["id"]: item for item in client.get(f"{API}/profiles").json()}
    assert listed["custom-label"]["label"] == "Custom Label"  # 回退人性化默认


def test_put_deletes_a_field_with_null_and_the_profile_when_empty(client, root):
    response = put(client, id="echo-t", translator_script=None)
    assert response.status_code == 200
    assert response.json()["has_translator"] is False
    # 整个条目空了 → 删掉（GET 不再列、用它提 job 会 unknown_profile）
    assert "echo-t" not in _read_profiles_file(root)
    assert "echo-t" not in [item["id"] for item in client.get(f"{API}/profiles").json()]
    assert resolve_profile(root, "echo-t") is None

    # 只删 reviewer、保留 label：条目仍在，只是没有命令
    _write_profiles_file(root, {"only-reviewer": {"reviewer": "r", "label": "R"}})
    assert put(client, id="only-reviewer", reviewer_script=None).json() == {
        "id": "only-reviewer",
        "label": "R",
        "has_translator": False,
        "has_reviewer": False,
    }
    assert _read_profiles_file(root)["only-reviewer"] == {"label": "R"}


def test_put_writes_atomically_without_leaving_tmp(client, root):
    put(client, id="echo-t", label="atomic")
    state = root / STATE_DIR
    assert sorted(path.name for path in state.iterdir()) == [PROFILES_FILE]
    assert json.loads((state / PROFILES_FILE).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# PUT：拒绝命令字符串与越界引用
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "reference",
    [
        "scripts/echo-t.sh --flag",  # 空格 → 参数拼接
        "scripts/echo-t.sh; rm -rf /",  # 分号
        "scripts/$(whoami).sh",  # $ 展开
        "scripts/`id`.sh",  # 反引号
        "scripts/a|b.sh",
        "scripts/a>b.sh",
        "scripts/echo-t.sh\nid",
        "scripts/echo-t.sh'",
        'scripts/echo-t.sh"',
        "/bin/sh -c id",  # 绝对路径 + 参数
        "echo-t.sh",  # 缺 scripts/ 前缀
        "scripts/",  # 没有文件名
    ],
)
def test_put_rejects_command_like_references_with_forbidden_field(client, root, reference):
    response = put(client, id="evil", translator_script=reference)
    assert response.status_code == 422, reference
    error = response.json()["error"]
    assert error["code"] == "forbidden_field", reference
    # 不回显违规值（形状违规的字符串可能整条就是命令）
    assert error["detail"] == {"field": "translator_script"}
    assert "evil" not in _read_profiles_file(root)


@pytest.mark.parametrize(
    "reference",
    [
        "scripts/nope.sh",  # 不存在
        "scripts/../../etc/passwd",  # 穿越（形状合法但解析到白名单目录外）
        "scripts/sub/../nope.sh",  # 子目录穿越
    ],
)
def test_put_rejects_escaped_or_missing_script_with_script_path_forbidden(
    client, root, scripts_dir, reference
):
    response = put(client, id="missing", reviewer_script=reference)
    assert response.status_code == 422, reference
    error = response.json()["error"]
    assert error["code"] == "script_path_forbidden"
    assert error["detail"]["field"] == "reviewer_script"
    assert error["detail"]["reference"] == reference
    # detail 里给出搜索过的白名单目录（不含命令）
    assert str(scripts_dir.resolve()) in error["detail"]["searched"]
    assert "missing" not in _read_profiles_file(root)


def test_put_rejects_symlink_escaping_the_whitelist(client, root, scripts_dir, tmp_path):
    outside = tmp_path / "outside.sh"
    outside.write_text("#!/bin/sh\n", encoding="utf-8")
    (scripts_dir / "evil.sh").symlink_to(outside)
    response = put(client, id="evil", translator_script="scripts/evil.sh")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "script_path_forbidden"
    assert "evil" not in _read_profiles_file(root)


def test_put_forbidden_fields_are_422_and_not_echoed(client, root):
    for field in ("translator", "reviewer", "command", "shell", "env", "api_key", "token"):
        response = put(
            client,
            id="evil",
            translator_script="scripts/echo-t.sh",
            **{field: "rm -rf / --api-key sk-xxx"},
        )
        assert response.status_code == 422, field
        error = response.json()["error"]
        assert error["code"] == "forbidden_field", field
        assert error["detail"]["field"] == field
        assert "rm -rf" not in response.text and "sk-xxx" not in response.text
    assert "evil" not in _read_profiles_file(root)


def test_put_requires_at_least_one_change_and_a_valid_id(client):
    only_id = client.put(f"{API}/profiles", json={"id": "echo-t"})
    assert only_id.status_code == 422
    assert only_id.json()["error"]["code"] == "validation_error"

    bad_id = put(client, id="Bad Id!", label="x")
    assert bad_id.status_code == 422
    assert bad_id.json()["error"]["code"] == "validation_error"

    long_label = put(client, id="echo-t", label="x" * 81)
    assert long_label.status_code == 422
    assert long_label.json()["error"]["code"] == "validation_error"


def test_put_forbidden_field_wins_over_other_validation_errors(client, root):
    """带了命令字段就先报 ``forbidden_field``，不被“缺 id / 没给可改字段”的校验盖掉。

    W08 冒烟实测过这个顺序：禁止字段的检查写成依赖（模型校验之前跑），否则调用方看到的
    是 ``validation_error``，会以为“只是参数没写全”，而不是“这类输入不接受”。
    """
    for body in (
        {"translator": "echo hacked"},  # 连 id 都没有
        {"id": "evil", "translator": "echo hacked"},  # 有 id 但没给可改字段
        {"id": "evil", "label": "x", "api_key": "sk-xxx"},  # 合法字段 + 禁止字段
    ):
        response = client.put(f"{API}/profiles", json=body)
        assert response.status_code == 422, body
        error = response.json()["error"]
        assert error["code"] == "forbidden_field", body
        assert "hacked" not in response.text and "sk-xxx" not in response.text
    assert "evil" not in _read_profiles_file(root)


# --------------------------------------------------------------------------- #
# 纯函数：脚本引用解析 / 原子写
# --------------------------------------------------------------------------- #
def test_script_dirs_lists_both_whitelists(tmp_path, monkeypatch):
    repo = tmp_path / "repo-scripts"
    repo.mkdir()
    store_base = tmp_path / "root"
    (store_base / "scripts").mkdir(parents=True)
    monkeypatch.setattr(profiles_module, "REPO_SCRIPTS_DIR", repo)
    dirs = script_dirs(store_base)
    assert [str(path) for path in dirs] == [
        str((store_base / "scripts").resolve()),
        str(repo.resolve()),
    ]
    assert script_dirs(store_base) == script_dirs(store_base)  # 稳定顺序（store_base 优先）


def test_script_dirs_skips_missing_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles_module, "REPO_SCRIPTS_DIR", tmp_path / "nope")
    assert script_dirs(tmp_path / "empty-root") == ()


def test_resolve_script_reference_error_codes(tmp_path, monkeypatch):
    repo = tmp_path / "repo-scripts"
    (repo / "sub").mkdir(parents=True)
    (repo / "sub" / "deep.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(profiles_module, "REPO_SCRIPTS_DIR", repo)

    from babeldoc_tools.common import ToolError

    with pytest.raises(ToolError) as shape:
        resolve_script_reference("scripts/a b.sh", tmp_path)
    assert shape.value.code == "forbidden_field"
    with pytest.raises(ToolError) as missing:
        resolve_script_reference("scripts/none.sh", tmp_path)
    assert missing.value.code == "script_path_forbidden"
    # 子目录引用合法（形状里允许 '/'）
    assert resolve_script_reference("scripts/sub/deep.sh", tmp_path) == (
        repo / "sub" / "deep.sh"
    ).resolve()


def test_save_profile_rejects_unknown_fields_and_keeps_bad_json_readable(tmp_path):
    with pytest.raises(ValueError):
        save_profile(tmp_path, "x", {"translator_script": "scripts/a.sh"})  # 键名错
    # 坏 JSON 起步：写一条仍然得到合法 JSON（不猜旧内容）
    state = tmp_path / STATE_DIR
    state.mkdir(parents=True)
    (state / PROFILES_FILE).write_text("{ broken", encoding="utf-8")
    save_profile(tmp_path, "x", {"label": "X"})
    assert _read_profiles_file(tmp_path) == {"x": {"label": "X"}}


def test_profiles_file_is_the_only_state_written(tmp_path):
    """写盘只落到 ``<store_base>/.bdt-serve/profiles.json``（没有第二份状态、不留 tmp）。"""
    save_profile(tmp_path, "a", {"translator": "cmd"})
    assert (tmp_path / STATE_DIR / PROFILES_FILE).is_file()
    assert sorted(path.name for path in (tmp_path / STATE_DIR).iterdir()) == [
        PROFILES_FILE
    ]
