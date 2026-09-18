"""Built-in text harnesses, invoked only through ``bdt harness-call``."""
from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass

from babeldoc_tools.common import ToolError


@dataclass(frozen=True)
class Harness:
    label: str
    harness: str
    model: str
    thinking_levels: tuple[str, ...]
    default_thinking: str


# 模型名用 agy 的基础名；档位由 `--effort` 单独给（`gemini-3.8-flash` + `--effort low`）。
BUILTINS = {
    "pi-deepseek-flash": Harness(
        "Pi · DeepSeek Flash", "pi", "deepseek/deepseek-v4-flash",
        ("off", "low", "high", "max"), "low",
    ),
    "pi-deepseek-v4-pro": Harness(
        "Pi · DeepSeek V4 Pro", "pi", "deepseek/deepseek-v4-pro",
        ("off", "high", "max"), "high",
    ),
    "agy-gemini-3-8-flash": Harness(
        "agy · Gemini 3.8 Flash", "agy", "gemini-3.8-flash",
        ("low", "medium", "high"), "low",
    ),
    "agy-gemini-3-1-pro": Harness(
        "agy · Gemini 3.1 Pro", "agy", "gemini-3.1-pro",
        ("low", "high"), "low",
    ),
}


def selection(profile_id: str, thinking: str | None = None) -> tuple[Harness, str]:
    harness = BUILTINS.get(profile_id)
    if harness is None:
        raise ToolError("harness_invalid", "Unknown built-in translation model")
    level = harness.default_thinking if thinking is None else thinking
    if level not in harness.thinking_levels:
        raise ToolError("harness_invalid", "Unsupported thinking level for this model")
    return harness, level


def harness_command(profile_id: str, thinking: str | None = None) -> str:
    _, level = selection(profile_id, thinking)
    return shlex.join([sys.executable, "-m", "babeldoc_tools", "harness-call",
                       "--profile", profile_id, "--thinking", level])


def harness_argv(profile_id: str, thinking: str | None = None) -> list[str]:
    harness, level = selection(profile_id, thinking)
    if harness.harness == "pi":
        return ["pi", "-p", "--no-session", "--mode", "json", "--model", harness.model,
                "--thinking", level, "--no-tools", "--no-extensions", "--no-skills",
                "--no-prompt-templates", "--no-context-files", "--no-approve", "--offline",
                "--system-prompt", "Follow the supplied text task. Return only the requested output."]
    # `agy` 的提示词只能经 stdin 的 stream-json 输入给：`-p` 要提示词做参数，
    # 把翻译提示词塞进 argv 既会撞命令行长度上限、也会把正文暴露在进程表里。
    return ["agy", "--model", harness.model, "--effort", level,
            "--disable-slash-commands", "--print-timeout", "30m",
            "--input-format", "stream-json", "--output-format", "stream-json"]


def harness_input(profile_id: str, prompt: str) -> str:
    """喂给 harness stdin 的内容：agy 收 NDJSON user 事件，pi 收裸提示词。"""
    harness, _ = selection(profile_id)
    if harness.harness != "agy":
        return prompt
    message = {"event": "user", "message": {"content": [{"type": "text", "text": prompt}]}}
    return json.dumps(message, ensure_ascii=False) + "\n"


def parse_response(harness: str, stdout: str) -> str:
    """Reject errors/partial turns; never mix thinking or streaming deltas into text."""
    try:
        if harness == "agy":
            result = None
            for line in stdout.split("\n"):
                if not line.strip():
                    continue
                event = json.loads(line)
                if event.get("event") == "result":
                    result = event.get("result")
                elif "response" in event:
                    # 兼容 `--output-format json` 的单对象信封。
                    result = event
            if not isinstance(result, dict):
                raise ValueError
            if result.get("error") or result.get("is_error") or result.get("status") == "ERROR":
                raise ValueError
            text = result["response"]
        else:
            texts = []
            complete = False
            for line in stdout.split("\n"):
                if not line.strip():
                    continue
                event = json.loads(line)
                if event.get("type") == "agent_end":
                    complete = True
                if event.get("type") != "message_end":
                    continue
                message = event["message"]
                if message.get("role") != "assistant":
                    continue
                if message.get("stopReason") != "stop" or message.get("errorMessage"):
                    raise ValueError
                texts.append("".join(part["text"] for part in message["content"]
                                     if part.get("type") == "text"))
            if not complete:
                raise ValueError
            text = "".join(texts)
        if not isinstance(text, str) or not text.strip():
            raise ValueError
        return text
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ToolError("harness_response", "Harness returned an invalid or incomplete response") from None


class HarnessStream:
    """Decode text deltas only; the terminal response remains authoritative."""

    def __init__(self, harness, emit):
        self.harness = harness
        self.emit = emit
        self.pending = ""
        self.text = ""

    def feed(self, chunk):
        self.pending += chunk
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                if self.harness == "pi":
                    update = event.get("assistantMessageEvent", {})
                    delta = update.get("delta") if update.get("type") == "text_delta" else None
                else:
                    update = event.get("step_update", {}) if event.get("event") == "step_update" else {}
                    delta = update.get("text_delta")
                if isinstance(delta, str) and delta:
                    self.text += delta
                    self.emit(delta)
            except (ValueError, AttributeError):
                # parse_response below rejects malformed/incomplete terminal output.
                continue

    def finish(self, text):
        if not text.startswith(self.text):
            raise ToolError("harness_response", "Stream differs from the final response")
        if remaining := text[len(self.text):]:
            self.emit(remaining)
        return text


def call_harness(profile_id: str, thinking: str | None, prompt: str, *, on_text=None) -> str:
    harness, _ = selection(profile_id, thinking)
    argv = harness_argv(profile_id, thinking)
    try:
        # No project files/context, no shell, no permission bypass. Prompt travels over stdin.
        # Keep the inherited process group so job cancellation reaches the harness too.
        with tempfile.TemporaryDirectory(prefix="bdt-harness-") as cwd:
            stream = HarnessStream(harness.harness, on_text) if on_text else None
            if stream:
                from babeldoc_tools.process_stream import run_stream

                result = run_stream(argv, input=harness_input(profile_id, prompt),
                                    cwd=cwd, timeout=1810, on_stdout=stream.feed)
            else:
                result = subprocess.run(  # noqa: S603 - built-in argv allowlist
                    argv, input=harness_input(profile_id, prompt), capture_output=True, text=True,
                    cwd=cwd, timeout=1810, check=False)
    except FileNotFoundError:
        raise ToolError("harness_missing", "Install the selected harness and sign in locally") from None
    except subprocess.TimeoutExpired:
        raise ToolError("harness_timeout", "Harness call timed out") from None
    except OSError:
        raise ToolError("harness_failed", "Cannot start the selected harness") from None
    if result.returncode:
        # CLI stderr can contain authentication details or prompt content.
        raise ToolError("harness_failed", "Harness call failed; check local CLI authentication and model access")
    text = parse_response(harness.harness, result.stdout)
    return stream.finish(text) if stream else text
