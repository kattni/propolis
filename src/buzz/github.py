"""A small async client for GitHub's notifications REST API."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote_plus

import httpx

from .store import Thread

API_URL = "https://api.github.com"
TOKEN_URL = (
    "https://github.com/settings/tokens/new?scopes=notifications&description=Buzz"
)
PER_PAGE = 50
#: Stop paging after this many pages, so a huge inbox can't stall a sync.
MAX_PAGES = 20
#: GitHub asks clients to poll no more often than this; it may ask for longer.
MIN_POLL_INTERVAL = 60

REASONS = {
    "approval_requested": "Approval requested",
    "assign": "Assigned",
    "author": "Author",
    "ci_activity": "CI activity",
    "comment": "Commented",
    "invitation": "Invitation",
    "manual": "Subscribed",
    "member_feature_requested": "Feature request",
    "mention": "Mentioned",
    "review_requested": "Review requested",
    "security_advisory_credit": "Advisory credit",
    "security_alert": "Security alert",
    "state_change": "State change",
    "subscribed": "Watching",
    "team_mention": "Team mentioned",
}

TYPES = {
    "CheckSuite": "CI",
    "PullRequest": "Pull request",
    "RepositoryDependabotAlertsThread": "Dependabot",
    "RepositoryInvitation": "Invitation",
    "RepositoryVulnerabilityAlert": "Security alert",
}

# Where to send the browser for subjects that don't have a page of their own.
_FALLBACK_PATHS = {
    "CheckSuite": "/actions",
    "RepositoryDependabotAlertsThread": "/security/dependabot",
    "RepositoryInvitation": "/invitations",
    "RepositoryVulnerabilityAlert": "/security",
}

_API_SUBJECT = re.compile(
    r"^https://api\.github\.com/repos/(?P<repo>[^/]+/[^/]+)"
    r"/(?P<kind>issues|pulls|commits|releases)/(?P<ref>[^/]+)$"
)
_API_COMMENT = re.compile(r"/(?P<kind>issues|pulls)/comments/(?P<id>\d+)$")
_NUMBER = re.compile(r"/(?:issues|pulls)/(\d+)$")


class GitHubError(Exception):
    """A request to GitHub failed."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class AuthenticationError(GitHubError):
    """GitHub didn't accept the token."""


def describe_reason(reason: str) -> str:
    """A readable version of why a notification was sent."""
    return REASONS.get(reason, reason.replace("_", " ").capitalize())


def describe_type(subject_type: str) -> str:
    """A readable version of a notification subject's type."""
    return TYPES.get(subject_type, subject_type)


def reference(thread: Thread) -> str:
    """Like ``beeware/toga#123``, or just the repository if there's no number."""
    if match := _NUMBER.search(thread.url or ""):
        return f"{thread.repo}#{match[1]}"
    return thread.repo


def thread_from_api(data: dict) -> Thread:
    subject = data["subject"]
    repository = data["repository"]
    return Thread(
        id=str(data["id"]),
        repo=repository["full_name"],
        repo_url=repository["html_url"],
        title=subject["title"],
        type=subject["type"],
        url=subject.get("url"),
        latest_comment_url=subject.get("latest_comment_url"),
        reason=data["reason"],
        unread=bool(data["unread"]),
        updated_at=data["updated_at"],
    )


def html_url(thread: Thread) -> str:
    """The github.com page for a thread, at its latest comment where possible.

    Notifications only carry API URLs, so this maps them to their web pages.
    """
    if match := _API_SUBJECT.match(thread.url or ""):
        repo, kind, ref = match["repo"], match["kind"], match["ref"]
        if kind == "releases":
            # The API URL has the release's numeric ID; the page needs its tag.
            return f"https://github.com/{repo}/releases"
        path = {"issues": "issues", "pulls": "pull", "commits": "commit"}[kind]
        url = f"https://github.com/{repo}/{path}/{ref}"
        if comment := _API_COMMENT.search(thread.latest_comment_url or ""):
            if comment["kind"] == "issues":
                url += f"#issuecomment-{comment['id']}"
            else:
                url += f"#discussion_r{comment['id']}"
        return url

    if thread.type == "Discussion":
        # Discussion notifications have no URL at all, so search by title.
        return f"{thread.repo_url}/discussions?discussions_q={quote_plus(thread.title)}"
    return thread.repo_url + _FALLBACK_PATHS.get(thread.type, "")


@dataclass(frozen=True)
class InboxSnapshot:
    #: The inbox's threads, or None if nothing has changed since the last fetch.
    threads: list[Thread] | None
    #: False if paging stopped at MAX_PAGES before reaching the end of the inbox.
    complete: bool
    #: Pass this back to `GitHubClient.inbox()` to only get changes.
    last_modified: str | None
    #: How long to wait, in seconds, before asking again.
    poll_interval: int


class GitHubClient:
    def __init__(
        self, token: str, *, transport: httpx.AsyncBaseTransport | None = None
    ):
        self._http = httpx.AsyncClient(
            base_url=API_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "Buzz-notifications-inbox",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            response = await self._http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise GitHubError(f"Couldn't reach GitHub ({exc}).") from exc

        status = response.status_code
        if status == 401:
            raise AuthenticationError("GitHub didn't accept the token.", status)
        if status >= 400:
            if response.headers.get("X-RateLimit-Remaining") == "0":
                raise GitHubError(
                    "GitHub's API rate limit was reached. Try later.", status
                )
            try:
                message = response.json()["message"]
            except (ValueError, KeyError, TypeError):
                message = response.reason_phrase
            raise GitHubError(f"GitHub said: {message} ({status}).", status)
        return response

    async def validate(self) -> str:
        """Check the token can read notifications, and return its account's login."""
        response = await self._request("GET", "/user")
        login = response.json()["login"]
        # Fine-grained tokens can read /user but not notifications; this
        # catches them (and classic tokens without the notifications scope).
        try:
            await self._request("GET", "/notifications", params={"per_page": 1})
        except GitHubError as exc:
            if exc.status in {403, 404}:
                raise AuthenticationError(
                    "The token can't read notifications. Use a classic token "
                    "with the 'notifications' scope.",
                    exc.status,
                ) from exc
            raise
        return login

    async def inbox(self, last_modified: str | None = None) -> InboxSnapshot:
        """Fetch every thread in the inbox (read and unread, but not done)."""
        headers = {"If-Modified-Since": last_modified} if last_modified else {}
        first = await self._request(
            "GET",
            "/notifications",
            params={"all": "true", "per_page": PER_PAGE},
            headers=headers,
        )
        poll_interval = max(
            int(first.headers.get("X-Poll-Interval", MIN_POLL_INTERVAL)),
            MIN_POLL_INTERVAL,
        )
        if first.status_code == 304:
            return InboxSnapshot(None, True, last_modified, poll_interval)

        response = first
        threads = [thread_from_api(item) for item in response.json()]
        for _ in range(MAX_PAGES - 1):
            if not (next_url := response.links.get("next", {}).get("url")):
                break
            response = await self._request("GET", next_url)
            threads.extend(thread_from_api(item) for item in response.json())

        return InboxSnapshot(
            threads=threads,
            complete="next" not in response.links,
            last_modified=first.headers.get("Last-Modified"),
            poll_interval=poll_interval,
        )

    async def mark_read(self, thread_id: str) -> None:
        await self._request("PATCH", f"/notifications/threads/{thread_id}")

    async def mark_done(self, thread_id: str) -> None:
        await self._request("DELETE", f"/notifications/threads/{thread_id}")

    async def unsubscribe(self, thread_id: str) -> None:
        """Stop notifications for the thread until you're mentioned or comment."""
        await self._request(
            "DELETE", f"/notifications/threads/{thread_id}/subscription"
        )
