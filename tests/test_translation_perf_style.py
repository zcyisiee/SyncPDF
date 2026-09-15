"""Focused checks for translation caching and style-preserving fast paths."""

from types import SimpleNamespace

import pytest
from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator
from babeldoc.format.pdf.document_il.midend.il_translator import (
    ParagraphTranslateTracker,
)
from babeldoc.translator import cache as cache_module
from babeldoc.translator.cache import TranslationCache


def test_translation_cache_reuses_process_local_value(monkeypatch):
    """A repeated paragraph should not issue a second SQLite lookup."""
    cache = TranslationCache("cache-test", {"model": "test"})
    monkeypatch.setattr(cache_module._TranslationCache, "create", lambda **_: None)
    cache.set("same source", "translated")

    def unexpected_database_lookup(**_kwargs):
        pytest.fail("local cache miss caused an unnecessary SQLite lookup")

    monkeypatch.setattr(
        cache_module._TranslationCache,
        "get_or_none",
        unexpected_database_lookup,
    )
    assert cache.get("same source") == "translated"


def test_token_count_cache_avoids_duplicate_tokenization():
    class Tokenizer:
        calls = 0

        def encode(self, text, disallowed_special=()):
            self.calls += 1
            return [text]

    translator = object.__new__(ILTranslator)
    translator.tokenizer = Tokenizer()
    translator._token_count_cache = {}
    translator._token_count_cache_limit = 4096

    assert translator.calc_token_count("repeated") == 1
    assert translator.calc_token_count("repeated") == 1
    assert translator.tokenizer.calls == 1


def test_unchanged_translation_keeps_original_composition():
    """Unchanged provider output must not rebuild styled compositions."""
    translator = object.__new__(ILTranslator)
    translator.parse_translate_output = lambda *_args, **_kwargs: pytest.fail(
        "unchanged output should skip composition rebuilding"
    )
    paragraph = SimpleNamespace(unicode="source")
    tracker = ParagraphTranslateTracker()
    translate_input = ILTranslator.TranslateInput("source", [])

    assert (
        translator.post_translate_paragraph(
            paragraph,
            tracker,
            translate_input,
            "source",
        )
        is False
    )
    assert paragraph.unicode == "source"
