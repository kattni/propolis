"""The events that make up an inbox's triage history.

Events never change once made, and they're the source of truth: the triage
state is whatever applying them in order gives (see `buzz.state`). Each one
names the device it was made on, which is the groundwork for devices to sync
by exchanging events.

Times are UTC, in the format of `buzz.store.now()`, so they sort as text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields


@dataclass(frozen=True, kw_only=True)
class Event:
    id: str
    at: str
    #: The device it was made on, or "github" for activity there.
    device: str


@dataclass(frozen=True, kw_only=True)
class LabelDefined(Event):
    """A label was made, or changed."""

    label_id: str
    name: str
    color: str
    flag_on_activity: bool


@dataclass(frozen=True, kw_only=True)
class LabelDeleted(Event):
    label_id: str


@dataclass(frozen=True, kw_only=True)
class LabelsReordered(Event):
    """The labels' new order. Any not in it keep their order, after it."""

    order: list[str]


@dataclass(frozen=True, kw_only=True)
class Labelled(Event):
    thread_id: str
    label_id: str


@dataclass(frozen=True, kw_only=True)
class Unlabelled(Event):
    thread_id: str
    label_id: str


@dataclass(frozen=True, kw_only=True)
class Viewed(Event):
    thread_id: str


@dataclass(frozen=True, kw_only=True)
class MarkedDone(Event):
    thread_id: str


@dataclass(frozen=True, kw_only=True)
class ActivityDetected(Event):
    """Activity that needs a response from you happened on a thread.

    ``at`` is when it happened on GitHub, not when Buzz noticed it.
    """

    thread_id: str


@dataclass(frozen=True, kw_only=True)
class Adopted(Event):
    """A notification's thread took the place of the thread added by hand for
    the same issue, pull request or discussion, labels and all."""

    thread_id: str
    previous: str


@dataclass(frozen=True, kw_only=True)
class Imported(Event):
    """A thread's triage state, carried over from before Buzz kept events."""

    thread_id: str
    #: Each of its labels, and when it was given.
    labels: dict[str, str]
    last_activity: str | None
    seen_at: str | None
    done_at: str | None


KINDS = {
    kind.__name__: kind
    for kind in (
        LabelDefined,
        LabelDeleted,
        LabelsReordered,
        Labelled,
        Unlabelled,
        Viewed,
        MarkedDone,
        ActivityDetected,
        Adopted,
        Imported,
    )
}
_COMMON = {field.name for field in fields(Event)}


def order(event: Event) -> tuple[str, str, str]:
    """The key that puts events in the order they're applied."""
    return (event.at, event.device, event.id)


def to_row(event: Event) -> tuple[str, str, str, str, str]:
    data = {
        field.name: getattr(event, field.name)
        for field in fields(event)
        if field.name not in _COMMON
    }
    return event.id, event.at, event.device, type(event).__name__, json.dumps(data)


def from_row(id: str, at: str, device: str, kind: str, data: str) -> Event | None:
    """The event stored in a row, or None if it's of a kind this version of
    Buzz doesn't know."""
    if (cls := KINDS.get(kind)) is None:
        return None
    known = {field.name for field in fields(cls)}
    values = {k: v for k, v in json.loads(data).items() if k in known}
    return cls(id=id, at=at, device=device, **values)
