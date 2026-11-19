"""What Propolis knows about each thread from GitHub."""

import json
from dataclasses import asdict, dataclass, fields
from datetime import datetime


@dataclass(frozen=True)
class ThreadInfo:
    """A thread's details, cached locally. Each device fetches its own."""

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
    """How far this device has checked the thread for activity."""
    reason: str | None = None
    """GitHub's latest reason for notifying you, such as ``mention``, or
    ``ci_failure`` for a pull request found only through CI polling."""

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    def to_dict(self) -> dict:
        data = asdict(self)
        if self.checked_until:
            data["checked_until"] = self.checked_until.isoformat()
        return data

    @classmethod
    def from_json(cls, text: str) -> "ThreadInfo":
        return cls.from_dict(json.loads(text))

    @classmethod
    def from_dict(cls, data: dict) -> "ThreadInfo":
        # Ignore anything a newer version of Propolis on another Mac added.
        known = {field.name for field in fields(cls)}
        data = {name: value for name, value in data.items() if name in known}
        if data.get("checked_until"):
            data["checked_until"] = datetime.fromisoformat(data["checked_until"])
        return cls(**data)
