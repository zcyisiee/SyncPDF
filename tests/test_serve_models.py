"""Offline model configuration and protocol security regression tests."""
import json
import shlex

import httpx
import pytest
from babeldoc_tools.__main__ import main
from babeldoc_tools.common import ToolError
from babeldoc_tools.serve import models
from babeldoc_tools.serve.app import create_app
from babeldoc_tools.serve.profiles import resolve_profile
from babeldoc_tools.serve.profiles import save_profile
from babeldoc_tools.serve.runner import build_job_argv
from babeldoc_tools.serve.store import DocumentStore
from fastapi.testclient import TestClient

KEY = "fake-test-secret-123"


def config(**changes):
    return {"id": "local", "label": "Local", "model": "test-model",
            "base_url": "https://provider.invalid/v1", "api_key": KEY, **changes}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(DocumentStore.for_root(tmp_path))) as client:
        yield client


def test_crud_offline_private_and_legacy_summary(client, tmp_path, monkeypatch):
    monkeypatch.setattr(httpx.Client, "post", lambda *_a, **_k: pytest.fail("save made network request"))
    response = client.put("/api/v1/models", json=config())
    assert response.status_code == 200
    assert KEY not in response.text
    path = tmp_path / ".bdt-serve/model-credentials/models.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert [item for item in client.get("/api/v1/profiles").json() if not item.get("builtin")] == [
        {"id": "local", "label": "Local", "has_translator": True, "has_reviewer": False}]
    update = config(label="Changed")
    del update["api_key"]
    assert client.put("/api/v1/models", json=update).status_code == 200
    assert models.require_model(tmp_path, "local")["api_key"] == KEY
    assert KEY not in client.get("/api/v1/models/local").text
    assert KEY not in client.get("/api/v1/models").text
    assert client.delete("/api/v1/models/local").status_code == 204
    assert client.get("/api/v1/models/local").status_code == 404


@pytest.mark.parametrize("url", ["https://u:secret@host/v1", "https://host/v1?key=secret",
                                "https://host/#secret", "http://remote.invalid", "file:///tmp/x"])
def test_invalid_url_errors_do_not_echo_inputs(client, url):
    response = client.put("/api/v1/models", json=config(base_url=url))
    assert response.status_code == 422
    assert KEY not in response.text and url not in response.text


def test_invalid_secret_and_extra_fields_never_echo(client):
    for body in (config(api_key={"secret": KEY}), config(command=KEY), {"api_key": KEY}):
        response = client.put("/api/v1/models", json=body)
        assert response.status_code == 422
        assert KEY not in response.text


def test_collisions_both_directions(client, tmp_path, monkeypatch):
    save_profile(tmp_path, "script", {"label": "Script"})
    assert client.put("/api/v1/models", json=config(id="script")).status_code == 409
    monkeypatch.setenv("BDT_PROFILE_ENV_TRANSLATOR", "cat")
    assert client.put("/api/v1/models", json=config(id="env")).status_code == 409
    assert client.put("/api/v1/models", json=config()).status_code == 200
    assert client.put("/api/v1/profiles", json={"id": "local", "label": "Script"}).status_code == 409
    monkeypatch.setenv("BDT_PROFILE_LOCAL_TRANSLATOR", "cat")
    with pytest.raises(ToolError, match="conflict"):
        resolve_profile(tmp_path, "local")


def mock_transport(monkeypatch, handler):
    original = httpx.Client

    def factory(**kwargs):
        assert kwargs["follow_redirects"] is False
        assert kwargs["trust_env"] is False
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(models.httpx, "Client", factory)


def test_adapter_prompt_protocol_and_secret_free_argv(tmp_path, monkeypatch, capsys):
    models.save_model(tmp_path, models.ModelUpdate(**config()))
    profile = resolve_profile(tmp_path, "local")
    argv = build_job_argv(workdir=tmp_path, action="run", from_stage="translate",
                          pages=None, dual=False, profile=profile)
    assert "--skip-ai-review" in argv
    assert KEY not in str(argv) and "provider.invalid" not in str(argv)
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer " + KEY
        assert json.loads(request.content)["messages"][0]["content"] == "Translate me"
        return httpx.Response(200, json={"choices": [{"message": {"content": "Result " + KEY}}]})

    mock_transport(monkeypatch, handler)
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("Translate me"))
    assert main(shlex.split(profile.translator)[3:]) == 0
    output = capsys.readouterr()
    assert output.out == "Result [redacted]"
    assert KEY not in output.err
    assert len(requests) == 1


@pytest.mark.parametrize("status,code", [(302, "model_address"), (401, "model_auth"),
                                          (404, "model_not_found"), (500, "model_request")])
def test_provider_errors_and_redirects_are_safe(tmp_path, monkeypatch, status, code):
    models.save_model(tmp_path, models.ModelUpdate(**config()))
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, headers={"location": "https://other.invalid"}, text=KEY)

    mock_transport(monkeypatch, handler)
    with pytest.raises(ToolError) as caught:
        models.call_model(tmp_path, "local", "hello")
    assert caught.value.code == code
    assert KEY not in str(caught.value)
    assert len(seen) == 1


def test_explicit_test_short_generation(client, monkeypatch):
    client.put("/api/v1/models", json=config())
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": KEY}}]})

    mock_transport(monkeypatch, handler)
    response = client.post("/api/v1/models/local/test")
    assert response.json() == {"ok": True}
    assert seen[0]["max_tokens"] == 8


def test_unsafe_storage_refused(tmp_path):
    models.save_model(tmp_path, models.ModelUpdate(**config()))
    path = tmp_path / ".bdt-serve/model-credentials/models.json"
    path.chmod(0o644)
    with pytest.raises(ToolError):
        models.load_models(tmp_path)
    path.unlink()
    path.symlink_to(tmp_path / "outside")
    with pytest.raises(ToolError):
        models.load_models(tmp_path)


def test_reviewer_selection_and_job_adapter(tmp_path, monkeypatch):
    import asyncio

    from babeldoc_tools.serve.runner import JobRunner

    (tmp_path / "doc").mkdir()
    models.save_model(tmp_path, models.ModelUpdate(**config()))
    models.save_model(tmp_path, models.ModelUpdate(**config(id="review")))
    runner = JobRunner(DocumentStore.for_root(tmp_path))

    async def no_pump():
        pass

    monkeypatch.setattr(runner, "_pump", no_pump)
    record = asyncio.run(runner.submit(did="doc", action="run", from_stage="translate",
                                      pages=None, dual=False, profile_id="local",
                                      reviewer_profile="review"))
    assert record.reviewer_profile == "review"
    assert KEY not in record.model_dump_json()
    profile = resolve_profile(tmp_path, "local")
    profile.reviewer = resolve_profile(tmp_path, "review").translator
    argv = build_job_argv(workdir=tmp_path, action="run", from_stage="translate",
                          pages=None, dual=False, profile=profile)
    assert "--reviewer" in argv and "--skip-ai-review" not in argv
    assert KEY not in str(argv)


def test_review_inline_context_is_bounded_and_text_only(tmp_path):
    from babeldoc_tools.run import _review_prompt

    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "document.md").write_text("source text")
    (agent / "translated.md").write_text("translated text")
    (agent / "layout_lint.json").write_text(json.dumps({"api_key": KEY, "verdict": "pass"}))
    prompt = _review_prompt(tmp_path, {"reviewer": models.model_command(tmp_path, "local")})
    assert "source text" in prompt and "translated text" in prompt
    assert "Text-only review" in prompt and KEY not in prompt
    (agent / "document.md").write_text("x" * 1_000_001)
    with pytest.raises(ToolError) as error:
        _review_prompt(tmp_path, {"reviewer": models.model_command(tmp_path, "local")})
    assert error.value.code == "model_context_too_large"


def test_timeout_error_does_not_echo_credentials(tmp_path, monkeypatch):
    models.save_model(tmp_path, models.ModelUpdate(**config()))

    def handler(request):
        raise httpx.ReadTimeout(KEY, request=request)

    mock_transport(monkeypatch, handler)
    with pytest.raises(ToolError) as error:
        models.call_model(tmp_path, "local", "hello")
    assert error.value.code == "model_timeout"
    assert KEY not in str(error.value)
