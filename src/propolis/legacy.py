"""Bringing stores from before Propolis kept events up to date.

Those stores changed their tables in place. Their labels, and each thread's
triage state, become events that come before any new ones, so replaying gives
the same views, and Propolis carries on from there.
"""

from __future__ import annotations

import sqlite3
from uuid import uuid4

from .events import (
    Event,
    Imported,
    LabelDefined,
    LabelDeleted,
    LabelsReordered,
    to_row,
)
from .state import ATTENTION, DEFAULT_LABELS

#: When imported events happened: before everything else, so that what Propolis
#: does from now on applies on top of them.
EPOCH = "1970-01-01T00:00:00.000000Z"

# Columns added to the old tables over time, for stores made before them.
_COLUMNS = (
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
# Settings that only the old remote control used.
_OLD_META = ("default_labels", "hosting", "tunnel_process", "remotes")


def is_legacy(db: sqlite3.Connection) -> bool:
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
    return "labels" in tables and "events" not in tables


def upgrade(
    db: sqlite3.Connection, device: str, stamp
) -> tuple[list[Event], list[tuple]]:
    """Take an old store's tables apart, into events and thread details, for
    `save()` to put in the new tables.

    :param stamp: Turns a time into the format events use.
    """
    db.row_factory = sqlite3.Row
    for table, column, declaration in _COLUMNS:
        columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
    (version,) = db.execute("PRAGMA user_version").fetchone()
    if version < 1:
        _upgrade_to_1(db, stamp)

    def time(value: str | None) -> str | None:
        try:
            return stamp(value) if value else None
        except ValueError:
            return None

    events: list[Event] = []

    def add(kind, **values):
        # Imported events all happen at EPOCH on this device, so their IDs put
        # them in order.
        events.append(
            kind(id=f"import:{len(events):08d}", at=EPOCH, device=device, **values)
        )

    # Labels whose names match a default label's become that label.
    defaults = {name.casefold(): label_id for label_id, name, _, _ in DEFAULT_LABELS}
    ids = {}
    for row in db.execute("SELECT * FROM labels ORDER BY position, id"):
        ids[row["id"]] = defaults.pop(row["name"].casefold(), None) or uuid4().hex
        add(
            LabelDefined,
            label_id=ids[row["id"]],
            name=row["name"],
            color=row["color"],
            flag_on_activity=bool(row["flag"]),
        )
    if db.execute("SELECT 1 FROM meta WHERE key = 'default_labels'").fetchone():
        # The default labels were added once, so any left were deleted.
        for label_id in defaults.values():
            add(LabelDeleted, label_id=label_id)
    add(LabelsReordered, order=list(ids.values()))

    labels: dict[str, dict[str, str]] = {}
    for row in db.execute("SELECT rowid, * FROM thread_labels ORDER BY rowid"):
        # Labels given before Propolis kept the time come first, in order.
        if (label_id := ids.get(row["label_id"])) is None:
            continue
        given = time(row["added_at"]) or f"1970-01-01T00:00:00.{row['rowid']:06d}Z"
        labels.setdefault(row["thread_id"], {})[label_id] = given

    details = []
    for row in db.execute("SELECT * FROM threads"):
        details.append(tuple(row[column] for column in _DETAILS))
        thread_labels = labels.get(row["id"], {})
        if attention := time(row["attention_at"]):
            thread_labels[ATTENTION] = attention
        state = (
            time(row["last_activity"]),
            time(row["seen_at"]),
            time(row["done_mark"]),
        )
        if thread_labels or any(state):
            add(
                Imported,
                thread_id=row["id"],
                labels=thread_labels,
                last_activity=state[0],
                seen_at=state[1],
                done_at=state[2],
            )

    db.execute("DROP TABLE thread_labels")
    db.execute("DROP TABLE labels")
    db.execute("DROP TABLE IF EXISTS ci_failures")
    db.execute("DROP TABLE IF EXISTS known_commits")
    db.execute("DROP TABLE threads")
    db.executemany("DELETE FROM meta WHERE key = ?", [(key,) for key in _OLD_META])
    db.row_factory = None
    return events, details


def save(db: sqlite3.Connection, events: list[Event], details: list[tuple]) -> None:
    """Save what `upgrade()` made, once the new tables exist."""
    db.executemany(
        f"INSERT INTO threads ({', '.join(_DETAILS)}) "
        f"VALUES ({', '.join('?' * len(_DETAILS))})",
        details,
    )
    db.executemany(
        "INSERT INTO events VALUES (?, ?, ?, ?, ?)", [to_row(e) for e in events]
    )


def _upgrade_to_1(db: sqlite3.Connection, stamp) -> None:
    """The first change to the old format: labels got places in the sidebar, and
    threads times for their activity."""
    labelled = "id IN (SELECT thread_id FROM thread_labels)"
    db.execute("UPDATE labels SET position = id")
    db.execute("UPDATE labels SET flag = 1 WHERE name IN ('Blocked', 'High priority')")
    # Until then, Propolis went by GitHub's unread flag. Start from it.
    rows = db.execute("SELECT id, unread, updated_at, done_mark FROM threads")
    db.executemany(
        "UPDATE threads SET last_activity = ?, seen_at = ?, checked_until = ?, "
        "done_mark = ? WHERE id = ?",
        [
            (
                stamp(row["updated_at"]),
                None if row["unread"] else stamp(row["updated_at"]),
                stamp(row["updated_at"]),
                stamp(row["done_mark"]) if row["done_mark"] else None,
                row["id"],
            )
            for row in rows.fetchall()
        ],
    )
    # Done takes a thread's labels off. Labelled threads that were done stay
    # in their labels, and aren't done any more.
    db.execute(f"UPDATE threads SET done_mark = NULL WHERE {labelled}")
