"""Triage actions on threads, mirrored to GitHub where it has an equivalent."""

import logging
import re
from collections.abc import Awaitable, Callable

import httpx2

from propolis.core.events import Event, Labelled, MarkedDone, Viewed
from propolis.core.github import GitHub
from propolis.core.store import Store

logger = logging.getLogger(__name__)

_REFERENCE = re.compile(r"(?P<repo>[\w.-]+/[\w.-]+)#(?P<number>\d+)")


class AddError(Exception):
    """Why a thread couldn't be added, in words for the user."""


class Triage:
    def __init__(self, store: Store, github: GitHub | None):
        """Without ``github``, this device isn't signed in, so triage reaches
        GitHub through another device that is."""
        self._store = store
        self._github = github

    async def add(self, reference: str) -> str:
        """Add an issue or pull request, given as ``owner/repo#123``, to the
        Inbox. One you've already filed stays where it is. Returns its key."""
        reference = reference.strip()
        if not (match := _REFERENCE.fullmatch(reference)):
            raise AddError("Enter it as owner/repo#123.")
        repo, number = match["repo"], int(match["number"])
        try:
            if self._github is None:
                # GitHub's public API has issues and pull requests in public
                # repos, without signing in.
                async with GitHub(None) as github:
                    info = await github.issue(repo, number)
            else:
                info = await self._github.issue(repo, number)
        except httpx2.HTTPStatusError as error:
            if error.response.status_code not in (404, 410):
                raise
            if self._github is None:
                raise AddError(
                    f"Couldn't find {reference}. This Mac isn't signed in to "
                    "GitHub, so it can add issues and pull requests in public "
                    "repos, but not discussions."
                ) from error
            # Discussions are numbered with issues, but aren't in the issues API.
            if (info := await self._github.discussion(repo, number)) is None:
                raise AddError(
                    f"Couldn't find {reference}. Propolis can add issues, pull "
                    "requests and discussions in public repos."
                ) from error
        thread = self._store.state.threads.get(info.key)
        in_a_view = (
            thread is not None
            and not thread.done
            and (bool(thread.labels) or thread.last_activity is not None)
        )
        if not in_a_view:
            self._store.add_to_inbox(info.key)
        # Keep details from a notification, which are richer.
        if self._store.info(info.key) is None:
            self._store.save_info(info)
        return info.key

    async def label(self, key: str, label_id: str) -> None:
        """Label a thread, marking it read. A flagging label also subscribes
        you to the thread, so its activity reaches Propolis."""
        self._store.label(key, label_id)
        await self._write_back(GitHub.mark_read, key)
        if self._store.state.labels[label_id].flag_on_activity:
            await self._write_back(GitHub.subscribe, key)

    async def unlabel(self, key: str, label_id: str) -> None:
        self._store.unlabel(key, label_id)

    async def view(self, key: str) -> None:
        self._store.mark_viewed(key)
        await self._write_back(GitHub.mark_read, key)

    async def done(self, key: str) -> None:
        self._store.mark_done(key)
        await self._write_back(GitHub.mark_done, key)

    async def write_back_events(self, events: list[Event]) -> None:
        """Make your other devices' triage on GitHub too, as far as it still
        stands. If they're signed in, they've done it already, but doing it
        again does no harm."""
        state = self._store.state
        labelled = {event.thread_id for event in events if isinstance(event, Labelled)}
        triaged = {
            event.thread_id
            for event in events
            if isinstance(event, (Labelled, Viewed, MarkedDone))
        }
        for key in sorted(triaged):
            # A label deleted before it was applied leaves nothing to write.
            if (thread := state.threads.get(key)) is None:
                continue
            if thread.done:
                await self._write_back(GitHub.mark_done, key)
            elif not thread.new_activity:
                # Not if there's been activity since, which you haven't seen.
                await self._write_back(GitHub.mark_read, key)
            if key in labelled and any(
                state.labels[label_id].flag_on_activity for label_id in thread.labels
            ):
                await self._write_back(GitHub.subscribe, key)

    async def _write_back(
        self, call: Callable[[GitHub, str], Awaitable[None]], key: str
    ) -> None:
        """Best effort: the change in Propolis stands even if GitHub's fails."""
        if self._github is None:
            return
        info = self._store.info(key)
        # Threads found only through CI polling have no notification yet.
        if info is None or info.notification_id is None:
            return
        try:
            await call(self._github, info.notification_id)
        except httpx2.HTTPError as error:
            logger.warning("Couldn't update %s on GitHub: %s", key, error)
