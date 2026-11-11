"""A small async client for the parts of GitHub's API that Propolis uses."""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

import httpx2

from propolis.core.activity import Activity
from propolis.core.threads import ThreadInfo

API = "https://api.github.com"

_ISSUE_URL = re.compile(r"/repos/(?P<repo>[^/]+/[^/]+)/(?:issues|pulls)/(?P<number>\d+)$")
_MENTION = re.compile(r"(?<![\w@/.-])@([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)")
_WEB_PAGE = {"Discussion": "discussions", "Release": "releases"}

_FAILED = {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ERROR"}
_MY_OPEN_PRS_CI = """
query {
  search(query: "is:pr is:open author:@me archived:false", type: ISSUE, first: 100) {
    nodes {
      ... on PullRequest {
        number
        title
        url
        repository { nameWithOwner }
        commits(last: 1) {
          nodes {
            commit {
              statusCheckRollup {
                contexts(first: 100) {
                  nodes {
                    __typename
                    ... on CheckRun { conclusion completedAt }
                    ... on StatusContext { state createdAt }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


_FIND_DISCUSSION = """
query($q: String!) {
  search(query: $q, type: DISCUSSION, first: 20) {
    nodes { ... on Discussion { number title url } }
  }
}
"""
_DISCUSSION = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    discussion(number: $number) {
      number
      title
      url
      updatedAt
      repository { nameWithOwner }
    }
  }
}
"""


@dataclass(frozen=True)
class Notification:
    info: ThreadInfo
    reason: str
    unread: bool
    updated_at: datetime
    last_read_at: datetime | None
    """Unreliable: GitHub can mark a thread read without setting this."""


def parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class GitHub:
    def __init__(self, token: str, transport: httpx2.AsyncBaseTransport | None = None):
        self._http = httpx2.AsyncClient(
            base_url=API,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "Propolis",
            },
            transport=transport,
            timeout=30,
        )
        self.poll_interval = 60
        """How many seconds GitHub asks clients to wait between polls."""

    async def __aenter__(self) -> "GitHub":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def me(self) -> str:
        """The signed-in user's login."""
        return (await self._get("/user")).json()["login"]

    async def notifications(
        self, since: datetime | None = None, last_modified: str | None = None
    ) -> tuple[list[Notification] | None, str | None]:
        """Notifications updated since ``since``, read or not.

        Returns the notifications and the ``Last-Modified`` value to pass next
        time. The notifications are ``None`` if nothing has changed.
        """
        params = {"all": "true", "per_page": "50"}
        if since:
            params["since"] = since.isoformat()
        headers = {"If-Modified-Since": last_modified} if last_modified else {}
        response = await self._http.get(
            "/notifications", params=params, headers=headers
        )
        if interval := response.headers.get("X-Poll-Interval"):
            self.poll_interval = int(interval)
        if response.status_code == 304:
            return None, last_modified
        response.raise_for_status()
        items = await self._all_pages(response)
        return (
            [_notification(item) for item in items],
            response.headers.get("Last-Modified"),
        )

    async def timeline(self, repo: str, number: int) -> list[Activity]:
        """Everything on an issue or pull request's timeline, as activity."""
        response = await self._get(
            f"/repos/{repo}/issues/{number}/timeline", params={"per_page": "100"}
        )
        return [
            activity
            for event in await self._all_pages(response)
            for activity in _activities(event)
        ]

    async def pushes(self, repo: str, number: int) -> list[Activity]:
        """The latest pushes to a pull request's branch, as activity.

        The timeline lists commits by git author, dated when they were made,
        so it can't say who pushed them or when. The branch's own activity,
        in the fork it comes from, can.
        """
        head = (await self._get(f"/repos/{repo}/pulls/{number}")).json()["head"]
        if not head["repo"]:
            # The fork has been deleted.
            return []
        # Newest first. A hundred is plenty between two checks.
        response = await self._get(
            f"/repos/{head['repo']['full_name']}/activity",
            params={"ref": head["ref"], "activity_type": "push", "per_page": "100"},
        )
        return [
            Activity(
                at=parse_time(push["timestamp"]),
                actor=push["actor"]["login"],
                actor_is_bot=push["actor"].get("type") == "Bot",
            )
            for push in response.json()
            if push["actor"]
        ]

    async def issue(self, repo: str, number: int) -> ThreadInfo:
        """An issue or pull request's details, wherever it lives now: GitHub
        redirects to issues that have moved to another repo."""
        response = await self._http.get(
            f"/repos/{repo}/issues/{number}", follow_redirects=True
        )
        response.raise_for_status()
        item = response.json()
        repo = item["repository_url"].removeprefix(f"{API}/repos/")
        return ThreadInfo(
            key=f"{repo}#{item['number']}",
            kind="PullRequest" if "pull_request" in item else "Issue",
            repo=repo,
            title=item["title"],
            url=item["html_url"],
            number=item["number"],
            # Only activity after you add it counts.
            checked_until=parse_time(item["updated_at"]),
            reason="added",
        )

    async def find_discussion(self, repo: str, title: str) -> tuple[int, str] | None:
        """The number and page of the discussion in ``repo`` with exactly this
        title, or None unless there's exactly one."""
        # Search finds titles containing the words; only an exact match counts.
        words = title.replace('"', " ")
        query = f'repo:{repo} in:title "{words}"'
        nodes = (await self._graphql(_FIND_DISCUSSION, q=query))["search"]["nodes"]
        matches = [node for node in nodes if node.get("title") == title]
        if len(matches) != 1:
            return None
        return matches[0]["number"], matches[0]["url"]

    async def discussion(self, repo: str, number: int) -> ThreadInfo | None:
        """A discussion's details, or None if there's no such discussion."""
        owner, _, name = repo.partition("/")
        data = await self._graphql(_DISCUSSION, owner=owner, name=name, number=number)
        if not (found := (data.get("repository") or {}).get("discussion")):
            return None
        repo = found["repository"]["nameWithOwner"]
        return ThreadInfo(
            key=f"{repo}#{found['number']}",
            kind="Discussion",
            repo=repo,
            title=found["title"],
            url=found["url"],
            number=found["number"],
            checked_until=parse_time(found["updatedAt"]),
            reason="added",
        )

    async def ci_failures(self) -> list[tuple[ThreadInfo, Activity]]:
        """The latest CI failure on each of your open pull requests.

        Covers both check runs (GitHub Actions and other apps) and commit
        statuses, on each pull request's latest commit.
        """
        failures = []
        for pr in (await self._graphql(_MY_OPEN_PRS_CI))["search"]["nodes"]:
            failed_at = [
                parse_time(context.get("completedAt") or context.get("createdAt"))
                for commit in pr["commits"]["nodes"]
                for context in (commit["commit"].get("statusCheckRollup") or {})
                .get("contexts", {})
                .get("nodes", [])
                if (context.get("conclusion") or context.get("state")) in _FAILED
            ]
            if failed_at := [at for at in failed_at if at]:
                repo = pr["repository"]["nameWithOwner"]
                info = ThreadInfo(
                    key=f"{repo}#{pr['number']}",
                    kind="PullRequest",
                    repo=repo,
                    title=pr["title"],
                    url=pr["url"],
                    number=pr["number"],
                    reason="ci_failure",
                )
                failures.append(
                    (info, Activity(at=max(failed_at), actor="", ci_failed=True))
                )
        return failures

    async def mark_read(self, notification_id: str) -> None:
        response = await self._http.patch(f"/notifications/threads/{notification_id}")
        response.raise_for_status()

    async def mark_done(self, notification_id: str) -> None:
        response = await self._http.delete(f"/notifications/threads/{notification_id}")
        response.raise_for_status()

    async def subscribe(self, notification_id: str) -> None:
        response = await self._http.put(
            f"/notifications/threads/{notification_id}/subscription",
            json={"ignored": False},
        )
        response.raise_for_status()

    async def _graphql(self, query: str, **variables) -> dict:
        """The data a GraphQL query returns. Something not found is missing
        from the data rather than an error."""
        response = await self._http.post(
            "/graphql", json={"query": query, "variables": variables}
        )
        response.raise_for_status()
        result = response.json()
        errors = [
            error
            for error in result.get("errors") or []
            if error.get("type") != "NOT_FOUND"
        ]
        if errors:
            raise RuntimeError(f"GitHub GraphQL error: {errors[0].get('message')}")
        return result.get("data") or {}

    async def _get(self, path: str, **kwargs) -> httpx2.Response:
        response = await self._http.get(path, **kwargs)
        response.raise_for_status()
        return response

    async def _all_pages(self, response: httpx2.Response) -> list:
        items = response.json()
        while next_page := response.links.get("next", {}).get("url"):
            response = await self._get(next_page)
            items += response.json()
        return items


def _notification(item: dict) -> Notification:
    subject = item["subject"]
    repo = item["repository"]["full_name"]
    kind = subject["type"]
    if match := _ISSUE_URL.search(subject.get("url") or ""):
        number = int(match["number"])
        page = "pull" if kind == "PullRequest" else "issues"
        key, url = f"{repo}#{number}", f"https://github.com/{repo}/{page}/{number}"
    else:
        # Discussions and releases have no number Propolis can use.
        number = None
        key = f"{repo}/notification/{item['id']}"
        url = f"https://github.com/{repo}/{_WEB_PAGE.get(kind, '')}".rstrip("/")
    return Notification(
        info=ThreadInfo(
            key=key,
            kind=kind,
            repo=repo,
            title=subject["title"],
            url=url,
            number=number,
            notification_id=item["id"],
            reason=item["reason"],
        ),
        reason=item["reason"],
        unread=item["unread"],
        updated_at=parse_time(item["updated_at"]),
        last_read_at=parse_time(item.get("last_read_at")),
    )


def _activities(event: dict) -> Iterator[Activity]:
    """The activity in one timeline event: who did it, when, and to whom."""
    match event.get("event"):
        case "committed":
            # Commits carry a git author, not a GitHub login, so there's no
            # way to tell whose they are. Pushes come from the branch instead.
            sources = []
        case "line-commented":
            sources = [
                (comment.get("user"), comment.get("created_at"), comment)
                for comment in event.get("comments", [])
            ]
        case "reviewed":
            sources = [(event.get("user"), event.get("submitted_at"), event)]
        case _:
            sources = [(event.get("actor"), event.get("created_at"), event)]

    for user, at, source in sources:
        if user and at:
            yield Activity(
                at=parse_time(at),
                actor=user["login"],
                actor_is_bot=user.get("type") == "Bot",
                mentions=_MENTION.findall(source.get("body") or ""),
                review_requested_from=(source.get("requested_reviewer") or {}).get(
                    "login"
                ),
                assigned_to=(source.get("assignee") or {}).get("login"),
            )
