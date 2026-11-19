"""Bringing what's happened on GitHub into the store."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace

from .github import (
    MIN_POLL_INTERVAL,
    AuthenticationError,
    GitHubClient,
    GitHubError,
    api_url,
    manual_reference,
)
from .store import MANUAL_PREFIX, Store, Thread, stamp

logger = logging.getLogger(__name__)

#: How many requests to send GitHub at once when checking many threads.
CONCURRENCY = 8
#: Look up the state of at most this many threads each time GitHub is checked.
STATE_LOOKUPS = 200


class WrongSignIn(Exception):
    """The store is another GitHub account's than the one signed in."""


class Sync:
    """Keeps a store up to date with GitHub."""

    def __init__(self, store: Store, client: GitHubClient):
        self.store = store
        self.client = client
        #: The signed-in GitHub login, once it's been checked.
        self.me: str | None = None
        #: How long GitHub asks to wait before checking again, in seconds.
        self.poll_interval = MIN_POLL_INTERVAL
        # GitHub can list a thread after checks have moved past its updated_at,
        # for example one moved back to the inbox after being marked as done.
        # So the first sync reads the whole inbox, and later ones only what's
        # changed.
        self._read_whole_inbox = True

    async def run_once(self, check_added: bool = False) -> None:
        """Fetch what's changed on GitHub, and record any activity that needs a
        response from you.

        :param check_added: Whether to check threads added by hand, which
            GitHub doesn't send notifications about, for activity too.
        :raises WrongSignIn: If GitHub is signed in as a different account from
            the one the store is for.
        """
        if self.me is None:
            me = await self.client.validate()
            # Its history is one account's, and can't be taken apart again.
            known = self.store.get_meta("login")
            if known and known.casefold() != me.casefold():
                raise WrongSignIn(
                    f"This account is {known}'s, but GitHub signed in as {me}. "
                    f"Sign in as {known}, or add an account for {me} with File → "
                    "Add Account…."
                )
            self.store.set_meta("login", me)
            self.me = me
        await self.sync_inbox()
        await self.sync_ci()
        if check_added:
            await self.check_added()
        await self.look_up_states()

    async def sync_inbox(self) -> None:
        """Put threads that are new in GitHub's inbox in Propolis's, and check
        the ones Propolis has for activity, if GitHub says they've been
        updated."""
        store, full = self.store, self._read_whole_inbox
        snapshot = await self.client.inbox(
            None if full else store.get_meta("last_modified"),
            None if full else store.get_meta("notifications_since"),
        )
        self.poll_interval = snapshot.poll_interval
        if snapshot.threads is None:
            return

        threads = await _gather([self.identify(t) for t in snapshot.threads])
        to_check = []
        with store.batch():
            self._place(threads, to_check)

        results = await _gather(
            [
                self.client.latest_activity(thread, since, self.me)
                for thread, since in to_check
            ],
            return_exceptions=True,
        )
        checked = True
        with store.batch():
            for (thread, _), result in zip(to_check, results):
                if isinstance(result, AuthenticationError):
                    raise result
                if isinstance(result, Exception):
                    logger.warning(
                        "Couldn't check %s for activity: %s", thread.id, result
                    )
                    checked = False
                    continue
                if result is not None:
                    store.record_activity(thread.id, result)
                store.set_checked(thread.id, thread.updated_at)
        if full:
            store.mark_gone(thread.id for thread in threads)
        # If a thread couldn't be checked, look at the same threads next time.
        if checked:
            if threads:
                since = max((thread.updated_at for thread in threads), key=stamp)
                store.set_meta("notifications_since", since)
            store.set_meta("last_modified", snapshot.last_modified)
        self._read_whole_inbox = False

    def _place(self, threads: list[Thread], to_check: list) -> None:
        """Put new threads in the inbox, and add the others to ``to_check`` if
        they need checking for activity."""
        store = self.store
        for thread in threads:
            known = store.get(thread.id)
            if known is None and (manual := store.thread_with_url(thread.url, True)):
                # A notification for a thread added by hand takes its place.
                store.adopt(manual, thread)
                known = store.get(thread.id)
            if known is None:
                # Everything in GitHub's inbox that Propolis hasn't had before
                # starts in the inbox, whoever was active on it.
                store.place([thread])
                continue
            store.update_details([thread])
            if known.checked_until is None or (
                stamp(thread.updated_at) > known.checked_until
            ):
                to_check.append((thread, known.checked_until))

    async def identify(self, thread: Thread) -> Thread:
        """Give a discussion's notification the discussion's URL, which it
        doesn't carry, by finding the one discussion in the repository with
        its title. Each discussion is searched for once, and again if its
        title changes while it isn't found."""
        if thread.type != "Discussion" or thread.url:
            return thread
        key = f"discussion:{thread.id}"
        found = json.loads(self.store.get_meta(key) or "{}")
        if found.get("url"):
            return replace(thread, url=found["url"])
        if found.get("title") == thread.title:
            return thread
        try:
            number = await self.client.find_discussion(thread.repo, thread.title)
        except GitHubError as exc:
            logger.warning("Couldn't look for discussion %s: %s", thread.title, exc)
            return thread
        url = api_url(thread.repo, "discussions", number) if number else None
        self.store.set_meta(key, json.dumps({"url": url, "title": thread.title}))
        return replace(thread, url=url) if url else thread

    async def sync_ci(self) -> None:
        """Count a CI failure on one of your open pull requests as activity on
        it, at the time the check failed. One Propolis doesn't have yet goes in
        the inbox, with the reason "Failing CI".

        The same failure, seen again, is the same activity, so it only counts
        once.
        """
        store = self.store
        try:
            failures = await self.client.ci_failures()
        except GitHubError as exc:
            logger.warning("Couldn't check your pull requests' CI: %s", exc)
            return
        with store.batch():
            for failure in failures:
                self._record_failure(failure)

    def _record_failure(self, failure) -> None:
        store = self.store
        ref = f"{failure.repo}#{failure.number}"
        if (thread_id := store.thread_with_url(failure.url)) is None:
            thread_id = store.add_manual(
                Thread(
                    id=f"{MANUAL_PREFIX}{ref}",
                    repo=failure.repo,
                    repo_url=f"https://github.com/{failure.repo}",
                    title=failure.title,
                    type="PullRequest",
                    url=failure.url,
                    latest_comment_url=None,
                    reason="ci_failure",
                    unread=False,
                    updated_at=failure.updated_at,
                ),
                seen=False,
            )
        store.record_activity(thread_id, failure.failed_at)

    async def check_added(self) -> None:
        """Look for activity on threads added by hand, which GitHub doesn't
        send notifications about."""
        store, client = self.store, self.client

        async def check(thread: Thread):
            try:
                fresh = await client.lookup(*manual_reference(thread.id))
                if thread.checked_until and (
                    stamp(fresh.updated_at) <= thread.checked_until
                ):
                    return None
                latest = await client.latest_activity(
                    replace(thread, updated_at=fresh.updated_at),
                    thread.checked_until,
                    self.me,
                )
            except GitHubError:
                return None  # Deleted, or no longer readable; try again next time.
            # Keep the ID even if the repository has been renamed.
            return replace(fresh, id=thread.id), latest

        found = await _gather(
            [check(thread) for thread in store.manual_threads_to_check()]
        )
        with store.batch():
            for fresh, latest in filter(None, found):
                store.update_manual(fresh, latest)

    async def look_up_states(self) -> None:
        """Find out whether issues, pull requests and discussions with new
        activity are open, merged and so on, for their icons."""
        store = self.store
        if not (threads := store.threads_to_look_up(STATE_LOOKUPS)):
            return
        try:
            states = await self.client.states(threads)
        except GitHubError as exc:
            # The icons just stay grey until the next check.
            logger.warning("Couldn't look up threads' states: %s", exc)
            return
        store.set_states(
            (thread.id, states.get(thread.id), thread.updated_at) for thread in threads
        )


async def _gather(calls, return_exceptions: bool = False) -> list:
    """Await calls, a few at a time."""
    limit = asyncio.Semaphore(CONCURRENCY)

    async def one(call):
        async with limit:
            return await call

    return await asyncio.gather(
        *(one(call) for call in calls), return_exceptions=return_exceptions
    )
