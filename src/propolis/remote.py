"""Sharing an inbox with Propolis on another Mac.

The Mac that's signed in to GitHub shares its inbox: it runs a `RemoteServer`,
which listens only on that Mac. A tunnel you run, such as Cloudflare's, makes it
reachable from elsewhere. Propolis on another Mac connects to it with a
`RemoteClient`, and the shared inbox becomes one of its accounts. Each change
made there goes to the sharing Mac, which makes every GitHub request itself, so
its GitHub sign-in never leaves it.

The protocol is JSON over HTTP, under ``/v1``. Every request carries the
sharing Mac's code as a bearer token.

- ``GET /v1/state`` returns the inbox (see `Store.snapshot()`), with an ETag.
  With ``If-None-Match``, it returns ``304`` if the inbox hasn't changed.
- ``POST /v1/<action>`` does one thing, such as ``done``, and returns any
  result.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx

from .actions import add_thread, apply_to_threads, label_threads
from .github import GitHubError, reference
from .store import ATTENTION, Store

#: Where the sharing Mac listens. The tunnel sends its traffic to
#: http://localhost:PORT.
PORT = 47615
API = "/v1"
MAX_BODY = 64 * 1024
MAX_THREADS_PER_ACTION = 1000
MAX_LABEL_NAME = 100
MAX_REFERENCE = 300


def new_code() -> str:
    """A code for sharing. The inbox may be reachable from the internet, so
    it's long."""
    return secrets.token_urlsafe(24)


class RemoteError(Exception):
    """Why Propolis on the other Mac couldn't do something, in words for the user."""


class NotPaired(RemoteError):
    """The other Mac doesn't accept this one's code."""


######################################################################
# The sharing Mac: what Propolis on another Mac may do to this inbox
######################################################################


@dataclass
class Response:
    status: int
    body: dict | None = None
    headers: dict[str, str] = field(default_factory=dict)


class BadRequest(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class HostAPI:
    """Carries out requests from Propolis on another Mac on this Mac's inbox.

    Deliberately narrow: it can read and triage the inbox and manage labels,
    but there's no way to read the GitHub sign-in, change settings, or stop
    sharing.
    """

    def __init__(
        self,
        store: Store,
        github: Callable[[], object | None],
        on_change: Callable[[str], None],
        on_refresh: Callable[[], None],
    ):
        """
        :param store: The inbox being shared.
        :param github: Returns the GitHub client, or None if not connected.
        :param on_change: Called with a description of each change made.
        :param on_refresh: Called when the other Mac asks for a refresh.
        """
        self.store = store
        self.github = github
        self.on_change = on_change
        self.on_refresh = on_refresh

    async def handle(
        self, method: str, path: str, headers: dict, body: bytes
    ) -> Response:
        path = urlsplit(path).path
        try:
            if method == "GET" and path == f"{API}/state":
                return self.state(headers.get("If-None-Match"))
            if method == "POST" and path.startswith(f"{API}/"):
                try:
                    request = json.loads(body or b"{}")
                except ValueError:
                    raise BadRequest("The request isn't JSON.") from None
                if not isinstance(request, dict):
                    raise BadRequest("The request isn't a JSON object.")
                return await self.act(path.removeprefix(f"{API}/"), request)
            return Response(404, {"error": f"No such request: {method} {path}"})
        except BadRequest as exc:
            return Response(exc.status, {"error": str(exc)})
        except ValueError as exc:  # From the store, such as a duplicate label.
            return Response(400, {"error": str(exc)})

    def state(self, if_none_match: str | None) -> Response:
        payload = json.dumps(self.store.snapshot(), sort_keys=True).encode()
        etag = '"' + hashlib.sha256(payload).hexdigest()[:32] + '"'
        if if_none_match == etag:
            return Response(304, None, {"ETag": etag})
        return Response(200, json.loads(payload), {"ETag": etag})

    async def act(self, action: str, request: dict) -> Response:
        store = self.store

        if action == "view":
            # Seeing it in Propolis, and reading it on GitHub.
            return await self._github(
                request, store.mark_seen, lambda c, i: c.mark_read(i), "read {n}"
            )
        if action == "done":
            return await self._github(
                request,
                store.mark_done,
                lambda c, i: c.mark_done(i),
                "marked {n} as done",
            )
        if action == "unsubscribe":
            # Only GitHub's side: the other Mac marks it as done separately.
            return await self._github(
                request,
                lambda ids: None,
                lambda c, i: c.unsubscribe(i),
                "unsubscribed from {n}",
            )
        if action == "label":
            ids, label_id = _thread_ids(request), self._label_id(request, True)
            if label_id == ATTENTION:
                store.set_attention(ids, True)
                return self._changed(f"flagged {_count(ids)} for Attention required")
            threads = [t for i in ids if (t := store.get(i))]
            label = store.label(label_id)
            await label_threads(store, self.github(), label, threads)
            return self._changed(f"moved {_count(ids)} to {label.name}")
        if action == "unlabel":
            ids, label_id = _thread_ids(request), self._label_id(request, True)
            if label_id == ATTENTION:
                store.set_attention(ids, False)
                return self._changed(f"cleared {_count(ids)} from Attention required")
            store.set_label(label_id, ids, False)
            name = store.label(label_id).name
            return self._changed(f"took {_count(ids)} out of {name}")
        if action == "define-label":
            name = _name(request)
            flag = _bool(request, "flag_on_activity", False)
            if request.get("label") is None:
                label = store.create_label(name, flag=flag)
                self.on_change(f"added the label {label.name}")
            else:
                label = store.edit_label(self._label_id(request), name, flag)
                self.on_change(f"changed the label {label.name}")
            return Response(200, {"label": label.id})
        if action == "delete-label":
            label = store.label(self._label_id(request))
            store.delete_label(label.id)
            return self._changed(f"deleted the label {label.name}")
        if action == "reorder-labels":
            order = request.get("order")
            if not isinstance(order, list) or not all(
                isinstance(i, str) for i in order
            ):
                raise BadRequest("order must be a list of label IDs.")
            store.reorder_labels(order)
            return self._changed("reordered the labels")
        if action == "add":
            return await self._add(request)
        if action == "refresh":
            self.on_refresh()
            return Response(200, {"ok": True})
        raise BadRequest(f"No such action: {action!r}", 404)

    async def _github(self, request: dict, local, remote, description: str) -> Response:
        """Change threads here, then on GitHub. GitHub's side is best effort:
        if it fails, the change here stands, and the failure is logged."""
        client = self.github()
        if client is None:
            return Response(503, {"error": "That Propolis isn't connected to GitHub."})
        threads = [t for i in _thread_ids(request) if (t := self.store.get(i))]
        failures = await apply_to_threads(self.store, client, threads, local, remote)
        if threads:
            self.on_change(description.format(n=_count(threads)))
        return Response(200, {"ok": True, "failed": [i for i, _ in failures]})

    async def _add(self, request: dict) -> Response:
        text = request.get("reference")
        if not isinstance(text, str) or len(text) > MAX_REFERENCE:
            raise BadRequest("reference must be text, like org/repo#123.")
        label_id = self._label_id(request) if request.get("label") else None
        if (client := self.github()) is None:
            return Response(503, {"error": "That Propolis isn't connected to GitHub."})
        try:
            thread = await add_thread(self.store, client, text, label_id)
        except GitHubError as exc:
            return Response(502, {"error": str(exc)})
        label = self.store.label(label_id) if label_id else None
        where = f" to {label.name}" if label else ""
        self.on_change(f"added {reference(thread)}{where}")
        return Response(200, {"thread": thread.id})

    def _label_id(self, request: dict, attention: bool = False) -> str:
        """The label a request names. ``attention`` allows Attention required."""
        label_id = request.get("label")
        if not isinstance(label_id, str):
            raise BadRequest("label must be a label's ID.")
        if (attention and label_id == ATTENTION) or self.store.label(label_id):
            return label_id
        raise BadRequest("That label has been deleted.", 404)

    def _changed(self, description: str) -> Response:
        self.on_change(description)
        return Response(200, {"ok": True})


def _thread_ids(request: dict) -> list[str]:
    ids = request.get("threads")
    if (
        not isinstance(ids, list)
        or len(ids) > MAX_THREADS_PER_ACTION
        or not all(isinstance(i, str) for i in ids)
    ):
        raise BadRequest("threads must be a list of thread IDs.")
    return ids


def _bool(request: dict, key: str, default: bool) -> bool:
    value = request.get(key, default)
    if not isinstance(value, bool):
        raise BadRequest(f"{key} must be true or false.")
    return value


def _name(request: dict) -> str:
    name = request.get("name")
    if not isinstance(name, str) or len(name) > MAX_LABEL_NAME:
        raise BadRequest(f"name must be text of up to {MAX_LABEL_NAME} characters.")
    return name


def _count(items) -> str:
    n = len(items)
    return f"{n} notification{'' if n == 1 else 's'}"


class RemoteServer:
    """Serves a `HostAPI` on 127.0.0.1, for a tunnel to forward to.

    Requests are read on worker threads, then carried out on the app's event
    loop, so the store and UI are only ever touched from the main thread.
    """

    def __init__(
        self,
        api: HostAPI,
        code: str,
        loop: asyncio.AbstractEventLoop,
        port: int = PORT,
    ):
        """:raises OSError: If the port is taken."""
        self.api = api
        #: The code. Changing it turns away a Mac using the old one.
        self.code = code
        self.loop = loop
        self.stopped = False
        self._server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        # Never wait for request threads: they may be waiting for the loop.
        self._server.daemon_threads = True
        self._server.block_on_close = False
        self._server.remote = self
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        # Connections already open, such as a tunnel's, are turned away too.
        self.stopped = True
        self._server.shutdown()
        self._server.server_close()


class _Handler(BaseHTTPRequestHandler):
    server_version = "Propolis"
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def log_message(self, format, *args):
        pass  # Keep request details out of the app's log.

    def _handle(self, method: str):
        remote: RemoteServer = self.server.remote
        if remote.stopped:
            self.close_connection = True
            self._send(Response(503, {"error": "Propolis there has stopped sharing."}))
            return
        given = self.headers.get("Authorization", "").encode()
        expected = f"Bearer {remote.code}".encode()
        if not remote.code or not hmac.compare_digest(given, expected):
            time.sleep(1)  # Make guessing slow.
            self._send(
                Response(
                    401, {"error": "Propolis on this Mac doesn't accept that code."}
                )
            )
            return
        try:
            body = self._read_body()
        except ValueError as exc:
            self._send(Response(413, {"error": str(exc)}))
            return

        future = asyncio.run_coroutine_threadsafe(
            remote.api.handle(method, self.path, dict(self.headers), body), remote.loop
        )
        try:
            response = future.result(timeout=120)
        except Exception as exc:  # noqa: BLE001 -- report it, don't drop the request
            response = Response(500, {"error": f"Something went wrong: {exc!r}"})
        self._send(response)

    def _read_body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            body = b""
            while size := int(self.rfile.readline().split(b";")[0].strip() or b"0", 16):
                body += self.rfile.read(size)
                self.rfile.readline()
                if len(body) > MAX_BODY:
                    raise ValueError("The request is too big.")
            while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                pass  # Trailers.
            return body
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("The request is too big.")
        return self.rfile.read(length) if length else b""

    def _send(self, response: Response):
        self.send_response(response.status)
        self.send_header("Cache-Control", "no-store")
        for name, value in response.headers.items():
            self.send_header(name, value)
        if response.status == 304:
            self.end_headers()
            return
        payload = json.dumps(response.body or {}).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


######################################################################
# The other Mac: triaging through the sharing one
######################################################################


def check_address(url: str) -> str:
    """Tidy up a sharing Mac's address, and insist on HTTPS except locally.
    Without a scheme, it's HTTPS."""
    url = url.strip().rstrip("/")
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if not parts.hostname:
        raise ValueError(f"“{url}” isn't a web address.")
    if parts.scheme != "https" and parts.hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("The address must start with https://.")
    return url


class RemoteClient:
    """Triages through Propolis on another Mac.

    It has the same `mark_read`, `mark_done` and `unsubscribe` methods as
    `GitHubClient`, so the UI can use either.
    """

    def __init__(
        self,
        url: str,
        code: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        """:raises ValueError: If ``url`` isn't an address Propolis can use."""
        self.url = check_address(url)
        self.host = urlsplit(self.url).hostname
        self._http = httpx.AsyncClient(
            base_url=self.url + API,
            headers={"Authorization": f"Bearer {code}", "User-Agent": "Propolis"},
            timeout=60,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise RemoteError(f"Couldn't reach {self.host}: {exc}") from exc

        status = response.status_code
        if status == 401:
            raise NotPaired(f"Propolis at {self.host} doesn't accept the code.")
        if status in {301, 302, 303, 307, 308}:
            raise RemoteError(
                f"{self.host} sent Propolis somewhere else, such as a sign-in page. "
                "It needs to reach Propolis on the other Mac directly."
            )
        tunnel = status in {502, 503, 504} or 520 <= status <= 530
        if tunnel and not _is_json(response):
            # From the tunnel, which can't reach Propolis behind it.
            raise RemoteError(
                f"{self.host} is there, but Propolis isn't answering through it. Is "
                "it open on the other Mac, and sharing?"
            )
        if status >= 400:
            message = None
            if _is_json(response):
                try:
                    message = response.json()["error"]
                except (ValueError, KeyError, TypeError):
                    pass
            raise RemoteError(message or f"{self.host} returned {status}.")
        return response

    async def state(self, etag: str | None = None) -> tuple[dict | None, str | None]:
        """The shared inbox, or None if it hasn't changed since ``etag``."""
        headers = {"If-None-Match": etag} if etag else {}
        response = await self._request("GET", "/state", headers=headers)
        if response.status_code == 304:
            return None, etag
        try:
            data = response.json()
        except ValueError:
            data = None
        if not (
            isinstance(data, dict)
            and isinstance(data.get("labels"), list)
            and isinstance(data.get("threads"), list)
        ):
            raise RemoteError(f"{self.host} doesn't seem to be Propolis.")
        return data, response.headers.get("ETag")

    async def action(self, action: str, /, **params) -> dict:
        # Positional-only, so an action can itself take a ``name``.
        response = await self._request("POST", f"/{action}", json=params)
        return response.json()

    async def mark_read(self, thread_id: str) -> None:
        await self.action("view", threads=[thread_id])

    async def mark_done(self, thread_id: str) -> None:
        await self.action("done", threads=[thread_id])

    async def unsubscribe(self, thread_id: str) -> None:
        await self.action("unsubscribe", threads=[thread_id])


def _is_json(response: httpx.Response) -> bool:
    return response.headers.get("content-type", "").startswith("application/json")
