"""The events that make up Propolis's triage history.

Events are immutable and are the source of truth: the current state is
whatever you get by applying them in order. That is what lets two devices
sync by exchanging events.
"""

import json
from dataclasses import dataclass, fields
from datetime import datetime


@dataclass(frozen=True, kw_only=True)
class Event:
    id: str
    at: datetime
    device: str


@dataclass(frozen=True, kw_only=True)
class LabelDefined(Event):
    """A label was created or reconfigured."""

    label_id: str
    name: str
    flag_on_activity: bool


@dataclass(frozen=True, kw_only=True)
class LabelDeleted(Event):
    label_id: str


@dataclass(frozen=True, kw_only=True)
class LabelsReordered(Event):
    """The labels' new order. The latest reorder wins."""

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

    ``at`` is when it happened on GitHub, not when Propolis noticed it.
    """

    thread_id: str


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
    )
}
_COMMON = {field.name for field in fields(Event)}


def order(event: Event) -> tuple:
    """The key that puts events into the order they are applied."""
    return (event.at, event.device, event.id)


def to_row(event: Event) -> tuple[str, str, str, str, str]:
    data = {
        field.name: getattr(event, field.name)
        for field in fields(event)
        if field.name not in _COMMON
    }
    return (
        event.id,
        event.at.isoformat(),
        event.device,
        type(event).__name__,
        json.dumps(data),
    )


def from_row(id: str, at: str, device: str, kind: str, data: str) -> Event:
    return KINDS[kind](
        id=id, at=datetime.fromisoformat(at), device=device, **json.loads(data)
    )
