"""
A GitHub notifications inbox with labels.
"""

from __future__ import annotations

import asyncio
import textwrap
import time
import traceback
import webbrowser
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from functools import partial

import toga
from toga.sources import AccessorColumn, ListSource
from toga.style.pack import BOLD, CENTER, COLUMN, NONE, PACK, ROW

from . import credentials, macos
from .github import (
    MIN_POLL_INTERVAL,
    TOKEN_URL,
    AuthenticationError,
    GitHubClient,
    GitHubError,
    describe_reason,
    describe_type,
    html_url,
    reference,
)
from .store import DONE, INBOX, UNREAD, Label, Store, Thread

VIEWS = ((INBOX, "📥  Inbox"), (UNREAD, "📬  Unread"), (DONE, "✅  Done"))

#: Fetch the whole inbox at least this often (seconds), even if GitHub says
#: nothing has changed, to pick up threads read or done on github.com.
FULL_SYNC_EVERY = 10 * 60
#: Don't open more browser tabs than this at once.
OPEN_LIMIT = 10
#: How many requests to send GitHub at once when changing many threads.
CONCURRENCY = 8


def ago(timestamp: str, now: datetime | None = None) -> str:
    """Describe an ISO 8601 timestamp relative to now, like "3 hr ago"."""
    then = datetime.fromisoformat(timestamp)
    seconds = ((now or datetime.now(UTC)) - then).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} hr ago"
    if seconds < 7 * 86400:
        days = int(seconds // 86400)
        return "yesterday" if days == 1 else f"{days} days ago"
    return then.astimezone().strftime("%-d %b %Y")


def sync_rows(source: ListSource, items: list[dict], key: str = "key") -> None:
    """Change ``source`` in place to match ``items``.

    Rows are matched by their ``key`` attribute, so rows that stay put are
    updated rather than replaced, and a table keeps their selection.
    """
    wanted = {item[key] for item in items}
    for row in [row for row in source if getattr(row, key) not in wanted]:
        source.remove(row)

    for index, item in enumerate(items):
        row = source[index] if index < len(source) else None
        if row is not None and getattr(row, key) == item[key]:
            for attr, value in item.items():
                if getattr(row, attr, None) != value:
                    setattr(row, attr, value)
        else:
            for other in source:
                if getattr(other, key) == item[key]:
                    source.remove(other)
                    break
            source.insert(index, item)


class Buzz(toga.App):
    ######################################################################
    # Dependencies (overridable, for trying the UI with fake data)
    ######################################################################

    def open_store(self) -> Store:
        self.paths.data.mkdir(parents=True, exist_ok=True)
        return Store(self.paths.data / "buzz.sqlite3")

    def load_token(self) -> str | None:
        try:
            return credentials.load_token(self.app_id)
        except credentials.KeyringError:
            traceback.print_exc()
            return None

    def save_token(self, token: str) -> None:
        credentials.save_token(self.app_id, token)

    def delete_token(self) -> None:
        credentials.delete_token(self.app_id)

    def make_client(self, token: str) -> GitHubClient:
        return GitHubClient(token)

    ######################################################################
    # Startup
    ######################################################################

    def startup(self):
        self.store = self.open_store()
        self.client: GitHubClient | None = None
        self.view: str | int = INBOX
        self.labels: list[Label] = []
        self.sync_task: asyncio.Task | None = None
        self.wake = asyncio.Event()
        self.force_full_sync = True
        self.last_full_sync = 0.0
        self.detail_pending = False
        self.shown_switches = None
        self.icons = {
            name: toga.Icon(f"resources/{name}")
            for name in ("unread", "check", "blank")
        }

        self.main_window = toga.MainWindow(size=(1320, 820))
        self.create_commands()
        self.inbox_view = self.build_inbox()
        self.token_view = self.build_token_form()
        self.update_title()

        self.token = self.load_token()
        self.reload()
        if self.token:
            self.show_inbox()
        else:
            self.show_token_form()
        self.main_window.show()
        macos.select_row(self.sidebar, 0)

    def on_running(self):
        if self.token:
            self.connect(self.token)

    def create_commands(self):
        mod, shift = toga.Key.MOD_1, toga.Key.SHIFT
        notification = toga.Group("Notification", order=25)
        labels = toga.Group("Labels", order=26)

        self.refresh_cmd = toga.Command(
            self.on_refresh,
            "Refresh",
            shortcut=mod + "r",
            icon="resources/refresh",
            tooltip="Check GitHub for new notifications",
            group=toga.Group.VIEW,
        )
        self.open_cmd = toga.Command(
            self.on_open,
            "Open on GitHub",
            shortcut=mod + "o",
            icon="resources/open",
            tooltip="Open the selected notifications in your browser",
            group=notification,
        )
        self.read_cmd = toga.Command(
            self.on_mark_read,
            "Mark as Read",
            shortcut=mod + shift + "u",
            icon="resources/read",
            tooltip="Mark the selected notifications as read",
            group=notification,
            section=1,
            order=1,
        )
        self.done_cmd = toga.Command(
            self.on_mark_done,
            "Mark as Done",
            shortcut=mod + "e",
            icon="resources/done",
            tooltip="Move the selected notifications out of the inbox",
            group=notification,
            section=1,
            order=2,
        )
        self.unsubscribe_cmd = toga.Command(
            self.on_unsubscribe,
            "Unsubscribe",
            shortcut=mod + shift + "m",
            icon="resources/unsubscribe",
            tooltip="Unsubscribe from the selected threads and mark them as done",
            group=notification,
            section=1,
            order=3,
        )
        self.new_label_cmd = toga.Command(
            self.on_new_label, "New Label", shortcut=mod + "l", group=labels, order=1
        )
        self.rename_label_cmd = toga.Command(
            self.on_rename_label, "Rename Label…", group=labels, order=2
        )
        self.delete_label_cmd = toga.Command(
            self.on_delete_label, "Delete Label…", group=labels, order=3
        )
        self.commands.add(
            self.refresh_cmd,
            self.open_cmd,
            self.read_cmd,
            self.done_cmd,
            self.unsubscribe_cmd,
            self.new_label_cmd,
            self.rename_label_cmd,
            self.delete_label_cmd,
        )
        if settings := toga.Command.standard(self, toga.Command.PREFERENCES):
            self.commands.add(settings)

        self.main_window.toolbar.add(
            self.refresh_cmd,
            self.open_cmd,
            self.read_cmd,
            self.done_cmd,
            self.unsubscribe_cmd,
        )

    ######################################################################
    # Layout
    ######################################################################

    def build_inbox(self) -> toga.Widget:
        # Sidebar: the built-in views, then the labels.
        self.sidebar = toga.Table(
            columns=[AccessorColumn("View", "name"), AccessorColumn("Count", "count")],
            show_headings=False,
            on_select=self.on_view_select,
            flex=1,
        )
        macos.make_sidebar(self.sidebar, [36])
        self.label_input = toga.TextInput(
            placeholder="New label", on_confirm=self.on_add_label, flex=1
        )
        sidebar = toga.Box(
            direction=COLUMN,
            children=[
                self.sidebar,
                toga.Box(
                    direction=ROW,
                    gap=6,
                    margin=8,
                    children=[
                        self.label_input,
                        toga.Button("Add", on_press=self.on_add_label),
                    ],
                ),
            ],
        )

        # The notifications in the current view.
        self.search = toga.TextInput(
            placeholder="Filter by title or repository",
            on_change=self.on_search,
            flex=1,
        )
        self.table = toga.Table(
            columns=[
                AccessorColumn("Title", "title"),
                AccessorColumn("Repository", "repo"),
                AccessorColumn("Type", "type"),
                AccessorColumn("Reason", "reason"),
                AccessorColumn("Labels", "labels"),
                AccessorColumn("Updated", "updated"),
            ],
            multiple_select=True,
            on_select=self.on_thread_select,
            on_activate=self.on_open,
            flex=1,
        )
        macos.set_column_widths(self.table, [170, 90, 120, 150, 85])
        self.status = toga.Label("", font_size=11, margin=(4, 10, 6, 10))
        listing = toga.Box(
            direction=COLUMN,
            children=[
                toga.Box(direction=ROW, margin=8, children=[self.search]),
                self.table,
                self.status,
            ],
        )

        # Details and labels for the selected notifications.
        self.detail_title = toga.Label("", font_size=15, font_weight=BOLD)
        self.detail_meta = toga.Label("")
        self.detail_open = toga.Button("Open on GitHub", on_press=self.on_open)
        self.detail_done = toga.Button("Mark as Done", on_press=self.on_mark_done)
        self.label_switches = toga.Box(direction=COLUMN, gap=4)
        detail = toga.Box(
            direction=ROW,
            gap=24,
            margin=14,
            children=[
                toga.Box(
                    direction=COLUMN,
                    gap=8,
                    flex=1,
                    children=[
                        self.detail_title,
                        self.detail_meta,
                        toga.Box(
                            direction=ROW,
                            gap=8,
                            children=[self.detail_open, self.detail_done],
                        ),
                    ],
                ),
                toga.Box(
                    direction=COLUMN,
                    gap=6,
                    width=260,
                    children=[
                        toga.Label("Labels", font_weight=BOLD),
                        self.label_switches,
                    ],
                ),
            ],
        )

        main = toga.SplitContainer(
            direction=toga.SplitContainer.HORIZONTAL,
            content=[
                (listing, 3),
                (toga.ScrollContainer(horizontal=False, content=detail), 1),
            ],
        )
        return toga.SplitContainer(content=[(sidebar, 1), (main, 5)])

    def build_token_form(self) -> toga.Widget:
        self.token_input = toga.PasswordInput(
            placeholder="ghp_…", on_confirm=self.on_save_token
        )
        self.token_message = toga.Label("")
        self.sign_out_button = toga.Button("Sign Out", on_press=self.on_sign_out)
        self.cancel_button = toga.Button("Cancel", on_press=self.on_cancel_token)
        form = toga.Box(
            direction=COLUMN,
            gap=12,
            width=480,
            children=[
                toga.Label("Connect to GitHub", font_size=20, font_weight=BOLD),
                toga.Label(
                    "Buzz reads your notifications using a personal access token\n"
                    "(classic) with the “notifications” scope. Fine-grained tokens\n"
                    "can't read notifications. The token is kept in your Keychain."
                ),
                toga.Box(
                    direction=ROW,
                    children=[
                        toga.Button(
                            "Create a Token on GitHub…",
                            on_press=lambda widget, **kwargs: webbrowser.open(
                                TOKEN_URL
                            ),
                        )
                    ],
                ),
                self.token_input,
                self.token_message,
                toga.Box(
                    direction=ROW,
                    gap=8,
                    children=[
                        self.sign_out_button,
                        toga.Box(flex=1),
                        self.cancel_button,
                        toga.Button("Connect", on_press=self.on_save_token),
                    ],
                ),
            ],
        )
        return toga.Box(
            direction=COLUMN,
            align_items=CENTER,
            justify_content=CENTER,
            children=[form],
        )

    def show_inbox(self):
        self.main_window.content = self.inbox_view

    def show_token_form(self, editing: bool = False):
        for button in (self.sign_out_button, self.cancel_button):
            button.style.display = PACK if editing else NONE
        self.token_message.text = ""
        self.main_window.content = self.token_view
        self.token_input.focus()

    def update_title(self):
        login = self.store.get_meta("login")
        self.main_window.title = (
            f"{self.formal_name} — {login}" if login else self.formal_name
        )

    ######################################################################
    # Showing data
    ######################################################################

    def reload(self):
        """Bring the sidebar and notification list up to date with the store."""
        self.labels = self.store.labels()
        counts = self.store.counts()
        sync_rows(
            self.sidebar.data,
            [
                {"key": key, "name": name, "count": counts.get(key) or ""}
                for key, name in VIEWS
            ]
            + [
                {
                    "key": label.id,
                    "name": f"{label.color}  {label.name}",
                    "count": counts.get(label.id) or "",
                }
                for label in self.labels
            ],
        )
        sync_rows(self.table.data, self.thread_rows())
        self.schedule_detail_update()

    def thread_rows(self) -> list[dict]:
        labels = self.store.labels_by_thread()
        return [
            self.thread_row(thread, labels.get(thread.id, []))
            for thread in self.store.threads(self.view, self.search.value)
        ]

    def thread_row(self, thread: Thread, labels: list[Label]) -> dict:
        if thread.unread:
            icon = self.icons["unread"]
        elif thread.done:
            icon = self.icons["check"]
        else:
            icon = self.icons["blank"]
        return {
            "key": thread.id,
            "title": (icon, thread.title),
            "repo": reference(thread),
            "type": describe_type(thread.type),
            "reason": describe_reason(thread.reason),
            "labels": ",  ".join(f"{label.color} {label.name}" for label in labels),
            "updated": ago(thread.updated_at),
        }

    def selected_threads(self) -> list[Thread]:
        rows = self.table.selection or []
        return [thread for row in rows if (thread := self.store.get(row.key))]

    def selected_index(self) -> int | None:
        rows = self.table.selection or []
        return min((self.table.data.index(row) for row in rows), default=None)

    def schedule_detail_update(self):
        # Selection events can arrive in bursts, and from inside a widget's own
        # handler; update once, after they've settled.
        if not self.detail_pending:
            self.detail_pending = True
            self.loop.call_soon(self.update_detail)

    def update_detail(self):
        self.detail_pending = False
        threads = self.selected_threads()
        connected = self.client is not None

        self.open_cmd.enabled = bool(threads)
        self.read_cmd.enabled = connected and any(t.unread for t in threads)
        self.done_cmd.enabled = connected and any(not t.done for t in threads)
        self.unsubscribe_cmd.enabled = connected and bool(threads)
        self.rename_label_cmd.enabled = self.delete_label_cmd.enabled = isinstance(
            self.view, int
        )
        self.detail_open.enabled = self.open_cmd.enabled
        self.detail_done.enabled = self.done_cmd.enabled

        if not threads:
            self.detail_title.text = "No notification selected"
            self.detail_meta.text = (
                "Select notifications to see their details and labels."
            )
        elif len(threads) == 1:
            thread = threads[0]
            state = "Done" if thread.done else "Unread" if thread.unread else "Read"
            self.detail_title.text = thread.title
            self.detail_meta.text = "  ·  ".join(
                [
                    reference(thread),
                    describe_type(thread.type),
                    describe_reason(thread.reason),
                    f"Updated {ago(thread.updated_at)}",
                    state,
                ]
            )
        else:
            self.detail_title.text = f"{len(threads)} notifications selected"
            self.detail_meta.text = "Label changes apply to all of them."

        assigned = self.store.labels_by_thread()
        switches = (
            tuple(t.id for t in threads),
            tuple(self.labels),
            tuple(tuple(assigned.get(t.id, ())) for t in threads),
        )
        if switches == self.shown_switches:
            return  # Don't rebuild (and flicker) the switches for nothing.
        self.shown_switches = switches

        self.label_switches.clear()
        if not self.labels:
            self.label_switches.add(
                toga.Label("Add a label in the sidebar\nto start organising.")
            )
        for label in self.labels:
            self.label_switches.add(
                toga.Switch(
                    f"{label.color} {label.name}",
                    value=bool(threads)
                    and all(label in assigned.get(t.id, []) for t in threads),
                    enabled=bool(threads),
                    on_change=partial(self.on_toggle_label, label.id),
                )
            )

    def sidebar_index(self, key: str | int) -> int | None:
        for index, row in enumerate(self.sidebar.data):
            if row.key == key:
                return index
        return None

    def select_view(self, key: str | int):
        self.view = key
        self.table.data = self.thread_rows()
        self.reload()
        if (index := self.sidebar_index(key)) is not None:
            macos.select_row(self.sidebar, index)

    ######################################################################
    # Handlers: navigation
    ######################################################################

    def on_view_select(self, widget, **kwargs):
        row = self.sidebar.selection
        if row is None:
            # Keep showing the current view, rather than an empty sidebar.
            if (index := self.sidebar_index(self.view)) is not None:
                self.loop.call_soon(macos.select_row, self.sidebar, index)
        elif row.key != self.view:
            self.view = row.key
            self.table.data = self.thread_rows()
        self.schedule_detail_update()

    def on_search(self, widget, **kwargs):
        self.table.data = self.thread_rows()
        self.schedule_detail_update()

    def on_thread_select(self, widget, **kwargs):
        self.schedule_detail_update()

    ######################################################################
    # Handlers: notification actions
    ######################################################################

    def on_refresh(self, command=None, **kwargs):
        if self.client is not None:
            self.force_full_sync = True
            self.wake.set()

    async def on_open(self, widget=None, **kwargs):
        threads = self.selected_threads()[:OPEN_LIMIT]
        for thread in threads:
            webbrowser.open(html_url(thread))
        # Opening a notification on github.com marks it as read; do the same.
        await self.update_threads(
            [t for t in threads if t.unread],
            self.store.mark_read,
            lambda client, thread_id: client.mark_read(thread_id),
        )

    async def on_mark_read(self, command=None, **kwargs):
        await self.update_threads(
            [t for t in self.selected_threads() if t.unread],
            self.store.mark_read,
            lambda client, thread_id: client.mark_read(thread_id),
        )

    async def on_mark_done(self, widget=None, **kwargs):
        await self.update_threads(
            [t for t in self.selected_threads() if not t.done],
            self.store.mark_done,
            lambda client, thread_id: client.mark_done(thread_id),
            select_next=True,
        )

    async def on_unsubscribe(self, command=None, **kwargs):
        async def unsubscribe(client: GitHubClient, thread_id: str):
            await client.unsubscribe(thread_id)
            await client.mark_done(thread_id)

        await self.update_threads(
            self.selected_threads(),
            self.store.mark_done,
            unsubscribe,
            select_next=True,
        )

    async def update_threads(
        self,
        threads: list[Thread],
        local: Callable[[Iterable[str]], None],
        remote: Callable[[GitHubClient, str], Awaitable[None]],
        select_next: bool = False,
    ):
        """Change threads locally straight away, then on GitHub; if GitHub
        refuses, put them back the way they were."""
        client = self.client
        if not threads or client is None:
            return

        index, before = self.selected_index(), len(self.table.data)
        local([t.id for t in threads])
        self.reload()
        # Like on github.com, move on to the next notification when the
        # selected ones leave the list.
        remaining = len(self.table.data)
        if select_next and index is not None and 0 < remaining < before:
            macos.select_row(self.table, min(index, remaining - 1))

        limit = asyncio.Semaphore(CONCURRENCY)

        async def send(thread: Thread):
            async with limit:
                await remote(client, thread.id)

        results = await asyncio.gather(
            *(send(t) for t in threads), return_exceptions=True
        )
        failures = [
            (t, r) for t, r in zip(threads, results) if isinstance(r, Exception)
        ]
        if failures:
            self.store.restore(t for t, _ in failures)
            self.reload()
            await self.main_window.dialog(
                toga.ErrorDialog(
                    "Couldn't update GitHub",
                    f"{len(failures)} of {len(threads)} notifications weren't "
                    f"changed.\n\n{failures[0][1]}",
                )
            )

    ######################################################################
    # Handlers: labels
    ######################################################################

    async def on_add_label(self, widget=None, **kwargs):
        try:
            self.store.create_label(self.label_input.value)
        except ValueError as exc:
            await self.main_window.dialog(
                toga.ErrorDialog("Couldn't add the label", str(exc))
            )
            return
        self.label_input.value = ""
        self.reload()

    def on_new_label(self, command=None, **kwargs):
        if self.main_window.content is self.inbox_view:
            self.label_input.focus()

    def current_label(self) -> Label | None:
        return next((label for label in self.labels if label.id == self.view), None)

    async def on_rename_label(self, command=None, **kwargs):
        if (label := self.current_label()) is None:
            return
        name = await self.ask_text(
            "Rename Label", f"Rename “{label.name}” to:", label.name, "Rename"
        )
        if name is None:
            return
        try:
            self.store.rename_label(label.id, name)
        except ValueError as exc:
            await self.main_window.dialog(
                toga.ErrorDialog("Couldn't rename the label", str(exc))
            )
            return
        self.reload()

    async def on_delete_label(self, command=None, **kwargs):
        if (label := self.current_label()) is None:
            return
        count = self.store.counts().get(label.id, 0)
        if await self.main_window.dialog(
            toga.ConfirmDialog(
                "Delete Label?",
                f"“{label.name}” will be removed from {count} notification"
                f"{'' if count == 1 else 's'}. The notifications themselves stay "
                "where they are.",
            )
        ):
            self.store.delete_label(label.id)
            self.select_view(INBOX)

    def on_toggle_label(self, label_id: int, switch: toga.Switch, **kwargs):
        if thread_ids := [t.id for t in self.selected_threads()]:
            self.store.set_label(label_id, thread_ids, switch.value)
            self.reload()

    async def ask_text(
        self, title: str, prompt: str, value: str = "", action: str = "OK"
    ) -> str | None:
        """Ask for a line of text in a small window. Returns None if cancelled."""
        answer = self.loop.create_future()
        window = toga.Window(
            title=title, size=(380, 140), resizable=False, minimizable=False
        )

        def finish(result: str | None):
            if not answer.done():
                answer.set_result(result)
            window.close()

        text = toga.TextInput(
            value=value, on_confirm=lambda widget, **kwargs: finish(text.value)
        )
        window.content = toga.Box(
            direction=COLUMN,
            gap=10,
            margin=16,
            children=[
                toga.Label(prompt),
                text,
                toga.Box(
                    direction=ROW,
                    gap=8,
                    children=[
                        toga.Box(flex=1),
                        toga.Button(
                            "Cancel", on_press=lambda widget, **kwargs: finish(None)
                        ),
                        toga.Button(
                            action, on_press=lambda widget, **kwargs: finish(text.value)
                        ),
                    ],
                ),
            ],
        )

        def on_close(window, **kwargs):
            if not answer.done():
                answer.set_result(None)
            return True

        window.on_close = on_close
        window.show()
        text.focus()
        return await answer

    ######################################################################
    # Handlers: account
    ######################################################################

    def preferences(self):
        self.show_token_form(editing=True)

    async def on_save_token(self, widget=None, **kwargs):
        token = self.token_input.value.strip()
        if not token:
            self.token_message.text = "Paste a token first."
            return

        self.token_message.text = "Checking the token…"
        client = self.make_client(token)
        try:
            login = await client.validate()
        except GitHubError as exc:
            self.token_message.text = textwrap.fill(str(exc), 60)
            return
        finally:
            await client.aclose()

        try:
            self.save_token(token)
        except credentials.KeyringError as exc:
            self.token_message.text = textwrap.fill(
                f"Couldn't save the token in the Keychain: {exc}", 60
            )
            return

        if login != self.store.get_meta("login"):
            # A different account: its notifications aren't this one's.
            self.store.forget_threads()
            self.store.set_meta("last_modified", None)
            self.store.set_meta("login", login)
            self.update_title()

        self.token = token
        self.token_input.value = ""
        self.show_inbox()
        self.reload()
        self.connect(token)

    def on_cancel_token(self, widget, **kwargs):
        self.token_input.value = ""
        self.show_inbox()

    async def on_sign_out(self, widget, **kwargs):
        if await self.main_window.dialog(
            toga.ConfirmDialog(
                "Sign Out?",
                "Buzz will forget the token and stop checking for notifications. "
                "Your labels are kept.",
            )
        ):
            self.disconnect()
            self.delete_token()
            self.token = None
            self.show_token_form()

    ######################################################################
    # Syncing with GitHub
    ######################################################################

    def connect(self, token: str):
        self.disconnect()
        self.client = self.make_client(token)
        self.force_full_sync = True
        self.sync_task = self.loop.create_task(self.sync_forever())
        self.schedule_detail_update()

    def disconnect(self):
        if self.sync_task is not None:
            self.sync_task.cancel()
            self.sync_task = None
        if self.client is not None:
            self.loop.create_task(self.client.aclose())
            self.client = None
        self.status.text = "Not connected to GitHub."
        self.schedule_detail_update()

    async def sync_forever(self):
        while True:
            delay = await self.sync_once()
            try:
                await asyncio.wait_for(self.wake.wait(), delay)
            except TimeoutError:
                pass
            self.wake.clear()

    async def sync_once(self) -> float:
        """Fetch the inbox once. Returns how long to wait before the next time."""
        full = (
            self.force_full_sync
            or time.monotonic() - self.last_full_sync > FULL_SYNC_EVERY
        )
        self.force_full_sync = False
        self.status.text = "Checking GitHub…"
        try:
            snapshot = await self.client.inbox(
                None if full else self.store.get_meta("last_modified")
            )
            if snapshot.threads is not None:
                self.store.sync_inbox(snapshot.threads, snapshot.complete)
                self.store.set_meta("last_modified", snapshot.last_modified)
            if full:
                self.last_full_sync = time.monotonic()
        except AuthenticationError as exc:
            self.status.text = f"⚠️ {exc} Update the token in Settings (⌘,)."
            return FULL_SYNC_EVERY
        except GitHubError as exc:
            self.status.text = f"⚠️ {exc}"
            return MIN_POLL_INTERVAL
        except Exception as exc:  # noqa: BLE001 -- keep checking after a bug
            traceback.print_exc()
            self.status.text = f"⚠️ Couldn't update the inbox: {exc!r}"
            return MIN_POLL_INTERVAL

        self.reload()
        checked = datetime.now().strftime("%H:%M")
        self.status.text = f"Up to date. Last checked at {checked}."
        if not snapshot.complete:
            self.status.text += (
                f" Only the newest {len(snapshot.threads)} were fetched."
            )
        return snapshot.poll_interval


def main():
    return Buzz()
