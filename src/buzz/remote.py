"""Remote control: letting another Buzz control this inbox, and controlling
another Buzz's inbox, through a Cloudflare named tunnel.

The Buzz being controlled (the *host*) serves a small JSON API on
``127.0.0.1``, and runs ``cloudflared`` to connect a Cloudflare tunnel to it.
The controlling Buzz talks to that API through the tunnel's public hostname.
The host's GitHub token never leaves the host: the host makes every GitHub
request itself.

Every request needs the host's access key, and the hostname should also be
protected with Cloudflare Access (see the README).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx

from .actions import add_thread, apply_to_threads, label_threads
from .github import GitHubError, reference
from .store import Store

#: The local port the host serves on. The tunnel's public hostname must send
#: its traffic to http://localhost:REMOTE_PORT.
REMOTE_PORT = 47321
API = "/v1"
MAX_BODY = 64 * 1024
MAX_THREADS_PER_ACTION = 1000
MAX_LABEL_NAME = 100
MAX_REFERENCE = 300
#: Where Homebrew puts cloudflared. Apps don't get the shell's PATH.
CLOUDFLARED_PATHS = ("/opt/homebrew/bin/cloudflared", "/usr/local/bin/cloudflared")


class RemoteError(Exception):
    """A request to another Buzz failed."""


######################################################################
# The host: what another Buzz may do to this one
######################################################################


@dataclass
class Response:
    status: int
    body: dict | None = None
    headers: dict[str, str] = field(default_factory=dict)


class BadRequest(Exception):
    pass


class HostAPI:
    """Carries out requests from a controlling Buzz on this Buzz's own inbox.

    Deliberately narrow: it can read and triage the inbox and manage labels,
    but there's no way to read the GitHub token, change settings, or turn
    remote control off.
    """

    def __init__(
        self,
        store: Store,
        github: Callable[[], object | None],
        on_change: Callable[[str], None],
        on_refresh: Callable[[], None],
    ):
        """
        :param store: This Buzz's own store (never a remote inbox's).
        :param github: Returns the GitHub client, or None if not connected.
        :param on_change: Called with a description of each change made.
        :param on_refresh: Called when the controller asks for a refresh.
        """
        self.store = store
        self.github = github
        self.on_change = on_change
        self.on_refresh = on_refresh

    async def handle(
        self, method: str, path: str, headers: dict, body: bytes
    ) -> Response:
        try:
            if method == "GET" and path == f"{API}/ping":
                return Response(
                    200, {"app": "Buzz", "login": self.store.get_meta("login")}
                )
            if method == "GET" and path == f"{API}/state":
                return self.state(headers.get("If-None-Match"))
            if method == "POST" and path == f"{API}/action":
                try:
                    request = json.loads(body or b"{}")
                except ValueError:
                    raise BadRequest("The request isn't JSON.") from None
                if not isinstance(request, dict):
                    raise BadRequest("The request isn't a JSON object.")
                return await self.action(request)
            return Response(404, {"error": f"No such request: {method} {path}"})
        except BadRequest as exc:
            return Response(400, {"error": str(exc)})
        except ValueError as exc:  # From the store, such as a duplicate label.
            return Response(400, {"error": str(exc)})

    def state(self, if_none_match: str | None) -> Response:
        payload = json.dumps(self.store.snapshot(), sort_keys=True).encode()
        etag = '"' + hashlib.sha256(payload).hexdigest()[:32] + '"'
        if if_none_match == etag:
            return Response(304, None, {"ETag": etag})
        return Response(200, json.loads(payload), {"ETag": etag})

    async def action(self, request: dict) -> Response:
        name = request.get("action")
        store = self.store

        if name == "mark_read":
            # Viewing it, which is seeing it in Buzz and reading it on GitHub.
            return await self._github(
                request,
                store.mark_seen,
                lambda c, i: c.mark_read(i),
                "read {n}",
            )
        if name == "mark_done":
            return await self._github(
                request,
                store.mark_done,
                lambda c, i: c.mark_done(i),
                "marked {n} as done",
            )
        if name == "unsubscribe":
            # Only the GitHub side: the controller marks it as done separately.
            return await self._github(
                request,
                lambda ids: None,
                lambda c, i: c.unsubscribe(i),
                "unsubscribed from {n}",
            )
        if name == "set_label":
            ids, on = _thread_ids(request), _bool(request, "on")
            label = self._label(request)
            if on:
                threads = [t for i in ids if (t := store.get(i))]
                await label_threads(store, self.github(), label, threads)
            else:
                store.set_label(label.id, ids, on)
            verb = "moved {n} to" if on else "took {n} out of"
            return self._changed(verb.format(n=_count(ids)) + f" {label.name}")
        if name == "set_attention":
            ids, on = _thread_ids(request), _bool(request, "on")
            store.set_attention(ids, on)
            verb = "flagged {n} for" if on else "cleared {n} from"
            return self._changed(verb.format(n=_count(ids)) + " Attention required")
        if name == "create_label":
            flag = _bool(request, "flag") if "flag" in request else False
            label = store.create_label(_name(request), flag=flag)
            self.on_change(f"added the label {label.name}")
            return Response(200, {"label": vars(label)})
        if name == "edit_label":
            label = self._label(request)
            edited = store.edit_label(label.id, _name(request), _bool(request, "flag"))
            return self._changed(f"changed the label {edited.name}")
        if name == "reorder_labels":
            order = request.get("order")
            if not isinstance(order, list) or not all(
                isinstance(i, int) and not isinstance(i, bool) for i in order
            ):
                raise BadRequest("order must be a list of label IDs.")
            store.reorder_labels(order)
            return self._changed("reordered the labels")
        if name == "delete_label":
            label = self._label(request)
            store.delete_label(label.id)
            return self._changed(f"deleted the label {label.name}")
        if name == "add_thread":
            return await self._add_thread(request)
        if name == "refresh":
            self.on_refresh()
            return Response(200, {"ok": True})
        raise BadRequest(f"No such action: {name!r}")

    async def _github(self, request: dict, local, remote, description: str) -> Response:
        """Change threads here, then on GitHub. GitHub's side is best effort:
        if it fails, the change here stands, and the failure is logged."""
        client = self.github()
        if client is None:
            return Response(503, {"error": "That Buzz isn't connected to GitHub."})
        threads = [t for i in _thread_ids(request) if (t := self.store.get(i))]
        failures = await apply_to_threads(self.store, client, threads, local, remote)
        if threads:
            self.on_change(description.format(n=_count(threads)))
        return Response(200, {"ok": True, "failed": [i for i, _ in failures]})

    async def _add_thread(self, request: dict) -> Response:
        ref = request.get("ref")
        if not isinstance(ref, str) or len(ref) > MAX_REFERENCE:
            raise BadRequest("ref must be text, like org/repo#123.")
        label = self._label(request) if request.get("label_id") is not None else None
        if (client := self.github()) is None:
            return Response(503, {"error": "That Buzz isn't connected to GitHub."})
        try:
            thread = await add_thread(
                self.store, client, ref, label.id if label else None
            )
        except GitHubError as exc:
            return Response(502, {"error": str(exc)})
        where = f" to {label.name}" if label else ""
        self.on_change(f"added {reference(thread)}{where}")
        return Response(200, {"thread_id": thread.id})

    def _label(self, request: dict):
        label_id = request.get("label_id")
        if not isinstance(label_id, int) or isinstance(label_id, bool):
            raise BadRequest("label_id must be a number.")
        for label in self.store.labels():
            if label.id == label_id:
                return label
        raise BadRequest(f"There's no label {label_id}.")

    def _changed(self, description: str) -> Response:
        self.on_change(description)
        return Response(200, {"ok": True})


def _thread_ids(request: dict) -> list[str]:
    ids = request.get("thread_ids")
    if (
        not isinstance(ids, list)
        or len(ids) > MAX_THREADS_PER_ACTION
        or not all(isinstance(i, str) for i in ids)
    ):
        raise BadRequest("thread_ids must be a list of strings.")
    return ids


def _bool(request: dict, key: str) -> bool:
    value = request.get(key)
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
    """Serves a `HostAPI` on 127.0.0.1, for cloudflared to forward to.

    Requests are read on worker threads, then carried out on the app's event
    loop, so the store and UI are only ever touched from the main thread.
    """

    def __init__(
        self,
        api: HostAPI,
        key: str,
        loop: asyncio.AbstractEventLoop,
        port: int = REMOTE_PORT,
    ):
        self.api = api
        self.key = key
        self.loop = loop
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
        self._server.shutdown()
        self._server.server_close()


class _Handler(BaseHTTPRequestHandler):
    server_version = "Buzz"
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def log_message(self, format, *args):
        pass  # Keep request details out of the app's log.

    def _handle(self, method: str):
        remote: RemoteServer = self.server.remote
        given = self.headers.get("Authorization", "").encode()
        expected = f"Bearer {remote.key}".encode()
        if not remote.key or not hmac.compare_digest(given, expected):
            time.sleep(1)  # Make guessing slow.
            self._send(Response(401, {"error": "Wrong access key."}))
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
# The host: running cloudflared
######################################################################


def find_cloudflared() -> str | None:
    if found := shutil.which("cloudflared"):
        return found
    return next((path for path in CLOUDFLARED_PATHS if os.access(path, os.X_OK)), None)


class Tunnel:
    """Runs ``cloudflared`` for a named tunnel, restarting it if it stops.

    The token goes to cloudflared in its environment (``TUNNEL_TOKEN``), not
    on its command line, where other programs could see it.
    """

    RESTART_DELAYS = (5, 10, 30, 60)

    def __init__(
        self,
        token: str,
        loop: asyncio.AbstractEventLoop,
        on_status: Callable[[str], None],
        executable: str | None = None,
    ):
        self.token = token
        self.loop = loop
        self.on_status = on_status
        self.executable = executable or find_cloudflared()
        self.process: subprocess.Popen | None = None
        self.connections = 0
        self.last_error = ""
        self._stopped = False
        self._failures = 0
        self._restart: asyncio.TimerHandle | None = None
        self._watcher: threading.Thread | None = None

    def start(self) -> None:
        self._stopped = False
        if self.executable is None:
            self.on_status(
                "cloudflared isn't installed. Install it with: brew install cloudflared"
            )
            return
        self.connections, self.last_error = 0, ""
        self.process = subprocess.Popen(
            [self.executable, "tunnel", "--no-autoupdate", "run"],
            env={**os.environ, "TUNNEL_TOKEN": self.token},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.on_status("Tunnel starting…")
        self._watcher = threading.Thread(
            target=self._watch, args=(self.process,), daemon=True
        )
        self._watcher.start()

    def stop(self) -> None:
        self._stopped = True
        if self._restart is not None:
            self._restart.cancel()
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if self._watcher is not None:
            # It finishes once cloudflared's output closes, closing it too.
            self._watcher.join(timeout=2)
            self._watcher = None
        self.process = None

    def process_record(self) -> str | None:
        """Identifies the running cloudflared, for `stop_stale_cloudflared()`."""
        if self.process is None or self.process.poll() is not None:
            return None
        started = _process_start(self.process.pid)
        return f"{self.process.pid} {started}" if started else None

    def _watch(self, process: subprocess.Popen) -> None:
        # Runs on its own thread; reports back to the event loop.
        with process.stderr:
            for line in process.stderr:
                self.loop.call_soon_threadsafe(self._log_line, line.strip())
        process.wait()
        self.loop.call_soon_threadsafe(self._exited, process)

    def _log_line(self, line: str) -> None:
        if "Registered tunnel connection" in line:
            self.connections += 1
            self._failures = 0
            self.on_status("Tunnel connected.")
        elif " ERR " in line:
            self.last_error = line.split(" ERR ", 1)[1]
            if not self.connections:
                self.on_status(f"Tunnel problem: {self.last_error}")

    def _exited(self, process: subprocess.Popen) -> None:
        if self._stopped or process is not self.process:
            return
        delay = self.RESTART_DELAYS[min(self._failures, len(self.RESTART_DELAYS) - 1)]
        self._failures += 1
        detail = f": {self.last_error}" if self.last_error else ""
        self.on_status(
            f"cloudflared stopped (exit {process.returncode}){detail}. "
            f"Retrying in {delay} s."
        )
        self._restart = self.loop.call_later(delay, self.start)


def _ps(pid: int, field: str) -> str:
    try:
        return subprocess.run(
            ["ps", "-p", str(pid), "-o", f"{field}="],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
    except OSError:
        return ""


def _process_start(pid: int) -> str:
    return _ps(pid, "lstart")


def stop_stale_cloudflared(record: str | None) -> None:
    """Stop a cloudflared left running by an earlier Buzz that didn't quit
    cleanly, so it doesn't keep a connection open to nothing.

    :param record: A `Tunnel.process_record()`. Only a process with the same
        ID *and* start time is stopped, so a reused process ID is left alone.
    """
    if not record:
        return
    pid_text, _, started = record.partition(" ")
    pid = int(pid_text)
    if _ps(pid, "comm").endswith("cloudflared") and _process_start(pid) == started:
        try:
            os.kill(pid, 15)
        except OSError:
            pass


######################################################################
# The controller: talking to another Buzz
######################################################################


def check_address(url: str) -> str:
    """Tidy up a remote Buzz's address, and insist on HTTPS except locally."""
    url = url.strip().rstrip("/")
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if not parts.hostname:
        raise ValueError("That doesn't look like an address.")
    if parts.scheme != "https" and parts.hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("The address must start with https://.")
    return url


class RemoteClient:
    """Controls another Buzz through its tunnel.

    It has the same `mark_read`, `mark_done` and `unsubscribe` methods as
    `GitHubClient`, so the UI can use either.
    """

    def __init__(
        self,
        url: str,
        key: str,
        access_id: str | None = None,
        access_secret: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        headers = {"Authorization": f"Bearer {key}", "User-Agent": "Buzz-remote"}
        if access_id and access_secret:
            headers["CF-Access-Client-Id"] = access_id
            headers["CF-Access-Client-Secret"] = access_secret
        self.url = check_address(url)
        self._http = httpx.AsyncClient(
            base_url=self.url + API, headers=headers, timeout=60, transport=transport
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise RemoteError(f"Couldn't reach {self.url} ({exc}).") from exc

        status = response.status_code
        if status in {301, 302, 303, 307, 308}:
            raise RemoteError(
                "Cloudflare Access asked for a sign-in. Add the service token's "
                "client ID and secret to this remote inbox."
            )
        if status == 401:
            raise RemoteError("The access key was refused. Ask for the current one.")
        if status == 403:
            raise RemoteError(
                "Cloudflare Access refused the service token. Check its ID, "
                "secret, and the Access policy."
            )
        if status in {502, 503, 504} or 520 <= status <= 530:
            try:
                message = response.json()["error"]
            except (ValueError, KeyError, TypeError):
                message = (
                    "Their Buzz isn't reachable. Remote control may be off, or "
                    f"their Mac may be asleep or offline. (Cloudflare said {status}.)"
                )
            raise RemoteError(message)
        if status >= 400:
            try:
                message = response.json()["error"]
            except (ValueError, KeyError, TypeError):
                message = f"{status} {response.reason_phrase}"
            raise RemoteError(message)
        return response

    async def ping(self) -> dict:
        response = await self._request("GET", "/ping")
        data = response.json()
        if data.get("app") != "Buzz":
            raise RemoteError("That address isn't a Buzz.")
        return data

    async def state(self, etag: str | None = None) -> tuple[dict | None, str | None]:
        """The remote inbox, or None if it hasn't changed since ``etag``."""
        headers = {"If-None-Match": etag} if etag else {}
        response = await self._request("GET", "/state", headers=headers)
        if response.status_code == 304:
            return None, etag
        return response.json(), response.headers.get("ETag")

    async def action(self, action: str, /, **params) -> dict:
        # Positional-only, so an action can itself take a ``name``.
        response = await self._request(
            "POST", "/action", json={**params, "action": action}
        )
        return response.json()

    async def mark_read(self, thread_id: str) -> None:
        await self.action("mark_read", thread_ids=[thread_id])

    async def mark_done(self, thread_id: str) -> None:
        await self.action("mark_done", thread_ids=[thread_id])

    async def unsubscribe(self, thread_id: str) -> None:
        await self.action("unsubscribe", thread_ids=[thread_id])
