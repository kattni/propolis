"""An inbox's triage state, made by applying its events in order."""

from __future__ import annotations

from dataclasses import dataclass, field

from .events import (
    ActivityDetected,
    Adopted,
    Event,
    Imported,
    LabelDefined,
    LabelDeleted,
    Labelled,
    LabelsReordered,
    MarkedDone,
    Unlabelled,
    Viewed,
)

#: The built-in label that activity on a flagging label's thread gives it. It's
#: also the Attention required view's key.
ATTENTION = "attention"

#: The labels every inbox starts with, in sidebar order: each one's ID, name,
#: colour, and whether activity on its threads flags them for attention.
DEFAULT_LABELS = (
    ("needs-review", "Needs review", "🔵", False),
    ("question", "Question", "🟣", False),
    ("deep-response", "Deep response", "🟤", False),
    # Activity may mean it's unblocked.
    ("blocked", "Blocked", "🔴", True),
    ("low-priority", "Low priority", "⚪", False),
    ("high-priority", "High priority", "🟠", True),
    ("in-progress", "In progress", "🟢", False),
    ("cold-storage", "Cold storage", "⚫", False),
)


@dataclass(frozen=True)
class LabelInfo:
    name: str
    color: str
    #: Whether activity on its threads flags them for attention.
    flag: bool = False


ATTENTION_LABEL = LabelInfo("Attention required", "⚠️")


def built_in_labels() -> dict[str, LabelInfo]:
    """The labels every inbox starts with.

    They're part of the starting state rather than events, so every device has
    them from the start, and syncing can't duplicate them. Changing or deleting
    one is an ordinary event with its ID.
    """
    return {
        ATTENTION: ATTENTION_LABEL,
        **{
            label_id: LabelInfo(name, color, flag)
            for label_id, name, color, flag in DEFAULT_LABELS
        },
    }


@dataclass
class ThreadState:
    #: Each of the thread's labels, Attention required included, and when it
    #: was given.
    labels: dict[str, str] = field(default_factory=dict)
    #: When the latest activity that needs a response from you happened.
    last_activity: str | None = None
    #: When you last viewed or labelled it.
    seen_at: str | None = None
    #: When it was marked as done, if it's done.
    done_at: str | None = None

    @property
    def new(self) -> bool:
        """Whether there's been activity since you last viewed or labelled it."""
        return self.last_activity is not None and (
            self.seen_at is None or self.last_activity > self.seen_at
        )


@dataclass
class State:
    labels: dict[str, LabelInfo] = field(default_factory=built_in_labels)
    threads: dict[str, ThreadState] = field(default_factory=dict)

    def apply(self, event: Event) -> None:
        match event:
            case LabelDefined():
                self.labels[event.label_id] = LabelInfo(
                    event.name, event.color, event.flag_on_activity
                )
            case LabelDeleted() if event.label_id != ATTENTION:
                self.labels.pop(event.label_id, None)
                for thread in self.threads.values():
                    thread.labels.pop(event.label_id, None)
            case LabelsReordered():
                given = [
                    label_id
                    for label_id in dict.fromkeys(event.order)
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
                # The label has been deleted. Giving it would take the thread
                # out of every view.
                pass
            case Labelled():
                thread = self._thread(event.thread_id)
                # Labelling it again keeps its place in the label.
                thread.labels.setdefault(event.label_id, event.at)
                _see(thread, event.at)
                if thread.done_at is not None and thread.done_at <= event.at:
                    thread.done_at = None
            case Unlabelled():
                if thread := self.threads.get(event.thread_id):
                    thread.labels.pop(event.label_id, None)
            case Viewed():
                _see(self._thread(event.thread_id), event.at)
            case MarkedDone():
                thread = self._thread(event.thread_id)
                thread.labels.clear()
                thread.done_at = event.at
            case ActivityDetected():
                self._activity(self._thread(event.thread_id), event.at)
            case Adopted() if event.previous in self.threads:
                adopted = self.threads.pop(event.previous)
                # Only activity can have come to the notification's thread
                # before it was adopted.
                if (found := self.threads.get(event.thread_id)) and found.last_activity:
                    self._activity(adopted, found.last_activity)
                self.threads[event.thread_id] = adopted
            case Imported():
                self.threads[event.thread_id] = ThreadState(
                    labels={
                        label_id: at
                        for label_id, at in event.labels.items()
                        if label_id in self.labels
                    },
                    last_activity=event.last_activity,
                    seen_at=event.seen_at,
                    done_at=event.done_at,
                )

    def flags(self, label_id: str) -> bool:
        label = self.labels.get(label_id)
        return label is not None and label.flag

    def _activity(self, thread: ThreadState, at: str) -> None:
        """Activity at ``at`` brings a thread back from done, and flags it if
        it had a flagging label by then.

        Replayed in order, activity is always the thread's latest. Activity
        that isn't, which can only come from a thread imported from before
        Propolis kept events, has happened already.
        """
        if thread.last_activity is not None and at <= thread.last_activity:
            return
        thread.last_activity = at
        if thread.done_at is not None and thread.done_at < at:
            thread.done_at = None
        if any(
            self.flags(label_id) and given < at
            for label_id, given in thread.labels.items()
        ):
            thread.labels.setdefault(ATTENTION, at)

    def _thread(self, thread_id: str) -> ThreadState:
        return self.threads.setdefault(thread_id, ThreadState())


def _see(thread: ThreadState, at: str) -> None:
    if thread.seen_at is None or thread.seen_at < at:
        thread.seen_at = at
