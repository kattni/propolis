"""Triage actions on threads, mirrored to GitHub where it has an equivalent."""

import logging
from collections.abc import Awaitable, Callable

import httpx2

from propolis.core.github import GitHub
from propolis.core.store import Store

logger = logging.getLogger(__name__)


class Triage:
    def __init__(self, store: Store, github: GitHub):
        self._store = store
        self._github = github

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
