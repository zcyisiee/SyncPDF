from __future__ import annotations

import logging
from typing import Any

import httpx
from babeldoc.babeldoc_exception.BabelDOCException import ContentFilterError
from babeldoc.translator.translator import BaseTranslator
from babeldoc.utils.atomic_integer import AtomicInteger

logger = logging.getLogger(__name__)

_CONTENT_FILTER_HINT = (
    "系统检测到输入或生成内容可能包含不安全或敏感内容，"
    "请您避免输入易产生敏感内容的提示语，感谢您的配合。"
)

_REQUEST_TIMEOUT_SECONDS = 600


class ExecutorTranslatorError(RuntimeError):
    def __init__(self, status_code: int, body: str) -> None:
        snippet = body if len(body) <= 200 else body[:200] + "..."
        super().__init__(
            f"executor translator gateway returned HTTP {status_code}: {snippet}"
        )
        self.status_code = status_code
        self.body = body


class ExecutorTranslator(BaseTranslator):
    name = "executor"

    def __init__(
        self,
        lang_in: str,
        lang_out: str,
        model: str,
        base_url: str,
        api_key: str,
        ignore_cache: bool = False,
    ) -> None:
        super().__init__(lang_in, lang_out, ignore_cache)
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._client = httpx.Client(
            limits=httpx.Limits(
                max_connections=None,
                max_keepalive_connections=None,
            ),
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )

        self.add_cache_impact_parameters("model", self.model)
        self.add_cache_impact_parameters("temperature", 0)

        self.token_count = AtomicInteger()
        self.prompt_token_count = AtomicInteger()
        self.completion_token_count = AtomicInteger()
        self.cache_hit_prompt_token_count = AtomicInteger()

    def do_llm_translate(
        self,
        text: str | None,
        rate_limit_params: dict | None = None,
    ) -> str | None:
        if text is None:
            return None

        body: dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 2048,
            "messages": [{"role": "user", "content": text}],
        }
        if rate_limit_params and rate_limit_params.get("request_json_mode"):
            body["response_format"] = {"type": "json_object"}

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        url = f"{self.base_url}/chat/completions"
        response = self._client.post(url, headers=headers, json=body)

        if response.status_code != 200:
            self._raise_for_response(response)

        try:
            payload = response.json()
        except ValueError as exc:
            raise ExecutorTranslatorError(response.status_code, response.text) from exc

        self._update_token_count(payload)

        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ExecutorTranslatorError(response.status_code, response.text) from exc

        if not isinstance(content, str):
            raise ExecutorTranslatorError(response.status_code, response.text)

        return content.strip()

    def do_translate(self, text, rate_limit_params: dict | None = None):
        raise NotImplementedError(
            "ExecutorTranslator only supports do_llm_translate; "
            "executor-driven translation must use the LLM path."
        )

    def _raise_for_response(self, response: httpx.Response) -> None:
        try:
            body_text = response.text
        except Exception:
            body_text = ""

        if 400 <= response.status_code < 500 and _CONTENT_FILTER_HINT in body_text:
            raise ContentFilterError(_CONTENT_FILTER_HINT)

        raise ExecutorTranslatorError(response.status_code, body_text)

    def _update_token_count(self, payload: dict) -> None:
        try:
            usage = payload.get("usage") or {}
            if not isinstance(usage, dict):
                return

            total = int(usage.get("total_tokens") or 0)
            prompt = int(usage.get("prompt_tokens") or 0)
            completion = int(usage.get("completion_tokens") or 0)
            if total:
                self.token_count.inc(total)
            if prompt:
                self.prompt_token_count.inc(prompt)
            if completion:
                self.completion_token_count.inc(completion)

            cache_hit = 0
            if "prompt_cache_hit_tokens" in usage:
                cache_hit += int(usage.get("prompt_cache_hit_tokens") or 0)
            details = payload.get("prompt_tokens_details") or {}
            if isinstance(details, dict):
                cache_hit += int(details.get("cached_tokens") or 0)
            if cache_hit:
                self.cache_hit_prompt_token_count.inc(cache_hit)
        except Exception:
            logger.exception(
                "ExecutorTranslator: failed to update token usage counters"
            )

    def get_formular_placeholder(self, placeholder_id: int | str):
        return "{v" + str(placeholder_id) + "}", f"{{\\s*v\\s*{placeholder_id}\\s*}}"

    def get_rich_text_left_placeholder(self, placeholder_id: int | str):
        return (
            f"<style id='{placeholder_id}'>",
            f"<\\s*style\\s*id\\s*=\\s*'\\s*{placeholder_id}\\s*'\\s*>",
        )

    def get_rich_text_right_placeholder(self, placeholder_id: int | str):
        return "</style>", r"<\s*\/\s*style\s*>"
