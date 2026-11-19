"""A small async client for the parts of GitHub's API that Buzz uses:
notifications, the timelines and pushes that say who was active on a thread,
and, with the GraphQL API, whether threads are open, which of your pull
requests are failing CI, and which discussion a notification is about."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import quote_plus, urlsplit

import httpx

from .activity import Activity, needs_response
from .store import MANUAL_PREFIX, Thread, stamp

API_URL = "https://api.github.com"
PER_PAGE = 50
#: GitHub asks clients to poll no more often than this; it may ask for longer.
MIN_POLL_INTERVAL = 60
#: How many threads' states to look up in one GraphQL request.
STATES_PER_REQUEST = 50

#: The states `GitHubClient.states()` reports. Issues are open, completed,
#: not planned or duplicates; pull requests are open, draft, merged or closed;
#: discussions are open, resolved, outdated or duplicates.
STATES = {
    "open",
    "completed",
    "not_planned",
    "duplicate",
    "draft",
    "merged",
    "closed",
    "resolved",
    "outdated",
}

_STATE_FIELDS = {
    "issues": "issueOrPullRequest",
    "pulls": "issueOrPullRequest",
    "discussions": "discussion",
}
_STATE_SELECTIONS = {
    "issueOrPullRequest": "__typename "
    "... on Issue { state stateReason } "
    "... on PullRequest { state isDraft }",
    "discussion": "__typename closed stateReason",
}

REASONS = {
    "added": "Added by hand",
    "approval_requested": "Approval requested",
    "assign": "Assigned",
    "author": "Author",
    "ci_activity": "CI activity",
    "ci_failure": "Failing CI",
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
    r"/(?P<kind>issues|pulls|commits|releases|discussions)/(?P<ref>[^/]+)$"
)
_API_COMMENT = re.compile(r"/(?P<kind>issues|pulls)/comments/(?P<id>\d+)$")
_NUMBER = re.compile(r"/(?:issues|pulls|discussions)/(\d+)$")

# What people type to add a thread by hand: org/repo#123, or a GitHub link.
_REPO = r"(?P<repo>[A-Za-z0-9-]+/[A-Za-z0-9._-]+)"
_REFERENCE = re.compile(rf"^{_REPO}#(?P<number>\d+)$")
_WEB_LINK = re.compile(
    rf"^(?:https?://)?(?:www\.)?github\.com/{_REPO}"
    r"/(?:issues|pull|discussions)/(?P<number>\d+)(?:[/?#].*)?$"
)

#: Timeline pages to read back from the newest when looking for activity.
MAX_TIMELINE_PAGES = 5
_MENTION = re.compile(r"(?<![\w@/.-])@([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)")

#: The CI conclusions and commit statuses that mean it failed.
_FAILED = {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ERROR"}
_MY_OPEN_PULLS_CI = """
query {
  search(query: "is:pr is:open author:@me archived:false", type: ISSUE, first: 100) {
    nodes {
      ... on PullRequest {
        number
        title
        updatedAt
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
    nodes { ... on Discussion { number title } }
  }
}
"""


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


def parse_reference(text: str) -> tuple[str, int]:
    """Read ``org/repo#123``, or a link to an issue, pull request or
    discussion, as a repository and number."""
    text = text.strip()
    if match := _REFERENCE.match(text) or _WEB_LINK.match(text):
        return match["repo"], int(match["number"])
    raise ValueError(
        "Use the form org/repo#123, or paste a link to an issue, pull request "
        "or discussion."
    )


def api_url(repo: str, kind: str, number: int) -> str:
    """The API URL of an issue, pull request or discussion, as notifications
    give it. ``kind`` is ``issues``, ``pulls`` or ``discussions``."""
    return f"{API_URL}/repos/{repo}/{kind}/{number}"


def is_manual(thread_id: str) -> bool:
    """Whether a thread was added by hand, rather than by a notification."""
    return thread_id.startswith(MANUAL_PREFIX)


def manual_reference(thread_id: str) -> tuple[str, int]:
    """The repository and number of a thread added by hand."""
    repo, _, number = thread_id.removeprefix(MANUAL_PREFIX).partition("#")
    return repo, int(number)


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


#: The sites GitHub's pages are on. (githubusercontent.com serves the files,
#: images and embedded frames on them.)
GITHUB_DOMAINS = ("github.com", "githubusercontent.com")


def is_github_url(url: str) -> bool:
    """Whether ``url`` is a page on GitHub, rather than on some other site."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    return parts.scheme in ("http", "https") and any(
        host == domain or host.endswith(f".{domain}") for domain in GITHUB_DOMAINS
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
        path = {
            "issues": "issues",
            "pulls": "pull",
            "commits": "commit",
            "discussions": "discussions",
        }[kind]
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


def _state(node: dict | None) -> str | None:
    """A GraphQL issue, pull request or discussion's state, as one of `STATES`."""
    match (node or {}).get("__typename"):
        case "Issue":
            if node["state"] == "OPEN":
                return "open"
            reason = node.get("stateReason")
            return {"NOT_PLANNED": "not_planned", "DUPLICATE": "duplicate"}.get(
                reason, "completed"
            )
        case "PullRequest":
            if node["state"] == "OPEN":
                return "draft" if node.get("isDraft") else "open"
            return "merged" if node["state"] == "MERGED" else "closed"
        case "Discussion":
            if not node.get("closed"):
                return "open"
            reason = node.get("stateReason")
            return {"OUTDATED": "outdated", "DUPLICATE": "duplicate"}.get(
                reason, "resolved"
            )
    return None


def _activities(event: dict) -> list[Activity]:
    """The activity in one timeline event: who did it, when, and to whom."""
    if event.get("event") == "committed":
        # Commits carry a git author, not a GitHub login, so there's no way to
        # tell whose they are. Pushes come from the branch instead.
        return []
    if "comments" in event:
        sources = [
            (comment.get("user"), comment.get("created_at"), comment)
            for comment in event["comments"]
        ]
    elif event.get("event") == "reviewed":
        sources = [(event.get("user"), event.get("submitted_at"), event)]
    else:
        sources = [
            (event.get("actor") or event.get("user"), event.get("created_at"), event)
        ]
    return [
        Activity(
            at=at,
            actor=user["login"],
            actor_is_bot=user.get("type") == "Bot",
            mentions=_MENTION.findall(source.get("body") or ""),
            review_requested_from=(source.get("requested_reviewer") or {}).get("login"),
            assigned_to=(source.get("assignee") or {}).get("login"),
        )
        for user, at, source in sources
        if user and user.get("login") and at
    ]


def _event_time(event: dict) -> str | None:
    if comments := event.get("comments"):
        return comments[0].get("created_at")
    return (
        event.get("created_at")
        or event.get("submitted_at")
        or (event.get("committer") or {}).get("date")
    )


@dataclass(frozen=True)
class CIFailure:
    """The latest CI failure on one of your open pull requests."""

    repo: str
    number: int
    title: str
    #: When the pull request was last updated.
    updated_at: str
    failed_at: str

    @property
    def url(self) -> str:
        return api_url(self.repo, "pulls", self.number)


@dataclass(frozen=True)
class InboxSnapshot:
    #: The threads, or None if nothing has changed since the last fetch.
    threads: list[Thread] | None
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
        # Tokens without the notifications scope can read /user, but not
        # notifications.
        try:
            await self._request("GET", "/notifications", params={"per_page": 1})
        except GitHubError as exc:
            if exc.status in {403, 404}:
                raise AuthenticationError(
                    "This sign-in can't read notifications.", exc.status
                ) from exc
            raise
        return login

    async def inbox(
        self, last_modified: str | None = None, since: str | None = None
    ) -> InboxSnapshot:
        """Fetch the threads in the inbox, read and unread (but not done), all
        of them, or only those updated since ``since``.

        :param last_modified: The ``last_modified`` of the last snapshot, to
            find out nothing has changed without using up the rate limit.
        """
        headers = {"If-Modified-Since": last_modified} if last_modified else {}
        params = {"all": "true", "per_page": PER_PAGE}
        if since:
            params["since"] = since
        first = await self._request(
            "GET", "/notifications", params=params, headers=headers
        )
        poll_interval = max(
            int(first.headers.get("X-Poll-Interval", MIN_POLL_INTERVAL)),
            MIN_POLL_INTERVAL,
        )
        if first.status_code == 304:
            return InboxSnapshot(None, last_modified, poll_interval)

        response = first
        threads = [thread_from_api(item) for item in response.json()]
        while next_url := response.links.get("next", {}).get("url"):
            response = await self._request("GET", next_url)
            threads.extend(thread_from_api(item) for item in response.json())

        return InboxSnapshot(
            threads=threads,
            last_modified=first.headers.get("Last-Modified"),
            poll_interval=poll_interval,
        )

    # A thread added by hand has no notification on GitHub, so marking it as
    # read or done, or unsubscribing from it, only changes it in Buzz.

    async def mark_read(self, thread_id: str) -> None:
        if not is_manual(thread_id):
            await self._request("PATCH", f"/notifications/threads/{thread_id}")

    async def mark_done(self, thread_id: str) -> None:
        if not is_manual(thread_id):
            await self._request("DELETE", f"/notifications/threads/{thread_id}")

    async def subscribe(self, thread_id: str) -> None:
        """Subscribe to a thread, so its activity keeps reaching you."""
        if not is_manual(thread_id):
            await self._request(
                "PUT",
                f"/notifications/threads/{thread_id}/subscription",
                json={"ignored": False},
            )

    async def lookup(self, repo: str, number: int) -> Thread:
        """Fetch an issue, pull request or discussion, as a thread to add by hand."""
        try:
            # Issues and pull requests share this endpoint; discussions don't.
            response = await self._request(
                "GET", f"/repos/{repo}/issues/{number}", follow_redirects=True
            )
            kind = "issues"
        except GitHubError as exc:
            if exc.status not in {404, 410}:
                raise
            try:
                response = await self._request(
                    "GET", f"/repos/{repo}/discussions/{number}", follow_redirects=True
                )
            except GitHubError as exc:
                if exc.status in {404, 410}:
                    raise GitHubError(
                        f"Couldn't find an issue, pull request or discussion "
                        f"{repo}#{number}.",
                        exc.status,
                    ) from None
                raise
            kind = "discussions"

        data = response.json()
        # GitHub's spelling of the name, even if it was typed differently.
        full_name = data["repository_url"].split("/repos/", 1)[1]
        if kind == "discussions":
            subject_type, url = (
                "Discussion",
                f"{API_URL}/repos/{full_name}/discussions/{number}",
            )
        elif pull := data.get("pull_request"):
            subject_type, url = "PullRequest", pull["url"]
        else:
            subject_type, url = "Issue", data["url"]
        return Thread(
            id=f"{MANUAL_PREFIX}{full_name}#{number}",
            repo=full_name,
            repo_url=f"https://github.com/{full_name}",
            title=data["title"],
            type=subject_type,
            url=url,
            latest_comment_url=None,
            reason="added",
            unread=False,
            updated_at=data["updated_at"],
        )

    async def latest_activity(
        self, thread: Thread, since: str | None, me: str
    ) -> str | None:
        """When the latest activity on a thread that needs a response from
        ``me`` happened, if any of it happened after ``since``.

        Anything anyone else does counts, and nothing you do does. A bot's
        activity counts only when it's aimed at you (see `needs_response()`).
        When there's no way to tell who was active, for a discussion, a release,
        or a thread in a private repository the token can't read, any update
        counts, so nothing is missed.
        """
        if thread.type == "CheckSuite" or thread.reason == "ci_activity":
            # CI failures are found by looking at your pull requests instead.
            return None
        match = _API_SUBJECT.match(thread.url or "")
        if not match or match["kind"] not in {"issues", "pulls"}:
            return thread.updated_at
        repo, number = match["repo"], match["ref"]

        try:
            events = await self._newest(
                f"/repos/{repo}/issues/{number}/timeline", since, _event_time
            )
            activities = [a for event in events for a in _activities(event)]
            if match["kind"] == "pulls":
                activities += await self.pushes(repo, number)
        except GitHubError as exc:
            if exc.status not in {404, 410}:
                raise
            # Hidden, such as in a private repository, or deleted.
            return thread.updated_at

        after = stamp(since) if since else ""
        return max(
            (
                activity.at
                for activity in activities
                if stamp(activity.at) > after and needs_response(activity, me)
            ),
            key=stamp,
            default=None,
        )

    async def pushes(self, repo: str, number: int | str) -> list[Activity]:
        """The latest pushes to a pull request's branch, as activity.

        The timeline lists commits by git author, dated when they were made, so
        it can't say who pushed them or when. The branch's own activity, in the
        fork it comes from, can.
        """
        pull = (await self._request("GET", f"/repos/{repo}/pulls/{number}")).json()
        head = pull["head"]
        if not head.get("repo"):
            return []  # The fork has been deleted.
        # Newest first. A hundred is plenty between two checks.
        response = await self._request(
            "GET",
            f"/repos/{head['repo']['full_name']}/activity",
            params={"ref": head["ref"], "activity_type": "push", "per_page": 100},
        )
        return [
            Activity(
                at=push["timestamp"],
                actor=push["actor"]["login"],
                actor_is_bot=push["actor"].get("type") == "Bot",
            )
            for push in response.json()
            if push.get("actor")
        ]

    async def ci_failures(self) -> list[CIFailure]:
        """The latest CI failure on each of your open pull requests that has
        one, on its latest commit.

        This covers check runs, from GitHub Actions and other apps, and commit
        statuses, such as pre-commit.ci's or Read the Docs'.
        """
        failures = []
        data = await self._graphql(_MY_OPEN_PULLS_CI)
        for pull in data["search"]["nodes"]:
            if not pull:
                continue
            failed_at = [
                context.get("completedAt") or context.get("createdAt")
                for commit in pull["commits"]["nodes"]
                for context in (
                    (commit["commit"].get("statusCheckRollup") or {}).get("contexts")
                    or {}
                ).get("nodes")
                or []
                if (context.get("conclusion") or context.get("state")) in _FAILED
            ]
            if failed_at := [at for at in failed_at if at]:
                failures.append(
                    CIFailure(
                        repo=pull["repository"]["nameWithOwner"],
                        number=pull["number"],
                        title=pull["title"],
                        updated_at=pull["updatedAt"],
                        failed_at=max(failed_at, key=stamp),
                    )
                )
        return failures

    async def find_discussion(self, repo: str, title: str) -> int | None:
        """The number of the discussion in ``repo`` with exactly this title, or
        None unless there's exactly one."""
        # Search finds titles containing the words; only an exact match counts.
        words = title.replace('"', " ")
        data = await self._graphql(
            _FIND_DISCUSSION, q=f'repo:{repo} in:title "{words}"'
        )
        matches = [n for n in data["search"]["nodes"] if n and n.get("title") == title]
        return matches[0]["number"] if len(matches) == 1 else None

    async def _graphql(self, query: str, **variables) -> dict:
        """The data a GraphQL query returns. Something not found is left out
        of the data, rather than being an error."""
        response = await self._request(
            "POST", "/graphql", json={"query": query, "variables": variables}
        )
        result = response.json()
        errors = [
            error
            for error in result.get("errors") or []
            if error.get("type") != "NOT_FOUND"
        ]
        if errors:
            raise GitHubError(f"GitHub said: {errors[0].get('message')}.")
        return result.get("data") or {}

    async def _newest(self, path: str, since: str | None, time_of) -> list[dict]:
        """Items from an oldest-first list, reading back from the newest page
        until reaching items from ``since`` or earlier."""
        response = await self._request("GET", path, params={"per_page": 100})
        if last_url := response.links.get("last", {}).get("url"):
            response = await self._request("GET", last_url)
        items: list[dict] = []
        for _ in range(MAX_TIMELINE_PAGES):
            page = response.json()
            items[:0] = page
            oldest = next((t for item in page if (t := time_of(item))), None)
            prev_url = response.links.get("prev", {}).get("url")
            if not prev_url or (
                since and oldest is not None and stamp(oldest) <= stamp(since)
            ):
                break
            response = await self._request("GET", prev_url)
        return items

    async def states(self, threads: Sequence[Thread]) -> dict[str, str | None]:
        """Look up whether issues, pull requests and discussions are open,
        merged and so on.

        :returns: Each thread's state (one of `STATES`), by ID, or None for
            one GitHub won't say about, such as in a private repository the
            token can't read. Threads that aren't issues, pull requests or
            discussions are left out.
        """
        subjects = [
            (thread.id, match["repo"], match["kind"], int(match["ref"]))
            for thread in threads
            if (match := _API_SUBJECT.match(thread.url or ""))
            and match["kind"] in _STATE_FIELDS
        ]
        states: dict[str, str | None] = {}
        for start in range(0, len(subjects), STATES_PER_REQUEST):
            batch = subjects[start : start + STATES_PER_REQUEST]
            variables, parameters, fields = {}, [], []
            for index, (_, repo, kind, number) in enumerate(batch):
                owner, name = repo.split("/", 1)
                variables |= {
                    f"o{index}": owner,
                    f"r{index}": name,
                    f"n{index}": number,
                }
                parameters += [
                    f"$o{index}: String!",
                    f"$r{index}: String!",
                    f"$n{index}: Int!",
                ]
                field = _STATE_FIELDS[kind]
                fields.append(
                    f"t{index}: repository(owner: $o{index}, name: $r{index}) "
                    f"{{ {field}(number: $n{index}) {{ {_STATE_SELECTIONS[field]} }} }}"
                )
            query = f"query({', '.join(parameters)}) {{ {' '.join(fields)} }}"
            response = await self._request(
                "POST", "/graphql", json={"query": query, "variables": variables}
            )
            # Repositories it can't read come back as null, with errors.
            data = response.json().get("data")
            if data is None:
                raise GitHubError(
                    "GitHub couldn't say whether threads were open "
                    f"({response.json().get('errors')})."
                )
            for index, (thread_id, _, kind, _) in enumerate(batch):
                repository = data.get(f"t{index}") or {}
                states[thread_id] = _state(repository.get(_STATE_FIELDS[kind]))
        return states

    async def unsubscribe(self, thread_id: str) -> None:
        """Stop notifications for the thread until you're mentioned or comment."""
        if not is_manual(thread_id):
            await self._request(
                "DELETE", f"/notifications/threads/{thread_id}/subscription"
            )
