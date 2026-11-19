import asyncio
import json

import httpx
import pytest

from propolis.github import (
    AuthenticationError,
    GitHubClient,
    GitHubError,
    html_url,
    is_github_url,
    parse_reference,
    thread_from_api,
)


def notification(id, subject_type="Issue", url=None, comment=None):
    return {
        "id": id,
        "reason": "mention",
        "unread": True,
        "updated_at": "2026-11-01T10:00:00Z",
        "repository": {
            "full_name": "beeware/toga",
            "html_url": "https://github.com/beeware/toga",
        },
        "subject": {
            "title": "Make the Table faster",
            "type": subject_type,
            "url": url,
            "latest_comment_url": comment,
        },
    }


API = "https://api.github.com/repos/beeware/toga"


@pytest.mark.parametrize(
    "subject_type, url, comment, expected",
    [
        ("Issue", f"{API}/issues/12", None, "/issues/12"),
        (
            "Issue",
            f"{API}/issues/12",
            f"{API}/issues/comments/99",
            "/issues/12#issuecomment-99",
        ),
        (
            "PullRequest",
            f"{API}/pulls/7",
            f"{API}/pulls/comments/5",
            "/pull/7#discussion_r5",
        ),
        ("Commit", f"{API}/commits/abc123", None, "/commit/abc123"),
        ("Release", f"{API}/releases/555", None, "/releases"),
        ("CheckSuite", None, None, "/actions"),
        (
            "Discussion",
            None,
            None,
            "/discussions?discussions_q=Make+the+Table+faster",
        ),
    ],
)
def test_html_url(subject_type, url, comment, expected):
    thread = thread_from_api(notification(1, subject_type, url, comment))
    assert html_url(thread) == "https://github.com/beeware/toga" + expected


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://github.com/beeware/toga/issues/12", True),
        ("https://gist.github.com/someone/abc123", True),
        ("https://raw.githubusercontent.com/beeware/toga/main/README.md", True),
        ("https://docs.python.org/3/", False),
        ("https://github.com.example.com/beeware", False),
        ("mailto:someone@example.com", False),
    ],
)
def test_is_github_url(url, expected):
    assert is_github_url(url) == expected


def run(client_call):
    return asyncio.run(client_call)


def client_for(handler):
    return GitHubClient("t0ken", transport=httpx.MockTransport(handler))


def test_inbox_pages_and_polling_headers():
    seen = []

    def handler(request):
        seen.append(request)
        assert request.headers["Authorization"] == "Bearer t0ken"
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=[notification(3)])
        return httpx.Response(
            200,
            json=[notification(1), notification(2)],
            headers={
                "Link": '<https://api.github.com/notifications?all=true&page=2>; rel="next"',
                "Last-Modified": "Wed, 18 Nov 2026 10:00:00 GMT",
                "X-Poll-Interval": "120",
            },
        )

    async def go():
        client = client_for(handler)
        try:
            return await client.inbox()
        finally:
            await client.aclose()

    snapshot = run(go())
    assert [t.id for t in snapshot.threads] == ["1", "2", "3"]
    assert snapshot.last_modified == "Wed, 18 Nov 2026 10:00:00 GMT"
    assert snapshot.poll_interval == 120
    assert seen[0].url.params["all"] == "true"


def test_inbox_not_modified():
    def handler(request):
        assert request.headers["If-Modified-Since"] == "then"
        return httpx.Response(304, headers={"X-Poll-Interval": "60"})

    snapshot = run(client_for(handler).inbox("then"))
    assert snapshot.threads is None
    assert snapshot.last_modified == "then"


def test_errors():
    def unauthorized(request):
        return httpx.Response(401, json={"message": "Bad credentials"})

    with pytest.raises(AuthenticationError):
        run(client_for(unauthorized).mark_read("1"))

    def forbidden(request):
        return httpx.Response(403, json={"message": "Nope"})

    with pytest.raises(GitHubError, match="Nope"):
        run(client_for(forbidden).mark_done("1"))


def test_validate_rejects_token_without_notifications_access():
    def handler(request):
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "octocat"})
        return httpx.Response(403, json={"message": "Resource not accessible"})

    with pytest.raises(AuthenticationError, match="notifications"):
        run(client_for(handler).validate())


SINCE = "2026-11-10T00:00:00Z"
LATER = "2026-11-11T00:00:00Z"


def comment(login, body="", bot=False):
    user = {"login": login, "type": "Bot" if bot else "User"}
    return {"event": "commented", "actor": user, "created_at": LATER, "body": body}


def push(login, bot=False):
    actor = {"login": login, "type": "Bot" if bot else "User"}
    return {"actor": actor, "timestamp": LATER}


@pytest.mark.parametrize(
    "events, pushes, expected",
    [
        # Only my own comment and push since then.
        ([comment("me")], [push("me")], None),
        # Someone else reviewed.
        (
            [{"event": "reviewed", "user": {"login": "them"}, "submitted_at": LATER}],
            [],
            LATER,
        ),
        # A bot's comment counts only if it's aimed at me.
        ([comment("ci[bot]", "All green", bot=True)], [], None),
        ([comment("ci[bot]", "@me, please look", bot=True)], [], LATER),
        # Someone else pushed to the branch, but a bot's push doesn't count.
        ([], [push("them")], LATER),
        ([], [push("pre-commit-ci[bot]", bot=True)], None),
    ],
)
def test_latest_activity_needs_a_response(events, pushes, expected):
    pull = thread_from_api(notification(1, "PullRequest", f"{API}/pulls/7"))

    def handler(request):
        path = request.url.path
        if path.endswith("/issues/7/timeline"):
            return httpx.Response(200, json=events)
        if path == "/repos/beeware/toga/pulls/7":
            head = {"ref": "fix", "repo": {"full_name": "them/toga"}}
            return httpx.Response(200, json={"head": head})
        assert path == "/repos/them/toga/activity"
        assert request.url.params["ref"] == "fix"
        return httpx.Response(200, json=pushes)

    assert run(client_for(handler).latest_activity(pull, SINCE, "me")) == expected


def test_ci_failures():
    def context(conclusion, at="2026-11-02T00:00:00Z"):
        return {"__typename": "CheckRun", "conclusion": conclusion, "completedAt": at}

    def pull(number, *contexts):
        rollup = {"contexts": {"nodes": list(contexts)}}
        return {
            "number": number,
            "title": "Faster tables",
            "updatedAt": "2026-11-01T00:00:00Z",
            "repository": {"nameWithOwner": "beeware/toga"},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": rollup}}]},
        }

    def handler(request):
        pulls = [
            pull(1, context("SUCCESS")),
            pull(2, context("FAILURE"), context("TIMED_OUT", "2026-11-03T00:00:00Z")),
        ]
        return httpx.Response(200, json={"data": {"search": {"nodes": pulls}}})

    [failure] = run(client_for(handler).ci_failures())
    assert (failure.number, failure.failed_at) == (2, "2026-11-03T00:00:00Z")
    assert failure.url == f"{API}/pulls/2"


def test_actions_use_the_right_endpoints():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(205 if request.method == "PATCH" else 204)

    async def go():
        client = client_for(handler)
        await client.mark_read("9")
        await client.mark_done("9")
        await client.subscribe("9")
        await client.unsubscribe("9")
        # Threads added by hand have no notification on GitHub to change.
        await client.mark_read("manual:beeware/toga#1")
        await client.mark_done("manual:beeware/toga#1")
        await client.subscribe("manual:beeware/toga#1")
        await client.unsubscribe("manual:beeware/toga#1")

    run(go())
    assert calls == [
        ("PATCH", "/notifications/threads/9"),
        ("DELETE", "/notifications/threads/9"),
        ("PUT", "/notifications/threads/9/subscription"),
        ("DELETE", "/notifications/threads/9/subscription"),
    ]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("beeware/toga#1234", ("beeware/toga", 1234)),
        (" beeware/briefcase-template#7 ", ("beeware/briefcase-template", 7)),
        ("https://github.com/beeware/toga/pull/42/files", ("beeware/toga", 42)),
        (
            "github.com/beeware/toga/discussions/1252#discussioncomment-1",
            ("beeware/toga", 1252),
        ),
    ],
)
def test_parse_reference(text, expected):
    assert parse_reference(text) == expected


@pytest.mark.parametrize(
    "text", ["beeware/toga", "toga#12", "https://example.com/a/b/issues/1"]
)
def test_parse_reference_rejects(text):
    with pytest.raises(ValueError, match="org/repo#123"):
        parse_reference(text)


def test_lookup_finds_pull_requests_and_discussions():
    def handler(request):
        path = request.url.path
        if path == "/repos/BeeWare/Toga/issues/7":
            return httpx.Response(
                200,
                json={
                    "title": "Faster tables",
                    "updated_at": "2026-11-01T10:00:00Z",
                    "url": f"{API}/issues/7",
                    "repository_url": API,
                    "pull_request": {"url": f"{API}/pulls/7"},
                },
            )
        if path == "/repos/beeware/toga/discussions/9":
            return httpx.Response(
                200,
                json={
                    "title": "Roadmap",
                    "updated_at": "2026-11-02T10:00:00Z",
                    "repository_url": API,
                },
            )
        return httpx.Response(404, json={"message": "Not Found"})

    async def go():
        client = client_for(handler)
        return await client.lookup("BeeWare/Toga", 7), await client.lookup(
            "beeware/toga", 9
        )

    pull, discussion = run(go())
    assert (pull.id, pull.repo, pull.type) == (
        "manual:beeware/toga#7",
        "beeware/toga",
        "PullRequest",
    )
    assert html_url(pull) == "https://github.com/beeware/toga/pull/7"
    assert discussion.type == "Discussion"
    assert html_url(discussion) == "https://github.com/beeware/toga/discussions/9"

    with pytest.raises(GitHubError, match="Couldn't find .* beeware/toga#3"):
        run(client_for(handler).lookup("beeware/toga", 3))


def test_states():
    def handler(request):
        body = json.loads(request.content)
        # Repositories and numbers are sent as variables, not in the query.
        assert body["variables"] == {"o0": "beeware", "r0": "toga", "n0": 1} | {
            "o1": "beeware",
            "r1": "toga",
            "n1": 2,
            "o2": "beeware",
            "r2": "toga",
            "n2": 3,
        }
        issue = {"__typename": "Issue", "state": "CLOSED", "stateReason": "NOT_PLANNED"}
        pull = {"__typename": "PullRequest", "state": "MERGED", "isDraft": False}
        return httpx.Response(
            200,
            json={
                "data": {
                    "t0": {"issueOrPullRequest": issue},
                    "t1": {"issueOrPullRequest": pull},
                    "t2": None,  # A repository the token can't read.
                },
                "errors": [{"type": "NOT_FOUND"}],
            },
        )

    threads = [
        thread_from_api(notification("1", "Issue", f"{API}/issues/1")),
        thread_from_api(notification("2", "PullRequest", f"{API}/pulls/2")),
        thread_from_api(notification("3", "Issue", f"{API}/issues/3")),
        thread_from_api(notification("4", "Release", f"{API}/releases/4")),
    ]
    states = run(client_for(handler).states(threads))
    assert states == {"1": "not_planned", "2": "merged", "3": None}
