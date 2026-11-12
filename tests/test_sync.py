import json
from datetime import UTC, datetime

import pytest

from propolis.core.folder import SharedFolder, WrongAccount
from propolis.core.state import ATTENTION
from propolis.core.store import Store
from propolis.core.sync import Sync
from propolis.core.triage import Triage

pytestmark = pytest.mark.anyio


def notification(number, updated_at, unread=True):
    return {
        "id": f"9{number}",
        "reason": "subscribed",
        "unread": unread,
        "updated_at": updated_at,
        "last_read_at": None,
        "repository": {"full_name": "beeware/toga"},
        "subject": {
            "title": f"Thread {number}",
            "type": "Issue",
            "url": f"https://api.github.com/repos/beeware/toga/issues/{number}",
        },
    }


def discussion(notification_id, title, updated_at):
    return {
        "id": notification_id,
        "reason": "subscribed",
        "unread": True,
        "updated_at": updated_at,
        "last_read_at": None,
        "repository": {"full_name": "beeware/toga"},
        # A discussion's notification doesn't say which discussion it is.
        "subject": {"title": title, "type": "Discussion", "url": None},
    }


def comment(login, at, body="", bot=False):
    return {
        "event": "commented",
        "actor": {"login": login, "type": "Bot" if bot else "User"},
        "created_at": at,
        "body": body,
    }


@pytest.fixture
def store(tmp_path):
    # Triage happens at noon, after the first notifications and before the
    # activity that follows.
    noon = datetime(2026, 11, 3, 12, 30, tzinfo=UTC)
    with Store(tmp_path / "propolis.db", clock=lambda: noon) as store:
        yield store


@pytest.fixture
def no_open_prs(fake_github):
    fake_github.routes["POST", "/graphql"] = {"data": {"search": {"nodes": []}}}


async def test_sync_imports_the_whole_inbox_at_startup(
    store, github, fake_github, no_open_prs
):
    fake_github.routes["GET", "/notifications"] = [
        notification(1, "2026-11-03T10:00:00Z"),
        notification(2, "2026-11-03T11:00:00Z", unread=False),
    ]

    await Sync(store, github).run_once()

    # Read or not, and whoever acted, it all starts in the Inbox. Only the
    # unread thread is new.
    inbox = store.state.inbox()
    assert inbox == ["beeware/toga#2", "beeware/toga#1"]
    assert [store.state.threads[key].new_activity for key in inbox] == [False, True]
    assert store.info("beeware/toga#1").reason == "subscribed"

    # GitHub can list a thread after polls have moved past its updated_at, for
    # example one moved back to the inbox after being marked done. Starting
    # Propolis again reads the whole inbox, so it catches up, without undoing
    # your triage.
    fake_github.routes["GET", "/notifications"].append(
        notification(3, "2026-11-03T09:00:00Z", unread=False)
    )
    store.mark_done("beeware/toga#1")

    await Sync(store, github).run_once()

    assert store.state.inbox() == ["beeware/toga#2", "beeware/toga#3"]


async def test_sync_flags_activity_that_needs_a_response(
    store, github, fake_github, no_open_prs
):
    fake_github.routes["GET", "/notifications"] = [
        notification(number, "2026-11-03T09:00:00Z") for number in range(1, 5)
    ]
    sync = Sync(store, github)
    await sync.run_once()
    for number in range(1, 5):
        store.label(f"beeware/toga#{number}", "high-priority")

    fake_github.routes["GET", "/notifications"] = [
        notification(1, "2026-11-03T13:00:00Z"),
        notification(2, "2026-11-03T14:00:00Z"),
        notification(3, "2026-11-03T15:00:00Z"),
        # Without the repo scope, a private repo's timeline is a 404. The fake
        # GitHub has no timeline for this one, so it returns a 404 too.
        notification(4, "2026-11-03T16:00:00Z"),
    ]
    timeline = "/repos/beeware/toga/issues/{}/timeline".format
    fake_github.routes["GET", timeline(1)] = [
        comment("freakboy3742", "2026-11-03T13:00:00Z")
    ]
    fake_github.routes["GET", timeline(2)] = [
        comment("kattni", "2026-11-03T13:30:00Z"),
        comment("codecov[bot]", "2026-11-03T14:00:00Z", "Coverage: 100%", bot=True),
    ]
    fake_github.routes["GET", timeline(3)] = [
        comment("cla-bot[bot]", "2026-11-03T15:00:00Z", "Thanks @kattni!", bot=True)
    ]

    await sync.run_once()

    assert store.state.labelled(ATTENTION) == [
        "beeware/toga#1",
        "beeware/toga#3",
        "beeware/toga#4",
    ]


async def test_sync_counts_pushes_to_a_pull_request(
    store, github, fake_github, no_open_prs
):
    def pull_request(updated_at):
        item = notification(5, updated_at)
        item["subject"]["type"] = "PullRequest"
        return item

    fake_github.routes["GET", "/notifications"] = [
        pull_request("2026-11-03T09:00:00Z")
    ]
    sync = Sync(store, github)
    await sync.run_once()
    store.mark_done("beeware/toga#5")

    # The author pushes from their fork without commenting. The timeline
    # doesn't say who pushed, but the branch's activity does.
    fake_github.routes["GET", "/notifications"] = [
        pull_request("2026-11-03T13:00:00Z")
    ]
    fake_github.routes["GET", "/repos/beeware/toga/issues/5/timeline"] = []
    fake_github.routes["GET", "/repos/beeware/toga/pulls/5"] = {
        "head": {"ref": "fix", "repo": {"full_name": "contributor/toga"}}
    }
    fake_github.routes["GET", "/repos/contributor/toga/activity"] = [
        {
            "activity_type": "push",
            "timestamp": "2026-11-03T13:00:00Z",
            "actor": {"login": "contributor", "type": "User"},
        }
    ]

    await sync.run_once()

    assert store.state.inbox() == ["beeware/toga#5"]


async def test_sync_records_failing_ci_on_my_pull_requests(store, github, fake_github):
    def check(conclusion, at):
        return {"__typename": "CheckRun", "conclusion": conclusion, "completedAt": at}

    fake_github.routes["GET", "/notifications"] = []
    fake_github.routes["POST", "/graphql"] = {
        "data": {
            "search": {
                "nodes": [
                    {
                        "number": 42,
                        "title": "Add a widget",
                        "url": "https://github.com/beeware/toga/pull/42",
                        "repository": {"nameWithOwner": "beeware/toga"},
                        "commits": {
                            "nodes": [
                                {
                                    "commit": {
                                        "statusCheckRollup": {
                                            "contexts": {
                                                "nodes": [
                                                    check("SUCCESS", "2026-11-03T12:00:00Z"),
                                                    check("FAILURE", "2026-11-03T12:05:00Z"),
                                                ]
                                            }
                                        }
                                    }
                                }
                            ]
                        },
                    }
                ]
            }
        }
    }
    sync = Sync(store, github)

    await sync.run_once()
    assert store.state.inbox() == ["beeware/toga#42"]
    assert store.info("beeware/toga#42").title == "Add a widget"

    store.mark_done("beeware/toga#42")
    await sync.run_once()
    assert store.state.inbox() == []


async def test_sync_finds_discussions_by_their_title(store, github, fake_github):
    def graphql(request):
        if "DISCUSSION" not in json.loads(request.content)["query"]:
            return {"data": {"search": {"nodes": []}}}  # No open pull requests
        found = [(7, "Ideas"), (8, "Ideas for Toga"), (9, "Help"), (10, "Help")]
        return {
            "data": {
                "search": {
                    "nodes": [
                        {
                            "number": number,
                            "title": title,
                            "url": f"https://github.com/beeware/toga/discussions/{number}",
                        }
                        for number, title in found
                    ]
                }
            }
        }

    fake_github.routes["POST", "/graphql"] = graphql
    fake_github.routes["GET", "/notifications"] = [
        discussion("d1", "Ideas", "2026-11-03T10:00:00Z"),
        # Two discussions have this title, so there's no telling which it is.
        discussion("d2", "Help", "2026-11-03T09:00:00Z"),
    ]

    await Sync(store, github).run_once()

    assert store.state.inbox() == ["beeware/toga#7", "beeware/toga/notification/d2"]
    assert store.info("beeware/toga#7").url == (
        "https://github.com/beeware/toga/discussions/7"
    )


async def test_a_mac_without_github_syncs_through_a_shared_folder(
    tmp_path, github, fake_github, no_open_prs
):
    fake_github.routes["GET", "/notifications"] = [
        notification(1, "2026-11-03T10:00:00Z")
    ]
    fake_github.routes["DELETE", "/notifications/threads/91"] = {}
    folder = tmp_path / "shared"
    folder.mkdir()

    with Store(tmp_path / "a.db") as a, Store(tmp_path / "b.db") as b:
        signed_in = Sync(a, github, SharedFolder(a, folder))
        not_signed_in = Sync(b, None, SharedFolder(b, folder))

        # The Mac that's signed in shares the thread, and its details.
        await signed_in.run_once()
        await not_signed_in.run_once()
        assert b.state.inbox() == ["beeware/toga#1"]
        assert b.info("beeware/toga#1").title == "Thread 1"

        # Triage on the other Mac reaches GitHub through it.
        await Triage(b, None).done("beeware/toga#1")
        await not_signed_in.run_once()
        await signed_in.run_once()

    assert a.state.threads["beeware/toga#1"].done
    assert ("DELETE", "/notifications/threads/91") in fake_github.requests


async def test_sync_wont_mix_two_github_accounts(tmp_path, github):
    folder = tmp_path / "shared"
    folder.mkdir()
    with Store(tmp_path / "theirs.db") as theirs:
        theirs.record_activity("beeware/toga#1", datetime(2026, 11, 3, 10, tzinfo=UTC))
        shared = SharedFolder(theirs, folder)
        shared.pull(signed_in_as="alex")
        shared.push()

    # This Mac is signed in as kattni, and the folder is alex's.
    with Store(tmp_path / "mine.db") as mine:
        with pytest.raises(WrongAccount):
            await Sync(mine, github, SharedFolder(mine, folder)).run_once()
        assert mine.state.threads == {}
        assert not list(folder.glob(f"{mine.device}*"))
