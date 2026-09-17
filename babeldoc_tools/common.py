"""公共小工具：workdir 路径、prompt 加载、子进程 Agent 调用、JSON 读写。

翻译/审查只有一种 provider 机制：把提示词写进子命令 stdin，从 stdout 读结果，
退出码 0 表示成功（:func:`run_translator`）。模型、档位、JSON 解包等 agy 专属
细节属于被调命令自己的事，由仓库 ``scripts/`` 下的 wrapper 承担。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

AGENT_DIR = "agent"
# 包位于 <repo>/babeldoc_tools/，技能资源仍在 <repo>/skills/document-translate/。
SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills" / "document-translate"
AGENTS_DIR = SKILL_ROOT / "agents"


class ToolError(RuntimeError):
    """工具层可预期的失败（会被 registry.invoke 转成机读错误）。"""

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
            f"{path}/agent 不存在：请先跑 bdt parse 生成解析产物",
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

    从 ``agents/<name>.md`` 读取；文件里若有 ```text 代码块则取块内内容。
    """
    candidates = [
        AGENTS_DIR / f"{name}.md",
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
    candidate = AGENTS_DIR / f"{name}.md"
    return candidate if candidate.exists() else None


def _run_subprocess(
    prompt: str, command: str, timeout_s: int, error_prefix: str,
    *, debug_recorder=None, debug_origin: str | None = None, debug_context: dict | None = None,
) -> str:
    """把 ``prompt`` 写进 ``command`` 的 stdin，返回它的 stdout。

    唯一协议：退出码 0 表示成功；非 0 / 超时 / 空 stdout 都以结构化错误抛出，
    错误码为 ``<error_prefix>_failed`` / ``_timeout`` / ``_empty``。命令经
    :func:`shlex.split` 拆成 argv，**不经 shell**——需要管道或 JSON 解包时请在
    仓库 wrapper 脚本里做。翻译与审查调用共用本函数。
    """
    if debug_recorder is None:
        from babeldoc.debug_recorder import get_current

        debug_recorder = get_current()
    capture = (
        debug_recorder.process(
            "review" if error_prefix == "reviewer" else "translate",
            debug_origin or error_prefix, command, prompt=prompt, timeout=timeout_s,
            **(debug_context or {}),
        )
        if debug_recorder else contextlib.nullcontext()
    )
    with capture as evidence:
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise ToolError(
                f"{error_prefix}_failed", f"命令无法解析（引号不匹配？）: {command}"
            ) from exc
        if not argv:
            raise ToolError(f"{error_prefix}_failed", "翻译/审查命令为空")
        try:
            # argv 由用户的显式 --translator/--reviewer 参数定义，shlex.split 后不经 shell；
            # 这是唯一的 provider 机制（stdin/stdout 子进程协议），非 shell 拼接。
            result = subprocess.run(  # noqa: S603
                argv,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError(
                f"{error_prefix}_timeout",
                f"命令超过 {timeout_s}s 超时: {command}",
                timeout=timeout_s,
            ) from exc
        except OSError as exc:
            # FileNotFoundError / PermissionError 等：命令不存在或不可执行
            raise ToolError(
                f"{error_prefix}_failed", f"无法执行 {command}: {exc}"
            ) from exc
        if evidence is not None:
            evidence["result"] = result
        if result.returncode != 0:
            raise ToolError(
                f"{error_prefix}_failed",
                f"{command} 退出码 {result.returncode}: {result.stderr[:500]}",
            )
        if not result.stdout.strip():
            raise ToolError(
                f"{error_prefix}_empty", f"{command} 的 stdout 为空"
            )
        return result.stdout


def run_translator(
    prompt: str, command: str, timeout_s: int = 1800, *, debug_recorder=None,
    debug_origin: str | None = None, debug_context: dict | None = None,
) -> str:
    """调用用户指定的翻译命令：stdin 收提示词，stdout 出译文，返回 stdout。

    不解析 JSON、不抽 usage——提示词与译文就是纯文本交换。
    """
    return _run_subprocess(
        prompt, command, timeout_s, "translator", debug_recorder=debug_recorder,
        debug_origin=debug_origin, debug_context=debug_context,
    )


def env_default(name: str, default=None):
    value = os.environ.get(name)
    return value if value else default
