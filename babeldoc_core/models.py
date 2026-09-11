"""Shared state and error types."""

from __future__ import annotations

from enum import Enum


class JobState(str, Enum):
    CREATED = "created"
    PARSED = "parsed"
    TRANSLATED = "translated"
    PROTOCOL_VALIDATED = "protocol_validated"
    REVIEWED = "reviewed"
    RECONSTRUCTED = "reconstructed"
    RENDERED = "rendered"
    ACCEPTED = "accepted"
    BLOCKED_PROTOCOL = "blocked_protocol"
    BLOCKED_TRANSLATION = "blocked_translation"
    BLOCKED_LAYOUT = "blocked_layout"
    NEEDS_HUMAN_REVIEW = "needs_human_review"


class JobError(RuntimeError):
    """A recoverable job error with a stable machine-readable code."""

    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


TERMINAL_FAILURES = {
    JobState.BLOCKED_PROTOCOL,
    JobState.BLOCKED_TRANSLATION,
    JobState.BLOCKED_LAYOUT,
    JobState.NEEDS_HUMAN_REVIEW,
}


STAGE_ORDER = (
    JobState.CREATED,
    JobState.PARSED,
    JobState.TRANSLATED,
    JobState.PROTOCOL_VALIDATED,
    JobState.REVIEWED,
    JobState.RECONSTRUCTED,
    JobState.RENDERED,
    JobState.ACCEPTED,
)
