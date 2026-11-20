import sqlite3
from dataclasses import replace

import pytest

from propolis.state import DEFAULT_LABELS
from propolis.store import ATTENTION, DONE, INBOX, UNREAD, Store, Thread


def thread(id, updated_at="2026-11-01T10:00:00Z", unread=True, title="A thread"):
    return Thread(
        id=id,
        repo="beeware/toga",
        repo_url="https://github.com/beeware/toga",
        title=title,
        type="Issue",
        url=f"https://api.github.com/repos/beeware/toga/issues/{id}",
        latest_comment_url=None,
        reason="mention",
        unread=unread,
        updated_at=updated_at,
    )


class Clock:
    """The store's clock, stopped at a time a test can change."""

    def __init__(self):
        self.time = "2026-11-10T00:00:00.000000Z"

    def __call__(self):
        return self.time


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr("propolis.store.now", clock)
    return clock


@pytest.fixture
def store():
    store = Store(":memory:")
    yield store
    store.close()


def ids(threads):
    return [t.id for t in threads]


def test_views(store):
    store.place(
        [
            thread("1", "2026-11-01T10:00:00Z", unread=False),
            thread("2", "2026-11-03T10:00:00Z"),
            thread("3", "2026-11-02T10:00:00Z"),
        ]
    )
    store.mark_done(["3"])

    assert ids(store.threads(INBOX)) == ["2", "1"]
    assert ids(store.threads(UNREAD)) == ["2"]
    assert ids(store.threads(DONE)) == ["3"]
    assert store.counts() == {INBOX: 2, ATTENTION: 0, UNREAD: 1, DONE: 1}
    assert store.new_counts() == {INBOX: 1, ATTENTION: 0}


def test_search(store):
    store.place([thread("1", title="Fix the Table"), thread("2", title="Docs")])
    assert ids(store.threads(INBOX, "table")) == ["1"]
    assert ids(store.threads(INBOX, "TOGA")) == ["1", "2"]  # Matches the repo.


def test_filters(store):
    pull = replace(thread("2"), type="PullRequest", reason="review_requested")
    store.place([thread("1"), pull])
    assert ids(store.threads(INBOX, filters={"type": "PullRequest"})) == ["2"]
    assert ids(store.threads(INBOX, filters={"reason": "mention"})) == ["1"]
    assert ids(store.threads(INBOX, filters={"type": "Issue", "reason": "x"})) == []


def test_filter_counts(store):
    pull = replace(thread("2"), type="PullRequest", reason="review_requested")
    store.place([thread("1"), pull, replace(thread("3"), type="PullRequest")])
    counts = store.filter_counts(INBOX, filters={"type": "PullRequest"})
    # A column's own filter doesn't narrow its counts; the others' do.
    assert counts["type"] == {"Issue": 1, "PullRequest": 2}
    assert counts["reason"] == {"review_requested": 1, "mention": 1}


def test_states_are_looked_up_again_after_activity(store):
    store.place([thread("1")])
    assert ids(store.threads_to_look_up(10)) == ["1"]
    store.set_states([("1", "open", "2026-11-01T10:00:00Z")])
    assert store.threads_to_look_up(10) == []

    store.update_details([thread("1", "2026-11-02T10:00:00Z")])
    assert store.get("1").state == "open"  # Kept until it's looked up again.
    assert ids(store.threads_to_look_up(10)) == ["1"]


def test_inbox_goes_by_activity_that_needs_a_response(store):
    store.place(
        [thread("1", "2026-11-01T10:00:00Z"), thread("2", "2026-11-02T10:00:00Z")]
    )

    # Your own comment updates a thread on GitHub, but doesn't move it up.
    store.update_details([thread("1", "2026-11-03T10:00:00Z")])
    assert ids(store.threads(INBOX)) == ["2", "1"]

    store.record_activity("1", "2026-11-03T10:00:00Z")
    assert ids(store.threads(INBOX)) == ["1", "2"]


def test_done_elsewhere(store):
    store.place([thread("1"), thread("2"), thread("3")])
    label = store.create_label("Later")
    store.set_label(label.id, ["3"], on=True)

    # Threads 1 and 3 aren't in GitHub's inbox any more, so they were marked as
    # done on github.com. Thread 3 has a label, so it's Propolis's to triage.
    store.mark_gone(["2"])
    assert ids(store.threads(DONE)) == ["1"]
    assert ids(store.threads(label.id)) == ["3"]


def test_done_takes_labels_off_until_there_is_new_activity(store, clock):
    store.place([thread("1")])
    label = store.create_label("Later")
    store.set_label(label.id, ["1"], on=True)
    store.mark_done(["1"])
    assert ids(store.threads(DONE)) == ["1"]
    assert store.labels_by_thread() == {}

    store.record_activity("1", "2026-11-09T00:00:00Z")  # From before it was done.
    assert ids(store.threads(DONE)) == ["1"]
    store.record_activity("1", "2026-11-11T00:00:00Z")
    assert ids(store.threads(INBOX)) == ["1"]


def test_done_only_goes_back_seven_days(store, clock):
    store.place([thread("1"), thread("2"), thread("3")])
    clock.time = "2026-11-02T00:00:00.000000Z"
    store.mark_done(["1"])
    clock.time = "2026-11-08T00:00:00.000000Z"
    store.mark_done(["2"])
    clock.time = "2026-11-10T09:00:00.000000Z"
    store.mark_done(["3"])

    assert sorted(ids(store.threads(DONE))) == ["2", "3"]
    assert store.counts()[DONE] == 2
    assert ids(store.threads(DONE, done_since="2026-11-10T00:00:00.000000Z")) == ["3"]


def test_only_flagging_labels_flag_activity(store, clock):
    store.place([thread("1"), thread("2")])
    blocked = store.create_label("Waiting", flag=True)
    later = store.create_label("Later")
    store.set_label(blocked.id, ["1"], on=True)
    store.set_label(later.id, ["2"], on=True)

    # Activity from before it was labelled doesn't flag it, even if Propolis
    # only notices it afterwards.
    store.record_activity("1", "2026-11-09T00:00:00Z")
    assert store.threads(ATTENTION) == []

    for thread_id in ("1", "2"):
        store.record_activity(thread_id, "2026-11-11T00:00:00Z")
    assert ids(store.threads(ATTENTION)) == ["1"]
    assert ids(store.threads(blocked.id)) == ["1"]  # It stays in its label too.
    # A label that doesn't flag shows that there's something new instead.
    assert store.get("2").new and not store.get("2").attention

    # Seeing it clears what's new, but not Attention required.
    clock.time = "2026-11-12T00:00:00.000000Z"
    store.mark_seen(["1"])
    assert not store.get("1").new and store.get("1").attention
    store.set_attention(["1"], on=False)
    assert store.threads(ATTENTION) == []


def test_labels(store):
    store.place([thread("1"), thread("2")])
    later = store.create_label("Later")
    review = store.create_label("  needs   a   reply ")
    assert review.name == "needs a reply"
    assert later.color != review.color

    store.set_label(later.id, ["1", "2"], on=True)
    store.set_label(review.id, ["1"], on=True)
    store.set_label(later.id, ["2"], on=False)
    assert store.labels_by_thread() == {"1": [later, review]}

    edited = store.edit_label(later.id, "Someday", flag=True)
    assert edited.flag
    assert store.labels()[-2:] == [edited, review]

    store.delete_label(review.id)
    assert store.labels_by_thread() == {"1": [edited]}


def test_reordering_labels(store):
    a, _, c = (store.create_label(name) for name in "ABC")
    store.reorder_labels([c.id, a.id])
    names = [label.name for label in store.labels()]
    assert names[:2] == ["C", "A"] and names[-1] == "B"


def test_labelled_threads_leave_the_inbox_for_good(store):
    store.place([thread("1"), thread("2"), thread("3")])
    label = store.create_label("Later")
    store.set_label(label.id, ["2"], on=True)
    store.set_label(label.id, ["1"], on=True)
    assert ids(store.threads(INBOX)) == ["3"]

    # New activity doesn't bring thread 1 back, or move it up the label.
    store.record_activity("1", "2099-01-01T00:00:00Z")
    assert ids(store.threads(INBOX)) == ["3"]
    assert ids(store.threads(label.id)) == ["2", "1"]


def test_default_labels(tmp_path):
    store = Store(tmp_path / "propolis.sqlite3")
    labels = store.labels()
    assert [label.name for label in labels] == [n for _, n, _, _ in DEFAULT_LABELS]
    assert [label.name for label in labels if label.flag] == [
        "Blocked",
        "High priority",
    ]

    # Deleted defaults don't come back.
    store.delete_label(labels[0].id)
    store.close()
    store = Store(tmp_path / "propolis.sqlite3")
    assert len(store.labels()) == len(DEFAULT_LABELS) - 1
    store.close()


def test_reopening_replays_the_same_triage(tmp_path, clock):
    store = Store(tmp_path / "propolis.sqlite3")
    store.place([thread("1")])
    waiting = store.create_label("Waiting", flag=True)
    store.set_label(waiting.id, ["1"], on=True)
    store.record_activity("1", "2026-11-11T00:00:00Z")
    # Dealt with after the activity that flagged it.
    clock.time = "2026-11-12T00:00:00.000000Z"
    store.set_attention(["1"], on=False)
    store.close()

    store = Store(tmp_path / "propolis.sqlite3")
    assert ids(store.threads(waiting.id)) == ["1"]
    assert store.threads(ATTENTION) == []
    store.close()


def test_old_stores_are_upgraded(tmp_path):
    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE threads (id TEXT PRIMARY KEY, repo TEXT NOT NULL,
            repo_url TEXT NOT NULL, title TEXT NOT NULL, type TEXT NOT NULL,
            url TEXT, latest_comment_url TEXT, reason TEXT NOT NULL,
            unread INTEGER NOT NULL, updated_at TEXT NOT NULL, done_mark TEXT);
        CREATE TABLE labels (id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE, color TEXT NOT NULL);
        CREATE TABLE thread_labels (thread_id TEXT NOT NULL, label_id INTEGER
            NOT NULL, PRIMARY KEY (thread_id, label_id));
        INSERT INTO threads VALUES ('1', 'beeware/toga', 'https://github.com',
            'A', 'Issue', NULL, NULL, 'mention', 1, '2026-11-01T10:00:00Z', NULL);
        INSERT INTO threads VALUES ('2', 'beeware/toga', 'https://github.com',
            'B', 'Issue', NULL, NULL, 'mention', 0, '2026-11-01T10:00:00Z',
            '2026-11-01T10:00:00Z');
        INSERT INTO labels (name, color) VALUES ('Later', '🔵'), ('blocked', '🔴');
        INSERT INTO thread_labels VALUES ('2', 1);
        """
    )
    old.close()

    # Upgrading it turns its tables into events, which opening it again
    # replays.
    Store(path).close()
    store = Store(path)
    # What was unread on GitHub has something new.
    assert store.get("1").new and not store.get("2").new
    # Done takes labels off now, so a labelled thread that was done isn't.
    later = store.labels()[0]
    assert ids(store.threads(later.id)) == ["2"] and not store.get("2").done
    assert [(label.name, label.flag) for label in store.labels()][:2] == [
        ("Later", False),
        ("blocked", True),
    ]
    store.close()


def test_label_names_must_be_unique_and_non_empty(store):
    store.create_label("Later")
    with pytest.raises(ValueError, match="already a label"):
        store.create_label("later")
    with pytest.raises(ValueError, match="needs a name"):
        store.create_label("   ")


def test_threads_update_from_github(store):
    store.place([thread("1")])
    store.update_details(
        [replace(thread("1", "2026-11-02T00:00:00Z"), title="Renamed")]
    )
    assert store.get("1").title == "Renamed"


def manual(number, updated_at="2026-11-01T10:00:00Z"):
    return replace(
        thread(f"manual:beeware/toga#{number}", updated_at, unread=False),
        url=f"https://api.github.com/repos/beeware/toga/issues/{number}",
        reason="added",
    )


def test_threads_added_by_hand(store):
    label = store.create_label("Later")
    store.set_label(label.id, [store.add_manual(manual(5))], on=True)
    store.add_manual(manual(6))
    assert store.add_manual(manual(6)) == "manual:beeware/toga#6"  # No duplicate.

    # They're not in GitHub's inbox, but that doesn't make them done.
    store.mark_gone([])
    assert ids(store.threads(INBOX)) == ["manual:beeware/toga#6"]

    # A notification for the same issue takes its place, label and all.
    store.set_attention(["manual:beeware/toga#5"], on=True)
    store.adopt("manual:beeware/toga#5", thread("5", "2026-11-05T00:00:00Z"))
    assert ids(store.threads(label.id)) == ids(store.threads(ATTENTION)) == ["5"]
    assert store.get("manual:beeware/toga#5") is None


def test_activity_on_threads_added_by_hand(store, clock):
    store.add_manual(manual(1))
    store.mark_done(["manual:beeware/toga#1"])

    # Activity that needs a response brings it back, with something new.
    store.update_manual(manual(1, "2026-11-12T00:00:00Z"), "2026-11-12T00:00:00Z")
    one = store.get("manual:beeware/toga#1")
    assert one.new and not one.done
