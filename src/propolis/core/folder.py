"""Syncing with your other devices through a folder they share."""

import json
import logging
import os
from pathlib import Path

from propolis.core.events import Event, from_json, to_json
from propolis.core.store import Store
from propolis.core.threads import ThreadInfo

logger = logging.getLogger(__name__)

_EVENTS = ".events.jsonl"
_THREADS = ".threads.json"
_ACCOUNT = "account.json"


class WrongAccount(Exception):
    """The folder syncs a different GitHub account's notifications."""


class NotReady(Exception):
    """The folder can't be synced with yet. Try again later."""


class SharedFolder:
    """A folder that a syncing service, such as iCloud Drive, Dropbox or
    Syncthing, keeps the same on each of your devices.

    Each device writes only its own two files, named after its device ID, so
    the syncing service never has two versions of a file to reconcile:

    - ``<device>.events.jsonl``: the device's events, one per line, appended
      as it syncs.
    - ``<device>.threads.json``: the details of every thread it knows.

    A device that isn't signed in to GitHub gets everything from these files.

    The folder is for one GitHub account's notifications. ``account.json``
    names it, written once by the first device signed in to GitHub to use the
    folder. Merged events can't be taken apart again, so a device whose store
    is for another account is turned away before anything is merged.
    """

    def __init__(self, store: Store, path: Path):
        self.path = path
        self._store = store
        self._events_file = path / f"{store.device}{_EVENTS}"
        self._threads_file = path / f"{store.device}{_THREADS}"
        self._in_folder: set[str] | None = None
        """The IDs of the events in the folder, from any device. Unknown until
        the first pull, which reads every file in full."""
        self._read_to: dict[Path, int] = {}
        """How far each event log has been read."""
        self._threads_read: dict[Path, tuple[int, int]] = {}
        """Each threads file's modification time and size when last read."""
        self._threads_written: str | None = None

    def pull(self, signed_in_as: str | None = None) -> list[Event]:
        """Bring in your other devices' events and thread details. Returns the
        events that are new here.

        ``signed_in_as`` is the GitHub login this device is signed in as, if
        it is. Raises ``WrongAccount`` if the folder is another account's.
        """
        if not self.path.is_dir():
            raise FileNotFoundError(f"The sync folder {self.path} isn't there.")
        self._check_account(signed_in_as)
        events = [
            event
            for log in sorted(self.path.glob(f"*{_EVENTS}"))
            for event in self._read_new_events(log)
        ]
        self._in_folder = (self._in_folder or set()) | {event.id for event in events}
        self._store.merge_info(
            info
            for path in sorted(self.path.glob(f"*{_THREADS}"))
            if path != self._threads_file
            for info in self._read_threads(path)
        )
        return self._store.merge(events)

    def _check_account(self, signed_in_as: str | None) -> None:
        account = self.path / _ACCOUNT
        try:
            owner = json.loads(account.read_text(encoding="utf-8"))["login"]
        except FileNotFoundError:
            owner = None
        except (ValueError, KeyError, TypeError) as error:
            raise NotReady(f"Couldn't read {account}. It may still be syncing.") from error
        mine = signed_in_as or self._store.get_meta("login")
        if owner is None:
            others = [
                log
                for log in self.path.glob(f"*{_EVENTS}")
                if log != self._events_file
            ]
            if others:
                # account.json is written before anything else, so it hasn't
                # reached this device yet.
                raise NotReady(
                    "The sync folder doesn't say whose notifications it has yet. "
                    "It may still be syncing."
                )
            if signed_in_as:
                account.write_text(json.dumps({"login": signed_in_as}), encoding="utf-8")
            return
        if mine and mine != owner:
            raise WrongAccount(
                f"This sync folder has {owner}'s GitHub notifications, but this "
                f"account is {mine}'s. Use a sync folder of its own, or add an "
                f"account for {owner} with File → Add Account…."
            )
        if mine is None:
            self._store.set_meta("login", owner)

    def push(self) -> None:
        """Write out this device's events that the folder doesn't have yet,
        and the details of the threads it knows."""
        if self._in_folder is None:
            self.pull()
        if events := self._store.events_except(self._in_folder):
            self._append(to_json(event) for event in events)
            self._in_folder.update(event.id for event in events)
        text = json.dumps([info.to_dict() for info in self._store.all_info()])
        if text != self._threads_written:
            # Replace the file in one step, so it's never seen half written.
            temporary = self.path / f".{self._threads_file.name}.tmp"
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, self._threads_file)
            self._threads_written = text

    def _append(self, lines) -> None:
        with self._events_file.open("a+b") as log:
            # If an earlier write was cut short, don't run on from its last line.
            if log.tell():
                log.seek(-1, os.SEEK_END)
                if log.read(1) != b"\n":
                    log.write(b"\n")
            log.write("".join(f"{line}\n" for line in lines).encode())

    def _read_new_events(self, path: Path) -> list[Event]:
        start = self._read_to.get(path, 0)
        with path.open("rb") as log:
            if log.seek(0, os.SEEK_END) < start:
                # Replaced by a shorter file, so read it all again.
                start = 0
            log.seek(start)
            data = log.read()
        # A line still being written or synced waits until it's complete.
        complete = data.rfind(b"\n") + 1
        self._read_to[path] = start + complete
        events = []
        for line in data[:complete].splitlines():
            if not line.strip():
                continue
            try:
                events.append(from_json(line))
            except (ValueError, KeyError, TypeError):
                # Perhaps from a newer version of Propolis.
                logger.warning("Skipped an event Propolis can't read in %s", path.name)
        return events

    def _read_threads(self, path: Path) -> list[ThreadInfo]:
        stat = path.stat()
        version = (stat.st_mtime_ns, stat.st_size)
        if self._threads_read.get(path) == version:
            return []
        try:
            infos = [
                ThreadInfo.from_dict(item)
                for item in json.loads(path.read_text(encoding="utf-8"))
            ]
        except (ValueError, KeyError, TypeError):
            # Perhaps still syncing. Try again next time.
            logger.warning("Couldn't read thread details from %s", path.name)
            return []
        self._threads_read[path] = version
        return infos
