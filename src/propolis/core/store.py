"""The event log on disk, and the only way to change triage state."""

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from propolis.core.events import (
    ActivityDetected,
    Event,
    LabelDefined,
    LabelDeleted,
    Labelled,
    LabelsReordered,
    MarkedDone,
    Unlabelled,
    Viewed,
    from_row,
    order,
    to_row,
)
from propolis.core.state import ATTENTION, State
from propolis.core.threads import ThreadInfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS thread_info (key TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS event (
    id TEXT PRIMARY KEY,
    at TEXT NOT NULL,
    device TEXT NOT NULL,
    kind TEXT NOT NULL,
    data TEXT NOT NULL
);
"""


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Store:
    def __init__(self, path: Path, clock: Callable[[], datetime] = _utcnow):
        self.path = path
        self._db = sqlite3.connect(path)
        self._db.executescript(SCHEMA)
        self._clock = clock
        self.device = self._device_id()
        self._rebuild()

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def define_label(
        self, name: str, flag_on_activity: bool = False, label_id: str | None = None
    ) -> str:
        """Create a label, or reconfigure an existing one. Returns its ID."""
        label_id = label_id or uuid4().hex
        self._append(
            LabelDefined(
                **self._new(),
                label_id=label_id,
                name=name,
                flag_on_activity=flag_on_activity,
            )
        )
        return label_id

    def delete_label(self, label_id: str) -> None:
        """Delete a label, taking it off every thread."""
        if label_id == ATTENTION:
            raise ValueError("Attention required is built in and can't be deleted.")
        self._append(LabelDeleted(**self._new(), label_id=label_id))

    def reorder_labels(self, order: list[str]) -> None:
        """Put the labels in this order. Any not given keep their relative
        order, after the ones given."""
        self._append(LabelsReordered(**self._new(), order=list(order)))

    def label(self, thread_id: str, label_id: str) -> None:
        self._append(Labelled(**self._new(), thread_id=thread_id, label_id=label_id))

    def unlabel(self, thread_id: str, label_id: str) -> None:
        self._append(
            Unlabelled(**self._new(), thread_id=thread_id, label_id=label_id)
        )

    def mark_done(self, thread_id: str) -> None:
        """Remove all of a thread's labels until it sees new activity."""
        self._append(MarkedDone(**self._new(), thread_id=thread_id))

    def mark_viewed(self, thread_id: str, at: datetime | None = None) -> None:
        """Record that you've seen the thread, now or at ``at``."""
        self._append(Viewed(**self._new(at), thread_id=thread_id))

    def record_activity(self, thread_id: str, at: datetime) -> None:
        """Record activity that needs a response, at GitHub's timestamp.

        The event's ID comes from the thread and the timestamp, so when
        several devices detect the same activity, only one event is kept.
        """
        self._append(
            ActivityDetected(
                id=f"activity:{thread_id}:{at.isoformat()}",
                at=at,
                device="github",
                thread_id=thread_id,
            )
        )

    # Local caches. Unlike events, these aren't synced between devices.

    def info(self, key: str) -> ThreadInfo | None:
        row = self._db.execute(
            "SELECT data FROM thread_info WHERE key = ?", (key,)
        ).fetchone()
        return ThreadInfo.from_json(row[0]) if row else None

    def save_info(self, info: ThreadInfo) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO thread_info VALUES (?, ?)",
                (info.key, info.to_json()),
            )

    def get_meta(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._db:
            self._db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    def _new(self, at: datetime | None = None) -> dict:
        return {"id": uuid4().hex, "at": at or self._clock(), "device": self.device}

    def _append(self, event: Event) -> None:
        with self._db:
            inserted = self._db.execute(
                "INSERT OR IGNORE INTO event VALUES (?, ?, ?, ?, ?)", to_row(event)
            ).rowcount
        if not inserted:
            return
        # Activity carries GitHub's timestamp, so it can arrive after events
        # that it precedes. Applying it out of order would give a different
        # state from a replay, so replay instead.
        if self._last is None or order(event) > self._last:
            self.state.apply(event)
            self._last = order(event)
        else:
            self._rebuild()

    def _rebuild(self) -> None:
        events = sorted(
            (
                from_row(*row)
                for row in self._db.execute(
                    "SELECT id, at, device, kind, data FROM event"
                )
            ),
            key=order,
        )
        self.state = State()
        for event in events:
            self.state.apply(event)
        self._last = order(events[-1]) if events else None

    def _device_id(self) -> str:
        device = self.get_meta("device")
        if device is None:
            device = uuid4().hex
            self.set_meta("device", device)
        return device
