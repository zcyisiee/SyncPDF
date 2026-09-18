"""Offline coverage of built-in selections, CLI protocols, and HTTP job wiring."""
import asyncio
import io
import json
import shlex
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from babeldoc_tools import harnesses
from babeldoc_tools.__main__ import main
from babeldoc_tools.common import ToolError
from babeldoc_tools.run import _review_prompt
from babeldoc_tools.serve.app import create_app
from babeldoc_tools.serve.profiles import resolve_profile
from babeldoc_tools.serve.runner import JobRunner
from babeldoc_tools.serve.runner import build_job_argv
from babeldoc_tools.serve.store import DocumentStore
from fastapi.testclient import TestClient


def pi_response(text="译文", stop="stop"):
    message = {"role": "assistant", "stopReason": stop, "content": [
        {"type": "thinking", "thinking": "private reasoning"}, {"type": "text", "text": text}]}
    return "\n".join(json.dumps(event) for event in [
        {"type": "session"},
        {"type": "message_update", "assistantMessageEvent": {"delta": "duplicate"}},
        {"type": "message_end", "message": message},
        {"type": "turn_end", "message": message},
        {"type": "agent_end", "messages": [message]},
    ])


@pytest.mark.parametrize("profile_id", list(harnesses.BUILTINS))
def test_adapter_uses_exact_argv_stdin_and_stdout(profile_id, monkeypatch, capsys):
    model = harnesses.BUILTINS[profile_id]
    prompt = "--model evil; $(touch forbidden)\nTranslate only this."
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        assert prompt not in argv
        assert not kwargs.get("shell") and not kwargs.get("start_new_session")
        assert Path(kwargs["cwd"]).is_dir()
        assert kwargs["timeout"] == 1810
        assert "--dangerously-skip-permissions" not in argv
        if model.harness == "pi":
            assert argv[argv.index("--model") + 1] == model.model
            assert argv[argv.index("--thinking") + 1] == model.default_thinking
            for flag in ("--no-session", "--no-tools", "--no-context-files", "--no-extensions",
                         "--no-skills", "--no-prompt-templates"):
                assert flag in argv
            assert kwargs["input"] == prompt
            output = pi_response()
        else:
            assert argv[argv.index("--model") + 1] == model.model
            assert argv[argv.index("--effort") + 1] == model.default_thinking
            assert argv[argv.index("--input-format") + 1] == "stream-json"
            # 提示词不能进 argv：agy 只能经 stdin 的 stream-json 输入收它。
            assert "-p" not in argv and "--print" not in argv
            payload = json.loads(kwargs["input"])
            assert payload["event"] == "user"
            assert payload["message"]["content"] == [{"type": "text", "text": prompt}]
            output = '{"event":"result","result":{"status":"SUCCESS","response":"译文"}}'
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(harnesses.subprocess, "run", run)
    monkeypatch.setattr("babeldoc_tools.process_stream.run_stream", run)
    monkeypatch.setattr("sys.stdin", io.StringIO(prompt))
    assert main(["harness-call", "--profile", profile_id]) == 0
    assert capsys.readouterr().out == "译文"
    assert len(seen) == 1


@pytest.mark.parametrize("profile,thinking", [
    ("arbitrary;cmd", "low"), ("pi-deepseek-flash", "medium"),
    ("pi-deepseek-v4-pro", "low"), ("agy-gemini-3-1-pro", "medium"),
    ("agy-gemini-3-8-flash", "max"), ("pi-deepseek-flash", "low;cmd"),
])
def test_reject_unsupported_before_spawn(profile, thinking, monkeypatch):
    monkeypatch.setattr(harnesses.subprocess, "run", lambda *_a, **_k: pytest.fail("spawned"))
    with pytest.raises(ToolError, match="Unknown|Unsupported"):
        harnesses.call_harness(profile, thinking, "prompt")


@pytest.mark.parametrize("harness,output", [
    ("pi", pi_response(stop="length")), ("pi", pi_response(stop="error")),
    ("pi", pi_response().rsplit("\n", 1)[0]), ("pi", "not json"),
    ("pi", pi_response(text="")), ("agy", '{"response":""}'),
    ("agy", '{"response":"partial", "is_error":true}'), ("agy", "[]"),
    ("agy", '{"event":"result","result":{"status":"ERROR","response":"partial"}}'),
    ("agy", '{"event":"init"}\n{"event":"step_update"}'),
])
def test_incomplete_or_failed_protocol_never_becomes_translation(harness, output):
    with pytest.raises(ToolError, match="invalid or incomplete"):
        harnesses.parse_response(harness, output)


@pytest.mark.parametrize("failure,code", [
    (FileNotFoundError(), "harness_missing"),
    (subprocess.TimeoutExpired("private", 1), "harness_timeout"),
    (subprocess.CompletedProcess([], 1, "private", "credential secret"), "harness_failed"),
])
def test_errors_do_not_echo_cli_output(failure, code, monkeypatch):
    def run(*_a, **_k):
        if isinstance(failure, Exception):
            raise failure
        return failure
    monkeypatch.setattr(harnesses.subprocess, "run", run)
    with pytest.raises(ToolError) as error:
        harnesses.call_harness("pi-deepseek-flash", "low", "prompt")
    assert error.value.code == code
    assert "private" not in str(error.value) and "credential secret" not in str(error.value)


def test_builtins_empty_store_preserve_old_configs_and_reserve_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(harnesses.subprocess, "run", lambda *_a, **_k: pytest.fail("generation"))
    store = DocumentStore.for_root(tmp_path)
    with TestClient(create_app(store)) as client:
        items = client.get("/api/v1/profiles").json()
        assert {item["id"] for item in items} == set(harnesses.BUILTINS)
        assert all(item["builtin"] and item["has_translator"] for item in items)
        legacy = tmp_path / ".bdt-serve/profiles.json"
        legacy.parent.mkdir(exist_ok=True)
        raw = '{"old":{"translator":"cat","custom":"keep"}}'
        legacy.write_text(raw)
        assert any(item["id"] == "old" for item in client.get("/api/v1/profiles").json())
        assert legacy.read_text() == raw
        profile_id = next(iter(harnesses.BUILTINS))
        assert client.put("/api/v1/profiles", json={"id": profile_id, "label": "overwrite"}).status_code == 409
        assert client.put("/api/v1/models", json={"id": profile_id, "label": "overwrite",
                          "model": "other", "base_url": "https://example.invalid"}).status_code == 409
        assert legacy.read_text() == raw


def test_http_run_and_retranslate_persist_thinking_validate_before_queue(tmp_path, monkeypatch):
    from babeldoc_tools.serve import candidates

    (tmp_path / "doc").mkdir()
    monkeypatch.setattr(JobRunner, "_pump", AsyncMock())
    monkeypatch.setattr(candidates, "paragraph_facts", lambda *_a: ("source", "target"))
    with TestClient(create_app(DocumentStore.for_root(tmp_path))) as client:
        base = "/api/v1/documents/doc"
        for endpoint, body in [(base + "/jobs", {"action": "run", "from": "translate"}),
                               (base + "/paragraphs/P01/retranslate", {})]:
            invalid = client.post(endpoint, json={**body, "profile": "agy-gemini-3-1-pro", "thinking": "medium"})
            assert invalid.status_code == 422
            assert invalid.json()["error"]["code"] == "harness_invalid"
            accepted = client.post(endpoint, json={**body, "profile": "agy-gemini-3-8-flash", "thinking": "medium"})
            assert accepted.status_code == 202, accepted.text
            jid = accepted.json()["job_id"]
            job = client.get("/api/v1/jobs/" + jid).json()
            assert job["thinking"] == "medium"
            assert job["profile"] == "agy-gemini-3-8-flash"
            client.post("/api/v1/jobs/" + jid + "/cancel")


def test_runner_resolves_thinking_and_review_same_harness(tmp_path, monkeypatch):
    from babeldoc_tools.serve import runner as runner_module

    (tmp_path / "doc").mkdir()
    runner = JobRunner(DocumentStore.for_root(tmp_path))
    monkeypatch.setattr(runner, "_pump", AsyncMock())
    record = asyncio.run(runner.submit(did="doc", action="run", from_stage="translate",
                        pages=None, dual=False, profile_id="pi-deepseek-flash", thinking="max",
                        reviewer_profile="pi-deepseek-flash"))
    captured = []
    monkeypatch.setattr(runner_module, "spawn_job", lambda argv, _cwd: captured.append(argv))
    monkeypatch.setattr(runner, "_launch", lambda *_a: None)
    asyncio.run(runner._start(record))
    argv = captured[0]
    command = argv[argv.index("--translator") + 1]
    assert shlex.split(command)[-2:] == ["--thinking", "max"]
    assert argv[argv.index("--reviewer") + 1] == command
    assert "--skip-ai-review" not in argv
    profile = resolve_profile(tmp_path, "pi-deepseek-v4-pro")
    argv = build_job_argv(workdir=tmp_path, action="run", from_stage="translate",
                          pages=None, dual=False, profile=profile)
    assert "--skip-ai-review" in argv
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "document.md").write_text("source text")
    prompt = _review_prompt(tmp_path, {"reviewer": command})
    assert "source text" in prompt and "Text-only review" in prompt


def test_retranslation_spawn_uses_selected_thinking(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from babeldoc_tools.serve import candidates
    from babeldoc_tools.serve import runner as runner_module

    workdir = tmp_path / "doc"
    workdir.mkdir()
    runner = JobRunner(DocumentStore.for_root(tmp_path))
    monkeypatch.setattr(runner, "_pump", AsyncMock())
    record = asyncio.run(runner.submit(did="doc", action="retranslate", from_stage=None,
                        pages=None, dual=False, profile_id="agy-gemini-3-8-flash",
                        thinking="medium", paragraph_id="P01", candidate_id="c_0001"))
    monkeypatch.setattr(candidates, "prepare_candidates", lambda *_a: SimpleNamespace(
        isolated=workdir, pid="P01"))
    captured = []
    monkeypatch.setattr(runner_module, "spawn_job", lambda argv, _cwd: captured.append(argv))
    monkeypatch.setattr(runner, "_launch", lambda *_a, **_k: None)
    asyncio.run(runner._start_retranslate(record, workdir))
    argv = captured[0]
    assert "translate" in argv
    command = shlex.split(argv[argv.index("--translator") + 1])
    assert command[-4:] == ["--profile", "agy-gemini-3-8-flash", "--thinking", "medium"]
