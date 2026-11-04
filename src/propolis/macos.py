"""macOS behaviour that Toga doesn't offer yet. On other platforms, it's a no-op."""

import sys
from collections.abc import Callable

import toga

_MACOS = sys.platform == "darwin"

if _MACOS:
    from rubicon.objc import Block, ObjCClass, ObjCInstance, objc_id
    from rubicon.objc.runtime import load_library

    load_library("AppKit")
    NSEvent = ObjCClass("NSEvent")
    NSIndexSet = ObjCClass("NSIndexSet")
    NSTableView = ObjCClass("NSTableView")
    NSWindow = ObjCClass("NSWindow")

_KEY_DOWN_MASK = 1 << 10
# Shift, Control, Option and Command. A key typed with any of these isn't a
# plain key.
_MODIFIERS = (1 << 17) | (1 << 18) | (1 << 19) | (1 << 20)
_DELETE_KEY_CODE = 51


class ListKeys:
    """Plain keys typed while a list has focus: ``on_number(n)`` for 1 to 9,
    and ``on_delete()`` for Delete.

    A key is swallowed only if its callback returns True. Keys typed into
    anything else, such as a text field or a web page, are left alone.

    This uses a local event monitor rather than menu shortcuts. A menu item
    with a shortcut that has no modifier can swallow that key from text fields.
    """

    def __init__(
        self, on_number: Callable[[int], bool], on_delete: Callable[[], bool]
    ):
        self._monitor = None
        if not _MACOS:
            return

        def handler(event):
            # Rubicon may hand over either a raw pointer or an ObjCInstance.
            event = event if isinstance(event, ObjCInstance) else ObjCInstance(event)
            match _plain_key_in_list(event):
                case None:
                    handled = False
                case "delete":
                    handled = on_delete()
                case number:
                    handled = on_number(int(number))
            # ctypes wants a plain address for a pointer result.
            return None if handled else event.ptr.value

        # The block must outlive the monitor, so keep a reference to it.
        self._block = Block(handler, objc_id, objc_id)
        self._monitor = NSEvent.addLocalMonitorForEventsMatchingMask(
            _KEY_DOWN_MASK, handler=self._block
        )

    def remove(self) -> None:
        if self._monitor is not None:
            NSEvent.removeMonitor(self._monitor)
            self._monitor = None


def _plain_key_in_list(event) -> str | None:
    """"1" to "9" or "delete", typed with no modifier while a list has focus."""
    if event.modifierFlags & _MODIFIERS:
        return None
    if event.keyCode == _DELETE_KEY_CODE:
        key = "delete"
    else:
        key = str(event.charactersIgnoringModifiers or "")
        if len(key) != 1 or key not in "123456789":
            return None
    window = event.window
    responder = window.firstResponder if window else None
    if responder is None or not responder.isKindOfClass(NSTableView):
        return None
    return key


def has_focus(table: toga.Table) -> bool:
    """Whether a Toga ``Table`` has keyboard focus."""
    if not _MACOS:
        return False
    native = table._impl.native_table
    window = native.window
    responder = window.firstResponder if window else None
    return responder is not None and responder.ptr.value == native.ptr.value


def disable_window_tabs() -> None:
    """Turn off macOS's window tabs, and the Show Tab Bar and Show All Tabs
    items it adds to the View menu."""
    if _MACOS:
        NSWindow.setAllowsAutomaticWindowTabbing(False)


def template_icon(icon: toga.Icon, description: str) -> toga.Icon:
    """Make an icon a template image, which macOS draws in the text colour:
    dark in light mode, light in dark mode, and white on a selected row.

    ``description`` is what VoiceOver reads for it.
    """
    if _MACOS:
        icon._impl.native.setTemplate(True)
        icon._impl.native.accessibilityDescription = description
    return icon


def fix_column_width(table: toga.Table, index: int, width: float) -> None:
    """Give a column of a Toga ``Table`` a fixed width, which Toga can't do yet.
    The other columns share the rest."""
    if _MACOS:
        column = table._impl.columns[index]
        column.minWidth = width
        column.maxWidth = width
        column.width = width
        column.resizingMask = 0  # NSTableColumnNoResizing


def select_row(table: toga.Table, index: int) -> None:
    """Select and reveal a row of a Toga ``Table``, which Toga can't do yet."""
    if _MACOS:
        native = table._impl.native_table
        native.selectRowIndexes(
            NSIndexSet.indexSetWithIndex(index), byExtendingSelection=False
        )
        native.scrollRowToVisible(index)
