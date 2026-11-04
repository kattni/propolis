from datetime import UTC, datetime, timedelta
from itertools import count

from propolis.core.store import Store
from propolis.core.threads import ThreadInfo
from propolis.views import INBOX, Filters, filter_options, rows, sidebar


def test_views_show_counts_and_thread_details(tmp_path):
    ticks = (datetime(2026, 11, 2, tzinfo=UTC) + timedelta(minutes=n) for n in count())
    with Store(tmp_path / "propolis.db", clock=lambda: next(ticks)) as store:
        for number, kind in [(1, "PullRequest"), (2, "Issue")]:
            key = f"beeware/toga#{number}"
            store.save_info(
                ThreadInfo(
                    key=key,
                    kind=kind,
                    repo="beeware/toga",
                    title=f"Thread {number}",
                    url=f"https://github.com/beeware/toga/issues/{number}",
                    number=number,
                )
            )
            store.record_activity(key, datetime(2026, 11, 1, number, tzinfo=UTC))
        later = store.define_label("Later")
        store.label("beeware/toga#2", later)

        [row] = rows(store, INBOX)
        assert (row.title, row.repo, row.kind) == (
            "● Thread 1",
            "beeware/toga",
            "Pull request",
        )
        # Labelling a thread counts as seeing it, so there's no marker.
        [row] = rows(store, later)
        assert row.title == "Thread 2"

        store.record_activity("beeware/toga#2", datetime(2026, 11, 3, tzinfo=UTC))

        # The number key that applies each label, its threads, and how many
        # of them have new activity.
        assert [
            (item.name, item.number, item.count, item.new) for item in sidebar(store)
        ] == [
            ("Inbox", None, 1, 1),
            ("Attention required", None, 0, 0),
            ("Needs review", 1, 0, 0),
            ("Question", 2, 0, 0),
            ("Deep response", 3, 0, 0),
            ("Blocked", 4, 0, 0),
            ("Low priority", 5, 0, 0),
            ("High priority", 6, 0, 0),
            ("In progress", 7, 0, 0),
            ("Cold storage", 8, 0, 0),
            ("Later", 9, 1, 1),
        ]


def test_filters_narrow_the_thread_list(tmp_path):
    with Store(tmp_path / "propolis.db") as store:
        for number, repo, reason, kind in [
            (1, "beeware/toga", "mention", "Issue"),
            (2, "beeware/toga", "subscribed", "PullRequest"),
            (3, "beeware/briefcase", "mention", "PullRequest"),
        ]:
            key = f"{repo}#{number}"
            store.save_info(
                ThreadInfo(
                    key=key,
                    kind=kind,
                    repo=repo,
                    title=f"Thread {number}",
                    url=f"https://github.com/{repo}/issues/{number}",
                    number=number,
                    reason=reason,
                )
            )
            store.record_activity(key, datetime(2026, 11, 1, number, tzinfo=UTC))

        options = filter_options(store)
        assert options.repos == ["beeware/briefcase", "beeware/toga"]
        assert options.reasons == [("mention", "Mentioned"), ("subscribed", "Watching")]
        assert options.kinds == [("Issue", "Issue"), ("PullRequest", "Pull request")]

        def numbers(**filters):
            return [row.key[-1] for row in rows(store, INBOX, Filters(**filters))]

        assert numbers(repo="beeware/toga") == ["2", "1"]
        assert numbers(reason="mention") == ["3", "1"]
        assert numbers(repo="beeware/toga", kind="PullRequest") == ["2"]
