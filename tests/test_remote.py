import asyncio
from dataclasses import replace

import pytest

from buzz.remote import HostAPI, NotPaired, RemoteClient, RemoteError, RemoteServer
from buzz.store import INBOX, Mirror, Store, Thread


def thread(id):
    return Thread(
        id=id,
        repo="beeware/toga",
        repo_url="https://github.com/beeware/toga",
        title=f"Thread {id}",
        type="Issue",
        url=f"https://api.github.com/repos/beeware/toga/issues/{id}",
        latest_comment_url=None,
        reason="mention",
        unread=True,
        updated_at="2026-11-01T10:00:00Z",
    )


class FakeGitHub:
    def __init__(self, refuse=()):
        self.calls, self.refuse = [], set(refuse)

    async def mark_read(self, thread_id):
        self.calls.append(("read", thread_id))

    async def subscribe(self, thread_id):
        self.calls.append(("subscribe", thread_id))

    async def mark_done(self, thread_id):
        if thread_id in self.refuse:
            raise RuntimeError("nope")
        self.calls.append(("done", thread_id))

    async def lookup(self, repo, number):
        return replace(thread("x"), id=f"manual:{repo}#{number}", repo=repo)


@pytest.fixture(autouse=True)
def no_slow_refusals(monkeypatch):
    monkeypatch.setattr("buzz.remote.time.sleep", lambda seconds: None)


async def serve(host: Store, github, code="s3cret"):
    """Share the host's inbox on a free local port; return a client for it."""
    changes = []
    api = HostAPI(host, lambda: github, changes.append, lambda: None)
    server = RemoteServer(api, code, asyncio.get_running_loop(), port=0)
    server.start()
    client = RemoteClient(f"http://127.0.0.1:{server.port}", "s3cret")
    return server, client, changes


def test_triaging_through_another_mac():
    host = Store(":memory:")
    host.place([thread("1"), thread("2")])
    label = host.create_label("Waiting", flag=True)
    github = FakeGitHub(refuse={"2"})

    async def go():
        server, client, changes = await serve(host, github)
        try:
            # The other Mac sees the host's inbox, and only re-fetches changes.
            mirror = Mirror()
            snapshot, etag = await client.state()
            mirror.load(snapshot)
            assert [t.id for t in mirror.threads(INBOX)] == ["1", "2"]
            assert mirror.labels() == host.labels()
            assert await client.state(etag) == (None, etag)

            await client.action("label", label=label.id, threads=["1"])
            assert [t.id for t in host.threads(label.id)] == ["1"]
            assert (await client.state(etag))[0] is not None
            await client.action("define-label", name="Later")
            await client.action("add", reference="beeware/toga#77", label=label.id)
            assert "manual:beeware/toga#77" in [t.id for t in host.threads(label.id)]
            assert [label.name for label in host.labels()][-2:] == ["Waiting", "Later"]

            # GitHub changes are made by the host, with its own client.
            # Labelling marks a thread as read, and a flagging label subscribes.
            assert github.calls == [("read", "1"), ("subscribe", "1")]
            await client.mark_done("1")
            assert github.calls[-1] == ("done", "1") and host.get("1").done
            # GitHub's side is best effort: the change stands if it fails.
            await client.mark_done("2")
            assert host.get("2").done

            with pytest.raises(RemoteError, match="No such action"):
                await client.action("delete-everything")
        finally:
            await client.aclose()
            server.stop()
        return changes

    changes = asyncio.run(go())
    assert changes == [
        "moved 1 notification to Waiting",
        "added the label Later",
        "added beeware/toga to Waiting",
        "marked 1 notification as done",
        "marked 1 notification as done",
    ]


def test_a_wrong_code_is_refused():
    async def go():
        server, client, _ = await serve(Store(":memory:"), None, code="other")
        try:
            with pytest.raises(NotPaired, match="doesn't accept the code"):
                await client.state()
        finally:
            await client.aclose()
            server.stop()

    asyncio.run(go())


def test_stopping_turns_away_open_connections():
    host = Store(":memory:")

    async def go():
        server, client, _ = await serve(host, None)
        try:
            await client.state()  # Leaves a connection open.
            server.stop()
            with pytest.raises(RemoteError, match="stopped sharing"):
                await client.state()
        finally:
            await client.aclose()

    asyncio.run(go())
    host.close()
