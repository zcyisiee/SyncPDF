"""provider profile 解析（``docs/frontend/api.md`` §3.4；W07 最小版，W08 扩成 CRUD + UI）。

profile 是**命令的唯一来源**：客户端只能说 profile id，translator/reviewer 命令字符串
由服务端在这里解析后直接进 argv 构造（见
:func:`babeldoc_tools.serve.runner.build_job_argv`），**永不回传给客户端** —— 响应里
只有 id，避免把密钥或任意 shell 命令经 HTTP 泄出去。

两个来源，环境变量覆盖文件：

1. ``<store_base>/.bdt-serve/profiles.json``::

       {
           "deepseek-flash": {
               "translator": "scripts/agy-translator.sh",
               "reviewer": "...",
           }
       }

2. ``BDT_PROFILE_<ID_UPPER>_TRANSLATOR`` / ``BDT_PROFILE_<ID_UPPER>_REVIEWER``
   （id 里的 ``-`` 写成 ``_``）：测试与本地临时覆盖用。

文件的条目可选带 ``label``（给前端看的人性化名字，没有就用 id 兜底）。W08 起增加
**写入口** :func:`save_profile`（``PUT /profiles`` 用），它只接受**脚本路径引用**
（``scripts/<name>``，见 :func:`resolve_script_reference`）并把它解析成绝对路径再落盘：
客户端永远无法直接写一个命令字符串或密钥进 profiles.json。

命令本身不由本模块校验：最终由 ``babeldoc_tools.common._run_subprocess`` 经
``shlex.split`` 执行（不经 shell）。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from pydantic import BaseModel

from babeldoc_tools.common import ToolError
from babeldoc_tools.serve.store import STATE_DIR

__all__ = [
    "ENV_PREFIX",
    "LABEL_FIELD",
    "PROFILES_FILE",
    "PROFILE_FIELDS",
    "PROFILE_ID_RE",
    "REPO_SCRIPTS_DIR",
    "SCRIPT_DIR_NAME",
    "SCRIPT_REF_RE",
    "Profile",
    "humanize_profile_id",
    "list_profile_ids",
    "load_profiles",
    "profile_env_command",
    "resolve_profile",
    "resolve_script_reference",
    "save_profile",
    "script_dirs",
]

#: ``<store_base>/.bdt-serve/profiles.json``。
PROFILES_FILE = "profiles.json"

#: env 覆盖前缀（``-`` → ``_``，大写）。
ENV_PREFIX = "BDT_PROFILE_"
#: env 覆盖字段（与 JSON 里的键同名）。
ENV_FIELDS = ("translator", "reviewer")
#: profiles.json 条目里可写、可被本模块读的字段（``save_profile`` 只认这三个键）。
PROFILE_FIELDS = ("label", "translator", "reviewer")
#: 人性化显示名字段（前端只从这里/ id 兜底拿显示文案）。
LABEL_FIELD = "label"

#: profile id 形状（与 ``docs/frontend/api.md`` §3.4 的 ``[a-z0-9-]{1,64}`` 一致）。
PROFILE_ID_RE = r"^[a-z0-9-]{1,64}$"

#: 脚本引用白名单目录名（相对 ``<store_base>`` 与仓库根各一个）。
SCRIPT_DIR_NAME = "scripts"
#: 仓库自带的脚本目录（``<repo>/scripts``）—— 与 ``<store_base>/scripts`` 同为白名单。
REPO_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / SCRIPT_DIR_NAME
#: 脚本路径引用的**唯一**合法形状：``scripts/`` 前缀 + 单段字符集（无空格/引号/``$``/``;``）。
SCRIPT_REF_RE = re.compile(r"^scripts/[A-Za-z0-9._/-]+$")


class Profile(BaseModel):
    """一个 provider profile 的**服务端**配置。

    ``translator``/``reviewer`` 是命令字符串（stdin 读提示词、stdout 出结果），只喂给
    argv 构造；它们不出现在任何 HTTP 响应里。``label`` 是可选的人性化显示名。
    """

    id: str
    model_profile: bool = False
    translator: str | None = None
    reviewer: str | None = None
    label: str | None = None


def profiles_path(store_base: Path | str) -> Path:
    """``<store_base>/.bdt-serve/profiles.json``。"""
    return Path(store_base) / STATE_DIR / PROFILES_FILE


def load_profiles(store_base: Path | str) -> dict[str, Profile]:
    """读 profiles.json；缺失 / 坏 JSON / 条目形状不符一律忽略（不猜、不假装有）。"""
    try:
        payload = json.loads(profiles_path(store_base).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    profiles: dict[str, Profile] = {}
    for profile_id, entry in payload.items():
        if not isinstance(profile_id, str) or not isinstance(entry, dict):
            continue
        profiles[profile_id] = Profile(
            id=profile_id,
            translator=_clean(entry.get("translator")),
            reviewer=_clean(entry.get("reviewer")),
            label=_clean(entry.get(LABEL_FIELD)),
        )
    return profiles


def profile_env_command(profile_id: str, field: str) -> str | None:
    """``BDT_PROFILE_<ID_UPPER>_<FIELD_UPPER>`` 覆盖值；没设/为空 → ``None``。"""
    name = f"{ENV_PREFIX}{profile_id.upper().replace('-', '_')}_{field.upper()}"
    return _clean(os.environ.get(name))


def list_profile_ids(store_base: Path | str) -> list[str]:
    """已知 profile id（文件 ∪ env），排序后返回。

    只列 **id**：``unknown_profile`` 的错误 detail 用它给前端提示，不能把命令串列出去。
    """
    from babeldoc_tools.serve.models import load_models

    ids = set(load_profiles(store_base)) | _env_profile_ids() | set(load_models(store_base))
    return sorted(ids)


def resolve_profile(store_base: Path | str, profile_id: str) -> Profile | None:
    """profile id → :class:`Profile`（env 覆盖文件里同名字段）；未知 id → ``None``。

    "已知" = 文件里有这个 id，或该 id 有 env 覆盖。条目存在但两个命令都为空时仍然算
    已知（是否真的能翻译由 ``bdt run`` 如实报错，这里不替它假装有命令）。
    """
    from babeldoc_tools.serve.models import load_models
    from babeldoc_tools.serve.models import model_command

    model = load_models(store_base).get(profile_id)
    if model is not None:
        if profile_id in load_profiles(store_base) or profile_id in _env_profile_ids():
            raise ToolError("profile_collision", "Model and script profile IDs conflict")
        return Profile(id=profile_id, label=model["label"], model_profile=True,
                       translator=model_command(store_base, profile_id))
    known = load_profiles(store_base).get(profile_id)
    translator = profile_env_command(profile_id, "translator") or (
        known.translator if known else None
    )
    reviewer = profile_env_command(profile_id, "reviewer") or (
        known.reviewer if known else None
    )
    if known is None and translator is None and reviewer is None:
        return None
    return Profile(
        id=profile_id,
        translator=translator,
        reviewer=reviewer,
        label=known.label if known else None,
    )


def humanize_profile_id(profile_id: str) -> str:
    """没有 ``label`` 时的兜底显示名：``deepseek-flash`` → ``Deepseek Flash``。"""
    parts = [part.capitalize() for part in profile_id.split("-") if part]
    return " ".join(parts) or profile_id


# --------------------------------------------------------------------------- #
# 写入口（PUT /profiles）：只接受脚本路径引用
# --------------------------------------------------------------------------- #
def script_dirs(store_base: Path | str) -> tuple[Path, ...]:
    """脚本引用的白名单目录（解析后的绝对路径，去重）：``<store_base>/scripts`` 与仓库 ``scripts/``。

    只返回**已存在**的目录：不存在的目录不可能命中一个真实脚本，也避免把它报进
    ``script_path_forbidden`` 的 ``searched`` 里误导人。
    """
    dirs: list[Path] = []
    for candidate in (Path(store_base) / SCRIPT_DIR_NAME, REPO_SCRIPTS_DIR):
        resolved = candidate.expanduser().resolve()
        if resolved.is_dir() and resolved not in dirs:
            dirs.append(resolved)
    return tuple(dirs)


def resolve_script_reference(
    reference: str, store_base: Path | str, *, field: str = "script"
) -> Path:
    """``scripts/<name>`` → 白名单目录内的脚本绝对路径（PUT /profiles 的校验点）。

    两道关：

    1. **形状**：必须是 ``scripts/`` 开头的相对引用，字符集只允许 ``[A-Za-z0-9._/-]`` —— 空格、
       引号、``;``、``$``、``|`` 之类 shell 元字符与绝对路径一律 **422 ``forbidden_field``**
       （客户端不能借这个字段塞命令）；
    2. **位置**：解析（``Path.resolve()``）后必须落在 :func:`script_dirs` 的某个目录**内**且
       确实是个文件 —— ``..`` 穿越、符号链接越界、不存在都是 **422 ``script_path_forbidden``**。

    通过后返回绝对路径：子进程继承 serve 的 cwd（= job 的 workdir），相对引用在那里
    解析不到，所以落盘的就是这个绝对路径。
    """
    text = reference.strip() if isinstance(reference, str) else ""
    if not SCRIPT_REF_RE.match(text):
        # 不回显原值：形状违规的字符串可能整个就是一条命令（W07 的 forbid 字段同一口径）。
        raise ToolError(
            "forbidden_field",
            (
                "只接受白名单目录内的脚本路径引用（形如 scripts/agy-translator.sh）；"
                "不接受命令字符串、shell 元字符或绝对路径"
            ),
            field=field,
        )
    name = text[len(SCRIPT_DIR_NAME) + 1 :]
    dirs = script_dirs(store_base)
    for directory in dirs:
        candidate = (directory / name).resolve()
        if directory not in candidate.parents:
            continue  # 穿透 / 符号链接越界
        if candidate.is_file():
            return candidate
    raise ToolError(
        "script_path_forbidden",
        f"脚本不在白名单目录内或不存在：{text}",
        field=field,
        reference=text[:120],
        searched=[str(directory) for directory in dirs],
    )


def save_profile(
    store_base: Path | str, profile_id: str, updates: dict[str, str | None]
) -> None:
    """按 ``updates`` 局部更新 ``profiles.json`` 里的一条（原子写）。

    - ``updates`` 的键只能是 :data:`PROFILE_FIELDS`（``label``/``translator``/``reviewer``），
      值 ``None`` = 删该字段、空条目 = 删整个 profile；
    - **只动目标 id 这一条**：文件里其它条目（含本模块不认识的自定义键）原样保留；
    - 写盘是**同目录 tmp + ``os.replace``**：读方（另一个 HTTP 请求/子进程）永远看到完整 JSON。
    """
    from babeldoc_tools.serve.models import _LOCK
    from babeldoc_tools.serve.models import load_models

    with _LOCK:
        if profile_id in load_models(store_base):
            raise ToolError("profile_collision", "ID already belongs to a model configuration")
        _save_script_profile(store_base, profile_id, updates)


def _save_script_profile(store_base, profile_id, updates):
    unknown = set(updates) - set(PROFILE_FIELDS)
    if unknown:
        raise ValueError(f"save_profile 不接受这些字段：{sorted(unknown)}")
    raw = _load_raw(store_base)
    entry = raw.get(profile_id)
    entry = dict(entry) if isinstance(entry, dict) else {}
    for key, value in updates.items():
        cleaned = _clean(value)
        if cleaned is None:
            entry.pop(key, None)
        else:
            entry[key] = cleaned
    if entry:
        raw[profile_id] = entry
    else:
        raw.pop(profile_id, None)
    _write_raw(store_base, raw)


def _load_raw(store_base: Path | str) -> dict[str, object]:
    """读 profiles.json 的**原始**结构（坏 JSON/非 dict → 空表，不猜、不备份）。"""
    try:
        payload = json.loads(profiles_path(store_base).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_raw(store_base: Path | str, payload: dict[str, object]) -> None:
    """原子写 profiles.json（同目录 tmp + replace；不新增第二个拼写点）。"""
    path = profiles_path(store_base)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(path)


def _env_profile_ids() -> set[str]:
    """``BDT_PROFILE_<ID>_<FIELD>`` 反推出 id（``_`` → ``-``，小写）。"""
    ids: set[str] = set()
    for name in os.environ:
        if not name.startswith(ENV_PREFIX):
            continue
        body = name[len(ENV_PREFIX) :]
        for field in ENV_FIELDS:
            suffix = f"_{field.upper()}"
            if body.endswith(suffix) and body[: -len(suffix)]:
                ids.add(body[: -len(suffix)].lower().replace("_", "-"))
    return ids


def _clean(value: object) -> str | None:
    """命令字符串归一：非字符串/空白 → ``None``（空命令不等于"没配命令"）。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
