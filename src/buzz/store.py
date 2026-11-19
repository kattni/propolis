"""Buzz's record of an inbox: its triage history, and what GitHub says about
its threads.

Labels only exist in Buzz: GitHub has no API for organising notifications.

Every thread in your GitHub inbox starts in Buzz's inbox. Giving it a label
moves it out of the inbox and into that label, where it stays, in the order you
labelled it, however much activity it gets. Some labels flag on activity: when
someone else acts on one of their threads, it's also put under Attention
required, which works like a label. Other labels just show that there's
something new on it.

Only activity that needs a response from you counts (see `buzz.activity`). Buzz
records when it happened, at GitHub's time, and when you last saw each thread,
so it can tell what's new.

The triage history is a log of events (see `buzz.events`), each named with the
device it was made on, and the triage state is what replaying them gives. What
GitHub says about each thread is kept beside them, as a cache: each device
fetches its own.
"""

from __future__ import annotations

import itertools
import sqlite3
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from . import legacy
from .events import (
    ActivityDetected,
    Adopted,
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
from .state import ATTENTION, ATTENTION_LABEL, LabelInfo, State, ThreadState

#: Coloured markers used to tell labels apart, assigned in rotation.
LABEL_COLORS = ("🔵", "🟢", "🟣", "🟠", "🔴", "🟡", "🟤", "⚫")

# The built-in views. A label's view is identified by the label's ID.
INBOX = "inbox"
UNREAD = "unread"
DONE = "done"
VIEWS = (INBOX, ATTENTION, UNREAD, DONE)

#: The thread details that `Inbox.threads()` can filter by.
FILTER_COLUMNS = ("type", "reason", "repo")

_META = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""
SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    at TEXT NOT NULL,
    device TEXT NOT NULL,
    kind TEXT NOT NULL,
    data TEXT NOT NULL
);;
-- What GitHub last said about each thread.
CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    repo_url TEXT NOT NULL,
    title TEXT NOT NULL,
    type TEXT NOT NULL,
    url TEXT,
    latest_comment_url TEXT,
    reason TEXT NOT NULL,
    -- Whether it's unread on GitHub, as GitHub last said.
    unread INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    -- Whether the issue, pull request or discussion is open, merged, etc.
    -- (one of github.STATES), or NULL if that isn't known.
    state TEXT,
    -- The thread's updated_at when its state was last looked up, so new
    -- activity, which may have changed it, means looking again.
    state_checked_at TEXT,
    -- How far it's been checked for activity: its updated_at at the time.
    checked_until TEXT
)
"""
#: The store's format.
VERSION = 2

#: Threads added by hand have IDs like ``manual:beeware/toga#123``, unlike the
#: numeric IDs of notification threads. So do pull requests found only because
#: their CI failed.
MANUAL_PREFIX = "manual:"
_DETAILS = (
    "id",
    "repo",
    "repo_url",
    "title",
    "type",
    "url",
    "latest_comment_url",
    "reason",
    "unread",
    "updated_at",
    "state",
    "state_checked_at",
    "checked_until",
)
#: The details another Mac needs to show a thread.
_SHARED_DETAILS = _DETAILS[:-2]

_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
#: How far ahead of this Mac's clock the latest event can be for what's done
#: here still to come after it. Any further ahead, and it's taken to be wrong.
MAX_SKEW = timedelta(hours=1)
# Orders a device's events made at the same moment as they were made.
_SEQUENCE = itertools.count()


def now() -> str:
    """The current UTC time, precise enough to order things done in a burst."""
    return datetime.now(UTC).strftime(_FORMAT)


def stamp(timestamp: str) -> str:
    """An ISO 8601 time, such as GitHub's, in the format of `now()`, so the two
    can be compared as text."""
    moment = datetime.fromisoformat(timestamp)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime(_FORMAT)


@dataclass(frozen=True)
class Thread:
    """A notification thread: one issue, pull request, discussion, etc."""

    id: str
    repo: str
    repo_url: str
    title: str
    type: str
    url: str | None
    latest_comment_url: str | None
    reason: str
    #: Whether it's unread on GitHub.
    unread: bool
    updated_at: str
    # From the triage state:
    done_mark: str | None = None
    attention_at: str | None = None
    # From GitHub:
    state: str | None = None
    state_checked_at: str | None = None
    # From the triage state:
    last_activity: str | None = None
    seen_at: str | None = None
    # From checking GitHub:
    checked_until: str | None = None

    @property
    def done(self) -> bool:
        return self.done_mark is not None

    @property
    def attention(self) -> bool:
        return self.attention_at is not None

    @property
    def new(self) -> bool:
        """Whether there's been activity since you last viewed or labelled it."""
        return self.last_activity is not None and (
            self.seen_at is None or self.last_activity > self.seen_at
        )


@dataclass(frozen=True)
class Label:
    id: str
    name: str
    color: str
    #: Whether activity on its threads flags them for attention.
    flag: bool = False
    #: Its place in the sidebar, from 1, which gives it its number key.
    position: int = 0


def _details(thread: Thread) -> Thread:
    """Just what GitHub says about a thread."""
    return replace(
        thread, done_mark=None, attention_at=None, last_activity=None, seen_at=None
    )


class Inbox:
    """An inbox: what's in each view, and the triage you can do to it.

    `Store` keeps one on this Mac. `Mirror` is a copy of one on another Mac.
    """

    #: The device that makes this inbox's events.
    device = "unknown"

    def __init__(self, state: State | None = None):
        self.state = state or State()
        #: What GitHub says about each thread, by ID.
        self._threads: dict[str, Thread] = {}

    def _append(self, events: list[Event]) -> None:
        """Record events, and apply them."""
        raise NotImplementedError

    def _save(self, threads: Iterable[Thread]) -> None:
        """Keep what GitHub says about threads."""
        for thread in threads:
            self._threads[thread.id] = _details(thread)

    def _new(self, at: str | None = None) -> dict:
        """The ID, time and device of a new event made here, now unless it's
        ``at``."""
        return {
            "id": f"{next(_SEQUENCE):012d}-{uuid4().hex[:16]}",
            "at": at or self._now(),
            "device": self.device,
        }

    def _now(self) -> str:
        return now()

    ######################################################################
    # Views
    ######################################################################

    def threads(
        self,
        view: str,
        search: str = "",
        filters: Mapping[str, str] | None = None,
    ) -> list[Thread]:
        """The threads in a view.

        The inbox and its unread view are ordered by the latest activity that
        needed a response from you, newest first, and the done view by GitHub's
        latest activity. A label, and the attention view, are ordered by when
        each thread was given it, oldest first, so threads stay put.

        :param view: `INBOX`, `ATTENTION`, `UNREAD`, `DONE`, or a label's ID.
        :param search: Only include threads whose title or repository contains
            this text (case-insensitive).
        :param filters: Only include threads with exactly these values, keyed
            by detail (any of `FILTER_COLUMNS`), like ``{"type": "Issue"}``.
        """
        filters = filters or {}
        for column in filters:
            if column not in FILTER_COLUMNS:
                raise ValueError(f"Can't filter threads by {column!r}.")
        search = search.strip().lower()

        def wanted(thread: Thread) -> bool:
            return (
                not search
                or search in thread.title.lower()
                or search in thread.repo.lower()
            ) and all(getattr(thread, c) == v for c, v in filters.items())

        found = [
            (state, thread)
            for thread_id, state in self.state.threads.items()
            if _in_view(view, state)
            and (details := self._threads.get(thread_id)) is not None
            and wanted(thread := self._with_state(details, state))
        ]
        if view in (INBOX, UNREAD):
            found.sort(
                key=lambda f: (f[0].last_activity, f[1].updated_at), reverse=True
            )
        elif view == DONE:
            found.sort(key=lambda f: f[1].updated_at, reverse=True)
        else:
            found.sort(key=lambda f: (f[0].labels[view], f[1].id))
        return [thread for _, thread in found]

    def filter_counts(
        self,
        view: str,
        search: str = "",
        filters: Mapping[str, str] | None = None,
    ) -> dict[str, dict[str, int]]:
        """For each of `FILTER_COLUMNS`, how many threads in a view have each
        value: how many choosing that value would show, given the search and
        the filters for the other columns. Takes the same arguments as
        `threads()`.
        """
        filters = filters or {}
        return {
            column: dict(
                Counter(
                    getattr(thread, column)
                    for thread in self.threads(
                        view,
                        search,
                        {c: v for c, v in filters.items() if c != column},
                    )
                )
            )
            for column in FILTER_COLUMNS
        }

    def get(self, thread_id: str) -> Thread | None:
        if (details := self._threads.get(thread_id)) is None:
            return None
        return self._with_state(details, self.state.threads.get(thread_id))

    def thread_with_url(self, url: str | None, manual: bool = False) -> str | None:
        """The ID of the thread for an issue, pull request or discussion, given
        its API URL. With ``manual``, only a thread added by hand counts.
        Otherwise, a notification's thread comes first."""
        if not url:
            return None
        found = sorted(
            (thread.id.startswith(MANUAL_PREFIX), thread.id)
            for thread in self._threads.values()
            if thread.url == url and (not manual or thread.id.startswith(MANUAL_PREFIX))
        )
        return found[0][1] if found else None

    def counts(self) -> dict[str, int]:
        """The number of threads in each view, keyed like `threads()`'s ``view``."""
        return self._count(lambda state: True, include=(UNREAD, DONE))

    def new_counts(self) -> dict[str, int]:
        """How many threads in the inbox, attention view and each label have
        activity you haven't seen."""
        return self._count(lambda state: state.new)

    def _count(self, counts, include=()) -> dict[str, int]:
        result = dict.fromkeys((INBOX, ATTENTION, *include), 0)
        for thread_id, state in self.state.threads.items():
            if thread_id not in self._threads or not counts(state):
                continue
            for view in (INBOX, *include):
                if _in_view(view, state):
                    result[view] += 1
            for label_id in state.labels:
                result[label_id] = result.get(label_id, 0) + 1
        return result

    def _with_state(self, details: Thread, state: ThreadState | None) -> Thread:
        if state is None:
            return details
        return replace(
            details,
            done_mark=state.done_at,
            attention_at=state.labels.get(ATTENTION),
            last_activity=state.last_activity,
            seen_at=state.seen_at,
        )

    ######################################################################
    # Triage
    ######################################################################

    def mark_seen(self, thread_ids: Iterable[str]) -> None:
        """Record that you've seen threads, which clears what's new on them. It
        doesn't clear Attention required: take that off to say you've dealt
        with it."""
        thread_ids = list(thread_ids)
        self._append([Viewed(**self._new(), thread_id=i) for i in thread_ids])
        self._mark_read(thread_ids)

    def mark_done(self, thread_ids: Iterable[str]) -> None:
        """Take threads out of every view, labels and attention included,
        until there's activity that needs a response from you."""
        thread_ids = list(thread_ids)
        self._append([MarkedDone(**self._new(), thread_id=i) for i in thread_ids])
        self._mark_read(thread_ids)

    def set_attention(self, thread_ids: Iterable[str], on: bool) -> None:
        """Flag threads as needing attention (``on=True``), or clear the flag.

        Flagging a thread that's already flagged keeps its place in the view.
        """
        self.set_label(ATTENTION, thread_ids, on)

    def set_label(self, label_id: str, thread_ids: Iterable[str], on: bool) -> None:
        """Give threads a label (``on=True``), or take it off them.

        Labelling a thread counts as seeing it, and brings it back from done. A
        thread that already has the label keeps its place in the label.
        """
        thread_ids = list(thread_ids)
        kind = Labelled if on else Unlabelled
        self._append(
            [kind(**self._new(), thread_id=i, label_id=label_id) for i in thread_ids]
        )
        if on:
            self._mark_read(thread_ids)

    def _mark_read(self, thread_ids: list[str]) -> None:
        self._save(
            replace(thread, unread=False)
            for i in thread_ids
            if (thread := self._threads.get(i)) is not None and thread.unread
        )

    ######################################################################
    # Labels
    ######################################################################

    def labels(self) -> list[Label]:
        """Every label, in sidebar order. (Attention required isn't one.)"""
        return [
            Label(label_id, info.name, info.color, info.flag, position)
            for position, (label_id, info) in enumerate(
                ((k, v) for k, v in self.state.labels.items() if k != ATTENTION), 1
            )
        ]

    def label(self, label_id: str) -> Label | None:
        return next((label for label in self.labels() if label.id == label_id), None)

    def create_label(
        self, name: str, color: str | None = None, flag: bool = False
    ) -> Label:
        name = self._check_name(name)
        if color is None:
            color = LABEL_COLORS[len(self.labels()) % len(LABEL_COLORS)]
        label_id = uuid4().hex
        self._append(
            [
                LabelDefined(
                    **self._new(),
                    label_id=label_id,
                    name=name,
                    color=color,
                    flag_on_activity=flag,
                )
            ]
        )
        return self.label(label_id)

    def edit_label(self, label_id: str, name: str, flag: bool) -> Label:
        """Rename a label, or change whether activity flags its threads. That
        only affects activity from then on."""
        if (label := self.label(label_id)) is None:
            raise ValueError("That label has been deleted.")
        name = self._check_name(name, label_id)
        self._append(
            [
                LabelDefined(
                    **self._new(),
                    label_id=label_id,
                    name=name,
                    color=label.color,
                    flag_on_activity=flag,
                )
            ]
        )
        return self.label(label_id)

    def reorder_labels(self, order: Iterable[str]) -> None:
        """Put the labels in this order, which changes their number keys. Any
        not given keep their order, after the ones given."""
        self._append([LabelsReordered(**self._new(), order=list(order))])

    def delete_label(self, label_id: str) -> None:
        if label_id == ATTENTION:
            raise ValueError("Attention required can't be deleted.")
        self._append([LabelDeleted(**self._new(), label_id=label_id)])

    def labels_by_thread(self) -> dict[str, list[Label]]:
        """Every thread's labels, in label order. Unlabelled threads are omitted,
        and Attention required isn't included."""
        labels = self.labels()
        result = {}
        for thread_id, state in self.state.threads.items():
            if found := [label for label in labels if label.id in state.labels]:
                result[thread_id] = found
        return result

    def _check_name(self, name: str, label_id: str | None = None) -> str:
        name = " ".join(name.split())
        if not name:
            raise ValueError("A label needs a name.")
        for other, info in self.state.labels.items():
            if other != label_id and info.name.casefold() == name.casefold():
                raise ValueError(f"There's already a label called {name!r}.")
        return name


def _in_view(view: str, state: ThreadState) -> bool:
    if view in (INBOX, UNREAD):
        # Attention required counts as a label: a thread there isn't in the
        # inbox.
        return (
            state.last_activity is not None
            and state.done_at is None
            and not state.labels
            and (view == INBOX or state.new)
        )
    if view == DONE:
        return state.done_at is not None
    return view in state.labels


class Store(Inbox):
    """An inbox kept on this Mac, in SQLite: its event log, and what GitHub
    says about its threads."""

    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute(_META)
        device = self._device_id()
        # Upgrading an old store happens all at once, or not at all.
        self.db.execute("BEGIN")
        try:
            imported = None
            if legacy.is_legacy(self.db):
                imported = legacy.upgrade(self.db, device, stamp)
            for statement in SCHEMA.split(";;"):
                self.db.execute(statement)
            if imported:
                legacy.save(self.db, *imported)
            self.db.execute(f"PRAGMA user_version = {VERSION}")
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        self.db.row_factory = sqlite3.Row
        super().__init__()
        self.device = device
        self._batches = 0
        self._stale = False
        self._last: tuple | None = None
        #: When the latest event in the log happened.
        self._latest = ""
        self._threads = {
            row["id"]: Thread(**{**dict(row), "unread": bool(row["unread"])})
            for row in self.db.execute("SELECT * FROM threads")
        }
        self._rebuild()

    def close(self) -> None:
        self.db.close()

    def _device_id(self) -> str:
        if (device := self.get_meta("device")) is None:
            device = uuid4().hex
            self.set_meta("device", device)
        return device

    ######################################################################
    # The event log
    ######################################################################

    def _now(self) -> str:
        """Now, or just after the latest event, if this Mac's clock is behind
        it, such as behind GitHub's. What you do here comes after what you've
        seen."""
        current = now()
        if self._latest < current:
            return current
        latest = datetime.strptime(self._latest, _FORMAT).replace(tzinfo=UTC)
        if latest - datetime.strptime(current, _FORMAT).replace(tzinfo=UTC) > MAX_SKEW:
            return current
        return (latest + timedelta(microseconds=1)).strftime(_FORMAT)

    def _append(self, events: list[Event]) -> None:
        with self.db:
            added = [
                event
                for event in events
                if self.db.execute(
                    "INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?, ?)",
                    to_row(event),
                ).rowcount
            ]
        for event in added:
            self._latest = max(self._latest, event.at)
            if not self._stale and (self._last is None or order(event) > self._last):
                self.state.apply(event)
                self._last = order(event)
            else:
                # Activity carries GitHub's time, so it can arrive after events
                # it came before. Applying it out of order would give a
                # different state from a replay, so replay instead.
                self._stale = True
        if self._stale and not self._batches:
            self._rebuild()

    @contextmanager
    def batch(self) -> Iterator[None]:
        """Replay the log, if that's needed, once at the end, rather than after
        each change. Until then, the triage state may be behind."""
        self._batches += 1
        try:
            yield
        finally:
            self._batches -= 1
            if self._stale and not self._batches:
                self._rebuild()

    def _rebuild(self) -> None:
        rows = self.db.execute("SELECT id, at, device, kind, data FROM events")
        events = sorted(
            (event for row in rows if (event := from_row(*row)) is not None),
            key=order,
        )
        self.state = State()
        for event in events:
            self.state.apply(event)
        self._last = order(events[-1]) if events else None
        self._latest = events[-1].at if events else ""
        self._stale = False

    def _save(self, threads: Iterable[Thread]) -> None:
        threads = [_details(thread) for thread in threads]
        with self.db:
            self.db.executemany(
                f"INSERT OR REPLACE INTO threads ({', '.join(_DETAILS)}) "
                f"VALUES ({', '.join('?' * len(_DETAILS))})",
                [tuple(getattr(t, column) for column in _DETAILS) for t in threads],
            )
        super()._save(threads)

    ######################################################################
    # Threads from GitHub's inbox
    ######################################################################

    def place(self, threads: Iterable[Thread]) -> None:
        """Put threads Buzz hasn't had before in the inbox, at their place in
        GitHub's order. Ones that are read on GitHub have nothing new."""
        new = [thread for thread in threads if thread.id not in self._threads]
        self._save(
            replace(thread, checked_until=stamp(thread.updated_at)) for thread in new
        )
        events = []
        for thread in new:
            at = stamp(thread.updated_at)
            events.append(_activity(thread.id, at))
            if not thread.unread:
                events.append(Viewed(**self._new(at), thread_id=thread.id))
        self._append(events)

    def update_details(self, threads: Iterable[Thread]) -> None:
        """Bring threads' details up to date with what GitHub says now."""
        self._save(
            replace(
                known,
                repo=thread.repo,
                repo_url=thread.repo_url,
                title=thread.title,
                type=thread.type,
                url=thread.url or known.url,
                latest_comment_url=thread.latest_comment_url,
                reason=thread.reason,
                unread=thread.unread,
                updated_at=thread.updated_at,
            )
            for thread in threads
            if (known := self._threads.get(thread.id)) is not None
        )

    def record_activity(self, thread_id: str, at: str) -> None:
        """Record activity on a thread that needs a response from you, at the
        time it happened on GitHub.

        It brings the thread back from done if it happened after the thread was
        done, and flags it for attention if it had a label that flags on
        activity by then. Activity from before you labelled it doesn't flag it,
        even if Buzz only notices it afterwards. Recording the same activity
        again changes nothing.
        """
        self._append([_activity(thread_id, stamp(at))])

    def set_checked(self, thread_id: str, until: str) -> None:
        """Record that a thread's activity has been checked up to ``until``."""
        if (thread := self._threads.get(thread_id)) is not None:
            self._save([replace(thread, checked_until=stamp(until))])

    def mark_gone(self, listed: Iterable[str]) -> None:
        """Given every thread in GitHub's inbox, mark the inbox's other threads
        as done: they were marked as done somewhere else, such as on
        github.com. Threads with a label are Buzz's to triage, so they stay
        where they are, and so do threads added by hand, which were never in
        GitHub's inbox."""
        listed = set(listed)
        gone = [
            thread_id
            for thread_id in self._threads
            if thread_id not in listed
            and not thread_id.startswith(MANUAL_PREFIX)
            and not (
                (state := self.state.threads.get(thread_id))
                and (state.done_at is not None or state.labels)
            )
        ]
        self.mark_done(gone)

    def threads_to_look_up(self, limit: int) -> list[Thread]:
        """Up to ``limit`` issues, pull requests and discussions whose state
        hasn't been looked up since their last activity, ones not done first,
        then the most recently active."""
        found = sorted(
            (
                thread
                for thread in self._threads.values()
                if thread.type in ("Issue", "PullRequest", "Discussion")
                and thread.url is not None
                and thread.state_checked_at != thread.updated_at
            ),
            key=lambda thread: thread.updated_at,
            reverse=True,
        )
        found.sort(key=lambda thread: self.get(thread.id).done)
        return [self.get(thread.id) for thread in found[:limit]]

    def set_states(self, states: Iterable[tuple[str, str | None, str]]) -> None:
        """Record what threads' states were looked up as.

        :param states: ``(thread_id, state, updated_at)`` for each thread,
            where ``updated_at`` is the thread's when it was looked up.
        """
        self._save(
            replace(thread, state=state, state_checked_at=updated_at)
            for thread_id, state, updated_at in states
            if (thread := self._threads.get(thread_id)) is not None
        )

    ######################################################################
    # Threads added by hand, and pull requests found by their CI
    ######################################################################

    def add_manual(self, thread: Thread, seen: bool = True) -> str:
        """Add a thread that no notification has brought in, such as an issue
        looked up by its number. Returns its ID, which is the existing thread's
        if Buzz already has this issue, pull request or discussion.

        :param seen: Whether you've seen it. A thread you add goes at the top of
            the inbox with nothing new on it; one Buzz finds by itself has
            nothing on it until its activity is recorded.
        """
        if thread.id in self._threads:
            return thread.id
        if (existing := self.thread_with_url(thread.url)) is not None:
            return existing
        self._save(
            [replace(thread, unread=False, checked_until=stamp(thread.updated_at))]
        )
        if seen:
            at = now()
            self._append(
                [
                    ActivityDetected(**self._new(at), thread_id=thread.id),
                    Viewed(**self._new(at), thread_id=thread.id),
                ]
            )
        return thread.id

    def manual_threads_to_check(self) -> list[Thread]:
        """Threads added by hand that are worth checking for activity: the
        ones that aren't done."""
        return [
            thread
            for thread_id in self._threads
            if thread_id.startswith(MANUAL_PREFIX)
            and not (thread := self.get(thread_id)).done
        ]

    def update_manual(self, fresh: Thread, latest_activity: str | None) -> None:
        """Bring a thread added by hand up to date with GitHub.

        :param latest_activity: When the latest activity that needs a response
            from you happened, if there's been any since it was last checked.
        """
        if (known := self._threads.get(fresh.id)) is not None:
            self._save(
                [
                    replace(
                        known,
                        title=fresh.title,
                        updated_at=fresh.updated_at,
                        checked_until=stamp(fresh.updated_at),
                    )
                ]
            )
        if latest_activity is not None:
            self.record_activity(fresh.id, latest_activity)

    def adopt(self, manual_id: str, thread: Thread) -> None:
        """Put a notification's thread in place of the thread added by hand for
        the same issue, keeping its labels (in the same places), attention,
        and what's new on it."""
        old = self._threads.pop(manual_id)
        with self.db:
            self.db.execute("DELETE FROM threads WHERE id = ?", (manual_id,))
        self._save(
            [
                replace(
                    thread,
                    state=old.state,
                    state_checked_at=old.state_checked_at,
                    checked_until=old.checked_until,
                )
            ]
        )
        self._append([Adopted(**self._new(), thread_id=thread.id, previous=manual_id)])

    ######################################################################
    # Sharing, with Buzz on another Mac
    ######################################################################

    def snapshot(self) -> dict:
        """Everything Buzz on another Mac needs to show this inbox, as
        JSON-ready data. (See `Mirror`.)"""
        return {
            "login": self.get_meta("login"),
            "labels": [
                {
                    "id": label.id,
                    "name": label.name,
                    "color": label.color,
                    "flag_on_activity": label.flag,
                }
                for label in self.labels()
            ],
            "threads": [
                {
                    **{c: getattr(self._threads[i], c) for c in _SHARED_DETAILS},
                    "labels": dict(state.labels),
                    "last_activity": state.last_activity,
                    "seen_at": state.seen_at,
                    "done_at": state.done_at,
                }
                for i, state in sorted(self.state.threads.items())
                if i in self._threads
            ],
        }

    ######################################################################
    # Settings and sync state
    ######################################################################

    def get_meta(self, key: str) -> str | None:
        found = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,))
        row = found.fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str | None) -> None:
        with self.db:
            if value is None:
                self.db.execute("DELETE FROM meta WHERE key = ?", (key,))
            else:
                self.db.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )


def _activity(thread_id: str, at: str) -> ActivityDetected:
    # The ID comes from the thread and the time, so the same activity found
    # again, on this device or another, is one event.
    return ActivityDetected(
        id=f"activity:{thread_id}:{at}", at=at, device="github", thread_id=thread_id
    )


class Mirror(Inbox):
    """A copy of an inbox kept on another Mac, from its `Store.snapshot()`.

    Changes made to it are only made here, to show straight away what the other
    Mac is about to do. The next snapshot replaces them.
    """

    device = "mirror"

    def __init__(self):
        super().__init__(State(labels={ATTENTION: ATTENTION_LABEL}))
        #: The GitHub account the other Mac is signed in to.
        self.login: str | None = None

    def load(self, snapshot: dict) -> None:
        """Replace everything with a `Store.snapshot()` of the other inbox.

        :raises ValueError: If it isn't one.
        """
        try:
            labels = {ATTENTION: ATTENTION_LABEL}
            for label in snapshot["labels"]:
                labels[str(label["id"])] = LabelInfo(
                    str(label["name"]),
                    str(label["color"]),
                    bool(label["flag_on_activity"]),
                )
            threads, details = {}, {}
            for thread in snapshot["threads"]:
                thread_id = str(thread["id"])
                details[thread_id] = Thread(
                    **{
                        **{column: thread[column] for column in _SHARED_DETAILS},
                        "id": thread_id,
                        "unread": bool(thread["unread"]),
                    }
                )
                threads[thread_id] = ThreadState(
                    labels=dict(thread["labels"]),
                    last_activity=thread["last_activity"],
                    seen_at=thread["seen_at"],
                    done_at=thread["done_at"],
                )
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ValueError(f"That isn't a Buzz inbox ({exc!r}).") from exc
        self.state = State(labels=labels, threads=threads)
        self._threads = details
        self.login = snapshot.get("login")

    def _append(self, events: list[Event]) -> None:
        for event in events:
            self.state.apply(event)
