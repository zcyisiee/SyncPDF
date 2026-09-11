"""公共小工具：workdir 路径、prompt 加载、模型调用（agy）、JSON 读写。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

AGENT_DIR = "agent"
SKILL_ROOT = Path(__file__).resolve().parents[2]  # skills/document-translate/
AGENTS_DIR = SKILL_ROOT / "agents"
PROMPTS_DIR = SKILL_ROOT / "prompts"  # legacy 提示词
TOOLS_DIR = SKILL_ROOT / "tools"


class ToolError(RuntimeError):
    """工具层可预期的失败（会被 dispatch 转成机读错误）。"""

    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def agent_dir(workdir) -> Path:
    return Path(workdir) / AGENT_DIR


def require_workdir(workdir) -> Path:
    path = Path(workdir)
    if not (path / AGENT_DIR).is_dir():
        raise ToolError(
            "workdir_missing",
            f"{path}/agent 不存在：请先跑 parse_document 生成解析产物",
            workdir=str(path),
        )
    return path


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ToolError("bad_json", f"{path} 解析失败: {exc}") from exc


def write_json(path, payload) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def read_jsonl(path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


_FENCE_RE = re.compile(r"```(?:text|markdown)\n(.*?)```", re.DOTALL)


def load_prompt(name: str, **substitutions) -> str:
    """加载提示词文件并替换 ``{key}`` 占位符。

    优先 ``agents/<name>.md``，回退 ``prompts/<name>.md``；文件里若有
    ```text 代码块则取块内内容（与 experiments/markdown_translate.py 一致）。
    """
    candidates = [
        AGENTS_DIR / f"{name}.md",
        PROMPTS_DIR / f"{name}.md",
        Path(name),
    ]
    for candidate in candidates:
        if candidate.exists():
            text = candidate.read_text(encoding="utf-8")
            blocks = _FENCE_RE.findall(text)
            body = blocks[0] if blocks else text
            for key, value in substitutions.items():
                body = body.replace("{" + key + "}", str(value))
            return body
    raise ToolError(
        "prompt_missing",
        f"提示词未找到: {name}（查找 {[str(c) for c in candidates]}）",
    )


def prompt_path(name: str) -> Path | None:
    for candidate in (AGENTS_DIR / f"{name}.md", PROMPTS_DIR / f"{name}.md"):
        if candidate.exists():
            return candidate
    return None


def run_model(
    prompt: str,
    model: str,
    effort: str | None,
    timeout_s: int = 1800,
    command: str = "agy",
) -> tuple[str, dict]:
    """调用外部翻译模型 CLI（默认 agy），返回 (response, usage)。

    ``effort`` 为空 / ``"none"`` / ``"default"`` / ``"auto"`` 时不传 ``--effort``
    （部分模型如 claude-* 不支持该参数）。
    """
    if shutil.which(command) is None:
        raise ToolError(
            "model_cli_missing",
            f"找不到可执行文件 {command}；可改用 translated_md 参数直接导入译文，"
            f"或指定 command/mcmd 指向本地 CLI",
        )
    started = time.time()
    cmd = [
        command,
        "--model",
        model,
        "--disable-slash-commands",
        "--print-timeout",
        f"{max(1, timeout_s // 60)}m",
        "--output-format",
        "json",
        "--print",
        prompt,
    ]
    if effort and str(effort).lower() not in ("none", "default", "auto"):
        cmd.insert(3, "--effort")
        cmd.insert(4, str(effort))
    try:
        # cmd 由本包参数/环境变量构造（command + model + prompt），非外部输入拼接
        result = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, timeout=timeout_s
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError(
            "model_timeout", f"模型调用超过 {timeout_s}s 超时", timeout=timeout_s
        ) from exc
    elapsed = round(time.time() - started, 1)
    if result.returncode != 0:
        raise ToolError(
            "model_failed",
            f"{command} 退出码 {result.returncode}: {result.stderr[:500]}",
        )
    raw = result.stdout.strip()
    usage: dict = {}
    try:
        payload = json.loads(raw)
        response = payload.get("response", "")
        usage = payload.get("usage") or {}
        usage["duration_seconds"] = payload.get("duration_seconds", elapsed)
        usage["num_turns"] = payload.get("num_turns")
        usage["conversation_id"] = payload.get("conversation_id")
    except json.JSONDecodeError:
        response = raw
        usage = {"duration_seconds": elapsed, "parse_error": True}
    usage.setdefault("model", model)
    if effort:
        usage.setdefault("effort", effort)
    return response, usage


def usage_path(workdir) -> Path:
    return agent_dir(workdir) / "usage.json"


def append_usage(workdir, key: str, usage: dict) -> dict:
    """把一次调用的 usage 记到 ``agent/usage.json``（key 如 "translate"/"retry"）。"""
    path = usage_path(workdir)
    current = read_json(path, default={}) or {}
    if key in current and isinstance(current[key], dict) and isinstance(usage, dict):
        merged = dict(current[key])
        for field, value in usage.items():
            if isinstance(value, (int, float)) and isinstance(merged.get(field), (int, float)):
                merged[field] = merged[field] + value
            else:
                merged[field] = value
        current[key] = merged
    else:
        current[key] = usage
    write_json(path, current)
    return current


def env_default(name: str, default=None):
    value = os.environ.get(name)
    return value if value else default
