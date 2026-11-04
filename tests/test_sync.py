import pytest

from propolis.core.store import Store
from propolis.core.sync import Sync

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


def comment(login, at, body="", bot=False):
    return {
        "event": "commented",
        "actor": {"login": login, "type": "Bot" if bot else "User"},
        "created_at": at,
        "body": body,
    }


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "propolis.db") as store:
        yield store


@pytest.fixture
def no_open_prs(fake_github):
    fake_github.routes["POST", "/graphql"] = {"data": {"search": {"nodes": []}}}


async def test_sync_records_activity_that_needs_a_response(
    store, github, fake_github, no_open_prs
):
    fake_github.routes["GET", "/notifications"] = [
        notification(1, "2026-11-03T10:00:00Z"),
        notification(2, "2026-11-03T11:00:00Z"),
        notification(3, "2026-11-03T12:00:00Z"),
        # GitHub can mark a thread read without saying when.
        notification(4, "2026-11-03T13:00:00Z", unread=False),
    ]
    timeline = "/repos/beeware/toga/issues/{}/timeline".format
    fake_github.routes["GET", timeline(1)] = [
        comment("freakboy3742", "2026-11-03T10:00:00Z")
    ]
    fake_github.routes["GET", timeline(2)] = [
        comment("kattni", "2026-11-03T10:30:00Z"),
        comment("codecov[bot]", "2026-11-03T11:00:00Z", "Coverage: 100%", bot=True),
    ]
    fake_github.routes["GET", timeline(3)] = [
        comment("cla-bot[bot]", "2026-11-03T12:00:00Z", "Thanks @kattni!", bot=True)
    ]
    fake_github.routes["GET", timeline(4)] = [
        comment("freakboy3742", "2026-11-03T13:00:00Z")
    ]

    await Sync(store, github).run_once()

    assert store.state.inbox() == ["beeware/toga#3", "beeware/toga#1"]
    assert store.info("beeware/toga#1").title == "Thread 1"


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
