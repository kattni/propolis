"""What Propolis knows about each thread from GitHub."""

import json
from dataclasses import asdict, dataclass, fields
from datetime import datetime

_TIMES = ("checked_until", "fetched_at")


@dataclass(frozen=True)
class ThreadInfo:
    """A thread's details, cached locally and shared with your other devices."""

    key: str
    """``owner/repo#123`` for issues and pull requests; otherwise derived from
    GitHub's notification thread ID."""
    kind: str
    """GitHub's subject type: ``Issue``, ``PullRequest``, ``Discussion``..."""
    repo: str
    title: str
    url: str
    number: int | None = None
    notification_id: str | None = None
    checked_until: datetime | None = None
    """How far the thread has been checked for activity."""
    reason: str | None = None
    """GitHub's latest reason for notifying you, such as ``mention``, or
    ``ci_failure`` for a pull request found only through CI polling."""
    fetched_at: datetime | None = None
    """When these details were fetched from GitHub, here or on the device that
    shared them. Fetching the same details again keeps the time."""

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    def to_dict(self) -> dict:
        data = asdict(self)
        for name in _TIMES:
            if data[name]:
                data[name] = data[name].isoformat()
        return data

    @classmethod
    def from_json(cls, text: str) -> "ThreadInfo":
        return cls.from_dict(json.loads(text))

    @classmethod
    def from_dict(cls, data: dict) -> "ThreadInfo":
        # Ignore anything a newer version of Propolis on another device added.
        known = {field.name for field in fields(cls)}
        data = {name: value for name, value in data.items() if name in known}
        for name in _TIMES:
            if data.get(name):
                data[name] = datetime.fromisoformat(data[name])
        return cls(**data)
