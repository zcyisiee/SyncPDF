"""Configuration that is safe to persist in a job manifest."""

from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from .protocol import SkipPolicy


@dataclass(slots=True)
class JobConfig:
    """Language, skip and bounded-repair settings for one document job."""

    lang_in: str = "en"
    lang_out: str = "zh"
    skip_policy: SkipPolicy = field(default_factory=SkipPolicy)
    max_translation_repairs: int = 2
    max_layout_repairs: int = 2
    layout: str = "native"
    provider_summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["skip_policy"] = self.skip_policy.to_dict()
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "JobConfig":
        value = dict(value or {})
        policy = value.get("skip_policy")
        if isinstance(policy, SkipPolicy):
            pass
        elif isinstance(policy, dict):
            value["skip_policy"] = SkipPolicy.from_dict(policy)
        elif policy is None:
            value.pop("skip_policy", None)
        value.pop("schema_version", None)
        return cls(**value)
