"""The GitHub accounts whose notifications Propolis triages on this device."""

import json
from pathlib import Path
from uuid import uuid4

DEFAULT = "default"
"""The account Propolis started with. Its store keeps its original place."""


class Accounts:
    """Each account has its own store, and so its own labels, history and sync
    folder, and its own GitHub sign-in. One is open at a time.

    The list lives in ``accounts.json`` beside the stores.
    """

    def __init__(self, data: Path):
        self._data = data
        self._file = data / "accounts.json"
        try:
            saved = json.loads(self._file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            saved = {}
        self.names: dict[str, str | None] = saved.get("names") or {DEFAULT: None}
        """Each account's GitHub login, once Propolis knows it."""
        current = saved.get("current", DEFAULT)
        self.current = current if current in self.names else DEFAULT

    def path(self, account: str) -> Path:
        """Where the account's store is."""
        if account == DEFAULT:
            return self._data / "propolis.db"
        (self._data / "accounts").mkdir(parents=True, exist_ok=True)
        return self._data / "accounts" / f"{account}.db"

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
