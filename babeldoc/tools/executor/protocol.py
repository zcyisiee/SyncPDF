from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

MAX_EVENT_LOG_SIZE = 1000
MAX_SEQUENCE = 2**63 - 1
SEQUENCE_HEADROOM = 100_000_000
MAX_INITIAL_SEQUENCE = MAX_SEQUENCE - SEQUENCE_HEADROOM


@dataclass(frozen=True)
class WorkerEvent:
    type: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class EventEnvelope:
    type: str
    execution_id: str
    sequence: int
    payload: dict[str, Any]

    def to_json_line(self) -> bytes:
        payload = {
            "type": self.type,
            "execution_id": self.execution_id,
            "sequence": self.sequence,
            "payload": self.payload,
        }
        return (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
