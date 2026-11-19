import asyncio

import pytest

from buzz.github import CIFailure, InboxSnapshot, api_url
from buzz.store import INBOX, Store, Thread
from buzz.sync import Sync, WrongSignIn


def notification(id, type="Issue", url=None, title="A thread"):
    return Thread(
        id=id,
        repo="beeware/toga",
        repo_url="https://github.com/beeware/toga",
        title=title,
        type=type,
        url=url,
        latest_comment_url=None,
        reason="mention",
        unread=True,
        updated_at="2026-11-01T10:00:00Z",
    )


class FakeGitHub:
    def __init__(self, threads=(), login="me", discussions=None, failures=()):
        self.threads, self.login, self.failures = list(threads), login, failures
        self.discussions = discussions or {}
        self.inbox_calls, self.searches = [], []

    async def validate(self):
        return self.login

    async def inbox(self, last_modified=None, since=None):
        self.inbox_calls.append((last_modified, since))
        return InboxSnapshot(self.threads, "Last-Modified", 60)

    async def latest_activity(self, thread, since, me):
        return None

    async def find_discussion(self, repo, title):
        self.searches.append(title)
        return self.discussions.get(title)

    async def ci_failures(self):
        return self.failures

    async def states(self, threads):
        return {}


@pytest.fixture
def store():
    store = Store(":memory:")
    yield store
    store.close()


def test_only_the_first_sync_reads_the_whole_inbox(store):
    github = FakeGitHub([notification("1")])
    sync = Sync(store, github)
    asyncio.run(sync.run_once())
    asyncio.run(sync.run_once())
    assert github.inbox_calls == [
        (None, None),
        ("Last-Modified", "2026-11-01T10:00:00Z"),
    ]
    assert [t.id for t in store.threads(INBOX)] == ["1"]


def test_discussions_are_found_by_title_once(store):
    github = FakeGitHub(
        [notification("1", "Discussion", title="Roadmap")], discussions={"Roadmap": 9}
    )
    sync = Sync(store, github)
    asyncio.run(sync.run_once())
    asyncio.run(sync.run_once())
    assert store.get("1").url == api_url("beeware/toga", "discussions", 9)
    assert github.searches == ["Roadmap"]


def test_failing_ci_counts_once(store):
    failure = CIFailure(
        "beeware/toga",
        7,
        "Faster tables",
        "2026-11-01T00:00:00Z",
        "2026-11-02T00:00:00Z",
    )
    sync = Sync(store, FakeGitHub(failures=[failure]))
    asyncio.run(sync.run_once())

    # A pull request Buzz didn't have goes in the inbox, with something new.
    [pull] = store.threads(INBOX)
    assert (pull.id, pull.reason, pull.new) == (
        "manual:beeware/toga#7",
        "ci_failure",
        True,
    )

    store.mark_seen([pull.id])
    asyncio.run(sync.run_once())
    assert not store.get(pull.id).new


def test_a_store_is_for_one_account(store):
    store.set_meta("login", "alice")
    sync = Sync(store, FakeGitHub(login="bob"))
    with pytest.raises(WrongSignIn, match="alice's, but GitHub signed in as bob"):
        asyncio.run(sync.run_once())
