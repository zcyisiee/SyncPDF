"""工具注册表 + 派发器（MCP 友好）。

用法::

    from babeldoc_tools import registry
    registry.list_tools()               # [{"name", "group", "description", "input_schema"}]
    registry.get_schema("layout_set")   # JSON Schema
    registry.dispatch("layout_set", {...})
    # → {"ok": true, "tool": "layout_set", "data": {...}}
    # → {"ok": false, "tool": "layout_set", "error": {"code", "message", ...}}

约定：
- 工具函数签名为 ``fn(args: dict) -> dict``；缺参数/类型不对由本模块统一拦截。
- 工具自身抛异常时统一包成 ``tool_exception``（附 traceback，便于排查）。
"""

from __future__ import annotations

import traceback
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from typing import Any


@dataclass
class ToolSpec:
    name: str
    fn: Callable[[dict], Any]
    description: str = ""
    group: str = "misc"
    input_schema: dict = field(default_factory=dict)
    output_hint: str = ""

    def public(self) -> dict:
        return {
            "name": self.name,
            "group": self.group,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_hint": self.output_hint,
        }


_REGISTRY: dict[str, ToolSpec] = {}
_ORDER: list[str] = []


def register(
    name: str,
    *,
    description: str = "",
    group: str = "misc",
    input_schema: dict | None = None,
    output_hint: str = "",
):
    """装饰器：注册一个工具。"""

    def decorator(fn: Callable[[dict], Any]):
        spec = ToolSpec(
            name=name,
            fn=fn,
            description=description or (fn.__doc__ or "").strip().splitlines()[0],
            group=group,
            input_schema=input_schema or {"type": "object", "properties": {}},
            output_hint=output_hint,
        )
        if name not in _REGISTRY:
            _ORDER.append(name)
        _REGISTRY[name] = spec
        return fn

    return decorator


def list_tools() -> list[dict]:
    return [_REGISTRY[name].public() for name in _ORDER]


def tool_names() -> list[str]:
    return list(_ORDER)


def get_schema(name: str) -> dict:
    spec = _REGISTRY.get(name)
    if spec is None:
        raise KeyError(name)
    return spec.input_schema


def get_spec(name: str) -> ToolSpec | None:
    return _REGISTRY.get(name)


def validate_args(schema: dict, args: dict) -> list[str]:
    """极简 JSON Schema 校验（required/type/enum/min/max/items/长度）。"""
    errors: list[str] = []
    if not isinstance(args, dict):
        return ["args 必须是 JSON 对象"]
    properties = schema.get("properties") or {}
    for key in schema.get("required") or []:
        if key not in args or args[key] is None:
            errors.append(f"缺少必填参数: {key}")
    for key, value in args.items():
        rule = properties.get(key)
        if rule is None:
            errors.append(f"未知参数: {key}（可用: {sorted(properties)}）")
            continue
        errors.extend(_validate_value(key, rule, value))
    return errors


_TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
}


def _validate_value(key: str, rule: dict, value) -> list[str]:
    errors: list[str] = []
    expected = rule.get("type")
    if expected:
        types = expected if isinstance(expected, list) else [expected]
        python_types = tuple(_TYPE_MAP[t] for t in types if t in _TYPE_MAP)
        if python_types:
            if isinstance(value, bool) and "boolean" not in types:
                errors.append(f"{key}: 期望 {expected}，得到 bool")
                return errors
            if not isinstance(value, python_types):
                errors.append(f"{key}: 期望 {expected}，得到 {type(value).__name__}")
                return errors
    if rule.get("enum") and value not in rule["enum"]:
        errors.append(f"{key}: 取值必须是 {rule['enum']} 之一，得到 {value!r}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if rule.get("minimum") is not None and value < rule["minimum"]:
            errors.append(f"{key}: 不能小于 {rule['minimum']}")
        if rule.get("maximum") is not None and value > rule["maximum"]:
            errors.append(f"{key}: 不能大于 {rule['maximum']}")
    if isinstance(value, list) and rule.get("items"):
        for index, item in enumerate(value):
            errors.extend(
                _validate_value(f"{key}[{index}]", rule["items"], item)
            )
    if isinstance(value, list) and rule.get("minItems") is not None:
        if len(value) < rule["minItems"]:
            errors.append(f"{key}: 至少 {rule['minItems']} 个元素")
    if isinstance(value, str) and rule.get("minLength") is not None:
        if len(value) < rule["minLength"]:
            errors.append(f"{key}: 长度至少 {rule['minLength']}")
    return errors


def dispatch(name: str, args: dict | None = None) -> dict:
    spec = _REGISTRY.get(name)
    if spec is None:
        return {
            "ok": False,
            "tool": name,
            "error": {
                "code": "unknown_tool",
                "message": f"未知工具 {name}",
                "available": tool_names(),
            },
        }
    args = args if args is not None else {}
    errors = validate_args(spec.input_schema, args)
    if errors:
        return {
            "ok": False,
            "tool": name,
            "error": {"code": "invalid_args", "message": "; ".join(errors), "details": errors},
        }
    try:
        data = spec.fn(args)
    except Exception as exc:  # noqa: BLE001 - 统一转成机读错误
        from babeldoc_tools.common import ToolError

        if isinstance(exc, ToolError):
            # 工具层可预期的失败：保留 code/message 与附加上下文
            error = {"code": exc.code, "message": exc.message}
            error.update(exc.extra)
            return {"ok": False, "tool": name, "error": error}
        return {
            "ok": False,
            "tool": name,
            "error": {
                "code": "tool_exception",
                "message": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(limit=8).splitlines()[-8:],
            },
        }
    if isinstance(data, dict) and data.get("ok") is False:
        # 工具内部显式返回失败：提升为标准错误结构
        error = data.get("error") or {
            "code": "tool_failed",
            "message": str(data.get("message") or "工具返回 ok=false"),
        }
        return {"ok": False, "tool": name, "error": error}
    return {"ok": True, "tool": name, "data": data}


# --------------------------------------------------------------------------- #
# 内置工具导入（在此集中注册，避免循环导入）
# --------------------------------------------------------------------------- #
def load_builtin_tools() -> None:
    from babeldoc_tools import layout  # noqa: F401
    from babeldoc_tools import parse  # noqa: F401
    from babeldoc_tools import report  # noqa: F401
    from babeldoc_tools import review  # noqa: F401
    from babeldoc_tools import translate  # noqa: F401
    from babeldoc_tools import version  # noqa: F401
