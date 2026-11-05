"""Bringing GitHub activity into the Store."""

from dataclasses import replace
from datetime import datetime

import httpx2

from propolis.core.activity import needs_response
from propolis.core.github import GitHub, Notification, parse_time
from propolis.core.store import Store


class Sync:
    def __init__(self, store: Store, github: GitHub):
        self._store = store
        self._github = github
        self._me: str | None = None
        self._read_whole_inbox = False

    async def run_once(self) -> None:
        """Fetch what's changed on GitHub and record any activity that counts.

        The first run reads your whole GitHub inbox, and later runs only what's
        changed since.
        """
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
        # GitHub can list a thread after polls have moved past its updated_at,
        # for example one moved back to the inbox after being marked done. So
        # the first sync after Propolis starts reads the whole inbox. That
        # costs pages of notifications, but no timelines for threads that
        # haven't changed. It also brings every thread's cached details up to
        # date.
        since, last_modified = None, None
        if self._read_whole_inbox:
            since = parse_time(self._store.get_meta("notifications_since"))
            last_modified = self._store.get_meta("notifications_last_modified")
        notifications, last_modified = await self._github.notifications(
            since=since, last_modified=last_modified
        )
        if notifications is None:
            return
        for notification in notifications:
            await self._check(notification)
        if notifications:
            since = max(notification.updated_at for notification in notifications)
            self._store.set_meta("notifications_since", since.isoformat())
        if last_modified:
            self._store.set_meta("notifications_last_modified", last_modified)
        self._read_whole_inbox = True

    async def _check(self, notification: Notification) -> None:
        """Put a new thread in the Inbox, or record the latest activity on a
        known one that needs a response."""
        notification = await self._identify(notification)
        info = notification.info
        previous = self._store.info(info.key)
        if info.key not in self._store.state.threads:
            # Everything in your GitHub inbox that Propolis hasn't placed yet
            # starts in the Inbox, whoever acted on it. Threads you've read on
            # GitHub have nothing new.
            self._store.record_activity(info.key, notification.updated_at)
            if not notification.unread:
                self._store.mark_viewed(info.key, at=notification.updated_at)
            checked = notification.updated_at
        elif notification.reason == "ci_activity":
            # CI failures are found by polling your pull requests instead.
            checked = notification.updated_at
        else:
            checked = previous.checked_until if previous else notification.updated_at
            if checked is None or notification.updated_at > checked:
                latest = await self._latest_needing_response(
                    notification, since=checked
                )
                if latest is not None:
                    self._store.record_activity(info.key, latest)
                checked = notification.updated_at
        self._store.save_info(replace(info, checked_until=checked))

    async def _identify(self, notification: Notification) -> Notification:
        """Give a discussion its number, which its notification doesn't carry,
        by finding the one discussion in the repo with its title.

        Unless it's found, it keeps a key made from the notification. So does
        one that's already in Propolis under that key, so it isn't duplicated.
        """
        info = notification.info
        if info.kind != "Discussion" or info.key in self._store.state.threads:
            return notification
        # Remember what was found, so each discussion is searched for once.
        found_key = f"discussion:{info.notification_id}"
        key = self._store.get_meta(found_key)
        if key and (known := self._store.info(key)):
            number, url = known.number, known.url
        elif found := await self._github.find_discussion(info.repo, info.title):
            number, url = found
            key = f"{info.repo}#{number}"
            self._store.set_meta(found_key, key)
        else:
            return notification
        info = replace(info, key=key, number=number, url=url)
        return replace(notification, info=info)

    async def _latest_needing_response(
        self, notification: Notification, since: datetime | None
    ) -> datetime | None:
        info = notification.info
        if info.number is None or info.kind == "Discussion":
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
