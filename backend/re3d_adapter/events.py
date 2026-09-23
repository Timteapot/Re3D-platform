from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import validate_contract
from .errors import IntegrityError


TERMINAL_EVENT_TYPES = {"job_succeeded", "job_failed", "job_cancelled"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class EventJournal:
    def __init__(
        self,
        path: Path,
        *,
        job_id: str,
        attempt: int,
        worker_id: str,
    ) -> None:
        self.path = path
        self.job_id = job_id
        self.attempt = attempt
        self.worker_id = worker_id
        self.events = self._load_existing()

    @property
    def next_sequence(self) -> int:
        return len(self.events) + 1

    @property
    def has_terminal_event(self) -> bool:
        return bool(self.events and self.events[-1]["type"] in TERMINAL_EVENT_TYPES)

    def has_completed_stage(self, stage: str) -> bool:
        return any(
            event["type"] == "stage_completed" and event.get("stage") == stage
            for event in self.events
        )

    def artifact_event(self, branch: str) -> dict[str, Any] | None:
        matches = [
            event
            for event in self.events
            if event["type"] == "artifact_created" and event.get("branch") == branch
        ]
        if len(matches) > 1:
            raise IntegrityError(f"multiple artifact events exist for branch {branch}")
        return matches[0] if matches else None

    def append(self, event_type: str, **payload: Any) -> dict[str, Any]:
        if self.has_terminal_event:
            raise IntegrityError("cannot append after a terminal event")
        event: dict[str, Any] = {
            "contract_version": "1.0",
            "event_id": str(uuid.uuid4()),
            "job_id": self.job_id,
            "attempt": self.attempt,
            "sequence": self.next_sequence,
            "occurred_at": utc_now(),
            "worker_id": self.worker_id,
            "type": event_type,
            **payload,
        }
        validate_contract("pipeline-event", event)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self.path.open("a", encoding="utf-8", newline="\n") as output:
            output.write(line)
            output.flush()
            os.fsync(output.fileno())
        self.events.append(event)
        return event

    def _load_existing(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        raw = self.path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            raise IntegrityError("event journal has an incomplete final line")
        events: list[dict[str, Any]] = []
        for line_number, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
            if not line.strip():
                raise IntegrityError(f"event journal contains a blank line at {line_number}")
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise IntegrityError(
                    f"event journal contains invalid JSON at line {line_number}"
                ) from exc
            validate_contract("pipeline-event", event)
            expected_sequence = line_number
            if event["sequence"] != expected_sequence:
                raise IntegrityError(
                    f"event sequence must be contiguous; expected {expected_sequence}"
                )
            if event["job_id"] != self.job_id or event["attempt"] != self.attempt:
                raise IntegrityError("event journal identity does not match the request")
            if events and events[-1]["type"] in TERMINAL_EVENT_TYPES:
                raise IntegrityError("event journal contains data after a terminal event")
            events.append(event)
        return events
