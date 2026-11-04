from datetime import UTC, datetime

import pytest

from propolis.core.activity import Activity, needs_response

AT = datetime(2026, 11, 3, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("activity", "expected"),
    [
        (Activity(AT, actor="freakboy3742"), True),
        (Activity(AT, actor="Kattni"), False),
        (Activity(AT, actor="codecov[bot]", actor_is_bot=True), False),
        (
            Activity(
                AT, actor="codecov[bot]", actor_is_bot=True, mentions={"kattni"}
            ),
            True,
        ),
        (
            Activity(
                AT,
                actor="some-bot[bot]",
                actor_is_bot=True,
                review_requested_from="kattni",
            ),
            True,
        ),
        (
            Activity(
                AT, actor="some-bot[bot]", actor_is_bot=True, assigned_to="kattni"
            ),
            True,
        ),
        (
            Activity(
                AT, actor="github-actions[bot]", actor_is_bot=True, ci_failed=True
            ),
            True,
        ),
    ],
    ids=[
        "someone-else",
        "me",
        "bot-not-aimed-at-me",
        "bot-mentions-me",
        "bot-requests-my-review",
        "bot-assigns-me",
        "ci-failed-on-my-pr",
    ],
)
def test_needs_response(activity, expected):
    assert needs_response(activity, me="kattni") is expected
