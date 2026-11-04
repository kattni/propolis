from datetime import UTC, datetime, timedelta

import pytest

from propolis.core.state import ATTENTION
from propolis.core.store import Store


class Clock:
    """A clock that moves forward a minute every time it's read."""

    def __init__(self):
        self.now = datetime(2026, 11, 3, 12, tzinfo=UTC)

    def __call__(self):
        self.now += timedelta(minutes=1)
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    with Store(tmp_path / "propolis.db", clock=clock) as store:
        yield store


def test_labels_keep_triage_order(store, clock):
    later = store.define_label("Later")
    for thread in ("a", "b", "c"):
        store.record_activity(thread, clock())

    store.label("b", later)
    store.label("a", later)
    store.label("b", later)

    assert store.state.labelled(later) == ["b", "a"]
    assert store.state.inbox() == ["c"]


def test_activity_flags_only_flagging_labels(store, clock):
    urgent = store.define_label("High priority", flag_on_activity=True)
    deep = store.define_label("Needs deep analysis")
    store.label("a", urgent)
    store.label("b", deep)

    store.record_activity("a", clock())
    store.record_activity("b", clock())

    a, b = store.state.threads["a"], store.state.threads["b"]
    assert a.labels.keys() == {urgent, ATTENTION}
    assert b.labels.keys() == {deep}
    assert b.new_activity

    store.mark_viewed("b")
    assert not b.new_activity

    store.unlabel("a", ATTENTION)
    assert a.labels.keys() == {urgent}


def test_done_clears_labels_until_new_activity(store, clock):
    deep = store.define_label("Needs deep analysis")
    store.record_activity("a", clock())
    store.record_activity("b", clock())
    store.label("a", deep)

    store.mark_done("a")
    assert store.state.labelled(deep) == []
    assert store.state.inbox() == ["b"]

    store.record_activity("a", clock())
    assert store.state.inbox() == ["a", "b"]


def test_reopening_rebuilds_the_same_state(store, clock):
    urgent = store.define_label("High priority", flag_on_activity=True)
    before_labelling = clock()
    store.label("a", urgent)

    # Detected after labelling, and then again by another device.
    store.record_activity("a", before_labelling)
    store.record_activity("a", before_labelling)

    assert store.state.threads["a"].labels.keys() == {urgent}
    with Store(store.path) as reopened:
        assert reopened.state == store.state
