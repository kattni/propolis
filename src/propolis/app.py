"""
Triage your GitHub notifications with labels that keep their order.
"""

import asyncio
import logging
import textwrap
import webbrowser
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import httpx2
import toga
from toga.sources import AccessorColumn, ListSource
from toga.style.pack import CENTER, COLUMN, RIGHT, ROW, Pack

from propolis.core.accounts import Accounts
from propolis.core.auth import (
    DeviceCode,
    SignInError,
    forget_secret,
    forget_token,
    load_secret,
    load_token,
    save_secret,
    sign_in,
)
from propolis.core.github import GitHub
from propolis.core.remote import PORT, NotPaired, Remote, RemoteError, Server, new_code
from propolis.core.state import ATTENTION
from propolis.core.store import Store
from propolis.core.sync import Sync, WrongSignIn
from propolis.core.triage import AddError, Triage
from propolis.macos import (
    ListKeys,
    disable_window_tabs,
    fix_column_width,
    has_focus,
    select_row,
    template_icon,
)
from propolis.views import (
    INBOX,
    Filters,
    SidebarItem,
    ThreadRow,
    filter_options,
    rows,
    shortcut_text,
    sidebar,
)

logger = logging.getLogger(__name__)


OCTICONS = Path(__file__).parent / "resources" / "octicons"


class TypeColumn(AccessorColumn):
    """Each thread's type, shown as GitHub's icon for it rather than in words."""

    def __init__(self):
        super().__init__("", "icon")
        self._icons: dict[str, toga.Icon] = {}

    def icon(self, row) -> toga.Icon:
        # One icon per type, so each can carry its type's name for VoiceOver.
        if row.kind not in self._icons:
            icon = toga.Icon(OCTICONS / row.icon)
            self._icons[row.kind] = template_icon(icon, row.kind or "Thread")
        return self._icons[row.kind]

    def text(self, row, default: str | None = None) -> str | None:
        return default

PLACEHOLDER = """<html style="color-scheme: light dark"><body style="
    font: 13px -apple-system, sans-serif; color: gray;
    display: grid; place-items: center; height: 90vh; margin: 0">
Select a thread to read it here.</body></html>"""
"""What the web view shows before a thread is selected."""

THREAD = toga.Group("Thread", order=22)
LABELS = toga.Group("Labels", order=25)
ACCOUNTS = toga.Group("Switch Account", parent=toga.Group.FILE, section=2, order=1)


class Propolis(toga.App):
    def startup(self):
        self.paths.data.mkdir(parents=True, exist_ok=True)
        self.accounts = Accounts(self.paths.data)
        self._open_store()
        self.github: GitHub | None = None
        self.sync: Sync | None = None
        self.triage: Triage | Remote | None = None
        """Your triage, on this Mac or through Propolis on another one."""
        self.remote: Remote | None = None
        """Propolis on the other Mac, if this account is there."""
        self.server: Server | None = None
        """Lets Propolis on another Mac triage through this one."""
        self.view = INBOX
        self._syncing = False
        self._synced_at: datetime | None = None
        self._poller: asyncio.Task | None = None
        self._signing_in: asyncio.Task | None = None
        self._shown: str | None = None
        self._numbered: dict[int, str] = {}
        self._label_commands: list[toga.Command] = []
        self._label_items: list[tuple] = []
        self._account_commands: list[toga.Command] = []
        self._list_keys = ListKeys(self._on_number, self._on_delete, self._on_move)
        disable_window_tabs()

        # Each command's tooltip also describes it in the Keyboard Shortcuts
        # window, so the two can't drift apart.
        self.refresh_command = toga.Command(
            self._refresh,
            "Refresh",
            shortcut=toga.Key.MOD_1 + "r",
            tooltip="Sync with GitHub now. Propolis also syncs every minute. "
            "Through another Mac, this has that Mac sync.",
            group=toga.Group.VIEW,
        )
        self.add_thread_command = toga.Command(
            self._add_thread,
            "Add Thread…",
            shortcut=toga.Key.MOD_1 + "t",
            tooltip="Add an issue, pull request or discussion to the Inbox by its "
            "number, such as beeware/toga#123, so you can file it like any other "
            "thread.",
            group=THREAD,
            order=-1,
        )
        self.open_command = toga.Command(
            self._open,
            "Open in Browser",
            shortcut=toga.Key.MOD_1 + "o",
            tooltip="Open the selected thread in your browser. "
            "Double-clicking a thread does the same.",
            group=THREAD,
        )
        self.done_command = toga.Command(
            self._done,
            "Done",
            shortcut=toga.Key.MOD_1 + "d",
            tooltip="Take the selected thread out of every view, and mark it done "
            "on GitHub. Activity from someone else brings it back to the Inbox.",
            group=THREAD,
        )
        self.unlabel_command = toga.Command(
            self._unlabel,
            "Remove from This View",
            # Delete, handled by ListKeys. See _on_delete.
            tooltip="Take this view's label off the selected thread. In Attention "
            "required, this is how you say you've dealt with it.",
            group=THREAD,
        )
        self.new_label_command = toga.Command(
            self._new_label,
            "New Label…",
            shortcut=toga.Key.MOD_1 + "n",
            tooltip="Create a label, and choose whether activity flags its threads "
            "for attention.",
            group=LABELS,
            section=1,
        )
        self.edit_label_command = toga.Command(
            self._edit_label,
            "Edit Label…",
            shortcut=toga.Key.MOD_1 + "i",
            tooltip="Rename the label selected in the sidebar, or change whether "
            "activity flags its threads. Double-clicking a label does the same.",
            group=LABELS,
            section=1,
            order=1,
        )
        self.delete_label_command = toga.Command(
            self._delete_label,
            "Delete Label…",
            shortcut=toga.Key.MOD_1 + toga.Key.SHIFT + toga.Key.BACKSPACE,
            tooltip="Delete the label selected in the sidebar, after asking. Its "
            "threads go back to the Inbox, unless they have other labels.",
            group=LABELS,
            section=2,
        )
        self.move_up_command = toga.Command(
            self._move_label_up,
            "Move Label Up",
            # ⌘↑, handled by ListKeys. See _on_move.
            tooltip="Move the selected label up the sidebar, changing its number.",
            group=LABELS,
            section=1,
            order=2,
        )
        self.move_down_command = toga.Command(
            self._move_label_down,
            "Move Label Down",
            # ⌘↓, handled by ListKeys. See _on_move.
            tooltip="Move the selected label down the sidebar, changing its number.",
            group=LABELS,
            section=1,
            order=3,
        )
        self.sign_out_command = toga.Command(
            self._sign_out,
            "Sign Out of GitHub",
            tooltip="Forget the saved GitHub sign-in.",
            group=toga.Group.FILE,
        )
        self.share_command = toga.Command(
            self._show_share,
            "Share This Inbox…",
            tooltip="Let Propolis on another Mac triage this account's "
            "notifications through this one, so that Mac doesn't need to sign in "
            "to GitHub.",
            group=toga.Group.FILE,
            section=1,
        )
        self.disconnect_command = toga.Command(
            self._disconnect_command,
            "Disconnect from Other Mac",
            tooltip="Stop triaging through Propolis on the other Mac. Nothing "
            "changes there.",
            group=toga.Group.FILE,
            section=1,
            order=1,
        )
        self.add_account_command = toga.Command(
            self._add_account,
            "Add Account…",
            tooltip="Triage another GitHub account's notifications, such as those "
            "of someone whose inbox you're taking over. Sign it in to GitHub, or "
            "connect to Propolis on a Mac that's signed in to it.",
            group=toga.Group.FILE,
            section=2,
        )
        self.clear_filters_command = toga.Command(
            self._clear_filters,
            "Clear Filters",
            shortcut=toga.Key.MOD_1 + "f",
            tooltip="Show every thread in the view again. Filters stay set as "
            "you switch views, and reset when you quit.",
            group=toga.Group.VIEW,
        )
        self.shortcuts_command = toga.Command(
            self._show_shortcuts,
            "Keyboard Shortcuts",
            shortcut=toga.Key.MOD_1 + "/",
            tooltip="Show this list.",
            group=toga.Group.HELP,
        )
        self.commands.add(
            self.refresh_command,
            self.add_thread_command,
            self.open_command,
            self.done_command,
            self.unlabel_command,
            self.new_label_command,
            self.edit_label_command,
            self.delete_label_command,
            self.move_up_command,
            self.move_down_command,
            self.sign_out_command,
            self.share_command,
            self.disconnect_command,
            self.add_account_command,
            self.clear_filters_command,
            self.shortcuts_command,
        )
        self._rebuild_account_commands()
        self._shortcuts_window: toga.Window | None = None
        self._share_window: toga.Window | None = None
        self._filters = Filters()
        self._filter_options = None
        self._updating_filters = False

        self.main_window = toga.MainWindow(size=(1200, 800))
        self.main_window.content = toga.Box()
        self._set_ready(False)
        self.main_window.show()

    async def on_running(self):
        await self._open_account()

    async def on_exit(self):
        self._list_keys.remove()
        await self._finish_tasks()
        await self._stop()
        if self.store:
            self.store.close()
        return True

    async def _finish_tasks(self):
        """Cancel every other task and wait for it to end, as ``asyncio.run``
        does on the way out. Toga's event loop just stops.

        AnyIO sets up each HTTPS connection in a worker thread, because the SSL
        context comes from truststore. The thread stops only when the task that
        opened the connection ends, and Python won't exit while it's running.
        """
        others = asyncio.all_tasks() - {asyncio.current_task()}
        for task in others:
            task.cancel()
        if others:
            await asyncio.wait(others)

    # Signing in and out

    def _show_sign_in(self, message: str = ""):
        self._set_ready(False)
        self.main_window.title = self.formal_name
        self._verification_uri = None
        centred = Pack(text_align=CENTER)
        self.code_label = toga.Label(
            "…",
            style=Pack(
                text_align=CENTER,
                font_size=28,
                font_family="monospace",
                margin=(8, 0, 16),
            ),
        )
        self.open_github_button = toga.Button(
            "Open GitHub", on_press=self._open_verification, enabled=False
        )
        if login := self.store.get_meta("login"):
            intro = f"Sign in to GitHub as {login} to triage their notifications."
            other_mac = []
        else:
            # An account with history of its own stays on this Mac.
            intro = "Sign in to GitHub to triage your notifications."
            other_mac = [
                toga.Label(
                    textwrap.fill(
                        "Or triage through Propolis on another Mac that's signed "
                        "in. This Mac then doesn't need to sign in.",
                        56,
                    ),
                    style=Pack(text_align=CENTER, margin=(28, 0, 8)),
                ),
                toga.Button("Connect to Another Mac…", on_press=self._show_connect),
            ]
        panel = toga.Box(
            children=[
                toga.Label(f"{message}\n\n{intro}" if message else intro, style=centred),
                toga.Label("Enter this code at github.com/login/device:", style=centred),
                self.code_label,
                self.open_github_button,
                *other_mac,
            ],
            style=Pack(direction=COLUMN, width=380, gap=4),
        )
        self.main_window.content = toga.Box(
            children=[panel],
            style=Pack(direction=COLUMN, justify_content=CENTER, align_items=CENTER),
        )
        self._signing_in = asyncio.create_task(self._sign_in())

    def _show_code(self, code: DeviceCode):
        self.code_label.text = code.user_code
        self._verification_uri = code.verification_uri
        self.open_github_button.enabled = True

    def _open_verification(self, widget, **kwargs):
        if self._verification_uri:
            webbrowser.open(self._verification_uri)

    async def _sign_in(self):
        try:
            token = await sign_in(self._show_code, self.accounts.current)
        except (SignInError, httpx2.HTTPError) as error:
            await self.main_window.dialog(
                toga.ErrorDialog("Couldn't sign in", str(error))
            )
            self._show_sign_in()
            return
        await self._start(token)

    async def _start(self, token: str):
        """Show your triage, sync with GitHub, and share it with another Mac if
        you've chosen to."""
        self.github = GitHub(token)
        self.sync = Sync(self.store, self.github)
        self.triage = Triage(self.store, self.github)
        self._show_triage()
        if self.store.get_meta("share") and (error := await self._start_sharing()):
            self.status.text = error
        await self._refresh()
        self._poller = asyncio.create_task(self._poll())

    def _show_triage(self):
        self._show_main()
        self._set_ready(True)
        self._reload()

    async def _poll(self):
        """Sync in the background, as often as GitHub allows."""
        while self.sync is not None:
            await asyncio.sleep(self._poll_delay())
            try:
                await self._refresh()
            except Exception:
                # Keep polling; the next sync may well succeed.
                logger.exception("Background sync failed")
                self.status.text = "Sync failed; see the log"

    def _poll_delay(self) -> float:
        return self.github.poll_interval if self.github else 60

    async def _sign_out(self, command, **kwargs):
        forget_token(self.accounts.current)
        await self._stop()
        self._show_sign_in()

    async def _stop(self):
        """Stop syncing and sharing, and close the connection to GitHub or to
        the other Mac."""
        # Polling stops by itself once self.sync or self.remote is None.
        # Cancelling it here would also cancel this call if it came from a
        # background sync.
        if self._poller and self._poller is not asyncio.current_task():
            self._poller.cancel()
        self._poller = None
        await self._close_server()
        if self.github:
            await self.github.aclose()
        if self.remote:
            await self.remote.aclose()
        self.github = self.sync = self.triage = self.remote = None

    def _set_ready(self, ready: bool):
        """Enable the commands that work once your triage is showing."""
        for command in [
            self.refresh_command,
            self.add_thread_command,
            self.open_command,
            self.done_command,
            self.new_label_command,
            self.clear_filters_command,
            *self._label_commands,
        ]:
            command.enabled = ready
        self.sign_out_command.enabled = ready and self.github is not None
        self.share_command.enabled = ready and self.github is not None
        self.disconnect_command.enabled = self.remote is not None
        self._enable_view_commands(ready)

    def _enable_view_commands(self, ready: bool = True):
        """Commands that depend on the selected view."""
        self.unlabel_command.enabled = ready and self.view != INBOX
        editable = ready and self.view not in (INBOX, ATTENTION)
        for command in [
            self.edit_label_command,
            self.delete_label_command,
            self.move_up_command,
            self.move_down_command,
        ]:
            command.enabled = editable

    # The main window

    def _show_main(self):
        self.sidebar_data = ListSource(accessors=["text", "view"], data=[])
        self.sidebar_table = toga.Table(
            columns=[AccessorColumn("View", "text")],
            data=self.sidebar_data,
            show_headings=False,
            on_select=self._on_view_selected,
            on_activate=self._edit_label,
            style=Pack(flex=1),
        )
        self.thread_table = toga.Table(
            columns=[
                TypeColumn(),
                AccessorColumn("Title", "title"),
                AccessorColumn("Repo", "repo"),
                AccessorColumn("When", "when"),
            ],
            on_select=self._on_thread_selected,
            on_activate=self._open,
            style=Pack(flex=1),
        )
        fix_column_width(self.thread_table, 0, 22)
        self.repo_filter, self.reason_filter, self.kind_filter = [
            toga.Selection(
                accessor="name", on_change=self._on_filter_changed, style=Pack(flex=1)
            )
            for _ in range(3)
        ]
        thread_pane = toga.Box(
            children=[
                toga.Box(
                    children=[
                        self.repo_filter,
                        self.reason_filter,
                        self.kind_filter,
                        toga.Button("Clear", on_press=self._clear_filters),
                    ],
                    style=Pack(direction=ROW, gap=8, margin=(6, 8)),
                ),
                self.thread_table,
            ],
            style=Pack(direction=COLUMN),
        )
        self._filter_options = None
        self.web_view = toga.WebView()
        self.web_view.set_content("about:blank", PLACEHOLDER)
        self.status = toga.Label("", style=Pack(margin=(4, 10)))
        self.main_window.content = toga.Box(
            children=[
                toga.SplitContainer(
                    content=[
                        (self.sidebar_table, 1),
                        (
                            toga.SplitContainer(
                                direction=toga.SplitContainer.HORIZONTAL,
                                content=[(thread_pane, 2), (self.web_view, 3)],
                            ),
                            5,
                        ),
                    ],
                    style=Pack(flex=1),
                ),
                self.status,
            ],
            style=Pack(direction=COLUMN),
        )

    def _reload(self):
        """Bring the sidebar, thread list and Labels menu up to date."""
        items = sidebar(self.triage)
        views = [item.view for item in items]
        if self.view not in views:
            # The label being viewed was deleted.
            self.view = INBOX
        self._numbered = {item.number: item.view for item in items if item.number}
        # Update rows in place where possible, so selections survive.
        if [row.view for row in self.sidebar_data] == views:
            for row, item in zip(self.sidebar_data, items):
                if row.text != item.text:
                    row.text = item.text
        else:
            # Labels were added, deleted or reordered.
            self.sidebar_data.clear()
            for item in items:
                self.sidebar_data.append({"text": item.text, "view": item.view})
            select_row(self.sidebar_table, views.index(self.view))
        if [(item.view, item.name, item.number) for item in items[2:]] != self._label_items:
            self._rebuild_label_commands(items[2:])
        self._update_filter_options()
        self._show_rows(rows(self.triage, self.view, self._filters))
        name = next(item.name for item in items if item.view == self.view)
        filtered = " (filtered)" if self._filters.active else ""
        account = (
            f" – {self._account_name(self.accounts.current)}"
            if len(self.accounts.names) > 1 or self.remote
            else ""
        )
        self.main_window.title = f"{name}{filtered}{account} – {self.formal_name}"
        self._enable_view_commands()

    def _show_rows(self, new_rows: list[ThreadRow]):
        data = self.thread_table.data
        keys = [row.key for row in new_rows]
        if [row.key for row in data] == keys:
            for row, new in zip(data, new_rows):
                for field, value in asdict(new).items():
                    if getattr(row, field) != value:
                        setattr(row, field, value)
            return
        # The list has changed, for example because a sync brought in a new
        # thread. Replace it, and keep the selected thread selected.
        selected = self.thread_table.selection
        self.thread_table.data = [asdict(row) for row in new_rows]
        if selected is not None and selected.key in keys:
            select_row(self.thread_table, keys.index(selected.key))

    def _on_view_selected(self, widget, **kwargs):
        if (row := widget.selection) and row.view != self.view:
            self.view = row.view
            self._reload()

    async def _on_thread_selected(self, widget, **kwargs):
        """Show the selected thread in the web view, which counts as viewing it."""
        row = widget.selection
        if row is None or row.key == self._shown:
            return
        self._shown = row.key
        if row.url:
            self.web_view.url = row.url
        await self._do(self.triage.view(row.key))
        self._reload()

    async def _refresh(self, command=None, **kwargs):
        if self.remote:
            await self._refresh_remote()
            return
        if self._syncing or self.sync is None:
            return
        self._syncing = True
        self.status.text = "Syncing…"
        try:
            await self.sync.run_once()
        except httpx2.HTTPStatusError as error:
            if error.response.status_code == 401:
                forget_token(self.accounts.current)
                await self._stop()
                self._show_sign_in("GitHub no longer accepts the saved sign-in.")
                return
            self.status.text = f"Sync failed: GitHub returned {error.response.status_code}"
        except (httpx2.HTTPError, RuntimeError) as error:
            self.status.text = f"Sync failed: {error}"
        except WrongSignIn as error:
            forget_token(self.accounts.current)
            await self._stop()
            self._show_sign_in(str(error))
            return
        else:
            self._name_account(self.store.get_meta("login"))
            self._synced_at = datetime.now()
            self._show_synced()
        finally:
            self._syncing = False
        self._reload()

    def _show_synced(self):
        if self._synced_at:
            shared = " Sharing this inbox with another Mac." if self.server else ""
            self.status.text = f"Last synced at {self._synced_at:%H:%M}.{shared}"

    # Sharing this inbox with another Mac

    def _show_share(self, command, **kwargs):
        if self._share_window in self.windows:
            self._share_window.show()
            return
        login = self.store.get_meta("login")
        code = toga.TextInput(
            value=self._share_code(),
            readonly=True,
            style=Pack(flex=1, font_family="monospace"),
        )
        message = toga.Label("", style=Pack(font_size=11, height=36))

        async def share(widget, **kwargs):
            message.text = ""
            if not widget.value:
                self.store.set_meta("share", "")
                await self._close_server()
            elif error := await self._start_sharing():
                message.text = textwrap.fill(error, 64)
                widget.value = False
            else:
                self.store.set_meta("share", "1")
            self._show_synced()

        def new(widget, **kwargs):
            code.value = new_code()
            save_secret(self._share_code_name(), code.value)
            if self.server:
                self.server.code = code.value
            message.text = textwrap.fill(
                "A Mac using the old code has to connect again with this one.", 64
            )

        explanation = (
            f"Propolis on another Mac can triage {login}'s notifications through "
            "this one, so it doesn't need to sign in to GitHub. Everything it does "
            "happens here.",
            f"To reach this Mac from elsewhere, point a Cloudflare tunnel at "
            f"http://localhost:{PORT}. Then, on the other Mac, choose File → Add "
            "Account…, then Connect to Another Mac…, and enter the tunnel's "
            "address and this code:",
        )
        window = toga.Window(title="Share This Inbox", size=(500, 320), resizable=False)
        window.content = toga.Box(
            children=[
                toga.Switch(
                    "Share this inbox", value=self.server is not None, on_change=share
                ),
                *[toga.Label(textwrap.fill(text, 66)) for text in explanation],
                code,
                message,
                toga.Box(
                    children=[
                        toga.Button("New Code", on_press=new),
                        toga.Box(style=Pack(flex=1)),
                        toga.Button(
                            "Done", on_press=lambda widget, **kwargs: window.close()
                        ),
                    ],
                    style=Pack(direction=ROW, gap=8),
                ),
            ],
            style=Pack(direction=COLUMN, margin=16, gap=12),
        )
        self._share_window = window
        window.show()

    async def _start_sharing(self) -> str | None:
        """Let another Mac triage through this one. Returns why it can't, if it
        can't."""
        if self.server:
            return None
        server = Server(
            self.store,
            self.triage,
            self._share_code(),
            refresh=self._refresh,
            on_change=self._reload,
        )
        try:
            await server.start(PORT)
        except OSError as error:
            await server.close()
            return f"Couldn't share this inbox on port {PORT}: {error.strerror or error}"
        self.server = server
        return None

    async def _close_server(self):
        if self.server:
            await self.server.close()
            self.server = None

    def _share_code_name(self) -> str:
        return f"share:{self.accounts.current}"

    def _share_code(self) -> str:
        if (code := load_secret(self._share_code_name())) is None:
            code = new_code()
            save_secret(self._share_code_name(), code)
        return code

    # Triaging through Propolis on another Mac

    def _show_connect(self, widget, **kwargs):
        address = toga.TextInput(
            placeholder="https://propolis.example.com", style=Pack(flex=1)
        )
        code = toga.PasswordInput(placeholder="Code", style=Pack(flex=1))
        message = toga.Label(
            textwrap.fill(
                "Propolis on the other Mac shows its code in File → Share This "
                "Inbox….",
                64,
            ),
            style=Pack(font_size=11, height=36),
        )
        window = toga.Window(
            title="Connect to Another Mac", size=(460, 200), resizable=False
        )

        async def connect(widget, **kwargs):
            connect_button.enabled = False
            message.text = "Connecting…"
            try:
                remote = Remote(address.value, code.value)
            except RemoteError as error:
                message.text = textwrap.fill(str(error), 64)
                connect_button.enabled = True
                return
            try:
                await remote.fetch()
            except RemoteError as error:
                await remote.aclose()
                message.text = textwrap.fill(str(error), 64)
                connect_button.enabled = True
                return
            window.close()
            await self._use_remote(remote, code.value)

        connect_button = toga.Button("Connect", on_press=connect)
        code.on_confirm = connect
        window.content = toga.Box(
            children=[
                address,
                code,
                message,
                toga.Box(
                    children=[
                        toga.Box(style=Pack(flex=1)),
                        toga.Button(
                            "Cancel", on_press=lambda widget, **kwargs: window.close()
                        ),
                        connect_button,
                    ],
                    style=Pack(direction=ROW, gap=8),
                ),
            ],
            style=Pack(direction=COLUMN, margin=16, gap=8),
        )
        window.show()
        address.focus()

    async def _use_remote(self, remote: Remote, code: str):
        """Triage this account through Propolis on another Mac from now on."""
        if self._signing_in:
            self._signing_in.cancel()
        account = self.accounts.current
        save_secret(f"connect:{account}", code)
        self.accounts.connect(account, remote.url)
        self.store.close()
        self.store = None
        await self._start_remote(remote)

    async def _start_remote(self, remote: Remote):
        self.remote = self.triage = remote
        self._show_waiting(f"Connecting to {remote.host}…")
        try:
            await remote.fetch()
        except NotPaired as error:
            await self._disconnect(str(error))
            return
        except RemoteError as error:
            self._show_waiting(str(error), retry=True)
            return
        self._name_account(remote.login)
        self._show_triage()
        self.status.text = self._remote_status()
        self._poller = asyncio.create_task(self._watch_remote())

    async def _watch_remote(self):
        """Keep up with the other Mac: its syncs, and triage there."""
        remote = self.remote
        delay = 5
        while self.remote is remote:
            try:
                changed = await remote.fetch(wait=True)
            except NotPaired as error:
                await self._disconnect(str(error))
                return
            except RemoteError as error:
                self.status.text = f"{error} Trying again…"
                await asyncio.sleep(delay)
                delay = min(delay * 2, 120)
                continue
            delay = 5
            if changed:
                self._reload()
            self.status.text = self._remote_status()

    async def _refresh_remote(self):
        self.status.text = f"Asking {self.remote.host} to sync with GitHub…"
        if await self._do(self.remote.refresh()):
            self.status.text = self._remote_status()
            self._reload()

    def _remote_status(self) -> str:
        return (
            f"Triaging through Propolis at {self.remote.host}, signed in to GitHub "
            f"as {self.remote.login}. Up to date at {datetime.now():%H:%M}."
        )

    async def _do(self, action: Awaitable) -> bool:
        """Do a triage action. Through another Mac it can fail, so say why, and
        return False."""
        try:
            await action
        except NotPaired as error:
            await self._disconnect(str(error))
            return False
        except RemoteError as error:
            self.status.text = str(error)
            return False
        return True

    def _show_waiting(self, message: str, retry: bool = False):
        """Shown until the other Mac answers."""
        self._set_ready(False)
        self.main_window.title = self.formal_name
        buttons = [toga.Button("Disconnect", on_press=self._disconnect_command)]
        if retry:
            buttons.insert(0, toga.Button("Try Again", on_press=self._try_again))
        panel = toga.Box(
            children=[
                toga.Label(textwrap.fill(message, 56), style=Pack(text_align=CENTER)),
                toga.Box(children=buttons, style=Pack(direction=ROW, gap=8)),
            ],
            style=Pack(direction=COLUMN, align_items=CENTER, gap=16),
        )
        self.main_window.content = toga.Box(
            children=[panel],
            style=Pack(direction=COLUMN, justify_content=CENTER, align_items=CENTER),
        )

    async def _try_again(self, widget, **kwargs):
        if self.remote:
            await self._start_remote(self.remote)

    async def _disconnect_command(self, widget, **kwargs):
        await self._disconnect()

    async def _disconnect(self, message: str = ""):
        """Stop triaging through the other Mac. The account is then on this Mac
        again, and not signed in."""
        account = self.accounts.current
        await self._stop()
        forget_secret(f"connect:{account}")
        self.accounts.disconnect(account)
        self._open_store()
        self._rebuild_label_commands([])
        self._show_sign_in(message)

    # Accounts

    def _open_store(self):
        """Open the account's store, unless the account is on another Mac."""
        account = self.accounts.current
        if account in self.accounts.remotes:
            self.store = None
        else:
            self.store = Store(self.accounts.path(account))

    async def _open_account(self):
        """Connect to the other Mac if the account is there. Otherwise, sync
        with GitHub if it's signed in, or ask it to sign in."""
        account = self.accounts.current
        if url := self.accounts.remotes.get(account):
            if code := load_secret(f"connect:{account}"):
                await self._start_remote(Remote(url, code))
            else:
                await self._disconnect(f"Connect to Propolis at {url} again.")
        elif token := load_token(account):
            await self._start(token)
        else:
            self._show_sign_in()

    async def _add_account(self, command, **kwargs):
        await self._switch_account(self.accounts.add())

    async def _switch_account(self, account: str):
        if self._signing_in:
            self._signing_in.cancel()
        if self._share_window in self.windows:
            self._share_window.close()
        await self._stop()
        while self._syncing:
            # A sync under way finishes with the store it started with.
            await asyncio.sleep(0.1)
        if self.store:
            self.store.close()
        self.accounts.switch(account)
        self._open_store()
        self.view = INBOX
        self._shown = None
        self._filters = Filters()
        self._filter_options = None
        self._rebuild_account_commands()
        self._rebuild_label_commands([])
        await self._open_account()

    def _name_account(self, login: str | None):
        """Name the account after its GitHub login, once that's known."""
        if login and login != self.accounts.names[self.accounts.current]:
            self.accounts.name(self.accounts.current, login)
            self._rebuild_account_commands()

    def _account_name(self, account: str) -> str:
        return self.accounts.names.get(account) or "New account"

    def _rebuild_account_commands(self):
        """A Switch Account menu, with the open account dimmed."""
        for command in self._account_commands:
            del self.commands[command.id]
        self._account_commands = [
            toga.Command(
                self._account_command_action(account),
                self._account_name(account),
                group=ACCOUNTS,
                order=order,
                id=f"account:{account}",
                enabled=account != self.accounts.current,
            )
            for order, account in enumerate(self.accounts.names)
        ]
        self.commands.add(*self._account_commands)

    def _account_command_action(self, account: str):
        async def switch(command, **kwargs):
            await self._switch_account(account)

        return switch

    # Filters

    def _update_filter_options(self):
        """Offer the repos, reasons and types among your threads, keeping
        the current choices where they still exist."""
        options = filter_options(self.triage)
        if options == self._filter_options:
            return
        self._filter_options = options
        self._updating_filters = True
        try:
            repo = self._set_choices(
                self.repo_filter,
                "All repos",
                [(repo, repo) for repo in options.repos],
                self._filters.repo,
            )
            reason = self._set_choices(
                self.reason_filter, "All reasons", options.reasons, self._filters.reason
            )
            kind = self._set_choices(
                self.kind_filter, "All types", options.kinds, self._filters.kind
            )
        finally:
            self._updating_filters = False
        self._filters = Filters(repo=repo, reason=reason, kind=kind)

    def _set_choices(self, selection, everything, choices, current):
        """Set a filter's choices, and select ``current`` if it's still one."""
        selection.items = [{"name": everything, "value": None}] + [
            {"name": name, "value": value} for value, name in choices
        ]
        for item in selection.items:
            if item.value == current:
                selection.value = item
                return current
        return None

    def _on_filter_changed(self, widget, **kwargs):
        if self._updating_filters:
            return
        self._filters = Filters(
            repo=self.repo_filter.value.value,
            reason=self.reason_filter.value.value,
            kind=self.kind_filter.value.value,
        )
        self._reload()

    def _clear_filters(self, widget=None, **kwargs):
        self._filters = Filters()
        self._filter_options = None  # so the choices are set again
        self._reload()

    # Adding a thread by its number

    def _add_thread(self, command, **kwargs):
        reference = toga.TextInput(placeholder="owner/repo#123", style=Pack(flex=1))
        message = toga.Label(
            "An issue, pull request or discussion to add to the Inbox.",
            # Room for two lines, so a longer message doesn't move the buttons.
            style=Pack(font_size=11, height=36),
        )
        window = toga.Window(title="Add Thread", size=(420, 160), resizable=False)

        async def add(widget, **kwargs):
            add_button.enabled = False
            message.text = "Looking it up on GitHub…"
            try:
                key = await self.triage.add(reference.value)
            except (AddError, RemoteError) as error:
                message.text = textwrap.fill(str(error), 60)
            except httpx2.HTTPError as error:
                message.text = textwrap.fill(f"Couldn't reach GitHub: {error}", 60)
            except RuntimeError as error:
                message.text = textwrap.fill(str(error), 60)
            else:
                window.close()
                self._show_thread(key)
                return
            add_button.enabled = True

        add_button = toga.Button("Add", on_press=add)
        reference.on_confirm = add
        window.content = toga.Box(
            children=[
                reference,
                message,
                toga.Box(
                    children=[
                        toga.Box(style=Pack(flex=1)),
                        toga.Button(
                            "Cancel", on_press=lambda widget, **kwargs: window.close()
                        ),
                        add_button,
                    ],
                    style=Pack(direction=ROW, gap=8),
                ),
            ],
            style=Pack(direction=COLUMN, margin=16, gap=8),
        )
        window.show()
        reference.focus()

    def _show_thread(self, key: str):
        """Switch to the view a thread is in, and select it there."""
        thread = self.triage.state.threads[key]
        views = [row.view for row in self.sidebar_data]
        view = next((view for view in views if view in thread.labels), INBOX)
        select_row(self.sidebar_table, views.index(view))
        if not self._filters.matches(self.triage.info(key)):
            self._filters = Filters()
            self._filter_options = None
        self._reload()
        keys = [row.key for row in self.thread_table.data]
        if key in keys:
            select_row(self.thread_table, keys.index(key))
            self.thread_table.focus()

    # Thread actions, on the selected thread

    async def _act_on_selection(self, action: Callable[[str], Awaitable[None]]):
        """Act on the selected thread, then select the one that takes its place,
        so you can keep triaging from the keyboard."""
        row = self.thread_table.selection
        if row is None:
            return
        index = list(self.thread_table.data).index(row)
        if not await self._do(action(row.key)):
            return
        self._reload()
        if count := len(self.thread_table.data):
            select_row(self.thread_table, min(index, count - 1))

    async def _open(self, widget, row=None, **kwargs):
        row = row or self.thread_table.selection
        if row is None:
            return
        if row.url:
            webbrowser.open(row.url)
        await self._do(self.triage.view(row.key))
        self._reload()

    async def _done(self, command, **kwargs):
        await self._act_on_selection(self.triage.done)

    async def _unlabel(self, command, **kwargs):
        if self.view != INBOX:
            view = self.view
            await self._act_on_selection(lambda key: self.triage.unlabel(key, view))

    def _on_delete(self) -> bool:
        """Delete in the thread list removes the thread from this view. The
        Inbox has no label to remove, so there it means Done."""
        if self.current_window is not self.main_window or self.triage is None:
            return False
        if not has_focus(self.thread_table):
            return False
        if self.view == INBOX:
            asyncio.create_task(self._done(self.done_command))
        else:
            asyncio.create_task(self._unlabel(self.unlabel_command))
        return True

    # Applying labels

    def _on_number(self, number: int) -> bool:
        """Apply the label with this number to the selected thread."""
        if self.current_window is not self.main_window or self.triage is None:
            return False
        if (label_id := self._numbered.get(number)) is None:
            return False
        asyncio.create_task(self._apply_label(label_id))
        return True

    async def _apply_label(self, label_id: str):
        await self._act_on_selection(lambda key: self.triage.label(key, label_id))

    def _rebuild_label_commands(self, labels: list[SidebarItem]):
        """A Labels menu, showing each label's number key."""
        for command in self._label_commands:
            del self.commands[command.id]
        self._label_commands = [
            toga.Command(
                self._label_command_action(item.view),
                f"{item.number}   {item.name}" if item.number else item.name,
                group=LABELS,
                order=order,
                id=f"label:{item.view}",
            )
            for order, item in enumerate(labels)
        ]
        self.commands.add(*self._label_commands)
        self._label_items = [(item.view, item.name, item.number) for item in labels]

    def _label_command_action(self, label_id: str):
        async def apply(command, **kwargs):
            await self._apply_label(label_id)

        return apply

    # Help

    def _show_shortcuts(self, command=None, **kwargs):
        if self._shortcuts_window in self.windows:
            self._shortcuts_window.show()
            return

        def describe(command: toga.Command):
            return (shortcut_text(command.shortcut), command.text, command.tooltip)

        sections = [
            (
                "Triage",
                [
                    (
                        "1 – 9",
                        "Apply a label",
                        "Apply that label to the selected thread, then move to the "
                        "next one. Works while the thread list or sidebar has "
                        "focus. Each label's number is shown beside it.",
                    ),
                    (
                        "↑ ↓",
                        "Move between threads",
                        "The selected thread shows below the list, which counts "
                        "as viewing it.",
                    ),
                    describe(self.done_command),
                    (
                        "⌫",
                        self.unlabel_command.text,
                        f"{self.unlabel_command.tooltip} In the Inbox, Delete "
                        "marks the thread done instead. Works while the thread "
                        "list has focus.",
                    ),
                    describe(self.open_command),
                    describe(self.add_thread_command),
                ],
            ),
            (
                "Labels",
                [
                    describe(self.new_label_command),
                    describe(self.edit_label_command),
                    describe(self.delete_label_command),
                    *[
                        (
                            keys,
                            command.text,
                            f"{command.tooltip} Works while the sidebar has focus.",
                        )
                        for keys, command in [
                            ("⌘↑", self.move_up_command),
                            ("⌘↓", self.move_down_command),
                        ]
                    ],
                ],
            ),
            (
                "Filtering, syncing and help",
                [
                    describe(self.clear_filters_command),
                    describe(self.refresh_command),
                    describe(self.shortcuts_command),
                ],
            ),
        ]

        children = []
        for heading, entries in sections:
            children.append(
                toga.Label(heading, style=Pack(font_weight="bold", font_size=14))
            )
            for keys, title, description in entries:
                children.append(
                    toga.Box(
                        children=[
                            toga.Label(
                                keys,
                                style=Pack(width=70, text_align=RIGHT, font_weight="bold"),
                            ),
                            toga.Box(
                                children=[
                                    toga.Label(title, style=Pack(font_weight="bold")),
                                    toga.Label(textwrap.fill(description, 62)),
                                ],
                                style=Pack(direction=COLUMN, flex=1, gap=2),
                            ),
                        ],
                        style=Pack(direction=ROW, gap=14),
                    )
                )

        window = toga.Window(title="Keyboard Shortcuts", size=(560, 620))
        window.content = toga.ScrollContainer(
            horizontal=False,
            content=toga.Box(
                children=children, style=Pack(direction=COLUMN, margin=20, gap=12)
            ),
        )
        self._shortcuts_window = window
        window.show()

    # Labels

    def _new_label(self, command, **kwargs):
        self._show_label_window()

    def _edit_label(self, widget=None, **kwargs):
        if self.view not in (INBOX, ATTENTION):
            self._show_label_window(self.view)

    def _on_move(self, offset: int) -> bool:
        """⌘↑ or ⌘↓ in the sidebar moves the selected label."""
        if self.current_window is not self.main_window or self.triage is None:
            return False
        if self.view in (INBOX, ATTENTION) or not has_focus(self.sidebar_table):
            return False
        asyncio.create_task(self._move_label(offset))
        return True

    async def _delete_label(self, command, **kwargs):
        """Delete the label selected in the sidebar, if you confirm it."""
        if self.view in (INBOX, ATTENTION):
            return
        label_id = self.view
        count = len(self.triage.state.labelled(label_id))
        threads = f"{count} thread{'' if count == 1 else 's'}"
        message = (
            f"Its {threads} will go back to the Inbox, unless they have other "
            "labels."
            if count
            else "No threads have this label."
        )
        name = self.triage.state.labels[label_id].name
        if await self.main_window.dialog(
            toga.ConfirmDialog(f"Delete “{name}”?", message)
        ):
            await self._do(self.triage.delete_label(label_id))
            self._reload()

    async def _move_label_up(self, command, **kwargs):
        await self._move_label(-1)

    async def _move_label_down(self, command, **kwargs):
        await self._move_label(1)

    async def _move_label(self, offset: int):
        order = [label_id for label_id in self.triage.state.labels if label_id != ATTENTION]
        if self.view not in order:
            return
        index = order.index(self.view)
        if 0 <= index + offset < len(order):
            order.insert(index + offset, order.pop(index))
            await self._do(self.triage.reorder_labels(order))
            self._reload()

    def _show_label_window(self, label_id: str | None = None):
        """The New Label window, or with ``label_id``, the Edit Label window."""
        label = self.triage.state.labels[label_id] if label_id else None
        name = toga.TextInput(
            value=label.name if label else "",
            placeholder="Label name",
            style=Pack(flex=1),
        )
        flag = toga.Switch(
            "Flag on activity", value=label.flag_on_activity if label else False
        )
        window = toga.Window(
            title="Edit Label" if label else "New Label",
            size=(380, 150),
            resizable=False,
        )

        async def save(widget, **kwargs):
            window.close()
            if text := name.value.strip():
                await self._do(
                    self.triage.define_label(
                        text, flag_on_activity=flag.value, label_id=label_id
                    )
                )
                self._reload()

        # Return in the name field creates or saves the label.
        name.on_confirm = save
        buttons = [
            toga.Box(style=Pack(flex=1)),
            toga.Button("Cancel", on_press=lambda widget, **kwargs: window.close()),
            toga.Button("Save" if label else "Create", on_press=save),
        ]
        window.content = toga.Box(
            children=[
                name,
                flag,
                toga.Box(children=buttons, style=Pack(direction=ROW, gap=8)),
            ],
            style=Pack(direction=COLUMN, margin=16, gap=12),
        )
        window.show()


def main():
    return Propolis()
