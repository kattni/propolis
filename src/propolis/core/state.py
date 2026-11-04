"""The current triage state, built by applying events in order."""

from dataclasses import dataclass, field
from datetime import datetime

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
)

ATTENTION = "attention-required"
"""The built-in label applied when a flagging label sees activity."""


@dataclass(frozen=True)
class Label:
    name: str
    flag_on_activity: bool = False


def _built_in_labels() -> dict[str, Label]:
    """The labels every Store starts with.

    They're part of the starting state rather than events, so every device has
    them from the start and syncing can't duplicate them. Reconfiguring one is
    an ordinary ``LabelDefined`` event with the same ID.
    """
    return {
        ATTENTION: Label("Attention required"),
        "needs-review": Label("Needs review"),
        "question": Label("Question"),
        "deep-response": Label("Deep response"),
        "blocked": Label("Blocked", flag_on_activity=True),
        "low-priority": Label("Low priority"),
        "high-priority": Label("High priority", flag_on_activity=True),
        "in-progress": Label("In progress"),
        "cold-storage": Label("Cold storage"),
    }


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
    labels: dict[str, Label] = field(default_factory=_built_in_labels)
    threads: dict[str, ThreadState] = field(default_factory=dict)

    def apply(self, event: Event) -> None:
        match event:
            case LabelDefined():
                self.labels[event.label_id] = Label(
                    event.name, event.flag_on_activity
                )
            case LabelDeleted() if event.label_id != ATTENTION:
                self.labels.pop(event.label_id, None)
                for thread in self.threads.values():
                    thread.labels.pop(event.label_id, None)
            case LabelsReordered():
                given = [
                    label_id
                    for label_id in event.order
                    if label_id in self.labels and label_id != ATTENTION
                ]
                rest = [
                    label_id
                    for label_id in self.labels
                    if label_id not in given and label_id != ATTENTION
                ]
                self.labels = {
                    ATTENTION: self.labels[ATTENTION],
                    **{label_id: self.labels[label_id] for label_id in given + rest},
                }
            case Labelled() if event.label_id not in self.labels:
                # The label was deleted, perhaps on another device. Applying
                # it would hide the thread from every view.
                pass
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
