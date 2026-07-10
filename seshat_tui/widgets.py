"""widgets.py — small reusable widgets shared across domains.

Panels here are lifted surfaces (background tiers + padding), never boxed
cages. Selection/active state is expressed with an underline convention
(a real CSS border-bottom on these widgets — unlike DataTable rows, these
are individual Widgets with a full box model).
"""

from __future__ import annotations

from typing import Callable, Iterable

from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Input, Static

from .colors import COLORS

# Single-letter dot codes used by callers (mirrors the reference's
# .dot.g/.dot.r/.dot.o/.dot.b CSS classes) mapped to an actual colored
# bullet glyph — these are display codes, not literal text to print.
_DOT_COLORS = {
    "g": COLORS["green"],
    "r": COLORS["red"],
    "o": COLORS["orange"],
    "b": COLORS["blue"],
}


class RailItem(Static):
    """One selectable line in a Rail sidebar (status filter, group, view)."""

    class Selected(Message):
        def __init__(self, key: str) -> None:
            self.key = key
            super().__init__()

    DEFAULT_CSS = """
    RailItem {
        height: 1;
        padding: 0 1;
        color: $text-2;
    }
    RailItem:hover {
        background: $surface-2;
        color: $text;
    }
    RailItem.-on {
        color: $text;
        border-bottom: solid $amber;
    }
    """

    def __init__(self, key: str, label: str, count: str = "", dot: str = "", *, id: str | None = None):
        self.key = key
        self._label = label
        self._count = count
        self._dot = dot
        super().__init__(self._content_markup(), id=id)

    def _content_markup(self) -> str:
        dot_color = _DOT_COLORS.get(self._dot)
        dot = f"[{dot_color}]●[/{dot_color}] " if dot_color else ""
        count = f"  [dim]{self._count}[/dim]" if self._count else ""
        return f"{dot}{self._label}{count}"

    def set_selected(self, selected: bool) -> None:
        self.set_class(selected, "-on")

    def on_click(self) -> None:
        self.post_message(self.Selected(self.key))


class RailHeader(Static):
    DEFAULT_CSS = """
    RailHeader {
        height: 1;
        margin-top: 1;
        padding: 0 1;
        color: $text-3;
        text-style: bold;
    }
    RailHeader:first-of-type { margin-top: 0; }
    """

    def __init__(self, label: str) -> None:
        super().__init__(label.upper())


class Rail(Vertical):
    """A filter sidebar: grouped, clickable, single-select-per-group items.

    A lifted panel in its own right (elevation, not a divider line) —
    styled directly here rather than requiring "panel" in every caller's
    classes= string.
    """

    DEFAULT_CSS = """
    Rail {
        width: 19;
        margin-right: 1;
        padding: 1 2;
        background: #16120D;
        border: round #33291B;
    }
    """

    selected_key: reactive[str | None] = reactive(None)

    def __init__(self, on_change: Callable[[str], None] | None = None, *, id: str | None = None):
        super().__init__(id=id)
        self.on_change = on_change
        self._items: dict[str, RailItem] = {}

    def build(self, sections: Iterable[tuple[str, list[tuple[str, str, str, str]]]], default_key: str) -> None:
        """sections: [(header, [(key, label, count, dot), ...]), ...]

        Rebuilds from scratch on every call (refresh cycles call this
        repeatedly with fresh counts) — remove_children() first, or the
        old RailHeader/RailItem widgets stay mounted alongside the new
        ones and the rail visibly duplicates itself.
        """
        self.remove_children()
        self._items.clear()
        for header, rows in sections:
            self.mount(RailHeader(header))
            for key, label, count, dot in rows:
                item = RailItem(key, label, count, dot)
                self._items[key] = item
                self.mount(item)
        self.selected_key = default_key

    def watch_selected_key(self, old: str | None, new: str | None) -> None:
        for key, item in self._items.items():
            item.set_selected(key == new)

    def on_rail_item_selected(self, message: RailItem.Selected) -> None:
        message.stop()
        self.selected_key = message.key
        if self.on_change:
            self.on_change(message.key)


class EmptyState(Vertical):
    """Absence state: glyph watermark, title, description, init command cards."""

    DEFAULT_CSS = """
    EmptyState {
        align: center middle;
        background: $surface;
        padding: 2 4;
    }
    EmptyState .glyph {
        text-align: center;
        color: $amber;
        margin-bottom: 1;
    }
    EmptyState .title {
        text-align: center;
        text-style: bold;
        color: $text;
        margin-bottom: 1;
    }
    EmptyState .desc {
        text-align: center;
        color: $text-2;
        margin-bottom: 1;
        width: 60;
    }
    EmptyState .cmd-card {
        background: $bg;
        border: solid $edge;
        padding: 1 2;
        margin-bottom: 1;
        width: 60;
    }
    EmptyState .cmd-label {
        color: $text-3;
        text-style: bold;
    }
    EmptyState .cmd-line {
        color: $text;
    }
    """

    def __init__(self, title: str, desc: str, commands: list[tuple[str, str]], glyph: str = "≡"):
        super().__init__()
        self._title = title
        self._desc = desc
        self._commands = commands
        self._glyph = glyph

    def compose(self):
        yield Static(
            f"[dim]░▒▓[/dim][#E8AE52]████[/#E8AE52][dim]▓▒░[/dim]\n"
            f"[#E8AE52]█[/#E8AE52] [#F6C56E]{self._glyph}[/#F6C56E] [#E8AE52]█[/#E8AE52]\n"
            f"[dim]░▒▓[/dim][#E8AE52]████[/#E8AE52][dim]▓▒░[/dim]",
            classes="glyph",
        )
        yield Static(self._title, classes="title")
        yield Static(self._desc, classes="desc")
        for label, cmd in self._commands:
            yield Static(
                f"[b]{label}[/b]\n[#E8AE52]$[/#E8AE52] {cmd}",
                classes="cmd-card",
            )


class CliEcho(Horizontal):
    """Ephemeral CLI-echo line shown after an action with a real CLI equivalent.

    Two-part Horizontal (not one Static with manual spacing) so "c copy"
    is genuinely right-aligned rather than just padded with spaces.
    """

    DEFAULT_CSS = """
    CliEcho {
        dock: bottom;
        offset-y: -1;
        height: 1;
        padding: 0 2;
        background: #16120D;
        color: $text-2;
        display: none;
    }
    CliEcho.-visible { display: block; }
    CliEcho #echo-text { width: 1fr; }
    CliEcho #echo-copy { width: auto; color: $text-3; }
    """

    def compose(self):
        yield Static("", id="echo-text")
        yield Static("", id="echo-copy")

    def show(self, command: str, note: str = "") -> None:
        tail = f"  [dim]— {note}[/dim]" if note else ""
        self.query_one("#echo-text", Static).update(
            f"[#74C767]↪[/#74C767] [#E8AE52]$[/#E8AE52] {command}{tail}"
        )
        self.query_one("#echo-copy", Static).update("[#E8AE52 b]c[/#E8AE52 b] copy")
        self.add_class("-visible")

    def hide(self) -> None:
        self.remove_class("-visible")


class FilterInput(Input):
    """Substring filter box, toggled by `/`."""

    DEFAULT_CSS = """
    FilterInput {
        display: none;
        height: 1;
        border: none;
        background: $surface-3;
        color: $text;
    }
    FilterInput.-visible { display: block; }
    """

    def __init__(self, on_change: Callable[[str], None] | None = None, *, id: str | None = None):
        super().__init__(placeholder="filter…", id=id)
        self._on_change = on_change

    def on_input_changed(self, message: Input.Changed) -> None:
        if self._on_change:
            self._on_change(message.value)
