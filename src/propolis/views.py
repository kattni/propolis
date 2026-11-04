"""What the UI shows, worked out from the Store. Nothing here imports Toga."""

from dataclasses import dataclass
from datetime import datetime

from propolis.core.state import ATTENTION
from propolis.core.store import Store

INBOX = "inbox"
"""The view ID of the Inbox. Every other view's ID is a label ID."""

NEW_ACTIVITY = "● "

_KINDS = {"PullRequest": "Pull request"}


@dataclass(frozen=True)
class SidebarItem:
    view: str
    name: str
    count: int
    new: int
    """How many of the threads have activity you haven't seen."""
    number: int | None = None
    """The number key that applies this label, if it has one."""

    @property
    def text(self) -> str:
        text = f"{self.number}   {self.name}" if self.number else self.name
        if self.new:
            return f"{text}  ({self.count}, {self.new} new)"
        if self.count:
            return f"{text}  ({self.count})"
        return text


@dataclass(frozen=True)
class ThreadRow:
    key: str
    title: str
    repo: str
    kind: str
    when: str
    """In the Inbox, the latest activity. In a label, when it was applied."""
    url: str | None


def sidebar(store: Store) -> list[SidebarItem]:
    """The Inbox, then Attention required, then the labels you can apply.

    Labels you can apply are numbered 1 to 9 in order: the defaults first, then
    yours in the order you made them. Attention required is applied by
    Propolis, not by you, so it has no number.
    """
    state = store.state

    def item(view: str, name: str, keys: list[str], number: int | None = None):
        new = sum(state.threads[key].new_activity for key in keys)
        return SidebarItem(view, name, len(keys), new, number)

    items = [
        item(INBOX, "Inbox", state.inbox()),
        item(ATTENTION, state.labels[ATTENTION].name, state.labelled(ATTENTION)),
    ]
    applicable = [label_id for label_id in state.labels if label_id != ATTENTION]
    for number, label_id in enumerate(applicable, start=1):
        items.append(
            item(
                label_id,
                state.labels[label_id].name,
                state.labelled(label_id),
                number if number <= 9 else None,
            )
        )
    return items


def rows(store: Store, view: str) -> list[ThreadRow]:
    state = store.state
    keys = state.inbox() if view == INBOX else state.labelled(view)
    result = []
    for key in keys:
        thread = state.threads[key]
        info = store.info(key)
        when = thread.last_activity if view == INBOX else thread.labels[view]
        title = info.title if info else key
        result.append(
            ThreadRow(
                key=key,
                title=f"{NEW_ACTIVITY}{title}" if thread.new_activity else title,
                repo=info.repo if info else "",
                kind=_KINDS.get(info.kind, info.kind) if info else "",
                when=_format(when),
                url=info.url if info else None,
            )
        )
    return result


def _format(at: datetime | None) -> str:
    return at.astimezone().strftime("%-d %b %H:%M") if at else ""
