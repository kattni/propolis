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

_KEY_DOWN_MASK = 1 << 10
# Shift, Control, Option and Command. A key typed with any of these isn't a
# plain number.
_MODIFIERS = (1 << 17) | (1 << 18) | (1 << 19) | (1 << 20)


class NumberKeys:
    """Calls ``on_number(n)`` when 1 to 9 is typed while a list has focus.

    The key is swallowed only if ``on_number`` returns True. Keys typed into
    anything else, such as a text field or a web page, are left alone.

    This uses a local event monitor rather than menu shortcuts. A menu item
    with a plain-number shortcut can swallow digits from text fields.
    """

    def __init__(self, on_number: Callable[[int], bool]):
        self._monitor = None
        if not _MACOS:
            return

        def handler(event):
            # Rubicon may hand over either a raw pointer or an ObjCInstance.
            event = event if isinstance(event, ObjCInstance) else ObjCInstance(event)
            number = _number_typed_in_list(event)
            if number is not None and on_number(number):
                return None
            # ctypes wants a plain address for a pointer result.
            return event.ptr.value

        # The block must outlive the monitor, so keep a reference to it.
        self._block = Block(handler, objc_id, objc_id)
        self._monitor = NSEvent.addLocalMonitorForEventsMatchingMask(
            _KEY_DOWN_MASK, handler=self._block
        )

    def remove(self) -> None:
        if self._monitor is not None:
            NSEvent.removeMonitor(self._monitor)
            self._monitor = None


def _number_typed_in_list(event) -> int | None:
    if event.modifierFlags & _MODIFIERS:
        return None
    characters = str(event.charactersIgnoringModifiers or "")
    if len(characters) != 1 or characters not in "123456789":
        return None
    window = event.window
    responder = window.firstResponder if window else None
    if responder is None or not responder.isKindOfClass(NSTableView):
        return None
    return int(characters)


def select_row(table: toga.Table, index: int) -> None:
    """Select and reveal a row of a Toga ``Table``, which Toga can't do yet."""
    if _MACOS:
        native = table._impl.native_table
        native.selectRowIndexes(
            NSIndexSet.indexSetWithIndex(index), byExtendingSelection=False
        )
        native.scrollRowToVisible(index)
