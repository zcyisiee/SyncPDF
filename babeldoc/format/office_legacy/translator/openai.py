"""
OpenAI compatible client class for translation services.
This class can be configured to use OpenAI's API or other OpenAI-compatible APIs.
"""

import json
from typing import Any

import requests
from loguru import logger

from babeldoc.format.office.translator.translator import Translator


class OpenAITranslator(Translator):
    """
    OpenAI-compatible translator client that extends the base Translator class.
    This client can be configured to work with OpenAI's API or other compatible APIs.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-3.5-turbo",
        source_lang: str = "auto",
        target_lang: str = "en",
        temperature: float = 0.3,
        max_tokens: int | None = None,
        timeout: int = 60,
    ):
        """
        Initialize the OpenAI-compatible translator.

        Args:
            api_key (str): API key for authentication
            base_url (str): Base URL for the API (default: OpenAI's API URL)
            model (str): Model name to use for translation
            source_lang (str): Source language code
            target_lang (str): Target language code
            temperature (float): Sampling temperature (lower = more deterministic)
            max_tokens (int, optional): Maximum number of tokens to generate
            timeout (int): Timeout for API requests in seconds
        """
        super().__init__(source_lang=source_lang, target_lang=target_lang)
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout

        # Construct headers with API key
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    def _build_prompt(self, text: list[str], style_hint: list[str]) -> str:
        """
        Build a prompt for the translation request.

        Args:
            text (str): Text to translate

        Returns:
            Formatted prompt string
        """
        source_lang_name = (
            "the source language"
            if self.source_lang == "auto"
            else f"{self.source_lang}"
        )
        target_lang_name = f"{self.target_lang}"
        return f"""Requirements:
1. Translate the following text array from {source_lang_name} into {target_lang_name}, ensuring the translation is accurate and semantically clear.
2. The translated results must align with the style hash array, preserving each element's original style hints.
3. For expressions that do not appear in the target language, you may leave an empty string as a placeholder.
4. Return only the translated result in JSON object with one key "translated" containing a list of result strings as its value.
5. Keep the length of the translated text array the same as the original text array, and do not seperate the text into multiple strings.
Please output the translated text array that meets the above requirements.

Text Array: {json.dumps(text)}
Style Hash Array: {json.dumps(style_hint)}"""

    def _build_uniform_prompt(self, text: list[str], _: str) -> str:
        """
        Build a simplified prompt when all style hints are the same.

        Args:
            text (List[str]): Text array to translate
            style_hint (str): The uniform style hint for all texts

        Returns:
            Formatted prompt string
        """
        source_lang_name = (
            "the source language"
            if self.source_lang == "auto"
            else f"{self.source_lang}"
        )
        target_lang_name = f"{self.target_lang}"
        return f"""Requirements:
1. Translate the following text array from {source_lang_name} into {target_lang_name}, ensuring the translation is accurate and semantically clear.
2. For expressions that do not appear in the target language, you may leave an empty string as a placeholder.
3. Return only the translated result in JSON object with one key "translated" containing a list of result strings as its value.
4. Keep the length of the translated text array the same as the original text array, and do not seperate the text into multiple strings.
Please output the translated text array that meets the above requirements.

Text Array: {json.dumps(text)}"""

    def _build_chat_messages(
        self, text: list[str], style_hint: list[str]
    ) -> list[dict[str, str]]:
        """
        Build chat messages for the chat completions API.

        Args:
            text (str): Text to translate

        Returns:
            List of message objects
        """
        # Check if all style hints are the same
        if all(hint == style_hint[0] for hint in style_hint):
            prompt = self._build_uniform_prompt(text, style_hint[0])
        else:
            prompt = self._build_prompt(text, style_hint)
        return [{"role": "user", "content": prompt}]

    def _make_api_request(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        """
        Make an API request to the chat completions endpoint.

        Args:
            messages: List of message objects

        Returns:
            API response as a dictionary
        """
        endpoint = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "response_format": {"type": "json_object"},
        }

        if self.max_tokens:
            payload["max_tokens"] = self.max_tokens

        response = None
        try:
            response = requests.post(
                endpoint,
                headers=self.headers,
                data=json.dumps(payload),
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            if response:
                logger.debug(f"Response: {response.json()}")
            raise Exception(f"API request failed: {str(e)}") from e

    def _extract_translation(self, response: dict[str, Any]) -> list[str]:
        """
        Extract the translated text from API response.

        Args:
            response: API response dictionary

        Returns:
            Translated text
        """
        try:
            content = response["choices"][0]["message"]["content"].strip()
            content = json.loads(content)
            return content["translated"]
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            raise Exception(
                f"Failed to extract translation from response {str(e)}\nResponse: {response}"
            ) from e

    def translate(
        self,
        text: str | list[str],
        style_hint: str | list[str] | None = None,
    ) -> str | list[str]:
        """
        Translate text from source language to target language.

        Args:
            text: String or list of strings to translate
            style_hint: String or list of strings indicating style for each text

        Returns:
            Translated text or list of translated texts
        """
        # Remember if input was a single string
        single_input = isinstance(text, str)

        # Normalize inputs to lists
        if single_input:
            text = [text]

        if isinstance(style_hint, str):
            style_hint = [style_hint]

        if style_hint is None:
            style_hint = ["A"] * len(text)
        elif len(style_hint) < len(text):
            # Extend style hints if needed
            style_hint = style_hint + [style_hint[-1]] * (len(text) - len(style_hint))

        # Process in batches if the list is too long (more than 20 items)
        batch_size = 20
        if len(text) > batch_size:
            result = []
            for i in range(0, len(text), batch_size):
                batch_text = text[i : i + batch_size]
                batch_style = style_hint[i : i + batch_size]
                result.extend(self.translate(batch_text, batch_style))

            # Return single string if input was a string
            if single_input:
                return result[0]
            return result

        logger.debug(f"正在翻译: {text}")
        messages = self._build_chat_messages(text, style_hint)
        response = self._make_api_request(messages)
        result = self._extract_translation(response)
        logger.debug(f"翻译结果: {result}")

        # Return single string if input was a string
        if single_input:
            return result[0]
        return result

    def translate_batch(
        self, texts: list[list[str]], style_hints: list[list[str]] | None = None
    ) -> list[list[str]]:
        """
        Translate multiple batches of texts.

        Args:
            texts: List of text lists to translate
            style_hints: List of style hint lists for each text list

        Returns:
            List of translated text lists
        """
        if style_hints is None:
            style_hints = [None] * len(texts)
        return [
            self.translate(text, style_hint)
            for text, style_hint in zip(texts, style_hints, strict=False)
        ]

    def update_config(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: int | None = None,
    ):
        """
        Update configuration parameters.

        Args:
            api_key: New API key
            base_url: New base URL
            model: New model name
            temperature: New temperature value
            max_tokens: New max_tokens value
            timeout: New timeout value
        """
        if api_key:
            self.api_key = api_key
            self.headers["Authorization"] = f"Bearer {self.api_key}"

        if base_url:
            self.base_url = base_url.rstrip("/")

        if model:
            self.model = model

        if temperature is not None:
            self.temperature = temperature

        if max_tokens is not None:
            self.max_tokens = max_tokens

        if timeout is not None:
            self.timeout = timeout
