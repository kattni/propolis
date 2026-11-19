"""Local storage for notification threads and labels, backed by SQLite.

Labels only exist locally: GitHub has no API for organising notifications, so
the store keeps them, along with a copy of every thread it has seen.

Every thread in your GitHub inbox starts in Buzz's inbox. Giving it a label
moves it out of the inbox and into that label, where it stays, in the order you
labelled it, however much activity it gets. Some labels flag on activity: when
someone else acts on one of their threads, it's also put under Attention
required. Other labels just show that there's something new on it.

Only activity that needs a response from you counts (see `buzz.activity`). The
store records when it happened, at GitHub's time, and when you last saw each
thread, so it can tell what's new.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

#: Coloured markers used to tell labels apart, assigned in rotation.
LABEL_COLORS = ("🔵", "🟢", "🟣", "🟠", "🔴", "🟡", "🟤", "⚫")

#: The labels every new store starts with, in sidebar order: each one's name,
#: colour, and whether activity on its threads flags them for attention.
DEFAULT_LABELS = (
    ("Needs review", "🔵", False),
    ("Question", "🟣", False),
    ("Deep response", "🟤", False),
    # Activity may mean it's unblocked.
    ("Blocked", "🔴", True),
    ("Low priority", "⚪", False),
    ("High priority", "🟠", True),
    ("In progress", "🟢", False),
    ("Cold storage", "⚫", False),
)

# The built-in views. A label's view is identified by the label's integer ID.
INBOX = "inbox"
ATTENTION = "attention"
UNREAD = "unread"
DONE = "done"

#: The thread columns that `Store.threads()` can filter by.
FILTER_COLUMNS = ("type", "reason", "repo")

SCHEMA = """
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
    -- When it was marked as done, or NULL.
    done_mark TEXT,
    -- When the thread was flagged as needing attention, or NULL.
    attention_at TEXT,
    -- Whether the issue, pull request or discussion is open, merged, etc.
    -- (one of github.STATES), or NULL if that isn't known.
    state TEXT,
    -- The thread's updated_at when its state was last looked up, so new
    -- activity, which may have changed it, means looking again.
    state_checked_at TEXT,
    -- When the latest activity that needs a response from you happened.
    last_activity TEXT,
    -- When you last viewed or labelled it.
    seen_at TEXT,
    -- How far it's been checked for activity: its updated_at at the time.
    checked_until TEXT
);
CREATE TABLE IF NOT EXISTS labels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    color TEXT NOT NULL,
    -- Whether activity on its threads flags them for attention.
    flag INTEGER NOT NULL DEFAULT 0,
    -- Its place in the sidebar, which gives it its number key.
    position INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS thread_labels (
    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    label_id INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
    added_at TEXT NOT NULL,
    PRIMARY KEY (thread_id, label_id)
);
-- The latest CI failure seen on each of your pull requests, by reference
-- (like beeware/toga#123), so each one only counts once.
CREATE TABLE IF NOT EXISTS ci_failures (
    ref TEXT PRIMARY KEY,
    failed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Columns added after the first release, for stores created before them.
MIGRATIONS = (
    ("threads", "attention_at", "TEXT"),
    ("thread_labels", "added_at", "TEXT NOT NULL DEFAULT ''"),
    ("threads", "state", "TEXT"),
    ("threads", "state_checked_at", "TEXT"),
    ("threads", "last_activity", "TEXT"),
    ("threads", "seen_at", "TEXT"),
    ("threads", "checked_until", "TEXT"),
    ("labels", "flag", "INTEGER NOT NULL DEFAULT 0"),
    ("labels", "position", "INTEGER NOT NULL DEFAULT 0"),
)
#: The store's format, for changes that need more than new columns.
VERSION = 1

_LABELLED = "id IN (SELECT thread_id FROM thread_labels)"
# Attention required counts as a label: a thread there isn't in the inbox.
_TRIAGED = f"(attention_at IS NOT NULL OR {_LABELLED})"
_NEW = "(last_activity IS NOT NULL AND (seen_at IS NULL OR last_activity > seen_at))"

#: Threads added by hand have IDs like ``manual:beeware/toga#123``, unlike the
#: numeric IDs of notification threads. So do pull requests found only because
#: their CI failed.
MANUAL_PREFIX = "manual:"
_MANUAL = "id LIKE 'manual:%'"
_THREAD_COLUMNS = (
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
    "done_mark",
    "attention_at",
    "state",
    "state_checked_at",
    "last_activity",
    "seen_at",
    "checked_until",
)
#: What Buzz knows about a thread, rather than what GitHub says about it.
_STATE_COLUMNS = (
    "done_mark",
    "attention_at",
    "state",
    "state_checked_at",
    "last_activity",
    "seen_at",
    "checked_until",
)
_LABEL_COLUMNS = ("id", "name", "color", "flag", "position")

_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


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
    done_mark: str | None = None
    attention_at: str | None = None
    state: str | None = None
    state_checked_at: str | None = None
    last_activity: str | None = None
    seen_at: str | None = None
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
    id: int
    name: str
    color: str
    #: Whether activity on its threads flags them for attention.
    flag: bool = False
    position: int = 0


class Store:
    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        for table, column, declaration in MIGRATIONS:
            columns = {
                row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")
            }
            if column not in columns:
                self.db.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
                )
        self._upgrade()
        self.db.commit()

    def _upgrade(self) -> None:
        (version,) = self.db.execute("PRAGMA user_version").fetchone()
        if version < 1:
            # Labels get a place in the sidebar, in the order they were made,
            # and the default labels that flag on activity start doing so.
            self.db.execute("UPDATE labels SET position = id")
            self.db.execute(
                "UPDATE labels SET flag = 1 WHERE name IN ('Blocked', 'High priority')"
            )
            # Until now, Buzz went by GitHub's unread flag. Start from it.
            rows = self.db.execute(
                "SELECT id, unread, updated_at, done_mark FROM threads"
            ).fetchall()
            self.db.executemany(
                "UPDATE threads SET last_activity = ?, seen_at = ?, "
                "checked_until = ?, done_mark = ? WHERE id = ?",
                [
                    (
                        stamp(row["updated_at"]),
                        None if row["unread"] else stamp(row["updated_at"]),
                        stamp(row["updated_at"]),
                        stamp(row["done_mark"]) if row["done_mark"] else None,
                        row["id"],
                    )
                    for row in rows
                ],
            )
            # Done now takes a thread's labels off. Labelled threads that were
            # done stay in their labels, and aren't done any more.
            self.db.execute(f"UPDATE threads SET done_mark = NULL WHERE {_LABELLED}")
        self.db.execute(f"PRAGMA user_version = {VERSION}")

    def close(self) -> None:
        self.db.close()

    ######################################################################
    # Threads from GitHub's inbox
    ######################################################################

    def place(self, threads: Iterable[Thread]) -> None:
        """Put threads Buzz hasn't had before in the inbox, at their place in
        GitHub's order. Ones that are read on GitHub have nothing new."""
        rows = []
        for thread in threads:
            at = stamp(thread.updated_at)
            rows.append(
                {
                    **vars(thread),
                    "done_mark": None,
                    "attention_at": None,
                    "last_activity": at,
                    "seen_at": None if thread.unread else at,
                    "checked_until": at,
                }
            )
        with self.db:
            self._insert("threads", _THREAD_COLUMNS, rows, ignore=True)

    def update_details(self, threads: Iterable[Thread]) -> None:
        """Bring threads' details up to date with what GitHub says now."""
        with self.db:
            self.db.executemany(
                """
                UPDATE threads SET
                    repo = ?, repo_url = ?, title = ?, type = ?,
                    url = COALESCE(?, url), latest_comment_url = ?, reason = ?,
                    unread = ?, updated_at = ?
                WHERE id = ?
                """,
                [
                    (
                        t.repo,
                        t.repo_url,
                        t.title,
                        t.type,
                        t.url,
                        t.latest_comment_url,
                        t.reason,
                        t.unread,
                        t.updated_at,
                        t.id,
                    )
                    for t in threads
                ],
            )

    def record_activity(self, thread_id: str, at: str) -> None:
        """Record activity on a thread that needs a response from you, at the
        time it happened on GitHub.

        It brings the thread back from done if it happened after the thread was
        done, and flags it for attention if it had a label that flags on
        activity by then. Activity from before you labelled it doesn't flag it,
        even if Buzz only notices it afterwards.
        """
        with self.db:
            self.db.execute(
                """
                UPDATE threads SET
                    last_activity = max(COALESCE(last_activity, ''), :at),
                    done_mark = CASE
                        WHEN done_mark < :at THEN NULL ELSE done_mark
                    END,
                    attention_at = CASE
                        WHEN attention_at IS NULL AND EXISTS (
                            SELECT 1 FROM thread_labels tl
                            JOIN labels l ON l.id = tl.label_id
                            WHERE tl.thread_id = threads.id AND l.flag
                                AND tl.added_at < :at
                        ) THEN :now
                        ELSE attention_at
                    END
                WHERE id = :id
                """,
                {"at": stamp(at), "now": now(), "id": thread_id},
            )

    def set_checked(self, thread_id: str, until: str) -> None:
        """Record that a thread's activity has been checked up to ``until``."""
        with self.db:
            self.db.execute(
                "UPDATE threads SET checked_until = ? WHERE id = ?",
                (stamp(until), thread_id),
            )

    def mark_gone(self, listed: Iterable[str]) -> None:
        """Given every thread in GitHub's inbox, mark the inbox's other threads
        as done: they were marked as done somewhere else, such as on
        github.com. Threads with a label are Buzz's to triage, so they stay
        where they are, and so do threads added by hand, which were never in
        GitHub's inbox."""
        listed = set(listed)
        gone = [
            row["id"]
            for row in self.db.execute(
                f"SELECT id FROM threads WHERE done_mark IS NULL "
                f"AND NOT {_TRIAGED} AND NOT {_MANUAL}"
            )
            if row["id"] not in listed
        ]
        with self.db:
            self.db.executemany(
                "UPDATE threads SET done_mark = ?, unread = 0 WHERE id = ?",
                [(now(), thread_id) for thread_id in gone],
            )

    ######################################################################
    # Views
    ######################################################################

    def threads(
        self,
        view: str | int,
        search: str = "",
        filters: Mapping[str, str] | None = None,
    ) -> list[Thread]:
        """The threads in a view.

        The inbox and its unread view are ordered by the latest activity that
        needed a response from you, newest first, and the done view by GitHub's
        latest activity. A label, and the attention view, are ordered by when
        each thread was added to them, oldest first, so threads stay put.

        :param view: `INBOX`, `ATTENTION`, `UNREAD`, `DONE`, or a label ID.
        :param search: Only include threads whose title or repository contains
            this text (case-insensitive).
        :param filters: Only include threads with exactly these values, keyed
            by column (any of `FILTER_COLUMNS`), like ``{"type": "Issue"}``.
        """
        sql = "SELECT t.* FROM threads t"
        params: list = []
        inbox = f"done_mark IS NULL AND NOT {_TRIAGED}"
        if view == INBOX:
            where, order = inbox, "last_activity DESC, updated_at DESC"
        elif view == UNREAD:
            where, order = f"{inbox} AND {_NEW}", "last_activity DESC, updated_at DESC"
        elif view == DONE:
            where, order = "done_mark IS NOT NULL", "updated_at DESC"
        elif view == ATTENTION:
            where, order = "attention_at IS NOT NULL", "attention_at, updated_at"
        else:
            sql += " JOIN thread_labels tl ON tl.thread_id = t.id AND tl.label_id = ?"
            params.append(view)
            where, order = "1", "tl.added_at, tl.rowid"

        if search := search.strip().lower():
            where += " AND (instr(lower(title), ?) > 0 OR instr(lower(repo), ?) > 0)"
            params += [search, search]

        for column, value in (filters or {}).items():
            # Only known columns are named in the SQL; values are parameters.
            if column not in FILTER_COLUMNS:
                raise ValueError(f"Can't filter threads by {column!r}.")
            where += f" AND t.{column} = ?"
            params.append(value)

        rows = self.db.execute(f"{sql} WHERE {where} ORDER BY {order}", params)
        return [self._thread(row) for row in rows]

    def filter_counts(
        self,
        view: str | int,
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
        row = self.db.execute(
            "SELECT * FROM threads WHERE id = ?", (thread_id,)
        ).fetchone()
        return self._thread(row) if row else None

    def thread_with_url(self, url: str | None, manual: bool = False) -> str | None:
        """The ID of the thread for an issue, pull request or discussion, given
        its API URL. With ``manual``, only a thread added by hand counts."""
        if not url:
            return None
        row = self.db.execute(
            f"SELECT id FROM threads WHERE url = ? {f'AND {_MANUAL}' if manual else ''} "
            f"ORDER BY {_MANUAL}",
            (url,),
        ).fetchone()
        return row["id"] if row else None

    def counts(self) -> dict[str | int, int]:
        """The number of threads in each view, keyed like `threads()`'s ``view``."""
        inbox, attention, unread, done = self.db.execute(
            f"""
            SELECT
                COUNT(*) FILTER (WHERE done_mark IS NULL AND NOT {_TRIAGED}),
                COUNT(*) FILTER (WHERE attention_at IS NOT NULL),
                COUNT(*) FILTER (
                    WHERE done_mark IS NULL AND NOT {_TRIAGED} AND {_NEW}
                ),
                COUNT(*) FILTER (WHERE done_mark IS NOT NULL)
            FROM threads
            """
        ).fetchone()
        counts: dict[str | int, int] = {
            INBOX: inbox,
            ATTENTION: attention,
            UNREAD: unread,
            DONE: done,
        }
        for row in self.db.execute(
            "SELECT label_id, COUNT(*) AS n FROM thread_labels GROUP BY label_id"
        ):
            counts[row["label_id"]] = row["n"]
        return counts

    def new_counts(self) -> dict[str | int, int]:
        """How many threads in the inbox, attention view and each label have
        activity you haven't seen."""
        inbox, attention = self.db.execute(
            f"""
            SELECT
                COUNT(*) FILTER (
                    WHERE done_mark IS NULL AND NOT {_TRIAGED} AND {_NEW}
                ),
                COUNT(*) FILTER (WHERE attention_at IS NOT NULL AND {_NEW})
            FROM threads
            """
        ).fetchone()
        counts: dict[str | int, int] = {INBOX: inbox, ATTENTION: attention}
        for row in self.db.execute(
            f"""
            SELECT tl.label_id, COUNT(*) AS n FROM thread_labels tl
            JOIN threads t ON t.id = tl.thread_id WHERE {_NEW}
            GROUP BY tl.label_id
            """
        ):
            counts[row["label_id"]] = row["n"]
        return counts

    ######################################################################
    # Triage
    ######################################################################

    def mark_seen(self, thread_ids: Iterable[str]) -> None:
        """Record that you've seen threads, which clears what's new on them. It
        doesn't clear Attention required: take that off to say you've dealt
        with it."""
        with self.db:
            self.db.executemany(
                "UPDATE threads SET seen_at = ?, unread = 0 WHERE id = ?",
                [(now(), i) for i in thread_ids],
            )

    def mark_done(self, thread_ids: Iterable[str]) -> None:
        """Take threads out of every view, labels and attention included,
        until there's activity that needs a response from you."""
        thread_ids = [(i,) for i in thread_ids]
        with self.db:
            self.db.executemany(
                "UPDATE threads SET done_mark = ?, unread = 0, attention_at = NULL "
                "WHERE id = ?",
                [(now(), i) for (i,) in thread_ids],
            )
            self.db.executemany(
                "DELETE FROM thread_labels WHERE thread_id = ?", thread_ids
            )

    def set_attention(self, thread_ids: Iterable[str], on: bool) -> None:
        """Flag threads as needing attention (``on=True``), or clear the flag.

        Flagging a thread that's already flagged keeps its place in the view.
        """
        with self.db:
            if on:
                self.db.executemany(
                    "UPDATE threads SET attention_at = ? "
                    "WHERE id = ? AND attention_at IS NULL",
                    [(now(), i) for i in thread_ids],
                )
            else:
                self.db.executemany(
                    "UPDATE threads SET attention_at = NULL WHERE id = ?",
                    [(i,) for i in thread_ids],
                )

    def threads_to_look_up(self, limit: int) -> list[Thread]:
        """Up to ``limit`` issues, pull requests and discussions whose state
        hasn't been looked up since their last activity, ones not done first,
        then the most recently active."""
        rows = self.db.execute(
            """
            SELECT * FROM threads
            WHERE type IN ('Issue', 'PullRequest', 'Discussion')
                AND url IS NOT NULL
                AND state_checked_at IS NOT updated_at
            ORDER BY done_mark IS NOT NULL, updated_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [self._thread(row) for row in rows]

    def set_states(self, states: Iterable[tuple[str, str | None, str]]) -> None:
        """Record what threads' states were looked up as.

        :param states: ``(thread_id, state, updated_at)`` for each thread,
            where ``updated_at`` is the thread's when it was looked up.
        """
        with self.db:
            self.db.executemany(
                "UPDATE threads SET state = ?, state_checked_at = ? WHERE id = ?",
                [
                    (state, updated_at, thread_id)
                    for thread_id, state, updated_at in states
                ],
            )

    @staticmethod
    def _thread(row: sqlite3.Row) -> Thread:
        return Thread(**{**dict(row), "unread": bool(row["unread"])})

    ######################################################################
    # Snapshots, for showing this inbox in another Buzz
    ######################################################################

    def snapshot(self) -> dict:
        """Everything another Buzz needs to show this inbox, as JSON-ready data."""
        return {
            "login": self.get_meta("login"),
            "threads": [
                {column: row[column] for column in _THREAD_COLUMNS}
                for row in self.db.execute("SELECT * FROM threads")
            ],
            "labels": [
                {column: row[column] for column in _LABEL_COLUMNS}
                for row in self.db.execute("SELECT * FROM labels")
            ],
            "thread_labels": [
                dict(row)
                for row in self.db.execute(
                    "SELECT thread_id, label_id, added_at FROM thread_labels"
                )
            ],
        }

    def load_snapshot(self, snapshot: dict) -> None:
        """Replace everything in this store with a `snapshot()` of another."""
        with self.db:
            self.db.execute("DELETE FROM thread_labels")
            self.db.execute("DELETE FROM threads")
            self.db.execute("DELETE FROM labels")
            self._insert("threads", _THREAD_COLUMNS, snapshot["threads"])
            self._insert("labels", _LABEL_COLUMNS, snapshot["labels"])
            self._insert(
                "thread_labels",
                ("thread_id", "label_id", "added_at"),
                snapshot["thread_labels"],
            )
        self.set_meta("login", snapshot.get("login"))

    def _insert(
        self,
        table: str,
        columns: tuple[str, ...],
        rows: list[dict],
        ignore: bool = False,
    ) -> None:
        # Only known columns are named in the SQL; values are parameters.
        self.db.executemany(
            f"INSERT {'OR IGNORE ' if ignore else ''}INTO {table} "
            f"({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
            [tuple(row.get(column) for column in columns) for row in rows],
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
        if (existing := self.get(thread.id)) is not None:
            return existing.id
        if (existing_id := self.thread_with_url(thread.url)) is not None:
            return existing_id
        at = now() if seen else None
        with self.db:
            self._insert(
                "threads",
                _THREAD_COLUMNS,
                [
                    {
                        **vars(thread),
                        "unread": 0,
                        "last_activity": at,
                        "seen_at": at,
                        "checked_until": stamp(thread.updated_at),
                    }
                ],
            )
        return thread.id

    def manual_threads_to_check(self) -> list[Thread]:
        """Threads added by hand that are worth checking for activity: the
        ones that aren't done."""
        rows = self.db.execute(
            f"SELECT * FROM threads WHERE {_MANUAL} AND done_mark IS NULL"
        )
        return [self._thread(row) for row in rows]

    def update_manual(self, fresh: Thread, latest_activity: str | None) -> None:
        """Bring a thread added by hand up to date with GitHub.

        :param latest_activity: When the latest activity that needs a response
            from you happened, if there's been any since it was last checked.
        """
        with self.db:
            self.db.execute(
                "UPDATE threads SET title = ?, updated_at = ?, checked_until = ? "
                "WHERE id = ?",
                (fresh.title, fresh.updated_at, stamp(fresh.updated_at), fresh.id),
            )
        if latest_activity is not None:
            self.record_activity(fresh.id, latest_activity)

    def adopt(self, manual_id: str, thread: Thread) -> None:
        """Put a notification's thread in place of the thread added by hand for
        the same issue, keeping its labels (in the same places), attention,
        and what's new on it."""
        old = self.get(manual_id)
        with self.db:
            self._insert(
                "threads",
                _THREAD_COLUMNS,
                [
                    {
                        **vars(thread),
                        **{column: getattr(old, column) for column in _STATE_COLUMNS},
                    }
                ],
            )
            self.db.execute(
                "INSERT OR IGNORE INTO thread_labels (thread_id, label_id, added_at) "
                "SELECT ?, label_id, added_at FROM thread_labels WHERE thread_id = ?",
                (thread.id, manual_id),
            )
            self.db.execute("DELETE FROM threads WHERE id = ?", (manual_id,))

    def ci_failure(self, ref: str) -> str | None:
        """When the latest CI failure seen on a pull request happened."""
        row = self.db.execute(
            "SELECT failed_at FROM ci_failures WHERE ref = ?", (ref,)
        ).fetchone()
        return row["failed_at"] if row else None

    def set_ci_failure(self, ref: str, failed_at: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO ci_failures (ref, failed_at) VALUES (?, ?) "
                "ON CONFLICT (ref) DO UPDATE SET failed_at = excluded.failed_at",
                (ref, stamp(failed_at)),
            )

    ######################################################################
    # Labels
    ######################################################################

    def labels(self) -> list[Label]:
        """Every label, in sidebar order."""
        return [
            self._label(row)
            for row in self.db.execute("SELECT * FROM labels ORDER BY position, id")
        ]

    @staticmethod
    def _label(row: sqlite3.Row) -> Label:
        return Label(**{**dict(row), "flag": bool(row["flag"])})

    def add_default_labels(self) -> None:
        """Create `DEFAULT_LABELS`, once. Deleted defaults don't come back."""
        if self.get_meta("default_labels") is not None:
            return
        existing = {label.name.casefold() for label in self.labels()}
        for name, color, flag in DEFAULT_LABELS:
            if name.casefold() not in existing:
                self.create_label(name, color, flag)
        self.set_meta("default_labels", "added")

    def create_label(
        self, name: str, color: str | None = None, flag: bool = False
    ) -> Label:
        name = self._check_name(name)
        count, last = self.db.execute(
            "SELECT COUNT(*), COALESCE(MAX(position), 0) FROM labels"
        ).fetchone()
        if color is None:
            color = LABEL_COLORS[count % len(LABEL_COLORS)]
        try:
            with self.db:
                cursor = self.db.execute(
                    "INSERT INTO labels (name, color, flag, position) "
                    "VALUES (?, ?, ?, ?)",
                    (name, color, flag, last + 1),
                )
        except sqlite3.IntegrityError:
            raise ValueError(f"There's already a label called {name!r}.") from None
        return Label(cursor.lastrowid, name, color, flag, last + 1)

    def edit_label(self, label_id: int, name: str, flag: bool) -> Label:
        """Rename a label, or change whether activity flags its threads. That
        only affects activity from then on."""
        name = self._check_name(name)
        try:
            with self.db:
                self.db.execute(
                    "UPDATE labels SET name = ?, flag = ? WHERE id = ?",
                    (name, flag, label_id),
                )
        except sqlite3.IntegrityError:
            raise ValueError(f"There's already a label called {name!r}.") from None
        row = self.db.execute("SELECT * FROM labels WHERE id = ?", (label_id,))
        return self._label(row.fetchone())

    def reorder_labels(self, order: Iterable[int]) -> None:
        """Put the labels in this order, which changes their number keys. Any
        not given keep their order, after the ones given."""
        current = [label.id for label in self.labels()]
        given = [label_id for label_id in dict.fromkeys(order) if label_id in current]
        rest = [label_id for label_id in current if label_id not in given]
        with self.db:
            self.db.executemany(
                "UPDATE labels SET position = ? WHERE id = ?",
                list(enumerate(given + rest, 1)),
            )

    def delete_label(self, label_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM labels WHERE id = ?", (label_id,))

    def labels_by_thread(self) -> dict[str, list[Label]]:
        """Every thread's labels, in label order. Unlabelled threads are omitted."""
        result: dict[str, list[Label]] = {}
        for row in self.db.execute(
            """
            SELECT tl.thread_id, l.*
            FROM thread_labels tl JOIN labels l ON l.id = tl.label_id
            ORDER BY l.position, l.id
            """
        ):
            result.setdefault(row["thread_id"], []).append(
                Label(
                    row["id"],
                    row["name"],
                    row["color"],
                    bool(row["flag"]),
                    row["position"],
                )
            )
        return result

    def set_label(self, label_id: int, thread_ids: Iterable[str], on: bool) -> None:
        """Add the label to the threads (``on=True``) or remove it from them.

        Labelling a thread counts as seeing it, and brings it back from done. A
        thread that already has the label keeps its place in the label.
        """
        thread_ids = list(thread_ids)
        with self.db:
            if on:
                self.db.executemany(
                    "INSERT OR IGNORE INTO thread_labels (label_id, thread_id, added_at) "
                    "VALUES (?, ?, ?)",
                    [(label_id, i, now()) for i in thread_ids],
                )
                self.db.executemany(
                    "UPDATE threads SET seen_at = ?, unread = 0, done_mark = NULL "
                    "WHERE id = ?",
                    [(now(), i) for i in thread_ids],
                )
            else:
                self.db.executemany(
                    "DELETE FROM thread_labels WHERE label_id = ? AND thread_id = ?",
                    [(label_id, i) for i in thread_ids],
                )

    @staticmethod
    def _check_name(name: str) -> str:
        name = " ".join(name.split())
        if not name:
            raise ValueError("A label needs a name.")
        return name

    ######################################################################
    # Settings and sync state
    ######################################################################

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,))
        found = row.fetchone()
        return found["value"] if found else None

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
