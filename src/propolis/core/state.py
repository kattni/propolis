"""The current triage state, built by applying events in order."""

from dataclasses import dataclass, field
from datetime import datetime

from propolis.core.events import (
    ActivityDetected,
    Event,
    LabelDefined,
    Labelled,
    MarkedDone,
    Unlabelled,
    Viewed,
)

ATTENTION = "attention-required"
"""The built-in label applied when a flagging label sees activity."""


@dataclass(frozen=True)
class Label:
    name: str
    flag_on_activity: bool = False


@dataclass
class ThreadState:
    labels: dict[str, datetime] = field(default_factory=dict)
    """Each of the thread's labels, and when it was applied."""
    last_activity: datetime | None = None
    seen_at: datetime | None = None
    done: bool = False

    @property
    def new_activity(self) -> bool:
        """Whether there's been activity since you last viewed or labelled it."""
        return self.last_activity is not None and (
            self.seen_at is None or self.last_activity > self.seen_at
        )


@dataclass
class State:
    labels: dict[str, Label] = field(
        default_factory=lambda: {ATTENTION: Label("Attention required")}
    )
    threads: dict[str, ThreadState] = field(default_factory=dict)

    def apply(self, event: Event) -> None:
        match event:
            case LabelDefined():
                self.labels[event.label_id] = Label(
                    event.name, event.flag_on_activity
                )
            case Labelled():
                thread = self._thread(event.thread_id)
                # Re-applying a label keeps the thread's original place.
                thread.labels.setdefault(event.label_id, event.at)
                thread.seen_at = event.at
                thread.done = False
            case Unlabelled():
                self._thread(event.thread_id).labels.pop(event.label_id, None)
            case Viewed():
                self._thread(event.thread_id).seen_at = event.at
            case MarkedDone():
                thread = self._thread(event.thread_id)
                thread.labels.clear()
                thread.done = True
            case ActivityDetected():
                thread = self._thread(event.thread_id)
                thread.last_activity = event.at
                thread.done = False
                if any(self._flags(label_id) for label_id in thread.labels):
                    thread.labels.setdefault(ATTENTION, event.at)

    def inbox(self) -> list[str]:
        """Threads with no labels that aren't done, most recent activity first."""
        untriaged = [
            (thread.last_activity, thread_id)
            for thread_id, thread in self.threads.items()
            if not (thread.labels or thread.done) and thread.last_activity is not None
        ]
        return [thread_id for _, thread_id in sorted(untriaged, reverse=True)]

    def labelled(self, label_id: str) -> list[str]:
        """Threads with ``label_id``, in the order it was applied."""
        tagged = [
            (thread.labels[label_id], thread_id)
            for thread_id, thread in self.threads.items()
            if label_id in thread.labels
        ]
        return [thread_id for _, thread_id in sorted(tagged)]

    def _flags(self, label_id: str) -> bool:
        label = self.labels.get(label_id)
        return label is not None and label.flag_on_activity

    def _thread(self, thread_id: str) -> ThreadState:
        return self.threads.setdefault(thread_id, ThreadState())
