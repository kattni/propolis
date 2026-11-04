from datetime import UTC, datetime, timedelta
from itertools import count

from propolis.core.store import Store
from propolis.core.threads import ThreadInfo
from propolis.views import INBOX, rows, sidebar


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
            ("Later", 7, 1, 1),
        ]
