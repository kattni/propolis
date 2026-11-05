"""Triage actions on threads, mirrored to GitHub where it has an equivalent."""

import logging
import re
from collections.abc import Awaitable, Callable

import httpx2

from propolis.core.github import GitHub
from propolis.core.store import Store

logger = logging.getLogger(__name__)

_REFERENCE = re.compile(r"(?P<repo>[\w.-]+/[\w.-]+)#(?P<number>\d+)")


class AddError(Exception):
    """Why a thread couldn't be added, in words for the user."""


class Triage:
    def __init__(self, store: Store, github: GitHub):
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
            info = await self._github.issue(repo, number)
        except httpx2.HTTPStatusError as error:
            if error.response.status_code not in (404, 410):
                raise
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
        await self._write_back(self._github.mark_read, key)
        if self._store.state.labels[label_id].flag_on_activity:
            await self._write_back(self._github.subscribe, key)

    async def unlabel(self, key: str, label_id: str) -> None:
        self._store.unlabel(key, label_id)

    async def view(self, key: str) -> None:
        self._store.mark_viewed(key)
        await self._write_back(self._github.mark_read, key)

    async def done(self, key: str) -> None:
        self._store.mark_done(key)
        await self._write_back(self._github.mark_done, key)

    async def _write_back(
        self, call: Callable[[str], Awaitable[None]], key: str
    ) -> None:
        """Best effort: the change in Propolis stands even if GitHub's fails."""
        info = self._store.info(key)
        # Threads found only through CI polling have no notification yet.
        if info is None or info.notification_id is None:
            return
        try:
            await call(info.notification_id)
        except httpx2.HTTPError as error:
            logger.warning("Couldn't update %s on GitHub: %s", key, error)
