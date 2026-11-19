"""Small AppKit touches that Toga doesn't expose yet.

Each one is cosmetic, so each fails quietly: if Toga's internals change, the
app keeps working and just looks a little plainer.
"""

from __future__ import annotations

import contextlib

import toga
from rubicon.objc import ObjCClass

NSIndexSet = ObjCClass("NSIndexSet")

# NSTableViewColumnAutoresizingStyle.firstColumnOnlyAutoresizingStyle
_FIRST_COLUMN_ONLY = 5


def _native(table: toga.Table):
    return table._impl.native_table


def select_row(table: toga.Table, index: int) -> None:
    """Select a row; Toga tables can't set their selection yet."""
    with contextlib.suppress(Exception):
        _native(table).selectRowIndexes(
            NSIndexSet.indexSetWithIndex(index), byExtendingSelection=False
        )


def set_column_widths(table: toga.Table, widths: list[int]) -> None:
    """Fix the widths of every column but the first, which gets the rest.

    Toga resizes all columns by the same amount, and a table starts out with no
    width at all, so without this every column ends up the same width.
    """
    with contextlib.suppress(Exception):
        native = _native(table)
        native.columnAutoresizingStyle = _FIRST_COLUMN_ONLY
        for column, width in zip(list(native.tableColumns)[1:], widths):
            column.width = width


def make_sidebar(table: toga.Table, widths: list[int]) -> None:
    """Make a table look more like a sidebar.

    (Don't set NSTableViewStyleSourceList here: Toga's table then gets asked
    for rows that don't exist, and the app crashes.)
    """
    with contextlib.suppress(Exception):
        _native(table).usesAlternatingRowBackgroundColors = False
    set_column_widths(table, widths)
