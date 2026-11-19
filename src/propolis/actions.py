"""Changing threads in Propolis, and on GitHub where it has an equivalent.

Changes on GitHub are best effort: if one fails, the change in Propolis stands,
and the failure is logged.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable

from .github import parse_reference
from .store import Label, Store, Thread

logger = logging.getLogger(__name__)

#: How many requests to send at once when changing many threads.
CONCURRENCY = 8


async def add_thread(
    store: Store, client, text: str, label_id: str | None = None
) -> Thread:
    """Add an issue, pull request or discussion by hand.

    :param text: Like ``beeware/toga#123``, or a link to it on github.com.
    :param label_id: The label to put it in; without one, it goes to the inbox.
    :raises ValueError: If ``text`` isn't a reference Propolis understands.
    :raises GitHubError: If GitHub can't find it.
    """
    repo, number = parse_reference(text)
    thread_id = store.add_manual(await client.lookup(repo, number))
    if label_id is not None:
        store.set_label(label_id, [thread_id], on=True)
    return store.get(thread_id)


async def write_back(
    client,
    thread_ids: Iterable[str],
    change: Callable[[object, str], Awaitable[None]],
) -> list[tuple[str, Exception]]:
    """Make a change for each thread with ``client``, one request each.

    :returns: Each thread that couldn't be changed, with the reason. They're
        logged too.
    """
    limit = asyncio.Semaphore(CONCURRENCY)

    async def send(thread_id: str):
        async with limit:
            await change(client, thread_id)

    thread_ids = list(thread_ids)
    results = await asyncio.gather(
        *(send(thread_id) for thread_id in thread_ids), return_exceptions=True
    )
    failures = [
        (thread_id, result)
        for thread_id, result in zip(thread_ids, results)
        if isinstance(result, Exception)
    ]
    for thread_id, exc in failures:
        logger.warning("Couldn't update %s: %s", thread_id, exc)
    return failures


async def apply_to_threads(
    store: Store,
    client,
    threads: list[Thread],
    local: Callable[[Iterable[str]], None],
    remote: Callable[[object, str], Awaitable[None]],
    after_local: Callable[[], None] | None = None,
) -> list[tuple[str, Exception]]:
    """Change threads in the store straight away, then with ``client``.

    :param local: Makes the change in the store, given the thread IDs.
    :param remote: Makes the change for one thread, given the client and ID.
    :param after_local: Called once the store has changed, before any requests.
    :returns: Each thread whose request failed, with the reason. The change in
        the store stands.
    """
    if not threads:
        return []
    local([t.id for t in threads])
    if after_local is not None:
        after_local()
    return await write_back(client, [t.id for t in threads], remote)


def to_view(threads: Iterable[Thread]) -> list[Thread]:
    """The threads that viewing would change: those with something new on them
    in Propolis, or that are unread on GitHub."""
    return [thread for thread in threads if thread.new or thread.unread]


async def label_threads(
    store: Store,
    client,
    label: Label,
    threads: list[Thread],
    after_local: Callable[[], None] | None = None,
) -> list[tuple[str, Exception]]:
    """Label threads, which marks them read. A label that flags on activity
    also subscribes you to them, so their activity keeps reaching you.

    :param client: GitHub, or None to change only the store.
    """
    unread = {thread.id for thread in threads if thread.unread}
    store.set_label(label.id, [thread.id for thread in threads], on=True)
    if after_local is not None:
        after_local()
    if client is None:
        return []

    async def change(client, thread_id: str):
        if thread_id in unread:
            await client.mark_read(thread_id)
        if label.flag:
            await client.subscribe(thread_id)

    return await write_back(client, [thread.id for thread in threads], change)
