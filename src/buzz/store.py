"""Local storage for notification threads and labels, backed by SQLite.

GitHub stays the source of truth for which threads are in the inbox and whether
they're unread. Labels only exist locally: GitHub has no API for organising
notifications, so the store keeps them, along with a copy of every thread it
has seen, so a labelled thread stays visible after it has been marked as done.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

#: Coloured markers used to tell labels apart, assigned in rotation.
LABEL_COLORS = ("🔵", "🟢", "🟣", "🟠", "🔴", "🟡", "🟤", "⚫")

# The built-in views. A label's view is identified by the label's integer ID.
INBOX = "inbox"
UNREAD = "unread"
DONE = "done"

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
    unread INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    -- NULL while the thread is in the inbox. Once it's done, this holds the
    -- thread's updated_at at that moment, so later activity can bring it back.
    done_mark TEXT
);
CREATE TABLE IF NOT EXISTS labels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    color TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS thread_labels (
    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    label_id INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
    PRIMARY KEY (thread_id, label_id)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


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
    unread: bool
    updated_at: str
    done_mark: str | None = None

    @property
    def done(self) -> bool:
        return self.done_mark is not None


@dataclass(frozen=True)
class Label:
    id: int
    name: str
    color: str


class Store:
    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    ######################################################################
    # Threads
    ######################################################################

    def sync_inbox(self, threads: Iterable[Thread], complete: bool) -> None:
        """Merge GitHub's current inbox into the store.

        :param threads: Every thread GitHub returned for the inbox.
        :param complete: Whether ``threads`` is the whole inbox. If it is, any
            thread missing from it was marked as done somewhere else (such as
            on github.com), so it's marked as done here too.
        """
        threads = list(threads)
        with self.db:
            self.db.executemany(
                """
                INSERT INTO threads (
                    id, repo, repo_url, title, type, url, latest_comment_url,
                    reason, unread, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    repo = excluded.repo,
                    repo_url = excluded.repo_url,
                    title = excluded.title,
                    type = excluded.type,
                    url = excluded.url,
                    latest_comment_url = excluded.latest_comment_url,
                    reason = excluded.reason,
                    unread = excluded.unread,
                    updated_at = excluded.updated_at,
                    -- New activity since the thread was marked as done brings
                    -- it back. Without new activity, a thread that's still in
                    -- GitHub's inbox is one whose "done" request is in flight.
                    done_mark = CASE
                        WHEN excluded.updated_at > threads.done_mark THEN NULL
                        ELSE threads.done_mark
                    END
                """,
                [
                    (
                        t.id,
                        t.repo,
                        t.repo_url,
                        t.title,
                        t.type,
                        t.url,
                        t.latest_comment_url,
                        t.reason,
                        t.unread,
                        t.updated_at,
                    )
                    for t in threads
                ],
            )
            if complete:
                seen = {t.id for t in threads}
                gone = [
                    row["id"]
                    for row in self.db.execute(
                        "SELECT id FROM threads WHERE done_mark IS NULL"
                    )
                    if row["id"] not in seen
                ]
                self.db.executemany(
                    "UPDATE threads SET done_mark = updated_at, unread = 0 "
                    "WHERE id = ?",
                    [(thread_id,) for thread_id in gone],
                )

    def threads(self, view: str | int, search: str = "") -> list[Thread]:
        """The threads in a view, most recently updated first.

        :param view: `INBOX`, `UNREAD`, `DONE`, or a label ID.
        :param search: Only include threads whose title or repository contains
            this text (case-insensitive).
        """
        if view == INBOX:
            where, params = "done_mark IS NULL", []
        elif view == UNREAD:
            where, params = "done_mark IS NULL AND unread = 1", []
        elif view == DONE:
            where, params = "done_mark IS NOT NULL", []
        else:
            where = "id IN (SELECT thread_id FROM thread_labels WHERE label_id = ?)"
            params = [view]

        if search := search.strip().lower():
            where += " AND (instr(lower(title), ?) > 0 OR instr(lower(repo), ?) > 0)"
            params += [search, search]

        rows = self.db.execute(
            f"SELECT * FROM threads WHERE {where} ORDER BY updated_at DESC", params
        )
        return [self._thread(row) for row in rows]

    def get(self, thread_id: str) -> Thread | None:
        row = self.db.execute(
            "SELECT * FROM threads WHERE id = ?", (thread_id,)
        ).fetchone()
        return self._thread(row) if row else None

    def counts(self) -> dict[str | int, int]:
        """The number of threads in each view, keyed like `threads()`'s ``view``."""
        inbox, unread, done = self.db.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE done_mark IS NULL),
                COUNT(*) FILTER (WHERE done_mark IS NULL AND unread = 1),
                COUNT(*) FILTER (WHERE done_mark IS NOT NULL)
            FROM threads
            """
        ).fetchone()
        counts: dict[str | int, int] = {INBOX: inbox, UNREAD: unread, DONE: done}
        for row in self.db.execute(
            "SELECT label_id, COUNT(*) AS n FROM thread_labels GROUP BY label_id"
        ):
            counts[row["label_id"]] = row["n"]
        return counts

    def mark_read(self, thread_ids: Iterable[str]) -> None:
        with self.db:
            self.db.executemany(
                "UPDATE threads SET unread = 0 WHERE id = ?",
                [(i,) for i in thread_ids],
            )

    def mark_done(self, thread_ids: Iterable[str]) -> None:
        with self.db:
            self.db.executemany(
                "UPDATE threads SET done_mark = updated_at, unread = 0 WHERE id = ?",
                [(i,) for i in thread_ids],
            )

    def restore(self, threads: Iterable[Thread]) -> None:
        """Put back the read and done state of threads, such as after a failed
        request to GitHub."""
        with self.db:
            self.db.executemany(
                "UPDATE threads SET unread = ?, done_mark = ? WHERE id = ?",
                [(t.unread, t.done_mark, t.id) for t in threads],
            )

    def forget_threads(self) -> None:
        """Remove every thread (and their labels), such as when switching to a
        different GitHub account. The labels themselves are kept."""
        with self.db:
            self.db.execute("DELETE FROM threads")

    @staticmethod
    def _thread(row: sqlite3.Row) -> Thread:
        return Thread(**{**dict(row), "unread": bool(row["unread"])})

    ######################################################################
    # Labels
    ######################################################################

    def labels(self) -> list[Label]:
        rows = self.db.execute("SELECT * FROM labels ORDER BY name COLLATE NOCASE")
        return [Label(**row) for row in rows]

    def create_label(self, name: str) -> Label:
        name = self._check_name(name)
        (count,) = self.db.execute("SELECT COUNT(*) FROM labels").fetchone()
        color = LABEL_COLORS[count % len(LABEL_COLORS)]
        try:
            with self.db:
                cursor = self.db.execute(
                    "INSERT INTO labels (name, color) VALUES (?, ?)", (name, color)
                )
        except sqlite3.IntegrityError:
            raise ValueError(f"There's already a label called {name!r}.") from None
        return Label(cursor.lastrowid, name, color)

    def rename_label(self, label_id: int, name: str) -> Label:
        name = self._check_name(name)
        try:
            with self.db:
                self.db.execute(
                    "UPDATE labels SET name = ? WHERE id = ?", (name, label_id)
                )
        except sqlite3.IntegrityError:
            raise ValueError(f"There's already a label called {name!r}.") from None
        row = self.db.execute("SELECT * FROM labels WHERE id = ?", (label_id,))
        return Label(**row.fetchone())

    def delete_label(self, label_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM labels WHERE id = ?", (label_id,))

    def labels_by_thread(self) -> dict[str, list[Label]]:
        """Every thread's labels, sorted by name. Unlabelled threads are omitted."""
        result: dict[str, list[Label]] = {}
        for row in self.db.execute(
            """
            SELECT tl.thread_id, l.id, l.name, l.color
            FROM thread_labels tl JOIN labels l ON l.id = tl.label_id
            ORDER BY l.name COLLATE NOCASE
            """
        ):
            result.setdefault(row["thread_id"], []).append(
                Label(row["id"], row["name"], row["color"])
            )
        return result

    def set_label(self, label_id: int, thread_ids: Iterable[str], on: bool) -> None:
        """Add the label to the threads (``on=True``) or remove it from them."""
        sql = (
            "INSERT OR IGNORE INTO thread_labels (label_id, thread_id) VALUES (?, ?)"
            if on
            else "DELETE FROM thread_labels WHERE label_id = ? AND thread_id = ?"
        )
        with self.db:
            self.db.executemany(sql, [(label_id, i) for i in thread_ids])

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
