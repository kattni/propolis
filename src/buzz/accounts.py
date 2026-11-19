"""The GitHub accounts whose notifications Buzz triages on this Mac."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

DEFAULT = "default"
"""The account Buzz started with. Its store and Keychain entries keep the names
they've always had."""


class Accounts:
    """The GitHub accounts you triage. One is open at a time.

    Each has its own store, so its own labels and history, and its own GitHub
    sign-in. The list lives in ``accounts.json``, beside the stores.
    """

    def __init__(self, data: Path):
        self._data = data
        self._file = data / "accounts.json"
        try:
            saved = json.loads(self._file.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            saved = {}
        #: Each account's GitHub login, once Buzz knows it.
        self.names: dict[str, str | None] = saved.get("names") or {DEFAULT: None}
        current = saved.get("current", DEFAULT)
        self.current = current if current in self.names else DEFAULT

    def path(self, account: str) -> Path:
        """Where the account's store is."""
        if account == DEFAULT:
            return self._data / "buzz.sqlite3"
        (self._data / "accounts").mkdir(parents=True, exist_ok=True)
        return self._data / "accounts" / f"{account}.sqlite3"

    def add(self) -> str:
        account = uuid4().hex
        self.names[account] = None
        self._save()
        return account

    def switch(self, account: str) -> None:
        self.current = account
        self._save()

    def name(self, account: str, login: str | None) -> None:
        self.names[account] = login
        self._save()

    def _save(self) -> None:
        self._file.write_text(
            json.dumps({"current": self.current, "names": self.names}),
            encoding="utf-8",
        )


def secret_name(name: str, account: str) -> str:
    """The name of an account's Keychain entry."""
    return name if account == DEFAULT else f"{name}:{account}"
