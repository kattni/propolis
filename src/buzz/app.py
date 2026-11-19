"""
A GitHub notifications inbox with labels.
"""

from __future__ import annotations

import asyncio
import re
import textwrap
import time
import traceback
import webbrowser
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial

import toga
from toga.sources import AccessorColumn, ListSource
from toga.style.pack import BOLD, CENTER, COLUMN, RIGHT, ROW

from . import auth, credentials, macos
from .accounts import Accounts, secret_name
from .actions import add_thread, apply_to_threads, label_threads, to_view
from .github import (
    MIN_POLL_INTERVAL,
    AuthenticationError,
    GitHubClient,
    GitHubError,
    describe_reason,
    describe_type,
    html_url,
    is_github_url,
    reference,
)
from .macos import SEPARATOR, MenuItem, Pill
from .remote import (
    PORT,
    HostAPI,
    NotPaired,
    RemoteClient,
    RemoteError,
    RemoteServer,
    new_code,
)
from .store import ATTENTION, DONE, INBOX, UNREAD, Inbox, Label, Mirror, Store, Thread
from .sync import Sync, WrongSignIn

ATTENTION_NAME = "⚠️ Attention required"
VIEWS = (
    (INBOX, "📥  Inbox"),
    (ATTENTION, "⚠️  Attention required"),
    (UNREAD, "📬  Unread"),
    (DONE, "✅  Done"),
)

#: The sections of the Filter menu: the thread column each one filters by, its
#: heading, and how to show a value.
FILTERS = (
    ("type", "Type", describe_type),
    ("reason", "Reason", describe_reason),
    ("repo", "Repository", str),
)
#: The Octicon at the start of each notification's row, and its colour, by
#: subject type and state, as GitHub shows them. (Icons without a colour are
#: drawn like the text.)
STATE_ICONS = {
    ("Issue", "open"): "issue-opened-green",
    ("Issue", "completed"): "issue-closed-purple",
    ("Issue", "not_planned"): "skip",
    ("Issue", "duplicate"): "duplicate",
    ("PullRequest", "open"): "git-pull-request-green",
    ("PullRequest", "draft"): "git-pull-request-draft",
    ("PullRequest", "merged"): "git-merge-purple",
    ("PullRequest", "closed"): "git-pull-request-closed-red",
    ("Discussion", "open"): "comment-discussion-green",
    ("Discussion", "resolved"): "discussion-closed-purple",
    ("Discussion", "outdated"): "discussion-outdated",
    ("Discussion", "duplicate"): "discussion-duplicate",
}
#: The Octicon for each subject type, when its state isn't known (or it has
#: none), and the one for types not listed here.
TYPE_ICONS = {
    "Issue": "issue-opened",
    "PullRequest": "git-pull-request",
    "Discussion": "comment-discussion",
    "Release": "tag",
    "Commit": "git-commit",
    "CheckSuite": "play",
    "WorkflowRun": "play",
    "RepositoryDependabotAlertsThread": "dependabot",
    "RepositoryVulnerabilityAlert": "alert",
    "RepositoryInvitation": "mail",
}
OTHER_TYPE_ICON = "bell"
#: What VoiceOver reads for each of those icons.
ICON_DESCRIPTIONS = {
    "issue-opened-green": "Open issue",
    "issue-closed-purple": "Closed issue",
    "skip": "Issue closed as not planned",
    "duplicate": "Duplicate issue",
    "git-pull-request-green": "Open pull request",
    "git-pull-request-draft": "Draft pull request",
    "git-merge-purple": "Merged pull request",
    "git-pull-request-closed-red": "Closed pull request",
    "comment-discussion-green": "Open discussion",
    "discussion-closed-purple": "Resolved discussion",
    "discussion-outdated": "Outdated discussion",
    "discussion-duplicate": "Duplicate discussion",
    "issue-opened": "Issue",
    "git-pull-request": "Pull request",
    "comment-discussion": "Discussion",
    "tag": "Release",
    "git-commit": "Commit",
    "play": "CI run",
    "dependabot": "Dependabot alert",
    "alert": "Security alert",
    "mail": "Invitation",
    "bell": "Notification",
}
#: The Filter menu's symbol, when it isn't filtering and when it is.
FILTER_SYMBOLS = (
    "line.3.horizontal.decrease.circle",
    "line.3.horizontal.decrease.circle.fill",
)
#: Labels 1 to 9 in the sidebar can be given with those number keys.
NUMBERED_LABELS = 9
#: Roughly how many characters fit across the sidebar, for the status line.
STATUS_WIDTH = 40

#: How often (seconds) to check threads added by hand for activity, since
#: GitHub doesn't send notifications about them.
ADDED_CHECK_EVERY = 10 * 60
#: Don't open more browser tabs than this at once.
OPEN_LIMIT = 10
#: How often (seconds) to check an inbox on another Mac for changes.
REMOTE_POLL_INTERVAL = 10

# Keychain entries, besides the GitHub token: the code another Mac needs to
# triage this account through this one, and the code for triaging an account
# through another Mac.
SHARE_CODE = "share-code"
CONNECT_CODE = "connect-code"

_KEY_SYMBOLS = {
    "mod 3": "⌃",
    "mod 2": "⌥",
    "shift": "⇧",
    "mod 1": "⌘",
    "backspace": "⌫",
    "up": "↑",
    "down": "↓",
}
_MODIFIER_ORDER = ["mod 3", "mod 2", "shift", "mod 1"]


@dataclass(eq=False)
class RemoteInbox:
    """An account's inbox on another Mac, triaged through Buzz there."""

    client: RemoteClient
    #: A copy of the inbox, refreshed from the other Mac's.
    mirror: Mirror = field(default_factory=Mirror)
    etag: str | None = None
    #: Changes sent to the other Mac but not finished yet. While there are
    #: any, refreshes are skipped, so they don't undo changes on their way.
    pending: int = 0
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None

    @property
    def host(self) -> str:
        return self.client.host


def local_time(timestamp: str) -> str:
    """An ISO 8601 timestamp in local time, like "19 Nov 2026, 14:05"."""
    return datetime.fromisoformat(timestamp).astimezone().strftime("%-d %b %Y, %H:%M")


def count_text(count: int, new: int = 0) -> str:
    """A view's count in the sidebar: how many threads it has, and how many of
    them have activity you haven't seen."""
    if not count:
        return ""
    return f"{count}, {new} new" if new else str(count)


def shortcut_text(shortcut) -> str:
    """A Toga shortcut such as ``<mod 1><shift>u``, as macOS shows it: ⇧⌘U."""
    if not shortcut:
        return ""
    shortcut = getattr(shortcut, "value", shortcut)
    names = re.findall(r"<([^>]+)>", shortcut)
    key = re.sub(r"<[^>]+>", "", shortcut)
    modifiers = [name for name in _MODIFIER_ORDER if name in names]
    keys = [name for name in names if name not in _MODIFIER_ORDER]
    return "".join(
        [_KEY_SYMBOLS[name] for name in modifiers]
        + [_KEY_SYMBOLS.get(name, name.title()) for name in keys]
        + [key.upper()]
    )


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


def type_icon(thread: Thread) -> str:
    """The Octicon for a thread: the one for its state, if that's known."""
    return STATE_ICONS.get((thread.type, thread.state)) or TYPE_ICONS.get(
        thread.type, OTHER_TYPE_ICON
    )


def pills_cell(
    cells: dict, key, pills: tuple[Pill, ...], description: str, fallback: str
):
    """A table cell that draws ``pills``, or shows ``fallback`` text if it
    can't. ``cells`` remembers each row's cell, by ``key``, so a cell that
    hasn't changed is the same widget, and the table leaves it alone."""
    if (cell := cells.get(key)) is None or cell[0] != pills:
        cell = cells[key] = (pills, macos.pills(pills, description) or fallback)
    return cell[1]


def prune(cells: dict, keys: Iterable) -> None:
    """Forget the cells of rows that aren't in a table any more."""
    for key in cells.keys() - set(keys):
        del cells[key]


class Buzz(toga.App):
    ######################################################################
    # Dependencies (overridable, for trying the UI with fake data)
    ######################################################################

    def open_accounts(self) -> Accounts:
        self.paths.data.mkdir(parents=True, exist_ok=True)
        return Accounts(self.paths.data)

    def open_store(self, account: str) -> Store:
        return Store(self.accounts.path(account))

    # Secrets belong to the open account.

    def load_secret(self, name: str) -> str | None:
        try:
            return credentials.load_secret(
                self.app_id, secret_name(name, self.accounts.current)
            )
        except credentials.KeyringError:
            traceback.print_exc()
            return None

    def save_secret(self, name: str, value: str) -> None:
        credentials.save_secret(
            self.app_id, secret_name(name, self.accounts.current), value
        )

    def delete_secret(self, name: str) -> None:
        credentials.delete_secret(self.app_id, secret_name(name, self.accounts.current))

    def load_token(self) -> str | None:
        return self.load_secret(credentials.ACCOUNT)

    def save_token(self, token: str) -> None:
        self.save_secret(credentials.ACCOUNT, token)

    def delete_token(self) -> None:
        self.delete_secret(credentials.ACCOUNT)

    def make_client(self, token: str) -> GitHubClient:
        return GitHubClient(token)

    async def sign_in(self, show_code: Callable[[auth.DeviceCode], None]) -> str:
        return await auth.sign_in(show_code)

    def make_remote_client(self, url: str, code: str) -> RemoteClient:
        return RemoteClient(url, code)

    def share_port(self) -> int:
        return PORT

    ######################################################################
    # Startup
    ######################################################################

    def startup(self):
        macos.disable_window_tabs()
        self.accounts = self.open_accounts()
        # The open account's inbox, if it's on this Mac.
        self.local_store: Store | None = None
        if self.accounts.current not in self.accounts.remotes:
            self.local_store = self.open_store(self.accounts.current)
        self.github: GitHubClient | None = None
        self.sync: Sync | None = None
        # The open account's inbox, if it's on another Mac.
        self.remote: RemoteInbox | None = None
        # Shown while there's no inbox yet.
        self.nothing = Mirror()
        self.status_text = ""
        # Letting Buzz on another Mac triage this account through this one.
        self.server: RemoteServer | None = None
        self.share_window: toga.Window | None = None
        self.last_remote_change = ""

        self.view: str = INBOX
        self.labels: list[Label] = []
        self.sync_task: asyncio.Task | None = None
        self.sign_in_task: asyncio.Task | None = None
        self.verification_url: str | None = None
        self.wake = asyncio.Event()
        self.check_added_now = True
        self.added_checked_at = 0.0
        self.detail_pending = False
        # The notification whose page is showing.
        self.reading: str | None = None
        # The value chosen in each section of the Filter menu, by column.
        self.chosen_filters: dict[str, str] = {}
        # The pills drawn in the sidebar's and the list's cells; see pills_cell.
        self.sidebar_cells: dict = {}
        # The labels shown beside the page's address.
        self.shown_labels = None
        # The Labels menu's command for each label, and what they show.
        self.label_commands: list[toga.Command] = []
        self.labels_in_menu: list | None = None
        self.account_commands: list[toga.Command] = []
        self.shortcuts_window: toga.Window | None = None

        self.icons = {
            name: toga.Icon(f"resources/{name}")
            for name in ("unread", "check", "blank")
        }
        self.icons["attention"] = toga.Icon("resources/octicons/alert-fill-yellow")
        for name, description in (
            ("unread", "New activity"),
            ("check", "Done"),
            ("attention", "Needs attention"),
        ):
            macos.describe_icon(self.icons[name], description)
        self.type_icons = {
            name: toga.Icon(f"resources/octicons/{name}")
            for name in {*STATE_ICONS.values(), *TYPE_ICONS.values(), OTHER_TYPE_ICON}
        }
        for name, icon in self.type_icons.items():
            if not name.endswith(("-green", "-purple", "-red")):
                macos.use_as_template(icon)
            macos.describe_icon(icon, ICON_DESCRIPTIONS.get(name, ""))

        self.main_window = toga.MainWindow(size=(1320, 820))
        self.create_commands()
        self.inbox_view = self.build_inbox()
        self.settings_view = self.build_settings()

        self.token = self.load_token()
        self.name_account()
        self.update_title()
        self.reload()
        if self.token or self.local_store is None:
            self.show_inbox()
        else:
            self.main_window.content = toga.Box()  # Until signing in starts.
        self.main_window.show()
        macos.select_row(self.sidebar, 0)

    async def on_running(self):
        # Toga makes the toolbar once startup() has finished.
        macos.add_toolbar_menus(self.main_window, [self.label_menu, self.filter_menu])
        await self.open_account()

    async def on_exit(self):
        self.stop_sharing()
        await self.finish_tasks()
        if self.github is not None:
            await self.github.aclose()
        if self.remote is not None:
            await self.remote.client.aclose()
        return True

    async def finish_tasks(self):
        """Cancel every other task and wait for it to end, as `asyncio.run()`
        does on the way out; Toga's event loop just stops. A task that's still
        running, such as one setting up an HTTPS connection in a worker
        thread, can keep Python from exiting."""
        others = asyncio.all_tasks() - {asyncio.current_task()}
        for task in others:
            task.cancel()
        if others:
            await asyncio.wait(others, timeout=5)

    async def open_account(self):
        """Connect to the other Mac if the open account is there. Otherwise,
        show its inbox and keep it up to date with GitHub, if it's signed in,
        or ask it to sign in."""
        account = self.accounts.current
        if (url := self.accounts.remotes.get(account)) is not None:
            code = self.load_secret(CONNECT_CODE)
            try:
                client = self.make_remote_client(url, code) if code else None
            except ValueError:
                client = None
            if client is None:
                await self.disconnect_remote(f"Connect to Buzz at {url} again.")
            else:
                self.start_remote(client)
        elif self.token:
            self.start_syncing(self.token)
        else:
            self.show_sign_in()

    def start_syncing(self, token: str):
        """Show the inbox, keep it up to date with GitHub, and share it with
        another Mac if you've chosen to."""
        self.connect(token)
        self.show_inbox()
        if self.local_store.get_meta("share") and (error := self.start_sharing()):
            self.report(f"⚠️ {error}")

    @property
    def store(self) -> Inbox:
        """The inbox being shown."""
        if self.remote is not None:
            return self.remote.mirror
        return self.local_store if self.local_store is not None else self.nothing

    @property
    def client(self):
        """What changes the inbox being shown: GitHub, or Buzz on the other Mac
        for an account that's there. None if not connected."""
        return self.remote.client if self.remote else self.github

    def login(self) -> str | None:
        """The open account's GitHub login, once that's known."""
        if self.remote is not None:
            return self.remote.mirror.login
        if self.local_store is None:
            return None
        return self.local_store.get_meta("login")

    def create_commands(self):
        mod, shift = toga.Key.MOD_1, toga.Key.SHIFT
        notification = toga.Group("Notification", order=25)
        self.labels_group = labels = toga.Group("Labels", order=26)
        self.accounts_group = toga.Group(
            "Switch Account", parent=toga.Group.FILE, section=2, order=1
        )

        # Each command's tooltip also describes it in the Keyboard Shortcuts
        # window, so the two can't drift apart.
        self.refresh_cmd = toga.Command(
            self.on_refresh,
            "Refresh",
            shortcut=mod + "r",
            icon="resources/refresh",
            tooltip="Check GitHub for new notifications now. Buzz also checks "
            "about once a minute.",
            group=toga.Group.VIEW,
        )
        self.clear_filters_cmd = toga.Command(
            self.on_clear_filters,
            "Clear Filters",
            shortcut=mod + "f",
            tooltip="Show every notification in the view again. Filters stay "
            "set as you switch views.",
            group=toga.Group.VIEW,
            order=1,
        )
        self.add_thread_cmd = toga.Command(
            self.on_add_thread,
            "Add by Number…",
            shortcut=mod + "t",
            icon="resources/add",
            tooltip="Add an issue, pull request or discussion, like "
            "beeware/toga#1234 or a link to it, to the label you're viewing, or "
            "the inbox.",
            group=notification,
            order=-1,
        )
        self.open_cmd = toga.Command(
            self.on_open,
            "Open on GitHub",
            shortcut=mod + "o",
            icon="resources/open",
            tooltip="Open the selected notifications in your browser, which "
            "marks them as read. Double-clicking one does the same.",
            group=notification,
        )
        self.read_cmd = toga.Command(
            self.on_mark_read,
            "Mark as Read",
            shortcut=mod + shift + "u",
            icon="resources/read",
            tooltip="Mark the selected notifications as read, here and on GitHub.",
            group=notification,
            section=1,
            order=1,
        )
        self.done_cmd = toga.Command(
            self.on_mark_done,
            "Mark as Done",
            shortcut=mod + "d",
            icon="resources/done",
            tooltip="Take the selected notifications out of every view, labels "
            "included, and mark them as done on GitHub. Activity from someone "
            "else brings them back to the inbox.",
            group=notification,
            section=1,
            order=2,
        )
        self.unsubscribe_cmd = toga.Command(
            self.on_unsubscribe,
            "Unsubscribe",
            shortcut=mod + shift + "m",
            icon="resources/unsubscribe",
            tooltip="Unsubscribe from the selected threads, and mark them as done.",
            group=notification,
            section=1,
            order=3,
        )
        self.remove_cmd = toga.Command(
            self.on_remove_from_view,
            "Remove from This View",
            # Delete, handled by on_delete_key.
            tooltip="Take the label you're viewing off the selected "
            "notifications. In Attention required, this is how you say you've "
            "dealt with them.",
            group=notification,
            section=2,
        )
        self.new_label_cmd = toga.Command(
            self.on_new_label,
            "New Label…",
            shortcut=mod + "n",
            tooltip="Make a label, and choose whether activity flags its "
            "notifications for attention.",
            group=labels,
            section=1,
            order=1,
        )
        self.edit_label_cmd = toga.Command(
            self.on_edit_label,
            "Edit Label…",
            shortcut=mod + "i",
            tooltip="Rename the label you're viewing, or change whether activity "
            "flags its notifications. Double-clicking a label does the same.",
            group=labels,
            section=1,
            order=2,
        )
        self.move_up_cmd = toga.Command(
            self.on_move_label_up,
            "Move Label Up",
            # ⌘↑, handled by on_move_key.
            tooltip="Move the label you're viewing up the sidebar, which changes "
            "its number.",
            group=labels,
            section=1,
            order=3,
        )
        self.move_down_cmd = toga.Command(
            self.on_move_label_down,
            "Move Label Down",
            # ⌘↓, handled by on_move_key.
            tooltip="Move the label you're viewing down the sidebar, which "
            "changes its number.",
            group=labels,
            section=1,
            order=4,
        )
        self.delete_label_cmd = toga.Command(
            self.on_delete_label,
            "Delete Label…",
            shortcut=mod + shift + toga.Key.BACKSPACE,
            tooltip="Delete the label you're viewing, after asking. Its "
            "notifications go back to the inbox, unless they have other labels.",
            group=labels,
            section=2,
        )
        self.sign_out_cmd = toga.Command(
            self.on_sign_out,
            "Sign Out of GitHub",
            tooltip="Forget this account's GitHub sign-in. Its labels are kept.",
            group=toga.Group.FILE,
            section=1,
        )
        self.add_account_cmd = toga.Command(
            self.on_add_account,
            "Add Account…",
            tooltip="Triage another GitHub account's notifications, with labels "
            "of its own. Sign it in to GitHub, or connect to Buzz on a Mac that's "
            "signed in to it.",
            group=toga.Group.FILE,
            section=2,
        )
        self.share_cmd = toga.Command(
            self.on_show_share,
            "Share This Inbox…",
            tooltip="Let Buzz on another Mac triage this account's notifications "
            "through this one, so that Mac doesn't need to sign in to GitHub.",
            group=toga.Group.FILE,
            section=1,
            order=1,
            enabled=False,
        )
        self.disconnect_cmd = toga.Command(
            self.on_disconnect_remote,
            "Disconnect from Other Mac",
            tooltip="Stop triaging through Buzz on the other Mac. Nothing changes "
            "there.",
            group=toga.Group.FILE,
            section=1,
            order=2,
            enabled=False,
        )
        self.shortcuts_cmd = toga.Command(
            self.on_show_shortcuts,
            "Keyboard Shortcuts",
            shortcut=mod + "/",
            tooltip="Show this list.",
            group=toga.Group.HELP,
        )
        self.commands.add(
            self.refresh_cmd,
            self.clear_filters_cmd,
            self.add_thread_cmd,
            self.open_cmd,
            self.read_cmd,
            self.done_cmd,
            self.unsubscribe_cmd,
            self.remove_cmd,
            self.new_label_cmd,
            self.edit_label_cmd,
            self.move_up_cmd,
            self.move_down_cmd,
            self.delete_label_cmd,
            self.sign_out_cmd,
            self.add_account_cmd,
            self.share_cmd,
            self.disconnect_cmd,
            self.shortcuts_cmd,
        )
        if settings := toga.Command.standard(self, toga.Command.PREFERENCES):
            self.commands.add(settings)
        self.rebuild_account_commands()

        self.main_window.toolbar.add(
            self.add_thread_cmd,
            self.refresh_cmd,
            self.open_cmd,
            self.read_cmd,
            self.done_cmd,
            self.unsubscribe_cmd,
        )
        self.label_menu = macos.ToolbarMenu(
            "buzz-label",
            "Label",
            "tag",
            self.label_menu_items,
            tooltip="Add or remove a label on the selected notifications. "
            "With the list selected, number keys add them too.",
        )
        self.filter_menu = macos.ToolbarMenu(
            "buzz-filter",
            "Filter",
            FILTER_SYMBOLS[0],
            self.filter_menu_items,
            space_before=True,
        )
        self.update_filter_menu()

    ######################################################################
    # Layout
    ######################################################################

    def build_inbox(self) -> toga.Widget:
        # Sidebar: the built-in views, then the labels.
        self.sidebar = toga.Table(
            columns=[AccessorColumn("View", "name"), AccessorColumn("Count", "count")],
            show_headings=False,
            on_select=self.on_view_select,
            on_activate=self.on_edit_label,
            flex=1,
        )
        macos.make_sidebar(self.sidebar, [None, 76])
        macos.on_list_keys(
            self.sidebar, on_number=self.on_number_key, on_move=self.on_move_key
        )
        self.label_input = toga.TextInput(
            placeholder="New label", on_confirm=self.on_add_label, flex=1
        )
        self.status = toga.Label("", font_size=11, margin=(0, 10, 8, 10))
        self.sidebar_column = toga.Box(
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
                self.status,
            ],
        )

        # The notifications in the current view.
        self.search = toga.TextInput(
            placeholder="Filter by title or repository",
            on_change=self.on_filter,
            flex=1,
        )
        self.table = toga.Table(
            columns=[
                AccessorColumn("", "kind"),
                AccessorColumn("Title", "title"),
                AccessorColumn("Repository", "repo"),
                AccessorColumn("Updated", "updated"),
            ],
            multiple_select=True,
            on_select=self.on_thread_select,
            on_activate=self.on_open,
            flex=1,
        )
        macos.set_column_widths(self.table, [22, None, 170, 130])
        macos.on_list_keys(
            self.table, on_number=self.on_number_key, on_delete=self.on_delete_key
        )
        listing = toga.Box(
            direction=COLUMN,
            children=[
                toga.Box(direction=ROW, margin=8, children=[self.search]),
                self.table,
            ],
        )

        # The selected notification's page on GitHub, under an address bar
        # (or, when there isn't just one, a note saying so).
        self.web_view = toga.WebView(flex=1)
        macos.open_links_elsewhere(self.web_view, is_github_url, webbrowser.open)
        macos.watch_navigation(self.web_view, self.on_navigation)
        self.back_button = toga.Button(
            "◀", on_press=lambda widget, **kwargs: macos.go_back(self.web_view)
        )
        self.forward_button = toga.Button(
            "▶", on_press=lambda widget, **kwargs: macos.go_forward(self.web_view)
        )
        reload_button = toga.Button(
            "↻", on_press=lambda widget, **kwargs: macos.reload_page(self.web_view)
        )
        for button, symbol, description in (
            (self.back_button, "chevron.backward", "Back"),
            (self.forward_button, "chevron.forward", "Forward"),
            (reload_button, "arrow.clockwise", "Reload"),
        ):
            macos.use_symbol(button, symbol, description)
        self.address = toga.TextInput(on_confirm=self.on_go_to_address, flex=1)
        # The labels of the notification being read, as pills (or as text, if
        # they can't be drawn).
        self.page_labels = macos.pills((), "") or toga.Label("")
        self.page = toga.Box(
            direction=COLUMN,
            flex=1,
            children=[
                toga.Box(
                    direction=ROW,
                    gap=4,
                    margin=(6, 8),
                    align_items=CENTER,
                    children=[
                        self.back_button,
                        self.forward_button,
                        reload_button,
                        self.address,
                        self.page_labels,
                    ],
                ),
                toga.Divider(),
                self.web_view,
            ],
        )
        self.placeholder_title = toga.Label("", font_size=15, font_weight=BOLD)
        self.placeholder_text = toga.Label("")
        self.placeholder = toga.Box(
            direction=COLUMN,
            gap=8,
            margin=14,
            flex=1,
            children=[self.placeholder_title, self.placeholder_text],
        )
        # Starts with the note; update_detail swaps the page in when there's
        # one to show. (Toga doesn't hide widgets with display=NONE yet.)
        self.detail = toga.Box(direction=COLUMN, children=[self.placeholder])

        main = toga.SplitContainer(
            direction=toga.SplitContainer.HORIZONTAL,
            content=[(listing, 2), (self.detail, 3)],
        )
        return toga.SplitContainer(content=[(self.sidebar_column, 1), (main, 4)])

    def build_settings(self) -> toga.Widget:
        self.account_label = toga.Label("")
        self.account_buttons = toga.Box(direction=ROW, gap=8)
        self.sign_in_button = toga.Button("Sign In…", on_press=self.on_sign_in_again)
        self.settings_sign_out_button = toga.Button(
            "Sign Out", on_press=self.on_sign_out
        )
        self.settings_disconnect_button = toga.Button(
            "Disconnect", on_press=self.on_disconnect_remote
        )
        form = toga.Box(
            direction=COLUMN,
            gap=12,
            width=480,
            children=[
                toga.Label("GitHub", font_size=20, font_weight=BOLD),
                self.account_label,
                self.account_buttons,
                toga.Box(
                    direction=ROW,
                    margin_top=8,
                    children=[
                        toga.Box(flex=1),
                        toga.Button("Done", on_press=self.on_close_settings),
                    ],
                ),
            ],
        )
        return toga.ScrollContainer(
            horizontal=False,
            content=toga.Box(
                direction=COLUMN,
                align_items=CENTER,
                margin=(40, 0),
                children=[form],
            ),
        )

    def show_inbox(self):
        self.cancel_sign_in()
        self.main_window.content = self.inbox_view

    def show_settings(self):
        self.cancel_sign_in()
        login = self.login()
        if self.remote is not None:
            self.account_label.text = textwrap.fill(
                f"Triaging through Buzz at {self.remote.host}"
                + (f", which is signed in to GitHub as {login}." if login else "."),
                64,
            )
            button = self.settings_disconnect_button
        elif self.token:
            self.account_label.text = (
                f"Signed in to GitHub as {login}." if login else "Signed in to GitHub."
            )
            button = self.settings_sign_out_button
        else:
            self.account_label.text = "Not signed in to GitHub."
            button = self.sign_in_button
        self.account_buttons.clear()
        self.account_buttons.add(button)
        self.main_window.content = self.settings_view

    def update_title(self):
        login = self.login()
        if self.remote is not None:
            title = f"{self.formal_name} — {login or 'New account'}, through {self.remote.host}"
        elif login:
            title = f"{self.formal_name} — {login}"
        elif len(self.accounts.names) > 1:
            title = f"{self.formal_name} — New account"
        else:
            title = self.formal_name
        if self.server is not None:
            title += " · 📡 Remote control on"
        self.main_window.title = title

    def report(self, text: str, remote: RemoteInbox | None = None):
        """Show a status message about the open account's inbox. ``remote`` is
        the inbox on another Mac it's about, if it's about one: if that isn't
        open any more, there's nothing to say."""
        if remote is not None and remote is not self.remote:
            return
        self.status_text = text
        # While another Mac can triage this inbox, keep saying what it last
        # did, whatever else happens.
        if self.server is not None and self.last_remote_change:
            text += f"   📡 Remotely {self.last_remote_change}."
        self.set_status(text)

    def set_status(self, text: str):
        """Show text in the status line, at the bottom of the sidebar."""
        self.status.text = textwrap.fill(text, STATUS_WIDTH)

    ######################################################################
    # Showing data
    ######################################################################

    def reload(self):
        """Bring the sidebar, notification list and Labels menu up to date with
        the store."""
        self.labels = self.store.labels()
        counts, new = self.store.counts(), self.store.new_counts()

        def count(key) -> str:
            return count_text(counts.get(key, 0), new.get(key, 0))

        sync_rows(
            self.sidebar.data,
            [{"key": key, "name": name, "count": count(key)} for key, name in VIEWS]
            + [
                {
                    "key": label.id,
                    "name": self.sidebar_label(number, label),
                    "count": count(label.id),
                }
                for number, label in enumerate(self.labels, 1)
            ],
        )
        prune(self.sidebar_cells, [label.id for label in self.labels])
        self.update_label_commands()
        sync_rows(self.table.data, self.thread_rows(keep=self.reading))
        self.schedule_detail_update()

    def sidebar_label(self, number: int, label: Label):
        """A label's sidebar cell: its number key, if it has one, and the
        label as a pill."""
        key = str(number) if number <= NUMBERED_LABELS else ""
        return pills_cell(
            self.sidebar_cells,
            label.id,
            (Pill(key, key=True), Pill(label.name, label.color)),
            f"{label.name}. With the list selected, press {key} to add it to "
            "the selected notifications."
            if key
            else label.name,
            f"{key}  {label.color}  {label.name}",
        )

    def update_label_commands(self):
        """A command in the Labels menu for each label, with its number key."""
        items = [(label.id, label.name) for label in self.labels]
        if items == self.labels_in_menu:
            return
        for command in self.label_commands:
            del self.commands[command.id]
        self.label_commands = [
            toga.Command(
                self.label_command_action(label_id),
                f"{number}   {name}" if number <= NUMBERED_LABELS else name,
                tooltip=f"Give the selected notifications the label {name}.",
                group=self.labels_group,
                order=number,
                id=f"label:{label_id}",
                enabled=bool(self.table.selection),
            )
            for number, (label_id, name) in enumerate(items, 1)
        ]
        self.commands.add(*self.label_commands)
        self.labels_in_menu = items

    def update_filter_menu(self):
        """Show on the Filter menu's button whether the list is filtered."""
        chosen = [
            f"{heading.lower()} “{describe(self.chosen_filters[column])}”"
            for column, heading, describe in FILTERS
            if column in self.chosen_filters
        ]
        self.filter_menu.update(
            label="Filtered" if chosen else "Filter",
            symbol=FILTER_SYMBOLS[bool(chosen)],
            tooltip=f"Filtering by {', '.join(chosen)}"
            if chosen
            else "Show only some of the notifications",
        )

    def filter_menu_items(self) -> list[MenuItem]:
        """The Filter menu: each value of each filter in the current view, with
        how many notifications choosing it would show."""
        chosen = self.chosen_filters
        counts = self.store.filter_counts(self.view, self.search.value, chosen)
        items = [MenuItem("Clear Filters", self.clear_filters, enabled=bool(chosen))]
        for column, heading, describe in FILTERS:
            found = counts[column]
            if column in chosen:
                # Keep offering it, so it can be cleared, even if it's gone.
                found.setdefault(chosen[column], 0)
            items += [SEPARATOR, MenuItem(heading, header=True)]
            items += [
                MenuItem(
                    describe(value),
                    partial(self.choose_filter, column, value),
                    state=chosen.get(column) == value,
                    badge=count,
                )
                for value, count in sorted(
                    found.items(), key=lambda item: describe(item[0]).casefold()
                )
            ]
            if not found:
                items.append(MenuItem("None here", enabled=False))
        return items

    def label_menu_items(self) -> list[MenuItem]:
        """The Label menu: attention, and each label, ticked if every selected
        notification has it (or a dash if some do), with its number key."""
        threads = self.selected_threads()
        assigned = self.store.labels_by_thread()

        def state(has: Callable[[Thread], bool]) -> bool | None:
            count = sum(1 for thread in threads if has(thread))
            return False if not count else (True if count == len(threads) else None)

        items = [
            MenuItem(
                ATTENTION_NAME,
                self.toggle_attention,
                state(lambda thread: thread.attention),
            ),
            SEPARATOR,
        ]
        if not self.labels:
            items.append(MenuItem("Add a label in the sidebar first", enabled=False))
        for number, label in enumerate(self.labels, 1):
            key = f"{number}   " if number <= NUMBERED_LABELS else ""
            items.append(
                MenuItem(
                    f"{key}{label.name}",
                    partial(self.toggle_label, label.id),
                    state(
                        lambda thread, label=label: (
                            label.id in {x.id for x in assigned.get(thread.id, [])}
                        )
                    ),
                )
            )
        return items

    def thread_rows(self, keep: str | None = None) -> list[dict]:
        """The rows for the notifications in the current view.

        :param keep: A notification to leave where it is in the list, if
            reading it is all that has taken it out of the Unread view, so it
            stays while it's being read.
        """
        labels = self.store.labels_by_thread()
        threads = self.store.threads(self.view, self.search.value, self.chosen_filters)
        if (
            keep is not None
            and self.view == UNREAD
            and keep not in {thread.id for thread in threads}
            and (index := self.row_index(keep)) is not None
            and (thread := self.store.get(keep)) is not None
            and not (thread.done or thread.attention or labels.get(keep))
        ):
            threads.insert(index, thread)
        return [self.thread_row(thread) for thread in threads]

    def thread_row(self, thread: Thread) -> dict:
        # Needing attention means new activity, so it takes the new-activity
        # dot's place.
        if thread.attention:
            icon = self.icons["attention"]
        elif thread.new:
            icon = self.icons["unread"]
        elif thread.done:
            icon = self.icons["check"]
        else:
            icon = self.icons["blank"]
        return {
            "key": thread.id,
            "kind": (self.type_icons[type_icon(thread)], ""),
            "title": (icon, thread.title),
            "repo": reference(thread),
            "updated": local_time(thread.updated_at),
        }

    def show_page_labels(self, thread: Thread | None):
        """Show a thread's labels beside the page's address, after ⚠️ if it
        needs attention."""
        labels = self.store.labels_by_thread().get(thread.id, []) if thread else []
        attention = thread is not None and thread.attention
        pills = ([Pill("⚠️")] if attention else []) + [
            Pill(label.name, label.color) for label in labels
        ]
        if pills == self.shown_labels:
            return
        self.shown_labels = pills
        names = ([ATTENTION_NAME] if attention else []) + [x.name for x in labels]
        if isinstance(self.page_labels, toga.Label):
            self.page_labels.text = ",  ".join(names)
        else:
            self.page_labels.show(pills, ", ".join(names))

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
        self.read_cmd.enabled = connected and bool(to_view(threads))
        self.done_cmd.enabled = connected and any(not t.done for t in threads)
        self.unsubscribe_cmd.enabled = connected and bool(threads)
        labelled = self.current_label() is not None
        self.remove_cmd.enabled = bool(threads) and (labelled or self.view == ATTENTION)
        for command in (
            self.edit_label_cmd,
            self.move_up_cmd,
            self.move_down_cmd,
            self.delete_label_cmd,
        ):
            command.enabled = labelled
        for command in self.label_commands:
            command.enabled = bool(threads)
        self.sign_out_cmd.enabled = bool(self.token)
        self.share_cmd.enabled = self.github is not None
        self.disconnect_cmd.enabled = self.remote is not None
        self.label_menu.update(enabled=bool(threads))

        if len(threads) == 1:
            self.show_page(threads[0])
        else:
            self.reading = None
            if threads:
                self.placeholder_title.text = f"{len(threads)} notifications selected"
                self.placeholder_text.text = "Label changes apply to all of them."
            else:
                self.placeholder_title.text = "No notification selected"
                self.placeholder_text.text = (
                    "Select a notification to see its page on GitHub."
                )
        shown, hidden = (
            (self.page, self.placeholder)
            if self.reading
            else (self.placeholder, self.page)
        )
        if shown not in self.detail.children:
            self.detail.replace(hidden, shown)
        self.show_page_labels(self.store.get(self.reading) if self.reading else None)

    def show_page(self, thread: Thread):
        """Show a notification's page on GitHub, which counts as viewing it: it
        marks it as read, as opening it on github.com does."""
        if thread.id == self.reading:
            return  # Leave it where they've scrolled or clicked to.
        self.reading = thread.id
        self.web_view.url = self.address.value = html_url(thread)
        self.loop.create_task(self.view_threads([thread]))

    def sidebar_index(self, key: str) -> int | None:
        for index, row in enumerate(self.sidebar.data):
            if row.key == key:
                return index
        return None

    def select_view(self, key: str):
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

    def on_filter(self, widget=None, **kwargs):
        self.table.data = self.thread_rows()
        self.schedule_detail_update()

    def choose_filter(self, column: str, value: str):
        """Show only the notifications with this value, or stop, if they're
        already the only ones shown."""
        if self.chosen_filters.get(column) == value:
            del self.chosen_filters[column]
        else:
            self.chosen_filters[column] = value
        self.update_filter_menu()
        self.on_filter()

    def clear_filters(self):
        self.chosen_filters.clear()
        self.update_filter_menu()
        self.on_filter()

    def on_clear_filters(self, command=None, **kwargs):
        self.clear_filters()

    def on_thread_select(self, widget, **kwargs):
        self.schedule_detail_update()

    def on_navigation(self, url: str, can_go_back: bool, can_go_forward: bool):
        if url:  # Keep showing the last address while the next one loads.
            self.address.value = url
        self.back_button.enabled = can_go_back
        self.forward_button.enabled = can_go_forward

    def on_go_to_address(self, widget, **kwargs):
        if address := widget.value.strip():
            self.web_view.url = address if "://" in address else f"https://{address}"

    ######################################################################
    # Handlers: notification actions
    ######################################################################

    def on_refresh(self, command=None, **kwargs):
        if self.remote is not None:
            # Ask the other Mac to check GitHub; the result shows shortly.
            self.send_remote("refresh")
        elif self.github is not None:
            self.check_added_now = True
            self.wake.set()

    async def on_open(self, widget=None, **kwargs):
        threads = self.selected_threads()[:OPEN_LIMIT]
        for thread in threads:
            webbrowser.open(html_url(thread))
        # Opening a notification on github.com marks it as read; do the same.
        await self.view_threads(threads)

    async def view_threads(self, threads: list[Thread]):
        """Record that you've seen threads, and mark them as read on GitHub.
        This doesn't clear Attention required: remove that to say you've dealt
        with them."""
        await self.update_threads(
            to_view(threads),
            self.store.mark_seen,
            lambda client, thread_id: client.mark_read(thread_id),
        )

    async def on_mark_read(self, command=None, **kwargs):
        await self.view_threads(self.selected_threads())

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

    def on_remove_from_view(self, command=None, **kwargs):
        """Take the view's label off the selected notifications: the label's,
        or Attention required."""
        if (label := self.current_label()) is not None:
            self.change_selected(
                lambda ids: self.store.set_label(label.id, ids, False),
                "unlabel",
                label=label.id,
            )
        elif self.view == ATTENTION:
            self.change_selected(
                lambda ids: self.store.set_attention(ids, False),
                "unlabel",
                label=ATTENTION,
            )

    def on_delete_key(self) -> bool:
        """Delete in the list takes the selected notifications out of the view.
        The inbox has no label to take off, so there it means Mark as Done."""
        if not self.selected_threads():
            return False
        if self.current_label() is not None or self.view == ATTENTION:
            self.on_remove_from_view()
        elif self.view in (INBOX, UNREAD) and self.client is not None:
            self.loop.create_task(self.on_mark_done())
        else:
            return False
        return True

    async def on_add_thread(self, command=None, **kwargs):
        if self.client is None:
            await self.main_window.dialog(
                toga.ErrorDialog(
                    "Not Connected to GitHub",
                    "Sign in to GitHub in Settings (⌘,) first.",
                )
            )
            return
        # It goes into the label being shown, or else the inbox.
        label = self.current_label()
        where = f"the label “{label.name}”" if label else "the inbox"
        answers = await self.ask_form(
            "Add to Buzz",
            [
                (
                    "ref",
                    f"Issue, pull request or discussion to add to {where}:",
                    "",
                    False,
                )
            ],
            "Add",
            note="For example beeware/toga#1234, or a link to it on GitHub.",
        )
        if answers is None or not answers["ref"].strip():
            return
        text, label_id = answers["ref"], label.id if label else None

        remote = self.remote
        self.set_status(f"Looking up {text.strip()}…")
        try:
            if remote is not None:
                result = await remote.client.action(
                    "add", reference=text, label=label_id
                )
                thread_id = result["thread"]
                remote.etag = None
                await self.remote_sync_once(remote)
            else:
                thread_id = (
                    await add_thread(self.local_store, self.github, text, label_id)
                ).id
        except (ValueError, GitHubError, RemoteError) as exc:
            self.set_status("")
            await self.main_window.dialog(toga.ErrorDialog("Couldn't add it", str(exc)))
            return

        if label is None and self.view != INBOX:
            self.select_view(INBOX)
        else:
            self.reload()
        if thread := self.store.get(thread_id):
            self.set_status(f"Added {reference(thread)}.")
            if (index := self.row_index(thread_id)) is not None:
                macos.select_row(self.table, index)
        else:
            self.set_status("")

    def row_index(self, thread_id: str) -> int | None:
        for index, row in enumerate(self.table.data):
            if row.key == thread_id:
                return index
        return None

    def move_selection_on(self, index: int | None, before: int):
        """Like on github.com, select the next notification when the selected
        ones have left the list (``before`` is how many rows there were)."""
        remaining = len(self.table.data)
        if index is not None and 0 < remaining < before:
            macos.select_row(self.table, min(index, remaining - 1))

    async def update_threads(
        self,
        threads: list[Thread],
        local: Callable[[Iterable[str]], None],
        remote: Callable[[GitHubClient, str], Awaitable[None]],
        select_next: bool = False,
    ):
        """Change threads in the inbox being shown straight away, then on
        GitHub (or the other Mac).

        GitHub's side is best effort: if it fails, the change in Buzz stands,
        and the failure is logged. If the other Mac doesn't make a change,
        though, the next look at its inbox puts it back, so say so.
        """
        client, store, remote_inbox = self.client, self.store, self.remote
        if not threads or client is None:
            return

        index, before = self.selected_index(), len(self.table.data)

        def after_local():
            self.reload()
            if select_next:
                self.move_selection_on(index, before)

        if remote_inbox is not None:
            remote_inbox.pending += 1
        try:
            failures = await apply_to_threads(
                store, client, threads, local, remote, after_local
            )
        finally:
            if remote_inbox is not None:
                self.remote_changed(remote_inbox)
        if failures and remote_inbox is not None:
            await self.main_window.dialog(
                toga.ErrorDialog(
                    "Couldn't change the other Mac's inbox",
                    f"{len(failures)} of {len(threads)} notifications weren't "
                    f"changed.\n\n{failures[0][1]}",
                )
            )

    ######################################################################
    # Handlers: labels
    ######################################################################

    def current_label(self) -> Label | None:
        return next((label for label in self.labels if label.id == self.view), None)

    async def create_label(self, name: str, flag: bool = False) -> bool:
        """Make a label in the inbox being shown. Returns whether it worked."""
        remote = self.remote
        try:
            if remote is not None:
                # The other Mac decides the new label's ID, so wait for it.
                await remote.client.action(
                    "define-label", name=name, flag_on_activity=flag
                )
            else:
                self.store.create_label(name, flag=flag)
        except (ValueError, RemoteError) as exc:
            await self.main_window.dialog(
                toga.ErrorDialog("Couldn't add the label", str(exc))
            )
            return False
        if remote is not None:
            remote.etag = None
            await self.remote_sync_once(remote)
        self.reload()
        return True

    async def on_add_label(self, widget=None, **kwargs):
        if await self.create_label(self.label_input.value):
            self.label_input.value = ""

    async def on_new_label(self, command=None, **kwargs):
        if (answer := await self.ask_label("New Label", "", False, "Create")) is None:
            return
        await self.create_label(*answer)

    async def on_edit_label(self, widget=None, **kwargs):
        if (label := self.current_label()) is None:
            return
        answer = await self.ask_label("Edit Label", label.name, label.flag, "Save")
        if answer is None:
            return
        name, flag = answer
        try:
            self.store.edit_label(label.id, name, flag)
        except ValueError as exc:
            await self.main_window.dialog(
                toga.ErrorDialog("Couldn't change the label", str(exc))
            )
            return
        self.send_remote(
            "define-label", label=label.id, name=name, flag_on_activity=flag
        )
        self.reload()

    async def on_delete_label(self, command=None, **kwargs):
        if (label := self.current_label()) is None:
            return
        count = self.store.counts().get(label.id, 0)
        if await self.main_window.dialog(
            toga.ConfirmDialog(
                f"Delete “{label.name}”?",
                f"It will be removed from {count} notification"
                f"{'' if count == 1 else 's'}. Any that have no other label go "
                "back to the inbox."
                if count
                else "No notifications have this label.",
            )
        ):
            self.store.delete_label(label.id)
            self.send_remote("delete-label", label=label.id)
            self.select_view(INBOX)

    def on_move_key(self, offset: int) -> bool:
        """⌘↑ and ⌘↓ in the sidebar move the label you're viewing."""
        if self.current_label() is None:
            return False
        self.move_label(offset)
        return True

    def on_move_label_up(self, command=None, **kwargs):
        self.move_label(-1)

    def on_move_label_down(self, command=None, **kwargs):
        self.move_label(1)

    def move_label(self, offset: int):
        """Move the label you're viewing up (-1) or down (1) the sidebar, which
        changes its number key."""
        if (label := self.current_label()) is None:
            return
        order = [x.id for x in self.labels]
        index = order.index(label.id)
        if not 0 <= index + offset < len(order):
            return
        order.insert(index + offset, order.pop(index))
        self.store.reorder_labels(order)
        self.send_remote("reorder-labels", order=order)
        self.reload()
        if (row := self.sidebar_index(label.id)) is not None:
            macos.select_row(self.sidebar, row)

    def on_number_key(self, index: int) -> bool:
        """1 to 9 give the selected notifications the label with that number."""
        if index >= len(self.labels) or not self.selected_threads():
            return False
        self.apply_label(self.labels[index].id)
        return True

    def label_command_action(self, label_id: str):
        def apply(command, **kwargs):
            self.apply_label(label_id)

        return apply

    def apply_label(self, label_id: str):
        """Give the selected notifications a label, which marks them as read.
        One that already has it keeps its place in it. If they've left the
        list, the next notification is selected."""
        label = next((x for x in self.labels if x.id == label_id), None)
        if label is None or not (threads := self.selected_threads()):
            return
        index, before = self.selected_index(), len(self.table.data)

        def after_local():
            self.reload()
            self.move_selection_on(index, before)

        if self.remote is not None:
            # The other Mac marks them as read on GitHub, and subscribes.
            ids = [thread.id for thread in threads]
            self.store.set_label(label_id, ids, True)
            self.send_remote("label", threads=ids, label=label_id)
            after_local()
        else:
            self.loop.create_task(
                label_threads(self.store, self.github, label, threads, after_local)
            )

    def toggle_label(self, label_id: str):
        """Add a label to the selected notifications, or take it off if they
        all have it."""
        assigned = self.store.labels_by_thread()
        if not all(
            label_id in {label.id for label in assigned.get(thread.id, [])}
            for thread in self.selected_threads()
        ):
            self.apply_label(label_id)
            return
        self.change_selected(
            lambda ids: self.store.set_label(label_id, ids, False),
            "unlabel",
            label=label_id,
        )

    def toggle_attention(self):
        on = not all(thread.attention for thread in self.selected_threads())
        self.change_selected(
            lambda ids: self.store.set_attention(ids, on),
            "label" if on else "unlabel",
            label=ATTENTION,
        )

    def change_selected(
        self, change: Callable[[list[str]], None], action: str, **params
    ):
        """Make a change to the selected notifications with ``change``, and on
        the other Mac with ``action``, for an account that's there.

        A change can take the selected rows out of the list; the selection
        then moves on.
        """
        if thread_ids := [t.id for t in self.selected_threads()]:
            index, before = self.selected_index(), len(self.table.data)
            change(thread_ids)
            self.send_remote(action, threads=thread_ids, **params)
            self.reload()
            self.move_selection_on(index, before)

    async def ask_label(
        self, title: str, name: str, flag: bool, action: str
    ) -> tuple[str, bool] | None:
        """Ask for a label's name, and whether activity flags its threads, in a
        small window. Returns None if cancelled."""
        answer = self.loop.create_future()
        window = toga.Window(
            title=title, size=(420, 190), resizable=False, minimizable=False
        )
        name_input = toga.TextInput(value=name, placeholder="Label name")
        flag_switch = toga.Switch("Flag on activity", value=flag)

        def finish(confirmed: bool):
            if not answer.done():
                answer.set_result(
                    (name_input.value, flag_switch.value) if confirmed else None
                )
            window.close()

        # Return in the name field makes or saves the label.
        name_input.on_confirm = lambda widget, **kwargs: finish(True)
        window.content = toga.Box(
            direction=COLUMN,
            gap=8,
            margin=16,
            children=[
                name_input,
                flag_switch,
                toga.Label(
                    "When someone else is active on one of its notifications,\n"
                    "it's also put under Attention required.",
                    font_size=11,
                ),
                toga.Box(
                    direction=ROW,
                    gap=8,
                    margin_top=6,
                    children=[
                        toga.Box(flex=1),
                        toga.Button(
                            "Cancel", on_press=lambda widget, **kwargs: finish(False)
                        ),
                        toga.Button(
                            action, on_press=lambda widget, **kwargs: finish(True)
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
        name_input.focus()
        return await answer

    async def ask_text(
        self, title: str, prompt: str, value: str = "", action: str = "OK"
    ) -> str | None:
        """Ask for a line of text in a small window. Returns None if cancelled."""
        answers = await self.ask_form(title, [("text", prompt, value, False)], action)
        return None if answers is None else answers["text"]

    async def ask_form(
        self,
        title: str,
        fields: list[tuple[str, str, str, bool]],
        action: str = "OK",
        note: str = "",
    ) -> dict[str, str] | None:
        """Ask for some lines of text in a small window.

        :param fields: ``(key, label, value, secret)`` for each line.
        :returns: The text entered for each key, or None if cancelled.
        """
        answer = self.loop.create_future()
        window = toga.Window(
            title=title,
            size=(440, 90 + 58 * len(fields) + (40 if note else 0)),
            resizable=False,
            minimizable=False,
        )
        inputs = {}

        def finish(confirmed: bool):
            if not answer.done():
                answer.set_result(
                    {k: w.value for k, w in inputs.items()} if confirmed else None
                )
            window.close()

        children = [toga.Label(note)] if note else []
        for key, label, value, secret in fields:
            kind = toga.PasswordInput if secret else toga.TextInput
            inputs[key] = kind(
                value=value, on_confirm=lambda widget, **kwargs: finish(True)
            )
            children += [toga.Label(label), inputs[key]]
        children.append(
            toga.Box(
                direction=ROW,
                gap=8,
                margin_top=6,
                children=[
                    toga.Box(flex=1),
                    toga.Button(
                        "Cancel", on_press=lambda widget, **kwargs: finish(False)
                    ),
                    toga.Button(action, on_press=lambda widget, **kwargs: finish(True)),
                ],
            )
        )
        window.content = toga.Box(direction=COLUMN, gap=6, margin=16, children=children)

        def on_close(window, **kwargs):
            if not answer.done():
                answer.set_result(None)
            return True

        window.on_close = on_close
        window.show()
        next(iter(inputs.values())).focus()
        return await answer

    ######################################################################
    # Help
    ######################################################################

    def on_show_shortcuts(self, command=None, **kwargs):
        if self.shortcuts_window in self.windows:
            self.shortcuts_window.show()
            return

        def describe(command: toga.Command, keys: str = "", where: str = ""):
            return (
                keys or shortcut_text(command.shortcut),
                command.text,
                f"{command.tooltip} {where}".strip(),
            )

        sections = [
            (
                "Triage",
                [
                    (
                        "1 – 9",
                        "Add a label",
                        (
                            "Give the selected notifications the label with that "
                            "number, which marks them as read, then move on to the "
                            "next one. Works while the list or the sidebar has "
                            "focus. Each label's number is beside it, and in the "
                            "Labels menu."
                        ),
                    ),
                    (
                        "↑ ↓",
                        "Move between notifications",
                        (
                            "The selected notification's page shows beside the "
                            "list, which marks it as read."
                        ),
                    ),
                    describe(self.done_cmd),
                    describe(
                        self.remove_cmd,
                        "⌫",
                        "In the inbox, Delete marks them as done instead. Works "
                        "while the list has focus.",
                    ),
                    describe(self.read_cmd),
                    describe(self.unsubscribe_cmd),
                    describe(self.open_cmd),
                    describe(self.add_thread_cmd),
                ],
            ),
            (
                "Labels",
                [
                    describe(self.new_label_cmd),
                    describe(self.edit_label_cmd),
                    describe(self.delete_label_cmd),
                    describe(
                        self.move_up_cmd, "⌘↑", "Works while the sidebar has focus."
                    ),
                    describe(
                        self.move_down_cmd, "⌘↓", "Works while the sidebar has focus."
                    ),
                ],
            ),
            (
                "Filtering, checking and help",
                [
                    describe(self.clear_filters_cmd),
                    describe(self.refresh_cmd),
                    describe(self.shortcuts_cmd),
                ],
            ),
        ]

        children = []
        for heading, entries in sections:
            children.append(toga.Label(heading, font_weight=BOLD, font_size=14))
            for keys, title, description in entries:
                children.append(
                    toga.Box(
                        direction=ROW,
                        gap=14,
                        children=[
                            toga.Label(
                                keys, width=70, text_align=RIGHT, font_weight=BOLD
                            ),
                            toga.Box(
                                direction=COLUMN,
                                flex=1,
                                gap=2,
                                children=[
                                    toga.Label(title, font_weight=BOLD),
                                    toga.Label(textwrap.fill(description, 62)),
                                ],
                            ),
                        ],
                    )
                )

        window = toga.Window(title="Keyboard Shortcuts", size=(560, 640))
        window.content = toga.ScrollContainer(
            horizontal=False,
            content=toga.Box(direction=COLUMN, margin=20, gap=12, children=children),
        )
        self.shortcuts_window = window
        window.show()

    ######################################################################
    # Signing in and out
    ######################################################################

    def preferences(self):
        self.show_settings()

    def on_close_settings(self, widget=None, **kwargs):
        if self.token or self.local_store is None:
            self.show_inbox()
        else:
            self.show_sign_in()

    def on_sign_in_again(self, widget=None, **kwargs):
        self.show_sign_in()

    def show_sign_in(self, message: str = ""):
        """Ask the open account to sign in to GitHub: show a code to enter on
        GitHub, and wait until it has been."""
        self.cancel_sign_in()
        login = self.login()
        intro = (
            f"Sign in to GitHub as {login} to triage their notifications."
            if login
            else "Sign in to GitHub to triage your notifications."
        )
        self.code_label = toga.Label(
            "…",
            text_align=CENTER,
            font_size=28,
            font_family="monospace",
            margin=(8, 0, 16),
        )
        self.verification_url = None
        self.open_github_button = toga.Button(
            "Open GitHub", on_press=self.on_open_verification, enabled=False
        )
        children = [
            toga.Label(
                "\n\n".join(
                    textwrap.fill(text, 56) for text in (message, intro) if text
                ),
                text_align=CENTER,
            ),
            toga.Label(
                f"Enter this code at {auth.VERIFICATION_URL.removeprefix('https://')}:",
                text_align=CENTER,
            ),
            self.code_label,
            self.open_github_button,
        ]
        if not login:
            # An account with history of its own stays on this Mac.
            children += [
                toga.Label(
                    textwrap.fill(
                        "Or triage through Buzz on another Mac that's signed in. "
                        "This Mac then doesn't need to sign in.",
                        56,
                    ),
                    text_align=CENTER,
                    margin=(28, 0, 8),
                ),
                toga.Button("Connect to Another Mac…", on_press=self.on_show_connect),
            ]
        self.main_window.content = toga.Box(
            direction=COLUMN,
            justify_content=CENTER,
            align_items=CENTER,
            children=[toga.Box(direction=COLUMN, width=380, gap=4, children=children)],
        )
        self.update_title()
        self.sign_in_task = self.loop.create_task(self.run_sign_in())

    def cancel_sign_in(self):
        if self.sign_in_task is not None:
            if self.sign_in_task is not asyncio.current_task():
                self.sign_in_task.cancel()
            self.sign_in_task = None

    def show_code(self, code: auth.DeviceCode):
        self.code_label.text = code.user_code
        self.verification_url = code.verification_uri
        self.open_github_button.enabled = True

    def on_open_verification(self, widget, **kwargs):
        if self.verification_url:
            webbrowser.open(self.verification_url)

    async def run_sign_in(self):
        while True:
            try:
                token = await self.sign_in(self.show_code)
                break
            except auth.CodeExpired:
                continue  # Show a new code.
            except auth.SignInError as exc:
                await self.main_window.dialog(
                    toga.ErrorDialog("Couldn't Sign In", str(exc))
                )

        # Check whose account it is before keeping the sign-in.
        client = self.make_client(token)
        try:
            login = await client.validate()
        except GitHubError as exc:
            await self.main_window.dialog(
                toga.ErrorDialog("Couldn't Sign In", str(exc))
            )
            self.show_sign_in()
            return
        finally:
            await client.aclose()
        # A store's history is one account's, and can't be taken apart again.
        known = self.login()
        if known and known.casefold() != login.casefold():
            self.show_sign_in(
                f"This account is {known}'s, but GitHub signed in as {login}. Sign "
                f"in as {known}, or add an account for {login} with File → Add "
                "Account…."
            )
            return
        try:
            self.save_token(token)
        except credentials.KeyringError as exc:
            await self.main_window.dialog(
                toga.ErrorDialog(
                    "Couldn't Sign In",
                    f"Couldn't save the sign-in in the Keychain: {exc}",
                )
            )
            self.show_sign_in()
            return
        self.sign_in_task = None
        self.local_store.set_meta("login", login)
        self.name_account()
        self.token = token
        self.reload()
        self.update_title()
        self.start_syncing(token)

    async def on_sign_out(self, widget=None, **kwargs):
        if not self.token:
            return
        if await self.main_window.dialog(
            toga.ConfirmDialog(
                "Sign Out of GitHub?",
                "Buzz will forget this account's GitHub sign-in and stop checking "
                "for notifications. Its labels are kept.",
            )
        ):
            self.disconnect()
            self.stop_sharing()
            self.delete_token()
            self.token = None
            self.show_sign_in()

    def sign_in_again(self, message: str):
        """The saved sign-in doesn't work any more: forget it, and ask again."""
        self.disconnect()
        self.stop_sharing()
        self.delete_token()
        self.token = None
        self.show_sign_in(message)

    ######################################################################
    # Accounts
    ######################################################################

    def name_account(self):
        """Name the open account after its GitHub login, once that's known."""
        login = self.login()
        if login and login != self.accounts.names.get(self.accounts.current):
            self.accounts.name(self.accounts.current, login)
            self.rebuild_account_commands()

    def account_name(self, account: str) -> str:
        return self.accounts.names.get(account) or "New account"

    def rebuild_account_commands(self):
        """The Switch Account menu, with the open account dimmed."""
        for command in self.account_commands:
            del self.commands[command.id]
        self.account_commands = [
            toga.Command(
                self.switch_account_action(account),
                self.account_name(account),
                group=self.accounts_group,
                order=order,
                id=f"account:{account}",
                enabled=account != self.accounts.current,
            )
            for order, account in enumerate(self.accounts.names)
        ]
        self.commands.add(*self.account_commands)

    def switch_account_action(self, account: str):
        async def switch(command, **kwargs):
            await self.switch_account(account)

        return switch

    async def on_add_account(self, command=None, **kwargs):
        await self.switch_account(self.accounts.add())

    async def switch_account(self, account: str):
        """Open another account, with its own inbox, labels and sign-in."""
        if account == self.accounts.current:
            return
        self.cancel_sign_in()
        self.disconnect()
        self.stop_sharing()
        if self.share_window in self.windows:
            self.share_window.close()
        await self.close_remote()
        if self.local_store is not None:
            self.local_store.close()
            self.local_store = None

        self.accounts.switch(account)
        if account not in self.accounts.remotes:
            self.local_store = self.open_store(account)
        self.token = self.load_token()
        self.show_new_inbox()
        await self.open_account()

    def show_new_inbox(self):
        """Start showing the open account's inbox, from its inbox view."""
        self.view, self.reading = INBOX, None
        self.chosen_filters.clear()
        self.update_filter_menu()
        self.search.value = ""
        self.sidebar_cells.clear()
        self.rebuild_account_commands()
        self.table.data = []
        self.reload()
        macos.select_row(self.sidebar, 0)
        self.update_title()

    ######################################################################
    # Syncing our own inbox with GitHub
    ######################################################################

    def connect(self, token: str):
        self.disconnect()
        self.github = self.make_client(token)
        self.sync = Sync(self.local_store, self.github)
        self.check_added_now = True
        self.sync_task = self.loop.create_task(self.sync_forever())
        self.schedule_detail_update()

    def disconnect(self):
        if self.sync_task is not None:
            self.sync_task.cancel()
            self.sync_task = None
        if self.github is not None:
            self.loop.create_task(self.github.aclose())
            self.github = None
        self.sync = None
        self.report("Not connected to GitHub.")
        self.schedule_detail_update()

    async def sync_forever(self):
        while (delay := await self.sync_once()) is not None:
            try:
                await asyncio.wait_for(self.wake.wait(), delay)
            except TimeoutError:
                pass
            self.wake.clear()

    async def sync_once(self) -> float | None:
        """Bring our inbox up to date with GitHub once. Returns how long to
        wait before the next time, or None to stop, because the sign-in doesn't
        work any more."""
        sync = self.sync
        check_added = (
            self.check_added_now
            or time.monotonic() - self.added_checked_at > ADDED_CHECK_EVERY
        )
        self.check_added_now = False
        self.report("Checking GitHub…")
        try:
            await sync.run_once(check_added)
        except AuthenticationError:
            self.loop.call_soon(
                self.sign_in_again, "GitHub no longer accepts the saved sign-in."
            )
            return None
        except WrongSignIn as exc:
            self.loop.call_soon(self.sign_in_again, str(exc))
            return None
        except GitHubError as exc:
            self.report(f"⚠️ {exc}")
            return MIN_POLL_INTERVAL
        except Exception as exc:  # noqa: BLE001 -- keep checking after a bug
            traceback.print_exc()
            self.report(f"⚠️ Couldn't update the inbox: {exc!r}")
            return MIN_POLL_INTERVAL

        if check_added:
            self.added_checked_at = time.monotonic()
        self.name_account()
        self.reload()
        self.update_title()
        self.report(f"Up to date. Last checked at {datetime.now():%H:%M}.")
        return sync.poll_interval

    ######################################################################
    # Sharing: letting Buzz on another Mac triage this account through this one
    ######################################################################

    def share_code(self) -> str:
        """The code another Mac must send, made the first time it's needed."""
        if not (code := self.load_secret(SHARE_CODE)):
            code = new_code()
            self.save_secret(SHARE_CODE, code)
        return code

    def start_sharing(self) -> str | None:
        """Let Buzz on another Mac triage this inbox through this one. Returns
        why it can't, if it can't."""
        if self.server is not None:
            return None
        api = HostAPI(
            self.local_store,
            github=lambda: self.github,
            on_change=self.on_remote_change,
            on_refresh=self.on_refresh_requested,
        )
        port = self.share_port()
        try:
            self.server = RemoteServer(api, self.share_code(), self.loop, port)
        except OSError as exc:
            return f"Couldn't share this inbox on port {port}: {exc.strerror or exc}"
        self.server.start()
        self.update_title()
        self.report(self.status_text)
        return None

    def stop_sharing(self) -> None:
        """Stop sharing. Whether to share next time is kept."""
        if self.server is not None:
            self.server.stop()
            self.server = None
            self.last_remote_change = ""
            self.update_title()
            self.report(self.status_text)

    def on_show_share(self, command=None, **kwargs):
        if self.share_window in self.windows:
            self.share_window.show()
            return
        if self.local_store is None:
            return
        code = toga.TextInput(
            value=self.share_code(), readonly=True, flex=1, font_family="monospace"
        )
        message = toga.Label("", font_size=11)

        def share(switch, **kwargs):
            if switch.value:
                if error := self.start_sharing():
                    switch.value = False
                    message.text = textwrap.fill(error, 66)
                    return
                self.local_store.set_meta("share", "on")
            else:
                self.local_store.set_meta("share", None)
                self.stop_sharing()
            message.text = ""

        def new(widget, **kwargs):
            code.value = new_code()
            self.save_secret(SHARE_CODE, code.value)
            if self.server is not None:
                self.server.code = code.value
            message.text = textwrap.fill(
                "A Mac using the old code has to connect again with this one.", 66
            )

        login = self.login()
        explanation = (
            (
                f"Buzz on another Mac can triage {login}'s notifications through "
                "this one, so it doesn't need to sign in to GitHub. Everything it "
                "does happens here, and your GitHub sign-in never leaves this Mac."
            ),
            (
                "To reach this Mac from elsewhere, point a tunnel, such as "
                f"Cloudflare's, at http://localhost:{PORT}. Then, on the other "
                "Mac, choose File → Add Account…, then Connect to Another Mac…, "
                "and enter the tunnel's address and this code:"
            ),
        )
        window = toga.Window(
            title="Share This Inbox",
            size=(520, 360),
            resizable=False,
            minimizable=False,
        )
        self.share_switch = toga.Switch(
            "Share this inbox", value=self.server is not None, on_change=share
        )
        window.content = toga.Box(
            direction=COLUMN,
            margin=16,
            gap=12,
            children=[
                self.share_switch,
                *[toga.Label(textwrap.fill(text, 68)) for text in explanation],
                code,
                message,
                toga.Box(
                    direction=ROW,
                    gap=8,
                    children=[
                        toga.Button("New Code", on_press=new),
                        toga.Box(flex=1),
                        toga.Button(
                            "Done", on_press=lambda widget, **kwargs: window.close()
                        ),
                    ],
                ),
            ],
        )
        self.share_window = window
        window.show()

    def on_remote_change(self, description: str) -> None:
        """The other Mac changed this inbox: show it, and say so."""
        self.last_remote_change = f"{description} at {datetime.now():%H:%M}"
        self.report(self.status_text)  # Adds the note about the change.
        self.reload()

    def on_refresh_requested(self) -> None:
        if self.github is not None:
            self.check_added_now = True
            self.wake.set()

    ######################################################################
    # Triaging an account through Buzz on another Mac
    ######################################################################

    def on_show_connect(self, widget=None, **kwargs):
        address = toga.TextInput(placeholder="https://buzz.example.com", flex=1)
        code = toga.PasswordInput(placeholder="Code", flex=1)
        message = toga.Label(
            textwrap.fill(
                "Buzz on the other Mac shows its code in File → Share This Inbox….",
                64,
            ),
            font_size=11,
        )
        window = toga.Window(
            title="Connect to Another Mac",
            size=(460, 200),
            resizable=False,
            minimizable=False,
        )

        async def connect(widget, **kwargs):
            connect_button.enabled = False
            message.text = "Connecting…"
            try:
                client = self.make_remote_client(address.value, code.value.strip())
            except ValueError as exc:
                message.text = textwrap.fill(str(exc), 64)
                connect_button.enabled = True
                return
            try:
                await client.state()
            except RemoteError as exc:
                await client.aclose()
                message.text = textwrap.fill(str(exc), 64)
                connect_button.enabled = True
                return
            window.close()
            await self.use_remote(client, code.value.strip())

        connect_button = toga.Button("Connect", on_press=connect)
        code.on_confirm = connect
        window.content = toga.Box(
            direction=COLUMN,
            margin=16,
            gap=8,
            children=[
                address,
                code,
                message,
                toga.Box(
                    direction=ROW,
                    gap=8,
                    children=[
                        toga.Box(flex=1),
                        toga.Button(
                            "Cancel", on_press=lambda widget, **kwargs: window.close()
                        ),
                        connect_button,
                    ],
                ),
            ],
        )
        self.connect_window = window
        window.show()
        address.focus()

    async def use_remote(self, client: RemoteClient, code: str):
        """Triage the open account through Buzz on another Mac from now on."""
        try:
            self.save_secret(CONNECT_CODE, code)
        except credentials.KeyringError as exc:
            await client.aclose()
            await self.main_window.dialog(
                toga.ErrorDialog(
                    "Couldn't Connect",
                    f"Couldn't save the code in the Keychain: {exc}",
                )
            )
            return
        self.cancel_sign_in()
        self.accounts.connect(self.accounts.current, client.url)
        if self.local_store is not None:
            self.local_store.close()
            self.local_store = None
        self.start_remote(client)

    def start_remote(self, client: RemoteClient):
        self.remote = RemoteInbox(client)
        self.remote.task = self.loop.create_task(self.remote_sync_forever(self.remote))
        self.report(f"Connecting to {client.host}…")
        self.show_new_inbox()
        self.show_inbox()
        self.schedule_detail_update()

    async def close_remote(self):
        if (remote := self.remote) is not None:
            self.remote = None
            if remote.task is not None:
                remote.task.cancel()
            await remote.client.aclose()

    async def on_disconnect_remote(self, widget=None, **kwargs):
        await self.disconnect_remote()

    async def disconnect_remote(self, message: str = ""):
        """Stop triaging through the other Mac. The account is then on this Mac
        again, and not signed in."""
        account = self.accounts.current
        await self.close_remote()
        try:
            self.delete_secret(CONNECT_CODE)
        except credentials.KeyringError:
            traceback.print_exc()
        self.accounts.disconnect(account)
        self.local_store = self.open_store(account)
        self.token = self.load_token()
        self.show_new_inbox()
        self.report("")
        self.show_sign_in(message)

    async def remote_sync_forever(self, remote: RemoteInbox) -> None:
        while True:
            await self.remote_sync_once(remote)
            try:
                await asyncio.wait_for(remote.wake.wait(), REMOTE_POLL_INTERVAL)
            except TimeoutError:
                pass
            remote.wake.clear()

    async def remote_sync_once(self, remote: RemoteInbox) -> None:
        if remote.pending:
            return  # Changes are on their way; look once they've arrived.
        try:
            snapshot, etag = await remote.client.state(remote.etag)
            if remote.pending or remote is not self.remote:
                return  # Out of date already.
            if snapshot is not None:
                remote.mirror.load(snapshot)
        except NotPaired as exc:
            self.report(
                f"⚠️ {exc} If it's made a new one, choose File → Disconnect from "
                "Other Mac, then connect again with that.",
                remote,
            )
            return
        except (RemoteError, ValueError) as exc:
            self.report(f"⚠️ {exc}", remote)
            return
        if snapshot is not None:
            remote.etag = etag
            self.name_account()
            self.reload()
            self.update_title()
        self.report(
            f"Triaging through {remote.host}. Last checked at "
            f"{datetime.now():%H:%M:%S}.",
            remote,
        )

    def send_remote(self, action: str, **params) -> None:
        """For an account on another Mac, make a change there too. (The change
        has already been made to the copy here.)"""
        if (remote := self.remote) is None:
            return
        remote.pending += 1

        async def send():
            try:
                await remote.client.action(action, **params)
            except RemoteError as exc:
                await self.main_window.dialog(
                    toga.ErrorDialog("Couldn't change the other Mac's inbox", str(exc))
                )
            finally:
                self.remote_changed(remote)

        self.loop.create_task(send())

    def remote_changed(self, remote: RemoteInbox) -> None:
        """A change to the other Mac's inbox has finished: fetch the real
        result."""
        remote.pending -= 1
        remote.etag = None
        remote.wake.set()


def main():
    return Buzz()
