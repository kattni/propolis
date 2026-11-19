import asyncio
import stat
import sys
from dataclasses import replace

import pytest

from buzz.remote import HostAPI, RemoteClient, RemoteError, RemoteServer, Tunnel
from buzz.store import INBOX, Store, Thread


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


async def serve(host: Store, github, key="s3cret"):
    """Serve the host's inbox on a free local port; return a client for it."""
    changes = []
    api = HostAPI(host, lambda: github, changes.append, lambda: None)
    server = RemoteServer(api, key, asyncio.get_running_loop(), port=0)
    server.start()
    client = RemoteClient(f"http://127.0.0.1:{server.port}", "s3cret")
    return server, client, changes


def test_controlling_a_remote_inbox():
    host = Store(":memory:")
    host.place([thread("1"), thread("2")])
    label = host.create_label("Blocked", flag=True)
    github = FakeGitHub(refuse={"2"})

    async def go():
        server, client, changes = await serve(host, github)
        try:
            # The controller sees the host's inbox, and only re-fetches changes.
            mirror = Store(":memory:")
            snapshot, etag = await client.state()
            mirror.load_snapshot(snapshot)
            assert [t.id for t in mirror.threads(INBOX)] == ["1", "2"]
            assert await client.state(etag) == (None, etag)

            await client.action(
                "set_label", label_id=label.id, thread_ids=["1"], on=True
            )
            assert [t.id for t in host.threads(label.id)] == ["1"]
            assert (await client.state(etag))[0] is not None
            await client.action("create_label", name="Later")
            await client.action("add_thread", ref="beeware/toga#77", label_id=label.id)
            assert "manual:beeware/toga#77" in [t.id for t in host.threads(label.id)]
            assert [label.name for label in host.labels()] == ["Blocked", "Later"]

            # GitHub changes are made by the host, with its own client.
            # Labelling marks a thread as read, and a flagging label subscribes.
            assert github.calls == [("read", "1"), ("subscribe", "1")]
            await client.mark_done("1")
            assert github.calls[-1] == ("done", "1") and host.get("1").done
            # GitHub's side is best effort: the change stands if it fails.
            await client.mark_done("2")
            assert host.get("2").done

            with pytest.raises(RemoteError, match="No such action"):
                await client.action("delete_everything")
        finally:
            await client.aclose()
            server.stop()
        return changes

    changes = asyncio.run(go())
    assert changes == [
        "moved 1 notification to Blocked",
        "added the label Later",
        "added beeware/toga to Blocked",
        "marked 1 notification as done",
        "marked 1 notification as done",
    ]


def test_wrong_access_key_is_refused():
    async def go():
        server, client, _ = await serve(Store(":memory:"), None, key="other")
        try:
            with pytest.raises(RemoteError, match="access key was refused"):
                await client.state()
        finally:
            await client.aclose()
            server.stop()

    asyncio.run(go())


def test_tunnel_runs_cloudflared_with_the_token_in_its_environment(tmp_path):
    fake = tmp_path / "cloudflared"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        "token = os.environ.get('TUNNEL_TOKEN')\n"
        "print(f'ARGS {sys.argv[1:]} TOKEN {token}', file=sys.stderr, flush=True)\n"
        "print('INF Registered tunnel connection connIndex=0', file=sys.stderr, flush=True)\n"
        "time.sleep(60)\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    async def go():
        statuses, lines = [], []
        tunnel = Tunnel("tok3n", asyncio.get_running_loop(), statuses.append, str(fake))
        original = tunnel._log_line
        tunnel._log_line = lambda line: (lines.append(line), original(line))
        tunnel.start()
        for _ in range(50):
            if "Tunnel connected." in statuses:
                break
            await asyncio.sleep(0.1)
        process = tunnel.process
        tunnel.stop()
        return statuses, lines, process

    statuses, lines, process = asyncio.run(go())
    assert statuses == ["Tunnel starting…", "Tunnel connected."]
    # The token is passed in the environment, not on the command line.
    assert lines[0] == "ARGS ['tunnel', '--no-autoupdate', 'run'] TOKEN tok3n"
    assert process.poll() is not None
