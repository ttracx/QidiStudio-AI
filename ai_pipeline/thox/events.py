"""The print-health event log.

An append-only record of everything the agent saw, decided and did. It is the
answer to "why did my print get paused at 3am", and it is deliberately written
even for actions that were *refused* - a refusal is a decision the operator
needs to see, especially when it is the autonomy policy declining to act.

Two stores, on purpose:

**A bounded ring in memory** serves the UI. It is what a panel polls or streams,
and it can never grow without limit no matter how long a print runs.

**A JSONL file per job** is the durable record. One line per event, appended and
flushed immediately, so a crash mid-print still leaves everything up to that
moment. JSONL rather than a single JSON document precisely because it survives
truncation: a half-written last line costs one event, not the whole file.

Events carry no credentials and no file paths outside the state root, so the log
is safe to ship to a UI wholesale.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

#: Events kept in memory for the UI.
_RING_SIZE = 500


class EventKind(str, Enum):
    MONITOR_STARTED = "monitor_started"
    MONITOR_STOPPED = "monitor_stopped"
    JOB_STARTED = "job_started"
    JOB_ENDED = "job_ended"
    SAMPLE = "sample"
    SUSPICION = "suspicion"
    ALERT = "alert"
    ACTION_TAKEN = "action_taken"
    ACTION_REFUSED = "action_refused"
    ACTION_SUGGESTED = "action_suggested"
    REVISION = "revision"
    ERROR = "error"

    @property
    def is_notable(self) -> bool:
        """Whether this deserves a notification rather than just a log line.

        Routine samples are the overwhelming majority of events and must not
        notify, or the operator mutes the whole channel and misses the one that
        mattered.
        """
        return self in {
            EventKind.ALERT,
            EventKind.ACTION_TAKEN,
            EventKind.ACTION_SUGGESTED,
            EventKind.ACTION_REFUSED,
            EventKind.REVISION,
            EventKind.ERROR,
        }


@dataclass
class Event:
    """One entry in the log."""

    kind: EventKind
    message: str
    at: float = field(default_factory=time.time)
    severity: float = 0.0
    job_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["kind"] = self.kind.value
        payload["notable"] = self.kind.is_notable
        return payload


class EventLog:
    """Thread-safe ring buffer plus per-job JSONL persistence."""

    def __init__(self, root: str | None = None) -> None:
        self._lock = threading.Lock()
        self._events: deque[Event] = deque(maxlen=_RING_SIZE)
        self._seq = 0
        self._root = root
        self._job_id = ""
        self._path: str | None = None

    # -- job lifecycle ------------------------------------------------------

    def begin_job(self, job_id: str) -> None:
        """Point persistence at a new job's file."""
        with self._lock:
            self._job_id = job_id
            self._path = None
            if not self._root or not job_id:
                return
            try:
                directory = os.path.join(self._root, "health", job_id)
                os.makedirs(directory, exist_ok=True)
                self._path = os.path.join(directory, "events.jsonl")
            except OSError as exc:
                # Losing durability must never stop monitoring; the ring buffer
                # still serves the UI.
                logger.warning(
                    "[THOX] cannot create event log directory (%s); "
                    "continuing in memory only",
                    type(exc).__name__,
                )
                self._path = None

    @property
    def job_id(self) -> str:
        return self._job_id

    # -- writing ------------------------------------------------------------

    def add(
        self,
        kind: EventKind,
        message: str,
        *,
        severity: float = 0.0,
        **data: Any,
    ) -> Event:
        with self._lock:
            self._seq += 1
            event = Event(
                kind=kind,
                message=message,
                severity=float(severity),
                job_id=self._job_id,
                data=data,
                seq=self._seq,
            )
            self._events.append(event)
            path = self._path

        if path:
            try:
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event.to_dict()) + "\n")
                    handle.flush()
            except OSError as exc:
                logger.warning(
                    "[THOX] could not append to event log (%s)", type(exc).__name__
                )

        level = logging.WARNING if event.kind.is_notable else logging.DEBUG
        logger.log(level, "[THOX][%s] %s", kind.value, message)
        return event

    # -- reading ------------------------------------------------------------

    def recent(self, limit: int = 100, *, since_seq: int = 0) -> list[dict[str, Any]]:
        """Most recent events, oldest first, optionally only newer than a seq.

        ``since_seq`` lets a UI poll without re-rendering the whole log; it is
        also what an SSE reconnect uses to catch up on what it missed.
        """
        with self._lock:
            events = [e for e in self._events if e.seq > since_seq]
        return [e.to_dict() for e in events[-limit:]]

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    def notable(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            events = [e for e in self._events if e.kind.is_notable]
        return [e.to_dict() for e in events[-limit:]]

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
