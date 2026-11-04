"""
Triage your GitHub notifications with labels that keep their order.
"""

import asyncio
import logging
import webbrowser
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import datetime

import httpx2
import toga
from toga.sources import AccessorColumn, ListSource
from toga.style.pack import CENTER, COLUMN, ROW, Pack

from propolis.core.auth import DeviceCode, SignInError, forget_token, load_token, sign_in
from propolis.core.github import GitHub
from propolis.core.store import Store
from propolis.core.sync import Sync
from propolis.core.triage import Triage
from propolis.macos import NumberKeys, select_row
from propolis.views import INBOX, ThreadRow, rows, sidebar

logger = logging.getLogger(__name__)

THREAD = toga.Group("Thread", order=22)
LABELS = toga.Group("Labels", order=25)


class Propolis(toga.App):
    def startup(self):
        self.paths.data.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.paths.data / "propolis.db")
        self.github: GitHub | None = None
        self.sync: Sync | None = None
        self.triage: Triage | None = None
        self.view = INBOX
        self._syncing = False
        self._poller: asyncio.Task | None = None
        self._shown: str | None = None
        self._numbered: dict[int, str] = {}
        self._label_commands: list[toga.Command] = []
        self._number_keys = NumberKeys(self._on_number)

        self.refresh_command = toga.Command(
            self._refresh,
            "Refresh",
            shortcut=toga.Key.MOD_1 + "r",
            group=toga.Group.VIEW,
        )
        self.open_command = toga.Command(
            self._open,
            "Open in Browser",
            shortcut=toga.Key.MOD_1 + "o",
            group=THREAD,
        )
        self.done_command = toga.Command(
            self._done, "Done", shortcut=toga.Key.MOD_1 + "e", group=THREAD
        )
        self.unlabel_command = toga.Command(
            self._unlabel,
            "Remove from This View",
            shortcut=toga.Key.MOD_1 + toga.Key.BACKSPACE,
            group=THREAD,
        )
        self.new_label_command = toga.Command(
            self._new_label,
            "New Label…",
            shortcut=toga.Key.MOD_1 + toga.Key.SHIFT + "n",
            group=LABELS,
            section=1,
        )
        self.sign_out_command = toga.Command(
            self._sign_out, "Sign Out of GitHub", group=toga.Group.FILE
        )
        self.commands.add(
            self.refresh_command,
            self.open_command,
            self.done_command,
            self.unlabel_command,
            self.new_label_command,
            self.sign_out_command,
        )

        self.main_window = toga.MainWindow(size=(1200, 800))
        self.main_window.content = toga.Box()
        self._set_signed_in(False)
        self.main_window.show()

    async def on_running(self):
        if token := load_token():
            await self._start(token)
        else:
            self._show_sign_in()

    async def on_exit(self):
        self._number_keys.remove()
        await self._close_github()
        self.store.close()
        return True

    # Signing in and out

    def _show_sign_in(self, message: str = ""):
        self._set_signed_in(False)
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
        intro = "Sign in to GitHub to triage your notifications."
        panel = toga.Box(
            children=[
                toga.Label(f"{message}\n\n{intro}" if message else intro, style=centred),
                toga.Label("Enter this code at github.com/login/device:", style=centred),
                self.code_label,
                self.open_github_button,
            ],
            style=Pack(direction=COLUMN, width=380, gap=4),
        )
        self.main_window.content = toga.Box(
            children=[panel],
            style=Pack(direction=COLUMN, justify_content=CENTER, align_items=CENTER),
        )
        asyncio.create_task(self._sign_in())

    def _show_code(self, code: DeviceCode):
        self.code_label.text = code.user_code
        self._verification_uri = code.verification_uri
        self.open_github_button.enabled = True

    def _open_verification(self, widget, **kwargs):
        if self._verification_uri:
            webbrowser.open(self._verification_uri)

    async def _sign_in(self):
        try:
            token = await sign_in(self._show_code)
        except (SignInError, httpx2.HTTPError) as error:
            await self.main_window.dialog(
                toga.dialogs.ErrorDialog("Couldn't sign in", str(error))
            )
            self._show_sign_in()
            return
        await self._start(token)

    async def _start(self, token: str):
        self.github = GitHub(token)
        self.sync = Sync(self.store, self.github)
        self.triage = Triage(self.store, self.github)
        self._show_main()
        self._rebuild_label_commands()
        self._set_signed_in(True)
        self._reload()
        await self._refresh()
        self._poller = asyncio.create_task(self._poll())

    async def _poll(self):
        """Sync in the background, as often as GitHub allows."""
        while self.github is not None:
            await asyncio.sleep(self._poll_delay())
            try:
                await self._refresh()
            except Exception:
                # Keep polling; the next sync may well succeed.
                logger.exception("Background sync failed")
                self.status.text = "Sync failed; see the log"

    def _poll_delay(self) -> float:
        return self.github.poll_interval

    async def _sign_out(self, command, **kwargs):
        forget_token()
        await self._close_github()
        self._show_sign_in()

    async def _close_github(self):
        # Polling stops by itself once self.github is None. Cancelling it here
        # would also cancel this call if it came from a background sync.
        if self._poller and self._poller is not asyncio.current_task():
            self._poller.cancel()
        self._poller = None
        if self.github:
            await self.github.aclose()
        self.github = self.sync = self.triage = None

    def _set_signed_in(self, signed_in: bool):
        for command in [
            self.refresh_command,
            self.open_command,
            self.done_command,
            self.new_label_command,
            self.sign_out_command,
            *self._label_commands,
        ]:
            command.enabled = signed_in
        self.unlabel_command.enabled = signed_in and self.view != INBOX

    # The main window

    def _show_main(self):
        self.sidebar_data = ListSource(accessors=["text", "view"], data=[])
        self.sidebar_table = toga.Table(
            columns=[AccessorColumn("View", "text")],
            data=self.sidebar_data,
            show_headings=False,
            on_select=self._on_view_selected,
        )
        self.thread_table = toga.Table(
            columns=[
                AccessorColumn("Title", "title"),
                AccessorColumn("Repo", "repo"),
                AccessorColumn("Type", "kind"),
                AccessorColumn("When", "when"),
            ],
            on_select=self._on_thread_selected,
            on_activate=self._open,
        )
        self.web_view = toga.WebView()
        self.status = toga.Label("", style=Pack(margin=(4, 10)))
        self.main_window.content = toga.Box(
            children=[
                toga.SplitContainer(
                    content=[
                        (self.sidebar_table, 1),
                        (
                            toga.SplitContainer(
                                direction=toga.SplitContainer.HORIZONTAL,
                                content=[(self.thread_table, 2), (self.web_view, 3)],
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
        """Bring the sidebar and thread list up to date with the Store."""
        items = sidebar(self.store)
        self._numbered = {item.number: item.view for item in items if item.number}
        # Update rows in place where possible, so selections survive.
        existing = {row.view: row for row in self.sidebar_data}
        for item in items:
            if row := existing.get(item.view):
                row.text = item.text
            else:
                self.sidebar_data.append({"text": item.text, "view": item.view})
        self._show_rows(rows(self.store, self.view))
        name = next(item.name for item in items if item.view == self.view)
        self.main_window.title = f"{name} – {self.formal_name}"
        self.unlabel_command.enabled = self.view != INBOX

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
        await self.triage.view(row.key)
        self._reload()

    async def _refresh(self, command=None, **kwargs):
        if self._syncing or self.sync is None:
            return
        self._syncing = True
        self.status.text = "Syncing…"
        try:
            await self.sync.run_once()
        except httpx2.HTTPStatusError as error:
            if error.response.status_code == 401:
                forget_token()
                await self._close_github()
                self._show_sign_in("GitHub no longer accepts the saved sign-in.")
                return
            self.status.text = f"Sync failed: GitHub returned {error.response.status_code}"
        except (httpx2.HTTPError, RuntimeError) as error:
            self.status.text = f"Sync failed: {error}"
        else:
            self.status.text = f"Last synced at {datetime.now():%H:%M}"
        finally:
            self._syncing = False
        self._reload()

    # Thread actions, on the selected thread

    async def _act_on_selection(self, action: Callable[[str], Awaitable[None]]):
        """Act on the selected thread, then select the one that takes its place,
        so you can keep triaging from the keyboard."""
        row = self.thread_table.selection
        if row is None:
            return
        index = list(self.thread_table.data).index(row)
        await action(row.key)
        self._reload()
        if count := len(self.thread_table.data):
            select_row(self.thread_table, min(index, count - 1))

    async def _open(self, widget, row=None, **kwargs):
        row = row or self.thread_table.selection
        if row is None:
            return
        if row.url:
            webbrowser.open(row.url)
        await self.triage.view(row.key)
        self._reload()

    async def _done(self, command, **kwargs):
        await self._act_on_selection(self.triage.done)

    async def _unlabel(self, command, **kwargs):
        if self.view != INBOX:
            view = self.view
            await self._act_on_selection(lambda key: self.triage.unlabel(key, view))

    # Labels

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

    def _rebuild_label_commands(self):
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
            for order, item in enumerate(sidebar(self.store)[2:])
        ]
        self.commands.add(*self._label_commands)

    def _label_command_action(self, label_id: str):
        async def apply(command, **kwargs):
            await self._apply_label(label_id)

        return apply

    def _new_label(self, command, **kwargs):
        name = toga.TextInput(placeholder="Label name", style=Pack(flex=1))
        flag = toga.Switch("Flag on activity")
        window = toga.Window(title="New Label", size=(360, 150), resizable=False)

        def create(widget, **kwargs):
            if text := name.value.strip():
                self.store.define_label(text, flag_on_activity=flag.value)
                self._rebuild_label_commands()
                self._reload()
            window.close()

        window.content = toga.Box(
            children=[
                name,
                flag,
                toga.Box(
                    children=[
                        toga.Box(style=Pack(flex=1)),
                        toga.Button(
                            "Cancel", on_press=lambda widget, **kwargs: window.close()
                        ),
                        toga.Button("Create", on_press=create),
                    ],
                    style=Pack(direction=ROW, gap=8),
                ),
            ],
            style=Pack(direction=COLUMN, margin=16, gap=12),
        )
        window.show()


def main():
    return Propolis()
