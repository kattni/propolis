import asyncio

import httpx
import pytest

from buzz.github import (
    AuthenticationError,
    GitHubClient,
    GitHubError,
    html_url,
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
    assert snapshot.complete
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


def test_inbox_stops_at_page_limit(monkeypatch):
    monkeypatch.setattr("buzz.github.MAX_PAGES", 2)

    def handler(request):
        page = int(request.url.params.get("page", 1))
        return httpx.Response(
            200,
            json=[notification(page)],
            headers={
                "Link": f'<https://api.github.com/notifications?page={page + 1}>; rel="next"'
            },
        )

    snapshot = run(client_for(handler).inbox())
    assert [t.id for t in snapshot.threads] == ["1", "2"]
    assert not snapshot.complete


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


def test_actions_use_the_right_endpoints():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(205 if request.method == "PATCH" else 204)

    async def go():
        client = client_for(handler)
        await client.mark_read("9")
        await client.mark_done("9")
        await client.unsubscribe("9")

    run(go())
    assert calls == [
        ("PATCH", "/notifications/threads/9"),
        ("DELETE", "/notifications/threads/9"),
        ("DELETE", "/notifications/threads/9/subscription"),
    ]
