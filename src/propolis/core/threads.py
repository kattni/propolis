"""What Propolis knows about each thread from GitHub."""

import json
from dataclasses import asdict, dataclass
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

    def to_json(self) -> str:
        data = asdict(self)
        if self.checked_until:
            data["checked_until"] = self.checked_until.isoformat()
        return json.dumps(data)

    @classmethod
    def from_json(cls, text: str) -> "ThreadInfo":
        data = json.loads(text)
        if data["checked_until"]:
            data["checked_until"] = datetime.fromisoformat(data["checked_until"])
        return cls(**data)
