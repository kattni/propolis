from datetime import UTC, datetime

import pytest

from propolis.core.remote import NotPaired, Remote, Server
from propolis.core.store import Store
from propolis.core.threads import ThreadInfo
from propolis.core.triage import Triage

pytestmark = pytest.mark.anyio


async def refresh():
    pass


@pytest.fixture
async def shared(tmp_path, github):
    """A store with one thread in the Inbox, shared with code "secret"."""
    with Store(tmp_path / "propolis.db") as store:
        store.save_info(
            ThreadInfo(
                key="beeware/toga#1",
                kind="Issue",
                repo="beeware/toga",
                title="Thread 1",
                url="https://github.com/beeware/toga/issues/1",
                number=1,
                notification_id="91",
            )
        )
        store.record_activity("beeware/toga#1", datetime(2026, 11, 3, tzinfo=UTC))
        server = Server(store, Triage(store, github), "secret", refresh)
        await server.start(port=0, host="127.0.0.1")
        yield store, f"http://127.0.0.1:{server.port}"
        await server.close()


async def test_another_mac_triages_through_this_one(shared, fake_github):
    store, url = shared
    fake_github.routes[("DELETE", "/notifications/threads/91")] = {}
    remote = Remote(url, "secret")

    await remote.fetch()
    assert remote.state.inbox() == ["beeware/toga#1"]
    assert remote.info("beeware/toga#1").title == "Thread 1"

    await remote.done("beeware/toga#1")
    await remote.aclose()

    assert store.state.threads["beeware/toga#1"].done
    assert ("DELETE", "/notifications/threads/91") in fake_github.requests
    assert remote.state.inbox() == []


async def test_another_mac_needs_the_code(shared):
    store, url = shared
    remote = Remote(url, "guess")

    with pytest.raises(NotPaired):
        await remote.done("beeware/toga#1")
    await remote.aclose()

    assert not store.state.threads["beeware/toga#1"].done
