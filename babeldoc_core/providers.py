"""Provider interfaces; no gateway, model, or secret is hard-coded here."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from typing import Protocol


class TranslatorProvider(Protocol):
    def translate(self, text: str, **kwargs: Any) -> str | dict[str, str]: ...


class ReviewerProvider(Protocol):
    def review(self, document: Any, **kwargs: Any) -> dict[str, Any]: ...


class LayoutProvider(Protocol):
    def reconstruct(self, document: Any, **kwargs: Any) -> Any: ...


Provider = TranslatorProvider | ReviewerProvider | LayoutProvider | Callable[..., Any]
