"""Triaging through Propolis on another Mac.

The Mac that's signed in to GitHub runs a ``Server``. It listens only on that
Mac, and a tunnel, such as Cloudflare's, makes it reachable from elsewhere.
Propolis on another Mac connects to it as a ``Remote``. Each change you make
there goes straight to the server, so the server's store is the only copy of
the history, and only it talks to GitHub.

The protocol is JSON over HTTP, under ``/v1``. Every request carries the
pairing code as a bearer token.

- ``GET /v1/state`` returns the state: the labels in order, and each thread
  that's in a view, with its details. With ``?unless=<version>``, it waits
  while the state is still that version, then returns it, or ``204`` if it
  hasn't changed after a while.
- ``POST /v1/<action>`` does one thing, such as ``done``, and returns any
  result, with the new state as ``state``.
"""

import asyncio
import contextlib
import hmac
import json
import logging
import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import h11
import httpx2

from propolis.core.state import Label, State, ThreadState
from propolis.core.store import Store
from propolis.core.threads import ThreadInfo
from propolis.core.triage import AddError, Triage

logger = logging.getLogger(__name__)

PORT = 47615
"""Where the server listens."""
WAIT = 50
"""How many seconds a request for changes waits. Cloudflare gives up on a
request after 100."""

_MAX_BODY = 64 * 1024
_BURST = 0.2
"""How long to wait after a change before sending it, so that a burst of
changes, such as a sync's, goes together."""


def new_code() -> str:
    """A pairing code. The server may be on the internet, so it's long."""
    return secrets.token_urlsafe(24)


class RemoteError(Exception):
    """Why Propolis on the other Mac couldn't do something, in words for the
    user."""


class NotPaired(RemoteError):
    """The other Mac doesn't accept this one's code."""


class _Refused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Server:
    """Lets Propolis on another Mac triage through this one."""

    def __init__(
        self,
        store: Store,
        triage: Triage,
        code: str,
        refresh: Callable[[], Awaitable[None]],
        on_change: Callable[[], None] = lambda: None,
        wait: float = WAIT,
    ):
        """``refresh`` syncs with GitHub. ``on_change`` is called after the
        other Mac changes something."""
        self.code = code
        """The pairing code. Changing it turns away a Mac using the old one."""
        self._store = store
        self._triage = triage
        self._refresh = refresh
        self._on_change = on_change
        self._wait = wait
        # The version starts again from 0 if Propolis restarts, so it names
        # the server too.
        self._instance = uuid4().hex[:8]
        self._changes = 0
        self._changed = asyncio.Event()
        self._unwatch = store.watch(self._count_change)
        self._server: asyncio.Server | None = None
        self._connections: set[asyncio.Task] = set()

    @property
    def version(self) -> str:
        return f"{self._instance}:{self._changes}"

    @property
    def port(self) -> int:
        return self._server.sockets[0].getsockname()[1]

    async def start(self, port: int = PORT, host: str = "localhost") -> None:
        """Listen on this Mac only. Raises ``OSError`` if the port is taken."""
        self._server = await asyncio.start_server(self._serve, host, port)

    async def close(self) -> None:
        self._unwatch()
        if self._server:
            self._server.close()
        # Including any waiting for changes, but not one that's closing it.
        for task in self._connections - {asyncio.current_task()}:
            task.cancel()

    def _count_change(self) -> None:
        self._changes += 1
        self._changed.set()
        self._changed = asyncio.Event()

    # HTTP

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._connections.add(asyncio.current_task())
        connection = h11.Connection(h11.SERVER)
        try:
            while True:
                request = await self._next(connection, reader)
                if not isinstance(request, h11.Request):
                    break
                body = b""
                while isinstance(event := await self._next(connection, reader), h11.Data):
                    body += event.data
                    if len(body) > _MAX_BODY:
                        await self._send(connection, writer, 413, {"error": "Too big."})
                        return
                if not isinstance(event, h11.EndOfMessage):
                    break
                try:
                    status, payload = await self._respond(request, body)
                except Exception:
                    logger.exception("Couldn't respond to another Mac")
                    status, payload = 500, {"error": "Propolis on this Mac went wrong."}
                await self._send(connection, writer, status, payload)
                if connection.our_state is h11.MUST_CLOSE:
                    break
                connection.start_next_cycle()
        except h11.RemoteProtocolError:
            with contextlib.suppress(h11.LocalProtocolError, ConnectionError):
                await self._send(connection, writer, 400, {"error": "Bad request."})
        except (ConnectionError, h11.LocalProtocolError):
            pass
        finally:
            self._connections.discard(asyncio.current_task())
            writer.close()

    async def _next(self, connection: h11.Connection, reader: asyncio.StreamReader):
        while (event := connection.next_event()) is h11.NEED_DATA:
            connection.receive_data(await reader.read(65536))
        return event

    async def _send(self, connection, writer, status: int, payload: dict | None):
        headers = [("Cache-Control", "no-store")]
        body = b""
        if payload is not None:
            body = json.dumps(payload).encode()
            headers += [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
            ]
        writer.write(connection.send(h11.Response(status_code=status, headers=headers)))
        if body:
            writer.write(connection.send(h11.Data(data=body)))
        writer.write(connection.send(h11.EndOfMessage()))
        await writer.drain()

    async def _respond(self, request: h11.Request, body: bytes) -> tuple[int, dict | None]:
        if not self._paired(request):
            return 401, {"error": "Propolis on this Mac doesn't accept that code."}
        target = urlsplit(request.target.decode("ascii", "replace"))
        if request.method == b"GET" and target.path == "/v1/state":
            unless = parse_qs(target.query).get("unless", [None])[0]
            if unless == self.version and not await self._wait_for_change():
                return 204, None
            return 200, self._state()
        if request.method == b"POST" and target.path.startswith("/v1/"):
            try:
                data = json.loads(body or b"{}")
                if not isinstance(data, dict):
                    raise _Refused(400, "Expected a JSON object.")
                result = await self._act(target.path.removeprefix("/v1/"), data)
            except _Refused as error:
                return error.status, {"error": str(error)}
            except (ValueError, KeyError, TypeError) as error:
                return 400, {"error": f"Bad request: {error}"}
            except AddError as error:
                return 422, {"error": str(error)}
            except httpx2.HTTPError as error:
                return 502, {"error": f"Couldn't reach GitHub: {error}"}
            return 200, {**result, "state": self._state()}
        return 404, {"error": "Not found."}

    def _paired(self, request: h11.Request) -> bool:
        for name, value in request.headers:
            if name == b"authorization":
                return hmac.compare_digest(value, f"Bearer {self.code}".encode())
        return False

    async def _wait_for_change(self) -> bool:
        changed = self._changed
        try:
            await asyncio.wait_for(changed.wait(), self._wait)
        except TimeoutError:
            return False
        await asyncio.sleep(_BURST)
        return True

    # Triage

    async def _act(self, action: str, data: dict) -> dict:
        triage = self._triage
        result = {}
        match action:
            case "view":
                await triage.view(self._thread(data))
            case "done":
                await triage.done(self._thread(data))
            case "label":
                await triage.label(self._thread(data), self._label(data))
            case "unlabel":
                await triage.unlabel(self._thread(data), self._label(data))
            case "add":
                result["thread"] = await triage.add(_text(data, "reference"))
            case "define-label":
                label_id = self._label(data) if data.get("label") else None
                name = _text(data, "name").strip()
                if not name:
                    raise _Refused(400, "A label needs a name.")
                flag = bool(data.get("flag_on_activity", False))
                result["label"] = await triage.define_label(name, flag, label_id)
            case "delete-label":
                await triage.delete_label(self._label(data))
            case "reorder-labels":
                order = data["order"]
                if not all(isinstance(label_id, str) for label_id in order):
                    raise _Refused(400, "The order is a list of label IDs.")
                await triage.reorder_labels(order)
            case "refresh":
                await self._refresh()
                return result
            case _:
                raise _Refused(404, "Not found.")
        self._on_change()
        return result

    def _thread(self, data: dict) -> str:
        key = _text(data, "thread")
        if key not in self._triage.state.threads:
            raise _Refused(404, f"Propolis on this Mac doesn't have {key}.")
        return key

    def _label(self, data: dict) -> str:
        label_id = _text(data, "label")
        if label_id not in self._triage.state.labels:
            raise _Refused(404, "That label has been deleted.")
        return label_id

    def _state(self) -> dict:
        state = self._triage.state
        keys = set(state.inbox())
        for label_id in state.labels:
            keys.update(state.labelled(label_id))
        return {
            "version": self.version,
            "login": self._store.get_meta("login"),
            "labels": [
                {"id": label_id, "name": label.name, "flag_on_activity": label.flag_on_activity}
                for label_id, label in state.labels.items()
            ],
            "threads": [
                _thread_to_dict(key, state.threads[key], self._triage.info(key))
                for key in sorted(keys)
            ],
        }


class Remote:
    """Triage through Propolis on another Mac. Nothing is kept on this Mac:
    each change goes straight to the other one, and what's shown is its state.

    It has the same interface as ``Triage``.
    """

    def __init__(self, url: str, code: str):
        """``url`` is the other Mac's address, such as its tunnel's. Without a
        scheme, it's HTTPS."""
        url = url.strip().rstrip("/")
        if "://" not in url:
            url = f"https://{url}"
        self.url = url
        try:
            self.host = httpx2.URL(url).host
        except httpx2.InvalidURL:
            self.host = ""
        if not self.host:
            raise RemoteError(f"“{url}” isn't a web address.")
        self._http = httpx2.AsyncClient(
            base_url=f"{url}/v1",
            headers={"Authorization": f"Bearer {code}", "User-Agent": "Propolis"},
            timeout=httpx2.Timeout(30, read=WAIT + 30),
        )
        self.state = State()
        self.login: str | None = None
        """The GitHub account the other Mac is signed in to."""
        self._version: str | None = None
        self._info: dict[str, ThreadInfo] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    def info(self, key: str) -> ThreadInfo | None:
        return self._info.get(key)

    async def fetch(self, wait: bool = False) -> bool:
        """Bring the state up to date. With ``wait``, first wait a while for it
        to change. Returns whether it changed."""
        params = {"unless": self._version} if wait and self._version else {}
        response = await self._request("GET", "/state", params=params)
        if response.status_code == 204:
            return False
        self._update(self._json(response))
        return True

    async def refresh(self) -> None:
        """Have the other Mac sync with GitHub now."""
        await self._act("refresh")

    async def add(self, reference: str) -> str:
        return (await self._act("add", reference=reference))["thread"]

    async def label(self, key: str, label_id: str) -> None:
        await self._act("label", thread=key, label=label_id)

    async def unlabel(self, key: str, label_id: str) -> None:
        await self._act("unlabel", thread=key, label=label_id)

    async def view(self, key: str) -> None:
        await self._act("view", thread=key)

    async def done(self, key: str) -> None:
        await self._act("done", thread=key)

    async def define_label(
        self, name: str, flag_on_activity: bool = False, label_id: str | None = None
    ) -> str:
        result = await self._act(
            "define-label", name=name, flag_on_activity=flag_on_activity, label=label_id
        )
        return result["label"]

    async def delete_label(self, label_id: str) -> None:
        await self._act("delete-label", label=label_id)

    async def reorder_labels(self, order: list[str]) -> None:
        await self._act("reorder-labels", order=order)

    async def _act(self, action: str, **data) -> dict:
        result = self._json(await self._request("POST", f"/{action}", json=data))
        self._update(result.pop("state"))
        return result

    async def _request(self, method: str, path: str, **kwargs) -> httpx2.Response:
        try:
            response = await self._http.request(method, path, **kwargs)
        except (httpx2.HTTPError, httpx2.InvalidURL) as error:
            raise RemoteError(f"Couldn't reach {self.host}: {_describe(error)}") from error
        if response.status_code == 401:
            raise NotPaired(
                f"Propolis at {self.host} doesn't accept the code. If it's made a "
                "new one, connect again with that."
            )
        if response.status_code in (502, 503, 504, 530) and not _is_json(response):
            # From the tunnel, which can't reach Propolis behind it.
            raise RemoteError(
                f"{self.host} is there, but Propolis isn't answering through it. "
                "Is it open on the other Mac, and sharing?"
            )
        if response.is_error:
            message = self._json(response).get("error") if _is_json(response) else None
            if response.status_code == 422 and message:
                raise AddError(message)
            raise RemoteError(message or f"{self.host} returned {response.status_code}.")
        return response

    def _json(self, response: httpx2.Response) -> dict:
        try:
            data = response.json()
        except ValueError as error:
            raise RemoteError(f"{self.host} doesn't seem to be Propolis.") from error
        if not isinstance(data, dict):
            raise RemoteError(f"{self.host} doesn't seem to be Propolis.")
        return data

    def _update(self, data: dict) -> None:
        self.state = State(
            labels={
                label["id"]: Label(label["name"], label["flag_on_activity"])
                for label in data["labels"]
            }
        )
        self._info = {}
        for thread in data["threads"]:
            key = thread["key"]
            self.state.threads[key] = ThreadState(
                labels={
                    label_id: datetime.fromisoformat(at)
                    for label_id, at in thread["labels"].items()
                },
                last_activity=_time(thread["last_activity"]),
                seen_at=_time(thread["seen_at"]),
                done=thread["done"],
            )
            if thread["info"]:
                self._info[key] = ThreadInfo.from_dict(thread["info"])
        self.login = data["login"]
        self._version = data["version"]


def _thread_to_dict(key: str, thread: ThreadState, info: ThreadInfo | None) -> dict:
    return {
        "key": key,
        "labels": {label_id: at.isoformat() for label_id, at in thread.labels.items()},
        "last_activity": thread.last_activity and thread.last_activity.isoformat(),
        "seen_at": thread.seen_at and thread.seen_at.isoformat(),
        "done": thread.done,
        "info": info.to_dict() if info else None,
    }


def _text(data: dict, name: str) -> str:
    value = data[name]
    if not isinstance(value, str):
        raise _Refused(400, f"Expected {name} to be text.")
    return value


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _is_json(response: httpx2.Response) -> bool:
    return response.headers.get("content-type", "").startswith("application/json")


def _describe(error: Exception) -> str:
    return str(error) or type(error).__name__
