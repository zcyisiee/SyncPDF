"""Local OpenAI-compatible configurations. Credentials never enter job commands."""
from __future__ import annotations

import json
import os
import shlex
import stat
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import SecretStr
from pydantic import field_validator

from babeldoc_tools.common import ToolError

_LOCK = threading.RLock()


class ModelUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9-]{1,64}$")
    label: str = Field(min_length=1, max_length=80)
    base_url: str = Field(max_length=2048)
    model: str = Field(min_length=1, max_length=200)
    api_key: SecretStr | None = None

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        try:
            url = urlsplit(value)
            valid = (url.scheme in ("http", "https") and url.hostname
                     and not url.username and not url.password and not url.query
                     and not url.fragment and url.port != 0
                     and not any(c.isspace() or ord(c) < 32 for c in value))
            # Cleartext credentials are only allowed on loopback for local providers.
            valid = valid and (url.scheme == "https" or url.hostname in
                              ("localhost", "127.0.0.1", "::1"))
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Use HTTPS (or loopback HTTP), without credentials/query/fragment")
        return value.rstrip("/")

    @field_validator("api_key")
    @classmethod
    def valid_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            key = value.get_secret_value()
            if not key or len(key) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in key):
                raise ValueError("Invalid API key")
        return value


def _directory(base: Path | str) -> Path:
    path = Path(base) / ".bdt-serve" / "model-credentials"
    if path.parent.is_symlink() or path.is_symlink():
        raise ToolError("model_storage", "Unsafe model storage directory")
    return path


def load_models(base: Path | str) -> dict:
    directory = _directory(base)
    path = directory / "models.json"
    if path.is_symlink():
        raise ToolError("model_storage", "Unsafe model storage file")
    if not path.exists():
        return {}
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError
            data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError
            return data
    except (OSError, ValueError):
        raise ToolError("model_storage", "Model storage is unreadable or permissions are unsafe") from None


def _write(base: Path | str, data: dict) -> None:
    directory = _directory(base)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    fd, name = tempfile.mkstemp(prefix=".models-", dir=directory)
    try:
        with os.fdopen(fd, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(data, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        Path(name).replace(directory / "models.json")
    finally:
        Path(name).unlink(missing_ok=True)


def metadata(entry: dict) -> dict:
    return {**{key: entry[key] for key in ("id", "label", "base_url", "model")},
            "has_api_key": bool(entry.get("api_key"))}


def require_model(base: Path | str, model_id: str) -> dict:
    entry = load_models(base).get(model_id)
    if entry is None:
        raise ToolError("unknown_model", "Model configuration not found")
    return entry


def save_model(base: Path | str, update: ModelUpdate) -> dict:
    from babeldoc_tools.harnesses import BUILTINS
    from babeldoc_tools.serve.profiles import load_profiles
    from babeldoc_tools.serve.profiles import profile_env_command

    with _LOCK:
        if update.id in BUILTINS:
            raise ToolError("profile_collision", "Built-in model IDs are reserved")
        if update.id in load_profiles(base) or any(profile_env_command(update.id, f) for f in ("translator", "reviewer")):
            raise ToolError("profile_collision", "ID already belongs to a script profile")
        data = load_models(base)
        entry = update.model_dump(exclude={"api_key"})
        entry["api_key"] = (update.api_key.get_secret_value() if update.api_key else
                            data.get(update.id, {}).get("api_key"))
        data[update.id] = entry
        _write(base, data)
        return metadata(entry)


def delete_model(base: Path | str, model_id: str) -> None:
    with _LOCK:
        data = load_models(base)
        if model_id not in data:
            raise ToolError("unknown_model", "Model configuration not found")
        del data[model_id]
        _write(base, data)


def model_command(base: Path | str, model_id: str) -> str:
    return shlex.join([sys.executable, "-m", "babeldoc_tools", "model-call",
                       "--store-base", str(Path(base).resolve()), "--model-profile", model_id])


def call_model(base: Path | str, model_id: str, prompt: str, *, test: bool = False) -> str:
    entry = require_model(base, model_id)
    # Revalidate disk metadata before any authenticated request.
    try:
        ModelUpdate.model_validate(entry)
    except ValueError:
        raise ToolError("model_address", "Invalid saved model configuration") from None
    key = entry.get("api_key")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    body = {"model": entry["model"], "messages": [{"role": "user", "content": prompt}]}
    if test:
        body["max_tokens"] = 8
    try:
        # No environment proxy/netrc, automatic retries, redirects, or response-body errors.
        with httpx.Client(timeout=60, follow_redirects=False, trust_env=False) as client:
            response = client.post(entry["base_url"] + "/chat/completions", headers=headers, json=body)
        status = response.status_code
        if status in (401, 403):
            raise ToolError("model_auth", "Authentication failed; check the saved API key")
        if 300 <= status < 400:
            raise ToolError("model_address", "Redirect refused; configure the final Base URL")
        if status == 404:
            raise ToolError("model_not_found", "Check the Base URL and model name")
        if status >= 400:
            raise ToolError("model_request", "Provider rejected the request; check model and quota")
        result = response.json()["choices"][0]["message"]["content"]
        if not isinstance(result, str) or not result.strip():
            raise ValueError
        # A malicious/echoing provider must not propagate the credential into artifacts.
        if key:
            result = result.replace(key, "[redacted]")
        return result
    except httpx.TimeoutException:
        raise ToolError("model_timeout", "Model request timed out") from None
    except httpx.RequestError:
        raise ToolError("model_address", "Cannot connect to the configured Base URL") from None
    except (ValueError, KeyError, IndexError, TypeError):
        raise ToolError("model_response", "Provider returned an invalid Chat Completions response") from None
