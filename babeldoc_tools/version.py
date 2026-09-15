"""版本工具：小文件快照/回滚（不含 55MB 的 state.pkl）。

快照内容（存在 ``<workdir>/agent/snapshots/<name>/``）:
- ``translated.jsonl``（写回 IR 用的 canonical 译文）
- ``translated.md``（模型原始输出，恢复后 apply 与之一致）
- ``layout_overrides.json``（排版覆盖）
- ``apply_report.json`` / ``review_verdict.json``（诊断用）

``state.pkl`` 不复制：它由 parse 阶段产生且很大；恢复后用 apply 从
``translated.md`` 重建写回结果即可。
"""

from __future__ import annotations

import datetime
import shutil
from pathlib import Path

from babeldoc_tools import common
from babeldoc_tools.registry import register

SNAPSHOT_FILES = (
    "translated.jsonl",
    "translated.md",
    "layout_overrides.json",
    "apply_report.json",
    "review_verdict.json",
)


def snapshots_dir(workdir) -> Path:
    return common.agent_dir(workdir) / "snapshots"


@register(
    "snapshot",
    group="version",
    description="快照小文件（译文/覆盖/报告），用于修改前存档与回滚",
    output_hint="name / path / files[]",
    input_schema={
        "type": "object",
        "properties": {
            "workdir": {"type": "string", "minLength": 1},
            "name": {"type": "string", "description": "默认时间戳，如 20260910-120000"},
            "note": {"type": "string"},
        },
        "required": ["workdir"],
    },
)
def snapshot(args: dict) -> dict:
    workdir = common.require_workdir(args["workdir"])
    name = args.get("name") or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = snapshots_dir(workdir) / name
    if target.exists() and not (target / "meta.json").exists():
        raise common.ToolError("snapshot_exists", f"快照已存在: {target}")
    target.mkdir(parents=True, exist_ok=True)
    copied = []
    for filename in SNAPSHOT_FILES:
        source = common.agent_dir(workdir) / filename
        if source.exists():
            shutil.copy2(source, target / filename)
            copied.append(filename)
    common.write_json(
        target / "meta.json",
        {
            "name": name,
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "note": args.get("note") or "",
            "files": copied,
        },
    )
    return {"name": name, "path": str(target), "files": copied}


@register(
    "restore",
    group="version",
    description="从快照恢复（默认恢复后重新 apply，使 IR 与译文一致）",
    output_hint="restored / apply_report",
    input_schema={
        "type": "object",
        "properties": {
            "workdir": {"type": "string", "minLength": 1},
            "name": {"type": "string", "minLength": 1},
            "apply": {"type": "boolean", "description": "恢复后自动 apply（默认 true）"},
        },
        "required": ["workdir", "name"],
    },
)
def restore(args: dict) -> dict:
    workdir = common.require_workdir(args["workdir"])
    source = snapshots_dir(workdir) / args["name"]
    if not source.is_dir():
        available = sorted(p.name for p in snapshots_dir(workdir).glob("*") if p.is_dir())
        raise common.ToolError(
            "snapshot_missing",
            f"快照不存在: {source}",
            available=available,
        )
    meta = common.read_json(source / "meta.json", default={}) or {}
    listed = list(meta.get("files") or [])
    restored = []
    for filename in SNAPSHOT_FILES:
        origin = source / filename
        target = common.agent_dir(workdir) / filename
        if not origin.exists():
            # 快照里没有覆盖文件 → 恢复到 "无覆盖" 状态（删除当前覆盖）
            if filename == "layout_overrides.json" and meta.get("files"):
                if target.exists():
                    target.unlink()
                    restored.append("layout_overrides.json (deleted)")
            continue
        shutil.copy2(origin, target)
        restored.append(filename)
    for filename in listed:  # 快照里有、但恢复集外的文件：记录一下便于排查
        if filename not in restored:
            restored.append(filename)
    result = {
        "restored": restored,
        "name": args["name"],
        "path": str(source),
        "apply_report": None,
    }
    if args.get("apply", True) and (common.agent_dir(workdir) / "translated.md").exists():
        from babeldoc_tools.translate import apply_translation

        result["apply_report"] = apply_translation({"workdir": str(workdir)})
    return result


@register(
    "list_snapshots",
    group="version",
    description="列出已有快照",
    output_hint="snapshots[]",
    input_schema={
        "type": "object",
        "properties": {"workdir": {"type": "string", "minLength": 1}},
        "required": ["workdir"],
    },
)
def list_snapshots(args: dict) -> dict:
    workdir = common.require_workdir(args["workdir"])
    items = []
    for path in sorted(snapshots_dir(workdir).glob("*")):
        if not path.is_dir():
            continue
        meta = common.read_json(path / "meta.json", default={}) or {}
        items.append({"name": path.name, **meta})
    return {"snapshots": items}
