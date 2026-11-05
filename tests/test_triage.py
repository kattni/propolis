import json

import pytest

from propolis.core.store import Store
from propolis.core.threads import ThreadInfo
from propolis.core.triage import AddError, Triage

pytestmark = pytest.mark.anyio


async def test_triage_writes_back_to_github(tmp_path, github, fake_github):
    thread = "/notifications/threads/91"
    for route in [("PATCH", thread), ("PUT", f"{thread}/subscription"), ("DELETE", thread)]:
        fake_github.routes[route] = {}

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
        urgent = store.define_label("High priority", flag_on_activity=True)
        triage = Triage(store, github)

        await triage.label("beeware/toga#1", urgent)
        await triage.done("beeware/toga#1")

    assert fake_github.requests == [
        ("PATCH", thread),
        ("PUT", f"{thread}/subscription"),
        ("DELETE", thread),
    ]


def discussions(request):
    if json.loads(request.content)["variables"]["number"] != 8:
        return {
            "data": {"repository": {"discussion": None}},
            "errors": [{"type": "NOT_FOUND", "message": "Could not resolve"}],
        }
    return {
        "data": {
            "repository": {
                "discussion": {
                    "number": 8,
                    "title": "Ideas",
                    "url": "https://github.com/beeware/toga/discussions/8",
                    "updatedAt": "2026-11-03T12:00:00Z",
                    "repository": {"nameWithOwner": "beeware/toga"},
                }
            }
        }
    }


async def test_adding_a_thread_puts_it_in_the_inbox(tmp_path, github, fake_github):
    fake_github.routes["GET", "/repos/beeware/toga/issues/42"] = {
        "number": 42,
        "title": "Add a widget",
        "html_url": "https://github.com/beeware/toga/pull/42",
        "repository_url": "https://api.github.com/repos/beeware/toga",
        "updated_at": "2026-11-03T12:00:00Z",
        "pull_request": {},
    }
    with Store(tmp_path / "propolis.db") as store:
        triage = Triage(store, github)

        assert await triage.add(" beeware/toga#42 ") == "beeware/toga#42"
        assert store.state.inbox() == ["beeware/toga#42"]
        assert not store.state.threads["beeware/toga#42"].new_activity
        info = store.info("beeware/toga#42")
        assert (info.title, info.kind) == ("Add a widget", "PullRequest")

        # A thread you've already filed stays where it is.
        store.label("beeware/toga#42", "question")
        await triage.add("beeware/toga#42")
        assert store.state.inbox() == []

        # Discussions are numbered with issues, but aren't in the issues API.
        fake_github.routes["POST", "/graphql"] = discussions
        assert await triage.add("BeeWare/Toga#8") == "beeware/toga#8"
        assert store.info("beeware/toga#8").kind == "Discussion"
        assert store.state.inbox() == ["beeware/toga#8"]

        with pytest.raises(AddError, match="owner/repo#123"):
            await triage.add("beeware/toga 42")
        # The fake GitHub has no issue 7, so it's a 404, as for a private repo.
        with pytest.raises(AddError, match="Couldn't find"):
            await triage.add("beeware/toga#7")
