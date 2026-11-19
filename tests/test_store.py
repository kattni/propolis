from dataclasses import replace

import pytest

from buzz.store import DONE, INBOX, UNREAD, Store, Thread


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


@pytest.fixture
def store():
    store = Store(":memory:")
    yield store
    store.close()


def ids(threads):
    return [t.id for t in threads]


def test_views(store):
    store.sync_inbox(
        [
            thread("1", "2026-11-01T10:00:00Z", unread=False),
            thread("2", "2026-11-03T10:00:00Z"),
            thread("3", "2026-11-02T10:00:00Z"),
        ],
        complete=True,
    )
    store.mark_done(["3"])

    assert ids(store.threads(INBOX)) == ["2", "1"]
    assert ids(store.threads(UNREAD)) == ["2"]
    assert ids(store.threads(DONE)) == ["3"]
    assert store.counts() == {INBOX: 2, UNREAD: 1, DONE: 1}


def test_search(store):
    store.sync_inbox(
        [thread("1", title="Fix the Table"), thread("2", title="Docs")], True
    )
    assert ids(store.threads(INBOX, "table")) == ["1"]
    assert ids(store.threads(INBOX, "TOGA")) == ["1", "2"]  # Matches the repo.


def test_done_elsewhere_and_resurfacing(store):
    store.sync_inbox([thread("1"), thread("2")], complete=True)

    # Thread 1 was marked as done on github.com, so it's no longer in the inbox.
    store.sync_inbox([thread("2")], complete=True)
    assert ids(store.threads(DONE)) == ["1"]

    # An incomplete inbox doesn't prove anything is done.
    store.sync_inbox([], complete=False)
    assert ids(store.threads(INBOX)) == ["2"]

    # New activity brings it back.
    store.sync_inbox([thread("1", "2026-11-05T00:00:00Z"), thread("2")], True)
    assert ids(store.threads(INBOX)) == ["1", "2"]


def test_done_request_in_flight_stays_done(store):
    store.sync_inbox([thread("1")], complete=True)
    store.mark_done(["1"])

    # GitHub hasn't processed the request yet, so the thread is still there,
    # but with no new activity.
    store.sync_inbox([thread("1")], complete=True)
    assert ids(store.threads(DONE)) == ["1"]


def test_restore(store):
    store.sync_inbox([thread("1")], complete=True)
    before = store.get("1")
    store.mark_done(["1"])
    store.restore([before])
    assert store.get("1") == before


def test_labels(store):
    store.sync_inbox([thread("1"), thread("2")], complete=True)
    later = store.create_label("Later")
    review = store.create_label("  needs   review ")
    assert review.name == "needs review"
    assert later.color != review.color

    store.set_label(later.id, ["1", "2"], on=True)
    store.set_label(review.id, ["1"], on=True)
    store.set_label(later.id, ["2"], on=False)
    assert store.labels_by_thread() == {"1": [later, review]}

    # Labelled threads stay in the label's view after they're done.
    store.mark_done(["1"])
    assert ids(store.threads(later.id)) == ["1"]
    assert store.counts()[later.id] == 1

    renamed = store.rename_label(later.id, "Someday")
    assert store.labels() == [review, renamed]

    store.delete_label(review.id)
    assert store.labels_by_thread() == {"1": [renamed]}


def test_label_names_must_be_unique_and_non_empty(store):
    store.create_label("Later")
    with pytest.raises(ValueError, match="already a label"):
        store.create_label("later")
    with pytest.raises(ValueError, match="needs a name"):
        store.create_label("   ")


def test_threads_update_from_github(store):
    store.sync_inbox([thread("1")], complete=True)
    store.sync_inbox(
        [replace(thread("1", "2026-11-02T00:00:00Z"), title="Renamed")], True
    )
    assert store.get("1").title == "Renamed"
