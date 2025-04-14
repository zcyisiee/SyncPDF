"""
Base Translator class for handling text translation across different services.
"""

from abc import ABC
from abc import abstractmethod


class Translator(ABC):
    """
    Abstract base class for translation services.
    All specific translator implementations should inherit from this class.
    """

    def __init__(self, source_lang: str = "auto", target_lang: str = "en"):
        """
        Initialize the translator with source and target languages.

        Args:
            source_lang (str): Source language code (default: "auto" for auto-detection)
            target_lang (str): Target language code (default: "en" for English)
        """
        self.source_lang = source_lang
        self.target_lang = target_lang

    @abstractmethod
    def translate(
        self,
        text: str | list[str],
        style_hint: str | list[str] | None = None,
    ) -> str | list[str]:
        """
        Translate the given text from source language to target language.

        Args:
            text: String or list of strings to translate

        Returns:
            Translated text or list of translated texts
        """
        pass

    @abstractmethod
    def translate_batch(
        self, texts: list[list[str]], style_hints: list[list[str]] | None = None
    ) -> list[list[str]]:
        """
        Translate a batch of texts efficiently.

        Args:
            texts: List of strings to translate

        Returns:
            List of translated strings
        """
        pass

    def set_languages(self, source_lang: str, target_lang: str):
        """
        Update the source and target languages.

        Args:
            source_lang (str): Source language code
            target_lang (str): Target language code
        """
        self.source_lang = source_lang
        self.target_lang = target_lang

    def get_languages(self) -> dict[str, str]:
        """
        Get the current source and target languages.

        Returns:
            Dictionary with source_lang and target_lang
        """
        return {"source_lang": self.source_lang, "target_lang": self.target_lang}
