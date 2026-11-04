"""Bringing GitHub activity into the Store."""

from dataclasses import replace
from datetime import datetime

import httpx2

from propolis.core.activity import needs_response
from propolis.core.github import GitHub, Notification, parse_time
from propolis.core.store import Store

CACHE_VERSION = "2"
"""Bump when ThreadInfo gains a field. 2: the notification reason."""


class Sync:
    def __init__(self, store: Store, github: GitHub):
        self._store = store
        self._github = github
        self._me: str | None = None

    async def run_once(self) -> None:
        """Fetch what's changed on GitHub and record any activity that counts."""
        if self._me is None:
            self._me = await self._github.me()
        await self._sync_notifications()
        await self._sync_ci()

    async def _sync_ci(self) -> None:
        # A failure keeps the timestamp of the check that failed, so seeing it
        # again on the next poll records nothing new.
        for info, activity in await self._github.ci_failures():
            if needs_response(activity, self._me):
                self._store.record_activity(info.key, activity.at)
            # Keep details from a notification, which are richer. Refresh
            # those of threads known only through CI polling.
            previous = self._store.info(info.key)
            if previous is None or previous.notification_id is None:
                self._store.save_info(info)

    async def _sync_notifications(self) -> None:
        # When the cached thread details gain a field, refetch every
        # notification once so the details catch up. That costs pages of
        # notifications, but no timelines, as nothing has changed.
        refetch = self._store.get_meta("cache_version") != CACHE_VERSION
        since = parse_time(self._store.get_meta("notifications_since"))
        last_modified = self._store.get_meta("notifications_last_modified")
        notifications, last_modified = await self._github.notifications(
            since=None if refetch else since,
            last_modified=None if refetch else last_modified,
        )
        if notifications is None:
            return
        for notification in notifications:
            # CI failures are found by polling your pull requests instead.
            if notification.reason != "ci_activity":
                await self._check(notification)
        if notifications:
            since = max(notification.updated_at for notification in notifications)
            self._store.set_meta("notifications_since", since.isoformat())
        if last_modified:
            self._store.set_meta("notifications_last_modified", last_modified)
        self._store.set_meta("cache_version", CACHE_VERSION)

    async def _check(self, notification: Notification) -> None:
        """Record the latest activity on a thread that needs a response."""
        info = notification.info
        previous = self._store.info(info.key)
        if previous:
            checked = previous.checked_until
        elif notification.unread:
            # The first time a thread is seen, start from when you last read it.
            checked = notification.last_read_at
        else:
            # You've read everything on it already.
            checked = notification.updated_at
        if checked is None or notification.updated_at > checked:
            latest = await self._latest_needing_response(notification, since=checked)
            if latest is not None:
                self._store.record_activity(info.key, latest)
            checked = notification.updated_at
        self._store.save_info(replace(info, checked_until=checked))

    async def _latest_needing_response(
        self, notification: Notification, since: datetime | None
    ) -> datetime | None:
        info = notification.info
        if info.number is None:
            # Discussions and releases have no timeline to check, so any
            # update counts.
            return notification.updated_at
        try:
            activities = await self._github.timeline(info.repo, info.number)
        except httpx2.HTTPStatusError as error:
            if error.response.status_code != 404:
                raise
            # A private repo: Propolis doesn't ask for the repo scope, so
            # GitHub hides the timeline. With no way to tell who acted, any
            # update counts.
            return notification.updated_at
        return max(
            (
                activity.at
                for activity in activities
                if (since is None or activity.at > since)
                and needs_response(activity, self._me)
            ),
            default=None,
        )
