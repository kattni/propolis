"""Small AppKit touches that Toga doesn't expose yet.

Each one that relies on Toga's internals fails quietly: if they change, the
app keeps working, but looks plainer, opens links to other sites in its web
view rather than the browser, or goes without the toolbar's menus.
"""

from __future__ import annotations

import contextlib
import functools
import math
import traceback
from collections.abc import Callable, Sequence
from ctypes import c_void_p
from dataclasses import dataclass

import toga
from rubicon.objc import (
    SEL,
    Block,
    NSInteger,
    NSMakeRect,
    NSMutableArray,
    NSMutableDictionary,
    NSObject,
    NSPoint,
    NSRect,
    ObjCBlock,
    ObjCClass,
    ObjCInstance,
    objc_const,
    objc_id,
    objc_method,
    objc_property,
    send_super,
)
from rubicon.objc.runtime import load_library

NSIndexSet = ObjCClass("NSIndexSet")

# NSTableViewColumnAutoresizingStyle.uniformColumnAutoresizingStyle, and
# NSTableColumnResizingOptions.
_UNIFORM = 1
_AUTORESIZING, _USER_RESIZING = 1, 2
# WKNavigationActionPolicy and WKNavigationType values.
_CANCEL, _ALLOW = 0, 1
_LINK_ACTIVATED = 0
# NSBackgroundStyle.emphasized: a cell in a selected row of a focused table.
_EMPHASIZED = 1
# NSFontWeightMedium
_MEDIUM = 0.23
# NSEventMaskKeyDown, the modifier flags (Shift, Control, Option, Command),
# and the key codes of Delete and the up and down arrows.
_KEY_DOWN_MASK = 1 << 10
_SHIFT, _COMMAND = 1 << 17, 1 << 20
_MODIFIERS = _SHIFT | (1 << 18) | (1 << 19) | _COMMAND
_DELETE_KEY = 51
_ARROW_KEYS = {126: -1, 125: 1}
# NSControlStateValue: off, on, and mixed (for "some of the selection").
_STATES = {False: 0, True: 1, None: -1}

#: The colours labels are stored with, and the system colours to draw them
#: in. System colours adapt to dark mode and to Increase Contrast.
LABEL_COLORS = {
    "🔵": "systemBlueColor",
    "🟢": "systemGreenColor",
    "🟣": "systemPurpleColor",
    "🟠": "systemOrangeColor",
    "🔴": "systemRedColor",
    "🟡": "systemYellowColor",
    "🟤": "systemBrownColor",
    "⚫": "labelColor",
    "⚪": "systemGrayColor",
}


def _native(table: toga.Table):
    return table._impl.native_table


def select_row(table: toga.Table, index: int) -> None:
    """Select a row; Toga tables can't set their selection yet."""
    with contextlib.suppress(Exception):
        _native(table).selectRowIndexes(
            NSIndexSet.indexSetWithIndex(index), byExtendingSelection=False
        )


def copy_to_clipboard(text: str) -> None:
    """Toga has no clipboard API yet."""
    # Looked up here: AppKit is only loaded once the app has started.
    pasteboard = ObjCClass("NSPasteboard").generalPasteboard
    pasteboard.clearContents()
    pasteboard.setString(text, forType="public.utf8-plain-text")


def set_column_widths(table: toga.Table, widths: list[int | None]) -> None:
    """Fix the widths of a table's columns, except the one given as None,
    which gets the rest of the table's width.

    Toga resizes all columns by the same amount, and a table starts out with no
    width at all, so without this every column ends up the same width.
    """
    with contextlib.suppress(Exception):
        native = _native(table)
        # Only columns that autoresize take up the slack.
        native.columnAutoresizingStyle = _UNIFORM
        for column, width in zip(native.tableColumns, widths):
            if width is None:
                column.resizingMask = _AUTORESIZING | _USER_RESIZING
            else:
                column.width = width
                column.resizingMask = _USER_RESIZING


def use_as_template(icon: toga.Icon) -> None:
    """Draw an icon in the colour of the text beside it, so it follows dark
    mode, and turns white on a selected row. (Only its shape is used.)"""
    with contextlib.suppress(Exception):
        icon._impl.native.template = True


def describe_icon(icon: toga.Icon, description: str) -> None:
    """Give an icon the description VoiceOver reads for it."""
    with contextlib.suppress(Exception):
        icon._impl.native.accessibilityDescription = description


def disable_window_tabs() -> None:
    """Turn off macOS's window tabs, and the Show Tab Bar and Show All Tabs
    items they add to the View menu."""
    with contextlib.suppress(Exception):
        ObjCClass("NSWindow").setAllowsAutomaticWindowTabbing(False)


def make_sidebar(table: toga.Table, widths: list[int | None]) -> None:
    """Make a table look more like a sidebar.

    (Don't set NSTableViewStyleSourceList here: Toga's table then gets asked
    for rows that don't exist, and the app crashes.)
    """
    with contextlib.suppress(Exception):
        _native(table).usesAlternatingRowBackgroundColors = False
    set_column_widths(table, widths)


def open_links_elsewhere(
    web_view: toga.WebView,
    stays: Callable[[str], bool],
    open_elsewhere: Callable[[str], None],
) -> None:
    """Pass the links someone follows in ``web_view`` to ``open_elsewhere``,
    unless ``stays`` says they belong in it. Links that ask for a new window
    go there too, since a web view can't open one.

    Everything else stays in the web view, including redirects (such as
    signing in through another site) and embedded frames.

    (Toga's ``on_navigation_starting`` can't do this: on macOS, allowing a
    navigation also starts it again as a plain GET in the main frame, which
    breaks form posts, such as signing in, and pulls embedded frames out
    into the whole page.)
    """
    with contextlib.suppress(Exception):
        native = web_view._impl.native
        policy = _navigation_policy_class().alloc().init()
        policy.toga_delegate = native.navigationDelegate
        policy.stays = stays
        policy.open_elsewhere = open_elsewhere
        native.navigationDelegate = policy
        # A web view doesn't keep its delegate alive.
        web_view._buzz_navigation_policy = policy


@functools.cache
def _navigation_policy_class():
    # Made the first time it's needed, so a problem making it fails quietly.
    class BuzzNavigationPolicy(NSObject):
        toga_delegate = objc_property(weak=True)
        stays = objc_property(object)
        open_elsewhere = objc_property(object)

        @objc_method
        def webView_decidePolicyForNavigationAction_decisionHandler_(
            self, web_view, action, decision_handler
        ) -> None:
            allow = True
            try:
                url = str(action.request.URL)
                frame = action.targetFrame
                if url.startswith(("http:", "https:", "mailto:")) and (
                    frame is None
                    or (
                        frame.isMainFrame
                        and action.navigationType == _LINK_ACTIVATED
                        and not self.stays(url)
                    )
                ):
                    allow = False
                    self.open_elsewhere(url)
            finally:
                # WebKit raises an exception if this is never called.
                ObjCBlock(decision_handler, None, NSInteger)(
                    _ALLOW if allow else _CANCEL
                )

        @objc_method
        def webView_didFinishNavigation_(self, web_view, navigation) -> None:
            # Toga's own delegate tells the WebView a page has loaded.
            if self.toga_delegate is not None:
                self.toga_delegate.webView_didFinishNavigation_(web_view, navigation)

    return BuzzNavigationPolicy


def go_back(web_view: toga.WebView) -> None:
    with contextlib.suppress(Exception):
        web_view._impl.native.goBack()


def go_forward(web_view: toga.WebView) -> None:
    with contextlib.suppress(Exception):
        web_view._impl.native.goForward()


def reload_page(web_view: toga.WebView) -> None:
    with contextlib.suppress(Exception):
        web_view._impl.native.reload()


def watch_navigation(
    web_view: toga.WebView, on_change: Callable[[str, bool, bool], None]
) -> None:
    """Call ``on_change(url, can_go_back, can_go_forward)`` whenever the page's
    address, or where Back and Forward can go, changes. That includes pages
    that change their address without loading a new page, as GitHub's do.
    ``url`` is "" while there's no page."""
    with contextlib.suppress(Exception):
        native = web_view._impl.native
        observer = _navigation_observer_class().alloc().init()
        observer.on_change = on_change
        # The address can change before the history does, so look again when
        # loading finishes, or the title changes (GitHub's pages change it as
        # they change their address).
        for key in ("URL", "canGoBack", "canGoForward", "loading", "title"):
            native.addObserver(observer, forKeyPath=key, options=0, context=None)
        # Observing something doesn't keep the observer alive.
        web_view._buzz_navigation_observer = observer


@functools.cache
def _navigation_observer_class():
    class BuzzNavigationObserver(NSObject):
        on_change = objc_property(object)

        @objc_method
        def observeValueForKeyPath_ofObject_change_context_(
            self, key_path, web_view, change, context: c_void_p
        ) -> None:
            try:
                back = web_view.backForwardList.backItem
                self.on_change(
                    _address(web_view.URL),
                    # Toga starts a web view on a blank page; there's no
                    # going back to that.
                    back is not None and _address(back.URL) != "",
                    bool(web_view.canGoForward),
                )
            except Exception:  # noqa: BLE001 -- don't take WebKit down too
                traceback.print_exc()

    return BuzzNavigationObserver


def _address(url) -> str:
    """An NSURL as text, or "" for none, or a blank page."""
    text = "" if url is None else str(url.absoluteString)
    return "" if text == "about:blank" else text


def use_symbol(button: toga.Button, symbol: str, description: str) -> None:
    """Show an SF Symbol on a button instead of its text (which stays if
    this fails). Call it before the button is laid out, so it's sized for
    the symbol."""
    with contextlib.suppress(Exception):
        if (image := _symbol(symbol, description)) is not None:
            native = button._impl.native
            native.image = image
            native.imagePosition = 1  # NSImageOnly
            native.toolTip = description


def _symbol(name: str, description: str):
    return ObjCClass("NSImage").imageWithSystemSymbolName(
        name, accessibilityDescription=description
    )


def on_list_keys(
    table: toga.Table,
    *,
    on_number: Callable[[int], bool] | None = None,
    on_delete: Callable[[], bool] | None = None,
    on_move: Callable[[int], bool] | None = None,
) -> None:
    """Handle keys typed while ``table`` has the focus:

    - ``on_number(index)`` for 1 to 9, with no modifier keys (``index`` is 0
      for 1);
    - ``on_delete()`` for Delete, with no modifier keys;
    - ``on_move(offset)`` for ⌘↑ (-1) and ⌘↓ (1).

    Each returns whether it handled the key. If it did, the table doesn't get
    it, so a number doesn't jump to a row starting with it. Held-down numbers
    and Deletes don't repeat.

    This watches keys, rather than giving menu items these shortcuts: a menu
    item with a plain key as its shortcut takes it from text fields, and one
    with ⌘↑ or ⌘↓ takes those from text fields and web pages too.
    """
    with contextlib.suppress(Exception):
        native = _native(table)

        def handle(key) -> bool:
            modifiers = key.modifierFlags & _MODIFIERS
            if on_move and modifiers == _COMMAND and key.keyCode in _ARROW_KEYS:
                return on_move(_ARROW_KEYS[key.keyCode])
            if modifiers:
                return False
            if on_delete and key.keyCode == _DELETE_KEY:
                return key.isARepeat() or on_delete()
            chars = str(key.charactersIgnoringModifiers or "")
            if on_number and len(chars) == 1 and chars in "123456789":
                return key.isARepeat() or on_number(int(chars) - 1)
            return False

        def monitor(event: objc_id) -> objc_id:
            try:
                key = ObjCInstance(event)
                if _is_focused(native, key.window) and handle(key):
                    return None
            except Exception:  # noqa: BLE001 -- let the key through instead
                traceback.print_exc()
            # A block has to give back the pointer itself.
            return event.value

        block = Block(monitor, objc_id, objc_id)
        token = ObjCClass("NSEvent").addLocalMonitorForEventsMatchingMask(
            _KEY_DOWN_MASK, handler=block
        )
        table._buzz_key_monitor = (block, token)


def _is_focused(native, window) -> bool:
    responder = window.firstResponder if window is not None else None
    return responder is not None and responder.ptr.value == native.ptr.value


######################################################################
# Labels drawn as pills, for table cells
######################################################################


@dataclass(frozen=True)
class Pill:
    """Something for `pills()` to draw."""

    text: str
    #: A label's stored colour (one of `LABEL_COLORS`), to draw the text as a
    #: pill in that colour. Without one, the text is drawn plainly.
    color: str | None = None
    #: Draw the text as a key on a keyboard, for a shortcut. A key with no
    #: text leaves a gap the size of a key.
    key: bool = False


def pills(items: Sequence[Pill], description: str) -> toga.Widget | None:
    """A widget that draws ``items`` in a row, with "+2" in place of any that
    don't fit. ``description`` is its tooltip, and what VoiceOver reads out.
    Change them with its ``show(items, description)``. Returns None if it
    can't be made: show text instead.

    It can go in a table cell (using Toga's beta support for widgets in
    cells), or in a box, where it's as wide as what it draws.
    """
    with contextlib.suppress(Exception):
        return _pills_widget_class()(tuple(items), description)
    return None


@functools.cache
def _pills_widget_class():
    # Made the first time it's needed, so a problem making it fails quietly.
    from toga_cocoa.widgets.base import Widget as NativeWidget

    view_class = _pills_view_class()

    class PillsImpl(NativeWidget):
        def create(self):
            self.native = view_class.alloc().init()
            self.native.setAccessibilityElement(True)
            self.native.setAccessibilityRole("AXStaticText")
            self.show()
            self.add_constraints()

        def show(self):
            self.native.items = self.interface._items
            self.native.toolTip = self.interface._description
            self.native.setAccessibilityLabel(self.interface._description)
            self.native.needsDisplay = True

        def set_background_color(self, color):
            pass  # Whatever it's in draws the background.

        def rehint(self):
            # As wide as the pills, for a layout. (A table decides the size of
            # its cells itself.)
            # (Rounded up, so layout's rounding can't push the last one out.)
            width = _draw_pills(self.interface._items, 1e6, 22, draw=False)
            self.interface.intrinsic.width = math.ceil(width) + 1
            self.interface.intrinsic.height = 22

    class Pills(toga.Widget):
        def __init__(self, items: tuple[Pill, ...], description: str):
            self._items = items
            self._description = description
            super().__init__()

        def _create(self):
            return PillsImpl(interface=self)

        def show(self, items, description: str) -> None:
            """Change what's drawn."""
            self._items = tuple(items)
            self._description = description
            with contextlib.suppress(Exception):
                self._impl.show()
            self.refresh()

    return Pills


@functools.cache
def _pills_view_class():
    # A table cell view, so the table tells it when its row is selected.
    class BuzzPillsView(ObjCClass("NSTableCellView")):
        items = objc_property(object)

        @objc_method
        def setBackgroundStyle_(self, style: NSInteger) -> None:
            send_super(
                __class__, self, "setBackgroundStyle:", style, argtypes=[NSInteger]
            )
            self.needsDisplay = True

        @objc_method
        def drawRect_(self, dirty: NSRect) -> None:
            try:
                size = self.bounds.size
                _draw_pills(
                    self.items or (),
                    size.width,
                    size.height,
                    self.backgroundStyle == _EMPHASIZED,
                )
            except Exception:  # noqa: BLE001 -- an empty cell beats a crash
                traceback.print_exc()

    return BuzzPillsView


def _draw_pills(
    items: Sequence[Pill],
    width: float,
    height: float,
    selected: bool = False,
    draw: bool = True,
) -> float:
    """Draw pills in a ``width`` by ``height`` space, or with ``draw=False``,
    just measure them. Returns how wide they are."""
    NSColor = ObjCClass("NSColor")
    NSFont = ObjCClass("NSFont")
    NSBezierPath = ObjCClass("NSBezierPath")
    # On a selected row's accent colour, everything is drawn in white.
    white = NSColor.alternateSelectedControlTextColor
    pill_height = min(18.0, height - 4)
    x = 1.0

    for index, item in enumerate(items):
        fill = None
        if item.key:
            color = white if selected else NSColor.secondaryLabelColor
            font = NSFont.monospacedDigitSystemFontOfSize(10, weight=_MEDIUM)
            box_height, padding, radius = pill_height - 2, 4.0, 4.0
            stroke = color.colorWithAlphaComponent(0.6)
        elif item.color:
            base = getattr(NSColor, LABEL_COLORS.get(item.color, "systemGrayColor"))
            if selected:
                color = white
                fill = white.colorWithAlphaComponent(0.2)
                stroke = white.colorWithAlphaComponent(0.6)
            else:
                # Towards black in light mode and white in dark mode, so the
                # text stands out from the tinted pill.
                color = (
                    base.blendedColorWithFraction(0.3, ofColor=NSColor.textColor)
                    or base
                )
                fill = base.colorWithAlphaComponent(0.14)
                stroke = base.colorWithAlphaComponent(0.5)
            font = NSFont.systemFontOfSize(11, weight=_MEDIUM)
            box_height, padding = pill_height, 7.0
            radius = box_height / 2
        else:
            color = white if selected else NSColor.labelColor
            font = NSFont.systemFontOfSize(11)
            box_height, padding, stroke = None, 0.0, None

        text = _attributed(item.text, font, color)
        text_size = text.size()
        box_width = text_size.width + 2 * padding
        if item.key:
            box_width = max(box_width, box_height)

        if index and x + box_width > width:
            more = _attributed(
                f"+{len(items) - index}",
                NSFont.systemFontOfSize(11),
                white if selected else NSColor.secondaryLabelColor,
            )
            if draw:
                more.drawAtPoint(NSPoint(x, (height - more.size().height) / 2))
            return x + more.size().width

        if not draw:
            pass
        elif box_height is not None and item.text:
            path = NSBezierPath.bezierPathWithRoundedRect(
                NSMakeRect(
                    x + 0.5,
                    (height - box_height) / 2 + 0.5,
                    box_width - 1,
                    box_height - 1,
                ),
                xRadius=radius,
                yRadius=radius,
            )
            if fill is not None:
                fill.setFill()
                path.fill()
            stroke.setStroke()
            path.lineWidth = 1
            path.stroke()
        if draw:
            text.drawAtPoint(
                NSPoint(
                    x + (box_width - text_size.width) / 2,
                    (height - text_size.height) / 2,
                )
            )
        x += box_width + 5
    return max(x - 5, 0.0)


def _attributed(text: str, font, color):
    font_key, color_key = _text_attribute_keys()
    attributes = NSMutableDictionary.alloc().init()
    attributes[font_key] = font
    attributes[color_key] = color
    return (
        ObjCClass("NSAttributedString")
        .alloc()
        .initWithString(text, attributes=attributes)
    )


@functools.cache
def _text_attribute_keys():
    appkit = load_library("AppKit")
    return (
        objc_const(appkit, "NSFontAttributeName"),
        objc_const(appkit, "NSForegroundColorAttributeName"),
    )


######################################################################
# Menus in the toolbar
######################################################################


@dataclass
class MenuItem:
    """An item in a `ToolbarMenu`."""

    title: str = ""
    action: Callable[[], object] | None = None
    #: A tick (True), a dash (None: some of the selection has it), or neither.
    state: bool | None = False
    enabled: bool = True
    #: A number shown at the end, such as how many threads it applies to.
    badge: int | None = None
    #: A heading for the items after it, rather than an item.
    header: bool = False
    separator: bool = False


SEPARATOR = MenuItem(separator=True)


class ToolbarMenu:
    """A toolbar button that shows a menu. The menu is made afresh, by
    calling ``items``, each time it opens.

    Put it in a window's toolbar with `add_toolbar_menus()`.
    """

    def __init__(
        self,
        identifier: str,
        label: str,
        symbol: str,
        items: Callable[[], Sequence[MenuItem]],
        tooltip: str = "",
        space_before: bool = False,
    ):
        """
        :param symbol: The name of the SF Symbol to show.
        :param space_before: Put a flexible space before it in the toolbar,
            pushing it, and anything after it, to the end.
        """
        self.identifier = identifier
        self.items = items
        self.space_before = space_before
        self.label, self.symbol, self.tooltip = label, symbol, tooltip
        self.enabled = True
        #: The NSMenuToolbarItem, once the toolbar has asked for it.
        self.native = None
        self._menu_delegate = None

    def update(
        self,
        *,
        label: str | None = None,
        symbol: str | None = None,
        tooltip: str | None = None,
        enabled: bool | None = None,
    ) -> None:
        """Change how the button looks, or whether it can be used."""
        changes = {
            name: value
            for name, value in [
                ("label", label),
                ("symbol", symbol),
                ("tooltip", tooltip),
                ("enabled", enabled),
            ]
            if value is not None and value != getattr(self, name)
        }
        for name, value in changes.items():
            setattr(self, name, value)
        if changes and self.native is not None:
            with contextlib.suppress(Exception):
                self._apply()

    def _apply(self) -> None:
        self.native.label = self.native.paletteLabel = self.label
        self.native.toolTip = self.tooltip
        self.native.image = _symbol(self.symbol, self.label)
        self.native.enabled = self.enabled

    def _make_native(self):
        item = (
            ObjCClass("NSMenuToolbarItem")
            .alloc()
            .initWithItemIdentifier(self.identifier)
        )
        item.showsIndicator = True
        item.autovalidates = False
        delegate = _menu_delegate_class().alloc().init()
        delegate.items = self.items
        menu = ObjCClass("NSMenu").alloc().initWithTitle(self.label)
        menu.autoenablesItems = False
        menu.delegate = delegate
        item.menu = menu
        # A menu doesn't keep its delegate alive.
        self._menu_delegate = delegate
        self.native = item
        self._apply()
        return item


def add_toolbar_menus(window: toga.MainWindow, menus: Sequence[ToolbarMenu]) -> None:
    """Add menus to the end of a window's toolbar, after its commands.

    Call this once Toga has made the toolbar, which is after the app's
    ``startup()``. Toga makes a new toolbar, without these, if the window's
    toolbar commands change.
    """
    with contextlib.suppress(Exception):
        native = window._impl.native
        delegate = _toolbar_delegate_class().alloc().init()
        # Toga makes the items for its commands; this adds the menus.
        delegate.toga_delegate = native.toolbar.delegate
        delegate.menus = {menu.identifier: menu for menu in menus}
        toolbar = ObjCClass("NSToolbar").alloc().initWithIdentifier("BuzzToolbar")
        toolbar.delegate = delegate
        native.toolbar = toolbar
        window._impl.native_toolbar = toolbar
        # A toolbar doesn't keep its delegate alive.
        window._buzz_toolbar_delegate = delegate
        # Changing the toolbar resizes the content, as Toga does when it does.
        if window.content is not None:
            window.content.refresh()


@functools.cache
def _toolbar_delegate_class():
    class BuzzToolbarDelegate(NSObject):
        toga_delegate = objc_property(weak=True)
        menus = objc_property(object)

        @objc_method
        def toolbarDefaultItemIdentifiers_(self, toolbar):
            identifiers = NSMutableArray.alloc().init()
            for identifier in self.toga_delegate.toolbarDefaultItemIdentifiers(toolbar):
                identifiers.addObject(identifier)
            for menu in self.menus.values():
                if menu.space_before:
                    identifiers.addObject("NSToolbarFlexibleSpaceItem")
                identifiers.addObject(menu.identifier)
            return identifiers

        @objc_method
        def toolbarAllowedItemIdentifiers_(self, toolbar):
            return self.toolbarDefaultItemIdentifiers(toolbar)

        @objc_method
        def toolbar_itemForItemIdentifier_willBeInsertedIntoToolbar_(
            self, toolbar, identifier, insert: bool
        ):
            if (menu := self.menus.get(str(identifier))) is not None:
                return menu._make_native()
            # (Toga's delegate is the window, so ``toolbar`` alone would be
            # the window's toolbar.)
            toga_delegate = self.toga_delegate
            return (
                toga_delegate.toolbar_itemForItemIdentifier_willBeInsertedIntoToolbar_(
                    toolbar, identifier, insert
                )
            )

    return BuzzToolbarDelegate


@functools.cache
def _menu_delegate_class():
    class BuzzMenuDelegate(NSObject):
        items = objc_property(object)
        actions = objc_property(object)

        @objc_method
        def menuNeedsUpdate_(self, menu) -> None:
            try:
                self.actions = _fill_menu(menu, self.items(), self)
            except Exception:  # noqa: BLE001 -- show what's there instead
                traceback.print_exc()

        @objc_method
        def performItem_(self, sender) -> None:
            try:
                self.actions[sender.tag]()
            except Exception:  # noqa: BLE001 -- keep the app running
                traceback.print_exc()

    return BuzzMenuDelegate


def _fill_menu(menu, items: Sequence[MenuItem], target) -> list[Callable]:
    """Replace what's in ``menu``. Returns each item's action, by tag."""
    NSMenuItem = ObjCClass("NSMenuItem")
    menu.removeAllItems()
    actions = []
    for item in items:
        if item.separator:
            menu.addItem(NSMenuItem.separatorItem())
            continue
        if item.header:
            menu.addItem(_section_header(item.title))
            continue
        native = NSMenuItem.alloc().initWithTitle(
            item.title,
            action=SEL("performItem:") if item.action else None,
            keyEquivalent="",
        )
        if item.action is not None:
            native.target = target
            native.tag = len(actions)
            actions.append(item.action)
        native.enabled = item.enabled and item.action is not None
        native.state = _STATES[item.state]
        if item.badge is not None:
            try:
                badge = ObjCClass("NSMenuItemBadge").alloc().initWithCount(item.badge)
                native.badge = badge
            except Exception:  # noqa: BLE001 -- before macOS 14
                native.title = f"{item.title}  ({item.badge})"
        menu.addItem(native)
    return actions


def _section_header(title: str):
    NSMenuItem = ObjCClass("NSMenuItem")
    try:
        return NSMenuItem.sectionHeaderWithTitle(title)
    except Exception:  # noqa: BLE001 -- before macOS 14
        item = NSMenuItem.alloc().initWithTitle(title, action=None, keyEquivalent="")
        item.enabled = False
        return item
