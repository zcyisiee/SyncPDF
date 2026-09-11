"""Stable Python registry for MAS-facing BabelDOC tools."""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from typing import Any, Callable
from typing import TypedDict


class ToolResult(TypedDict, total=False):
    ok: bool
    tool: str
    job_id: str | None
    data: dict[str, Any]
    warnings: list[str]
    artifacts: list[str]
    error: dict[str, Any]


@dataclass
class ToolSpec:
    name: str
    fn: Callable[[dict[str, Any]], dict[str, Any]]
    group: str = "misc"
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "group": self.group, "description": self.description, "input_schema": self.input_schema}


_REGISTRY: dict[str, ToolSpec] = {}


def register(name: str, *, group: str = "misc", description: str = "", input_schema: dict[str, Any] | None = None):
    def decorator(fn):
        _REGISTRY[name] = ToolSpec(name, fn, group, description or (fn.__doc__ or "").strip(), input_schema or {"type": "object", "properties": {}})
        return fn

    return decorator


def tool_names() -> list[str]:
    return list(_REGISTRY)


def list_tools() -> list[dict[str, Any]]:
    return [spec.public() for spec in _REGISTRY.values()]


def get_schema(name: str) -> dict[str, Any]:
    if name not in _REGISTRY:
        raise KeyError(name)
    return _REGISTRY[name].input_schema


def _validate(schema: dict[str, Any], args: dict[str, Any]) -> list[str]:
    if not isinstance(args, dict):
        return ["args must be an object"]
    properties = schema.get("properties") or {}
    errors = [
        f"missing required argument: {key}"
        for key in schema.get("required", [])
        if key not in args and not (key == "workdir" and args.get("job_id"))
    ]
    for key, value in args.items():
        rule = properties.get(key)
        if rule is None:
            errors.append(f"未知参数: {key}")
            continue
        expected = rule.get("type")
        expected_types = expected if isinstance(expected, list) else [expected]
        matches = any(
            (kind == "string" and isinstance(value, str))
            or (kind == "object" and isinstance(value, dict))
            or (kind == "array" and isinstance(value, list))
            or (kind == "boolean" and isinstance(value, bool))
            or (kind == "integer" and isinstance(value, int) and not isinstance(value, bool))
            for kind in expected_types
        )
        if expected and not matches:
            errors.append(f"{key}: expected {expected}")
        elif expected == "string" and not isinstance(value, str):
            errors.append(f"{key}: expected string")
        elif expected == "object" and not isinstance(value, dict):
            errors.append(f"{key}: expected object")
        elif expected == "array" and not isinstance(value, list):
            errors.append(f"{key}: expected array")
        elif expected == "boolean" and not isinstance(value, bool):
            errors.append(f"{key}: expected boolean")
        elif expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
            errors.append(f"{key}: expected integer")
        if "enum" in rule and value not in rule["enum"]:
            errors.append(f"{key}: expected one of {rule['enum']}")
    return errors


def dispatch(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    args = args or {}
    spec = _REGISTRY.get(name)
    base = {"ok": False, "tool": name, "job_id": args.get("job_id"), "data": {}, "warnings": [], "artifacts": []}
    if spec is None:
        base["error"] = {"code": "unknown_tool", "message": f"unknown tool: {name}", "available": tool_names()}
        return base
    errors = _validate(spec.input_schema, args)
    if errors:
        base["error"] = {"code": "invalid_args", "message": "; ".join(errors), "details": errors}
        return base
    try:
        result = spec.fn(args) or {}
    except Exception as exc:  # noqa: BLE001
        from babeldoc_core import JobError

        if isinstance(exc, JobError):
            base["error"] = {"code": exc.code, "message": exc.message, **exc.details}
        elif isinstance(exc, FileNotFoundError):
            base["error"] = {"code": "artifact_missing", "message": str(exc)}
        else:
            base["error"] = {"code": "tool_exception", "message": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc(limit=5).splitlines()[-5:]}
        return base
    base.update({"ok": True, "data": result.get("data", result), "job_id": result.get("job_id", args.get("job_id")), "warnings": result.get("warnings", []), "artifacts": result.get("artifacts", [])})
    return base


def clear() -> None:
    _REGISTRY.clear()


def load_builtin_tools() -> None:
    """Compatibility hook; root tools register during package import."""

    return None
