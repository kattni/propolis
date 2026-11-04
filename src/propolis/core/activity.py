"""Deciding whether activity on a thread needs a response from you."""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Activity:
    """One thing that happened on a thread, as reported by GitHub.

    The GitHub client builds these from a thread's timeline. It reports CI
    failures (``ci_failed``) only for your own pull requests.
    """

    at: datetime
    actor: str
    actor_is_bot: bool = False
    mentions: Collection[str] = ()
    review_requested_from: str | None = None
    assigned_to: str | None = None
    ci_failed: bool = False


def needs_response(activity: Activity, me: str) -> bool:
    """Whether ``activity`` needs a response from the user ``me``.

    Other people's activity always counts, and your own never does. A bot's
    activity counts only when it is aimed at you: it mentions you, requests
    your review or assigns you. GitHub logins are case-insensitive.
    """
    me = me.casefold()
    if activity.ci_failed:
        return True
    if activity.actor.casefold() == me:
        return False
    if not activity.actor_is_bot:
        return True
    return (
        me in {login.casefold() for login in activity.mentions}
        or (activity.review_requested_from or "").casefold() == me
        or (activity.assigned_to or "").casefold() == me
    )
