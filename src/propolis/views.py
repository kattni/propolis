"""What the UI shows, worked out from the Store. Nothing here imports Toga."""

import re
from dataclasses import dataclass
from datetime import datetime

from propolis.core.state import ATTENTION
from propolis.core.store import Store
from propolis.core.threads import ThreadInfo

INBOX = "inbox"
"""The view ID of the Inbox. Every other view's ID is a label ID."""

NEW_ACTIVITY = "● "

_KINDS = {"PullRequest": "Pull request"}
_ICONS = {
    "Issue": "issue-opened",
    "PullRequest": "git-pull-request",
    "Discussion": "comment-discussion",
    "Release": "tag",
    "Commit": "git-commit",
    "CheckSuite": "workflow",
}
"""GitHub's Octicon for each type of thread. Any other type gets a bell."""

REASONS = {
    "added": "Added by you",
    "approval_requested": "Approval requested",
    "assign": "Assigned",
    "author": "Author",
    "ci_failure": "Failing CI",
    "comment": "Commented",
    "invitation": "Invitation",
    "manual": "Subscribed",
    "member_feature_requested": "Feature requested",
    "mention": "Mentioned",
    "review_requested": "Review requested",
    "security_advisory_credit": "Security advisory credit",
    "security_alert": "Security alert",
    "state_change": "State change",
    "subscribed": "Watching",
    "team_mention": "Team mentioned",
}
"""GitHub's notification reasons, in plain words. ``subscribed`` means you're
watching the repo; ``manual`` means you subscribed to the thread itself."""


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
    icon: str
    """The name of the Octicon for its type, such as ``git-pull-request``."""
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


@dataclass(frozen=True)
class Filters:
    """Narrow the thread list. ``None`` means any."""

    repo: str | None = None
    reason: str | None = None
    kind: str | None = None

    @property
    def active(self) -> bool:
        return any([self.repo, self.reason, self.kind])

    def matches(self, info: ThreadInfo | None) -> bool:
        if not self.active:
            return True
        return (
            info is not None
            and self.repo in (None, info.repo)
            and self.reason in (None, info.reason)
            and self.kind in (None, info.kind)
        )


@dataclass(frozen=True)
class FilterOptions:
    repos: list[str]
    reasons: list[tuple[str, str]]
    """Each reason, and its name."""
    kinds: list[tuple[str, str]]
    """Each type, and its name."""


def filter_options(store: Store) -> FilterOptions:
    """The repos, reasons and types among threads in any view."""
    state = store.state
    keys = set(state.inbox())
    for label_id in state.labels:
        keys.update(state.labelled(label_id))
    infos = [info for key in keys if (info := store.info(key))]

    def named(values: set[str], names: dict[str, str]) -> list[tuple[str, str]]:
        return sorted(
            ((value, names.get(value, value)) for value in values),
            key=lambda pair: pair[1],
        )

    return FilterOptions(
        repos=sorted({info.repo for info in infos}),
        reasons=named({info.reason for info in infos if info.reason}, REASONS),
        kinds=named({info.kind for info in infos}, _KINDS),
    )


def rows(store: Store, view: str, filters: Filters = Filters()) -> list[ThreadRow]:
    state = store.state
    keys = state.inbox() if view == INBOX else state.labelled(view)
    result = []
    for key in keys:
        thread = state.threads[key]
        info = store.info(key)
        if not filters.matches(info):
            continue
        when = thread.last_activity if view == INBOX else thread.labels[view]
        title = info.title if info else key
        result.append(
            ThreadRow(
                key=key,
                title=f"{NEW_ACTIVITY}{title}" if thread.new_activity else title,
                repo=info.repo if info else "",
                kind=_KINDS.get(info.kind, info.kind) if info else "",
                icon=_ICONS.get(info.kind, "bell") if info else "bell",
                when=_format(when),
                url=info.url if info else None,
            )
        )
    return result


_KEY_SYMBOLS = {
    "mod 3": "⌃",
    "mod 2": "⌥",
    "shift": "⇧",
    "mod 1": "⌘",
    "backspace": "⌫",
    "up": "↑",
    "down": "↓",
}
_MODIFIER_ORDER = ["mod 3", "mod 2", "shift", "mod 1"]


def shortcut_text(shortcut: str | None) -> str:
    """A Toga shortcut such as ``<mod 1><mod 2><up>``, as macOS shows it: ⌥⌘↑."""
    if not shortcut:
        return ""
    names = re.findall(r"<([^>]+)>", shortcut)
    key = re.sub(r"<[^>]+>", "", shortcut)
    modifiers = [name for name in _MODIFIER_ORDER if name in names]
    keys = [name for name in names if name not in _MODIFIER_ORDER]
    return "".join(
        [_KEY_SYMBOLS[name] for name in modifiers]
        + [_KEY_SYMBOLS.get(name, name.title()) for name in keys]
        + [key.upper()]
    )


def _format(at: datetime | None) -> str:
    return at.astimezone().strftime("%-d %b %H:%M") if at else ""
