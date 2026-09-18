"""``/api/v1/profiles``：provider profile 的读写（``api.md`` §3.5，W08）。

前端只能看见 **id 与 label**（``GET`` 的响应形状里根本没有命令字段）：translator/reviewer
命令字符串（可能内嵌密钥）永不进任何 HTTP 响应，只在服务端解析后进 argv。

写入口（``PUT``）只接受**脚本路径引用**：``translator_script``/``reviewer_script`` 必须是
``scripts/<name>``，且解析后落在 ``<store_base>/scripts/`` 或仓库 ``scripts/`` 白名单目录内
（越界/符号链接穿越/不存在都拒）。服务端把它解析成绝对路径再写进
``<store_base>/.bdt-serve/profiles.json``，因此客户端**无法**直接写一个命令字符串、参数
或密钥进 profile。

字段语义（``PUT`` 的 body，字段**缺席**与显式 ``null`` 不同）：

===============  ===================================  =====================
字段             值                                   效果
===============  ===================================  =====================
``id``           必填，``[a-z0-9-]{1,64}``            目标 profile
``label``        字符串（≤80）                        设显示名；``null``/空串清掉
``translator_script``  ``scripts/<name>`` 或 ``null``   设/删 translator（``null`` = 删该字段）
``reviewer_script``    同上                              设/删 reviewer
===============  ===================================  =====================

三个字段都空 → **删掉整个 profile**（id 不再出现在 ``GET`` 里，用它提交 job 会
``422 unknown_profile``）。``translator``/``reviewer``/``api_key`` 之类字段收到即
``422 forbidden_field``（不是"参数错了"，是"这类输入不接受"），不静默忽略：这个判断由
:func:`babeldoc_tools.serve.routers.jobs.reject_forbidden_body` **依赖**在模型校验之前执行，
所以 body 里带了这类字段时，先报的一定是它，而不是别的缺字段校验。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter
from fastapi import Depends

from babeldoc_tools.harnesses import BUILTINS
from babeldoc_tools.serve.profiles import humanize_profile_id
from babeldoc_tools.serve.profiles import list_profile_ids
from babeldoc_tools.serve.profiles import resolve_profile
from babeldoc_tools.serve.profiles import resolve_script_reference
from babeldoc_tools.serve.profiles import save_profile
from babeldoc_tools.serve.routers.jobs import reject_forbidden_body
from babeldoc_tools.serve.schemas import API_PREFIX
from babeldoc_tools.serve.schemas import ProfileListItem
from babeldoc_tools.serve.schemas import ProfileUpdateRequest
from babeldoc_tools.serve.store import DocumentStore

__all__ = ["SCRIPT_FIELDS", "profile_item", "profiles_router"]

#: ``PUT /profiles`` 里"脚本引用"字段 → profiles.json / :class:`Profile` 的字段名。
SCRIPT_FIELDS = {
    "translator_script": "translator",
    "reviewer_script": "reviewer",
}


def profile_item(store_base: Path | str, profile_id: str) -> ProfileListItem:
    """一个 profile 的对外摘要：**只有** id/label/has_*（命令字符串永不出现）。"""
    profile = resolve_profile(store_base, profile_id)
    label = (profile.label if profile is not None else None) or humanize_profile_id(
        profile_id
    )
    return ProfileListItem(
        id=profile_id,
        label=label,
        builtin=profile_id in BUILTINS,
        model=BUILTINS[profile_id].model if profile_id in BUILTINS else None,
        thinking_levels=list(BUILTINS[profile_id].thinking_levels) if profile_id in BUILTINS else [],
        default_thinking=BUILTINS[profile_id].default_thinking if profile_id in BUILTINS else None,
        has_translator=bool(profile is not None and profile.translator),
        has_reviewer=bool(profile is not None and profile.reviewer),
    )


def profiles_router(store: DocumentStore) -> APIRouter:
    """按 store 生成 profile 路由（profiles.json 落在 ``store.store_base`` 下）。"""
    router = APIRouter(prefix=API_PREFIX, tags=["profiles"])

    @router.get(
        "/profiles",
        response_model=list[ProfileListItem],
        response_model_exclude_defaults=True,
        summary="profile 列表（只有 id/label）",
        description=(
            "已知 profile id（``<store_base>/.bdt-serve/profiles.json`` ∪ "
            "``BDT_PROFILE_<ID>_<FIELD>`` 环境变量），按 id 排序。"
            "**响应里没有命令字段**：translator/reviewer 命令只存在于服务端。"
            "``has_translator``/``has_reviewer`` 只说明配没配，不说明配的是什么。"
        ),
    )
    def list_profiles() -> list[ProfileListItem]:
        return [
            profile_item(store.store_base, profile_id)
            for profile_id in list_profile_ids(store.store_base)
        ]

    @router.put(
        "/profiles",
        response_model=ProfileListItem,
        response_model_exclude_defaults=True,
        summary="新建 / 更新 / 删除 profile（只接受脚本路径引用）",
        description=(
            "局部更新一条 profile 并原子写回 profiles.json。"
            "``translator_script``/``reviewer_script`` 是**脚本路径引用**（``scripts/<name>``，"
            "必须落在 ``<store_base>/scripts/`` 或仓库 ``scripts/`` 内，符号链接越界拒绝），"
            "不是命令字符串：含 shell 元字符 → 422 ``forbidden_field``，白名单外/不存在 → "
            "422 ``script_path_forbidden``。字段缺席 = 不动，显式 null = 删该字段，"
            "三个字段都空 = 删整个 profile。响应形状与 GET 的条目相同（无命令）。"
        ),
        responses={
            422: {
                "description": (
                    "forbidden_field / script_path_forbidden / validation_error"
                )
            }
        },
    )
    async def put_profile(
        payload: ProfileUpdateRequest,
        _forbidden: Annotated[None, Depends(reject_forbidden_body)],
    ) -> ProfileListItem:
        provided = payload.model_dump(exclude_unset=True)
        updates: dict[str, str | None] = {}
        if "label" in provided:
            updates["label"] = payload.label
        for field, key in SCRIPT_FIELDS.items():
            if field not in provided:
                continue
            reference = getattr(payload, field)
            # 空串与 null 同义：删掉这个字段（前端表单清空 = 不再用脚本）。
            updates[key] = (
                str(resolve_script_reference(reference, store.store_base, field=field))
                if reference
                else None
            )
        if updates:
            save_profile(store.store_base, payload.id, updates)
        return profile_item(store.store_base, payload.id)

    return router
