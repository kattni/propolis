"""Deciding whether activity on a thread needs a response from you."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass


@dataclass(frozen=True)
class Activity:
    """One thing that happened on a thread, as GitHub reports it."""

    #: When it happened, as an ISO 8601 time.
    at: str
    #: Who did it.
    actor: str
    actor_is_bot: bool = False
    #: Who it @mentions.
    mentions: Collection[str] = ()
    review_requested_from: str | None = None
    assigned_to: str | None = None
    #: A failing CI run on one of your pull requests.
    ci_failed: bool = False


def needs_response(activity: Activity, me: str) -> bool:
    """Whether ``activity`` needs a response from the user ``me``.

    Other people's activity counts, and your own never does. A bot's activity
    counts only when it's aimed at you: it mentions you, requests your review,
    or assigns you. A failing CI run on your pull request always counts. GitHub
    logins are case-insensitive.
    """
    if activity.ci_failed:
        return True
    me = me.casefold()
    if activity.actor.casefold() == me:
        return False
    if not activity.actor_is_bot:
        return True
    return (
        me in {login.casefold() for login in activity.mentions}
        or (activity.review_requested_from or "").casefold() == me
        or (activity.assigned_to or "").casefold() == me
    )
