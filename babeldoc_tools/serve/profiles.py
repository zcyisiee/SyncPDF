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

本模块**只读**文件（缺失/坏 JSON → 没有 profile），也不校验命令本身：命令是同样的
本地配置，最终由 ``babeldoc_tools.common._run_subprocess`` 经 ``shlex.split`` 执行
（不经 shell）。写入口留给 W08。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel

from babeldoc_tools.serve.store import STATE_DIR

__all__ = [
    "ENV_PREFIX",
    "PROFILES_FILE",
    "PROFILE_ID_RE",
    "Profile",
    "list_profile_ids",
    "load_profiles",
    "profile_env_command",
    "resolve_profile",
]

#: ``<store_base>/.bdt-serve/profiles.json``。
PROFILES_FILE = "profiles.json"

#: env 覆盖前缀（``-`` → ``_``，大写）。
ENV_PREFIX = "BDT_PROFILE_"
#: env 覆盖字段（与 JSON 里的键同名）。
ENV_FIELDS = ("translator", "reviewer")

#: profile id 形状（与 ``docs/frontend/api.md`` §3.4 的 ``[a-z0-9-]{1,64}`` 一致）。
PROFILE_ID_RE = r"^[a-z0-9-]{1,64}$"


class Profile(BaseModel):
    """一个 provider profile 的**服务端**配置。

    ``translator``/``reviewer`` 是命令字符串（stdin 读提示词、stdout 出结果），只喂给
    argv 构造；它们不出现在任何 HTTP 响应里。
    """

    id: str
    translator: str | None = None
    reviewer: str | None = None


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
    ids = set(load_profiles(store_base)) | _env_profile_ids()
    return sorted(ids)


def resolve_profile(store_base: Path | str, profile_id: str) -> Profile | None:
    """profile id → :class:`Profile`（env 覆盖文件里同名字段）；未知 id → ``None``。

    "已知" = 文件里有这个 id，或该 id 有 env 覆盖。条目存在但两个命令都为空时仍然算
    已知（是否真的能翻译由 ``bdt run`` 如实报错，这里不替它假装有命令）。
    """
    known = load_profiles(store_base).get(profile_id)
    translator = profile_env_command(profile_id, "translator") or (
        known.translator if known else None
    )
    reviewer = profile_env_command(profile_id, "reviewer") or (
        known.reviewer if known else None
    )
    if known is None and translator is None and reviewer is None:
        return None
    return Profile(id=profile_id, translator=translator, reviewer=reviewer)


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
