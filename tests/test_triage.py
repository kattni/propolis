import pytest

from propolis.core.store import Store
from propolis.core.threads import ThreadInfo
from propolis.core.triage import Triage

pytestmark = pytest.mark.anyio


async def test_triage_writes_back_to_github(tmp_path, github, fake_github):
    thread = "/notifications/threads/91"
    for route in [("PATCH", thread), ("PUT", f"{thread}/subscription"), ("DELETE", thread)]:
        fake_github.routes[route] = {}

    with Store(tmp_path / "propolis.db") as store:
        store.save_info(
            ThreadInfo(
                key="beeware/toga#1",
                kind="Issue",
                repo="beeware/toga",
                title="Thread 1",
                url="https://github.com/beeware/toga/issues/1",
                number=1,
                notification_id="91",
            )
        )
        urgent = store.define_label("High priority", flag_on_activity=True)
        triage = Triage(store, github)

        await triage.label("beeware/toga#1", urgent)
        await triage.done("beeware/toga#1")

    assert fake_github.requests == [
        ("PATCH", thread),
        ("PUT", f"{thread}/subscription"),
        ("DELETE", thread),
    ]
