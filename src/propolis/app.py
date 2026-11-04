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

import httpx2
import toga
from toga.sources import AccessorColumn, ListSource
from toga.style.pack import CENTER, COLUMN, RIGHT, ROW, Pack

from propolis.core.auth import DeviceCode, SignInError, forget_token, load_token, sign_in
from propolis.core.github import GitHub
from propolis.core.state import ATTENTION
from propolis.core.store import Store
from propolis.core.sync import Sync
from propolis.core.triage import Triage
from propolis.macos import NumberKeys, select_row
from propolis.views import (
    INBOX,
    Filters,
    ThreadRow,
    filter_options,
    rows,
    shortcut_text,
    sidebar,
)

logger = logging.getLogger(__name__)

PLACEHOLDER = """<html style="color-scheme: light dark"><body style="
    font: 13px -apple-system, sans-serif; color: gray;
    display: grid; place-items: center; height: 90vh; margin: 0">
Select a thread to read it here.</body></html>"""
"""What the web view shows before a thread is selected."""

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

        # Each command's tooltip also describes it in the Keyboard Shortcuts
        # window, so the two can't drift apart.
        self.refresh_command = toga.Command(
            self._refresh,
            "Refresh",
            shortcut=toga.Key.MOD_1 + "r",
            tooltip="Sync with GitHub now. Propolis also syncs every minute.",
            group=toga.Group.VIEW,
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
            shortcut=toga.Key.MOD_1 + "e",
            tooltip="Take the selected thread out of every view, and mark it done "
            "on GitHub. Activity from someone else brings it back to the Inbox.",
            group=THREAD,
        )
        self.unlabel_command = toga.Command(
            self._unlabel,
            "Remove from This View",
            shortcut=toga.Key.MOD_1 + toga.Key.BACKSPACE,
            tooltip="Take this view's label off the selected thread. In Attention "
            "required, this is how you say you've dealt with it.",
            group=THREAD,
        )
        self.new_label_command = toga.Command(
            self._new_label,
            "New Label…",
            shortcut=toga.Key.MOD_1 + toga.Key.SHIFT + "n",
            tooltip="Create a label, and choose whether activity flags its threads "
            "for attention.",
            group=LABELS,
            section=1,
        )
        self.edit_label_command = toga.Command(
            self._edit_label,
            "Edit Label…",
            shortcut=toga.Key.MOD_1 + "i",
            tooltip="Rename, reconfigure or delete the label selected in the "
            "sidebar. Double-clicking a label does the same.",
            group=LABELS,
            section=1,
            order=1,
        )
        self.move_up_command = toga.Command(
            lambda command, **kwargs: self._move_label(-1),
            "Move Label Up",
            shortcut=toga.Key.MOD_1 + toga.Key.MOD_2 + toga.Key.UP,
            tooltip="Move the selected label up the sidebar, changing its number.",
            group=LABELS,
            section=1,
            order=2,
        )
        self.move_down_command = toga.Command(
            lambda command, **kwargs: self._move_label(1),
            "Move Label Down",
            shortcut=toga.Key.MOD_1 + toga.Key.MOD_2 + toga.Key.DOWN,
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
        self.clear_filters_command = toga.Command(
            self._clear_filters,
            "Clear Filters",
            shortcut=toga.Key.MOD_1 + toga.Key.SHIFT + "f",
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
            self.open_command,
            self.done_command,
            self.unlabel_command,
            self.new_label_command,
            self.edit_label_command,
            self.move_up_command,
            self.move_down_command,
            self.sign_out_command,
            self.clear_filters_command,
            self.shortcuts_command,
        )
        self._shortcuts_window: toga.Window | None = None
        self._filters = Filters()
        self._filter_options = None
        self._updating_filters = False

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
                toga.ErrorDialog("Couldn't sign in", str(error))
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
            self.clear_filters_command,
            *self._label_commands,
        ]:
            command.enabled = signed_in
        self._enable_view_commands(signed_in)

    def _enable_view_commands(self, signed_in: bool = True):
        """Commands that depend on the selected view."""
        self.unlabel_command.enabled = signed_in and self.view != INBOX
        editable = signed_in and self.view not in (INBOX, ATTENTION)
        for command in [
            self.edit_label_command,
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
        shortcuts = shortcut_text(self.shortcuts_command.shortcut)
        sidebar_box = toga.Box(
            children=[
                self.sidebar_table,
                toga.Divider(),
                toga.Label(
                    f"Press {shortcuts} for keyboard shortcuts",
                    style=Pack(margin=(6, 10), font_size=11),
                ),
            ],
            style=Pack(direction=COLUMN),
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
            style=Pack(flex=1),
        )
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
                        (sidebar_box, 1),
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
        """Bring the sidebar and thread list up to date with the Store."""
        items = sidebar(self.store)
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
        self._update_filter_options()
        self._show_rows(rows(self.store, self.view, self._filters))
        name = next(item.name for item in items if item.view == self.view)
        filtered = " (filtered)" if self._filters.active else ""
        self.main_window.title = f"{name}{filtered} – {self.formal_name}"
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

    # Filters

    def _update_filter_options(self):
        """Offer the repos, reasons and types among your threads, keeping
        the current choices where they still exist."""
        options = filter_options(self.store)
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
                    describe(self.unlabel_command),
                    describe(self.open_command),
                ],
            ),
            (
                "Labels",
                [
                    describe(command)
                    for command in [
                        self.new_label_command,
                        self.edit_label_command,
                        self.move_up_command,
                        self.move_down_command,
                    ]
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

    def _labels_changed(self):
        self._rebuild_label_commands()
        self._reload()

    def _new_label(self, command, **kwargs):
        self._show_label_window()

    def _edit_label(self, widget=None, **kwargs):
        if self.view not in (INBOX, ATTENTION):
            self._show_label_window(self.view)

    def _move_label(self, offset: int):
        order = [label_id for label_id in self.store.state.labels if label_id != ATTENTION]
        if self.view not in order:
            return
        index = order.index(self.view)
        if 0 <= index + offset < len(order):
            order.insert(index + offset, order.pop(index))
            self.store.reorder_labels(order)
            self._labels_changed()

    def _show_label_window(self, label_id: str | None = None):
        """The New Label window, or with ``label_id``, the Edit Label window."""
        label = self.store.state.labels[label_id] if label_id else None
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

        def save(widget, **kwargs):
            if text := name.value.strip():
                self.store.define_label(
                    text, flag_on_activity=flag.value, label_id=label_id
                )
                self._labels_changed()
            window.close()

        async def delete(widget, **kwargs):
            count = len(self.store.state.labelled(label_id))
            threads = f"{count} thread{'' if count == 1 else 's'}"
            message = (
                f"Its {threads} will go back to the Inbox, unless they have "
                "other labels."
                if count
                else "No threads have this label."
            )
            if await window.dialog(
                toga.ConfirmDialog(f"Delete “{label.name}”?", message)
            ):
                self.store.delete_label(label_id)
                self._labels_changed()
                window.close()

        buttons = [toga.Button("Delete…", on_press=delete)] if label else []
        buttons += [
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
