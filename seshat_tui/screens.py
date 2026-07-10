"""screens.py — modal overlays and the boot splash.

Modal mechanism confirmed in the §1.5 scan: Textual's ModalScreen ships a
dimmed backdrop by default (background: $background 60%), matching the
reference's overlay treatment. The boot splash is a plain Screen (no dim
needed — it IS the whole terminal) pushed on App.on_mount, auto-dismissing
via set_timer or on any keypress.
"""

from __future__ import annotations

from typing import Callable

from textual import events
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Input, ListItem, ListView, RichLog, Static

from .colors import COLORS, EMBLEM
from .graph import Edge, GovernanceGraph, GovernanceNode

# Verified figlet block wordmark (seshat_tui_FINAL_source.py WORD list) —
# my own earlier hand-drawn ▄▀ approximation didn't actually spell SESHAT
# correctly once rendered. This one is confirmed to render as SESHAT.
_BOOT_WORD_ROWS = [
    ("  ███████╗ ███████╗ ███████╗ ██╗  ██╗  █████╗  ████████╗", "#F6C56E"),
    ("  ██╔════╝ ██╔════╝ ██╔════╝ ██║  ██║ ██╔══██╗ ╚══██╔══╝", "#E8AE52"),
    ("  ███████╗ █████╗   ███████╗ ███████║ ███████║    ██║   ", "#E8AE52"),
    ("  ╚════██║ ██╔══╝   ╚════██║ ██╔══██║ ██╔══██║    ██║   ", "#A07E3E"),
    ("  ███████║ ███████╗ ███████║ ██║  ██║ ██║  ██║    ██║   ", "#5F5340"),
    ("  ╚══════╝ ╚══════╝ ╚══════╝ ╚═╝  ╚═╝ ╚═╝  ╚═╝    ╚═╝   ", "#5F5340"),
]
BOOT_WORDMARK = "\n".join(f"[{color}]{row}[/{color}]" for row, color in _BOOT_WORD_ROWS)


class BootSplashScreen(Screen):
    """Figlet wordmark + five-line system check. Skippable, ~1.2s auto-dismiss."""

    DEFAULT_CSS = """
    BootSplashScreen {
        background: $bg;
        align: center middle;
    }
    BootSplashScreen #boot-body {
        width: auto;
        height: auto;
        padding: 2 4;
    }
    """

    def __init__(self, checks: list[tuple[str, str]], emblem: str) -> None:
        super().__init__()
        self._checks = checks
        self._emblem = emblem

    def compose(self):
        lines = [BOOT_WORDMARK, ""]
        lines.append(f"     [#9A8B6E]local environmental agent harness[/#9A8B6E]  [#E8AE52]{self._emblem}[/#E8AE52]")
        lines.append("")
        lines.append("[#5F5340]" + "─" * 59 + "[/#5F5340]")
        for label, detail in self._checks:
            lines.append(f"[#74C767]✓[/#74C767] [#9A8B6E]{label:<12}[/#9A8B6E] [#5F5340]{detail}[/#5F5340]")
        lines.append("")
        lines.append("[#9A8B6E]ready[/#9A8B6E] [#E8AE52]█[/#E8AE52]")
        # No Center wrapper — the Screen's own `align: center middle` already
        # centers this direct child. Center + width:auto/height:auto on the
        # child produced a 0x0-sized widget (a real Textual sizing trap,
        # same family as the height:1+border bug — two containers each
        # deferring size computation to the other, both landing on zero).
        yield Static("\n".join(lines), id="boot-body")

    def on_mount(self) -> None:
        self.set_timer(1.2, self._dismiss_once)

    def on_key(self, event: events.Key) -> None:
        self._dismiss_once()

    def on_click(self) -> None:
        self._dismiss_once()

    def _dismiss_once(self) -> None:
        if self.is_attached:
            self.dismiss()


class HelpOverlayScreen(ModalScreen):
    """Context-sensitive `?` keybinding reference, grouped This-domain /
    Navigation / Global."""

    BINDINGS = [("escape", "dismiss_help", "Close")]

    DEFAULT_CSS = """
    HelpOverlayScreen {
        align: center top;
        padding-top: 3;
    }
    HelpOverlayScreen #help-card {
        width: 70;
        max-width: 92%;
        background: $surface-2;
        border: solid $amber-dim;
        padding: 0;
    }
    HelpOverlayScreen .hh {
        padding: 1 2;
        border-bottom: solid $edge;
    }
    HelpOverlayScreen .hbody {
        padding: 1 2;
    }
    HelpOverlayScreen .hgt {
        color: $text-3;
        text-style: bold;
        margin-top: 1;
    }
    HelpOverlayScreen .hrow {
        color: $text-2;
    }
    """

    def __init__(self, domain_label: str, groups: list[tuple[str, list[tuple[str, str]]]]) -> None:
        super().__init__()
        self._domain_label = domain_label
        self._groups = groups

    def compose(self):
        with Vertical(id="help-card"):
            yield Static(f"[b #F6C56E]Keybindings[/b #F6C56E]  [#9A8B6E]context: {self._domain_label}[/#9A8B6E]", classes="hh")
            with Vertical(classes="hbody"):
                for group_name, rows in self._groups:
                    yield Static(group_name.upper(), classes="hgt")
                    for key, desc in rows:
                        yield Static(f"[#E8AE52 b]{key:>6}[/#E8AE52 b]  [#C3B492]{desc}[/#C3B492]", classes="hrow")

    def action_dismiss_help(self) -> None:
        self.dismiss()

    def on_click(self, event: events.Click) -> None:
        if event.widget is self:
            self.dismiss()


class DryRunModal(ModalScreen):
    """Agreements dry-run input flow: actor / action / scope -> Decision.

    Read-only evaluation via agreements.check_action(); emits no receipt,
    writes nothing.
    """

    BINDINGS = [("escape", "dismiss_modal", "Close")]

    DEFAULT_CSS = """
    DryRunModal { align: center middle; }
    DryRunModal #card {
        width: 60;
        background: $surface-2;
        border: solid $amber-dim;
        padding: 1 2;
    }
    DryRunModal .lbl { color: $text-3; margin-top: 1; }
    DryRunModal #result { margin-top: 1; padding: 1; background: $surface-3; }
    """

    def __init__(self, check_fn: Callable[[str, str, str], object]) -> None:
        super().__init__()
        self._check_fn = check_fn

    def compose(self):
        with Vertical(id="card"):
            yield Static("[b #F6C56E]Dry-run check[/b #F6C56E]  [#9A8B6E](read-only — no receipt, no write)[/#9A8B6E]")
            yield Static("actor", classes="lbl")
            yield Input(value="claude-code", id="actor")
            yield Static("action", classes="lbl")
            yield Input(placeholder="e.g. start_project", id="action")
            yield Static("scope (optional)", classes="lbl")
            yield Input(id="scope")
            yield Static("[#9A8B6E]↵ in the action field runs the check · esc closes[/#9A8B6E]", classes="lbl")
            yield Static("", id="result")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        actor = self.query_one("#actor", Input).value.strip() or "claude-code"
        action = self.query_one("#action", Input).value.strip()
        scope = self.query_one("#scope", Input).value.strip() or None
        if not action:
            return
        decision = self._check_fn(actor, action, scope)
        verdict = "[#74C767 b]ALLOW[/#74C767 b]" if decision.allowed else "[#DD6E5A b]DENY[/#DD6E5A b]"
        rule_line = f"\n[#9A8B6E]rule:[/#9A8B6E] {decision.rule}" if decision.rule else ""
        self.query_one("#result", Static).update(
            f"{verdict}  mode=[b]{decision.mode}[/b]{rule_line}\n[#C3B492]{decision.reason}[/#C3B492]"
        )

    def action_dismiss_modal(self) -> None:
        self.dismiss()


class RegisterModal(ModalScreen):
    """Projects register flow: name / port / directory / start (required),
    stop (optional). Calls registry.add() directly — the same write path
    mcp_server.register_project uses — and emits a register_project receipt.
    Not gated by the Agreement: CLI/TUI actions at the terminal are a human
    action, same as start/stop already are in this file.
    """

    BINDINGS = [("escape", "dismiss_modal", "Cancel")]

    DEFAULT_CSS = """
    RegisterModal { align: center middle; }
    RegisterModal #card {
        width: 64;
        background: $surface-2;
        border: solid $amber-dim;
        padding: 1 2;
    }
    RegisterModal .lbl { color: $text-3; margin-top: 1; }
    RegisterModal #result { margin-top: 1; padding: 1; background: $surface-3; }
    """

    def __init__(self, register_fn: Callable[[dict], tuple[bool, str]]) -> None:
        super().__init__()
        self._register_fn = register_fn

    def compose(self):
        with Vertical(id="card"):
            yield Static("[b #F6C56E]Register project[/b #F6C56E]")
            yield Static("name", classes="lbl")
            yield Input(id="name")
            yield Static("port", classes="lbl")
            yield Input(id="port")
            yield Static("directory", classes="lbl")
            yield Input(id="directory")
            yield Static("start command", classes="lbl")
            yield Input(id="start")
            yield Static("stop command (optional)", classes="lbl")
            yield Input(id="stop")
            yield Static("[#9A8B6E]↵ in the stop field submits · esc cancels[/#9A8B6E]", classes="lbl")
            yield Static("", id="result")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "stop":
            return
        name = self.query_one("#name", Input).value.strip()
        port_raw = self.query_one("#port", Input).value.strip()
        directory = self.query_one("#directory", Input).value.strip()
        start = self.query_one("#start", Input).value.strip()
        stop = self.query_one("#stop", Input).value.strip()

        if not (name and port_raw and directory and start):
            self.query_one("#result", Static).update("[#DD6E5A]name, port, directory, and start are required.[/#DD6E5A]")
            return
        try:
            port = int(port_raw)
        except ValueError:
            self.query_one("#result", Static).update("[#DD6E5A]port must be a number.[/#DD6E5A]")
            return

        ok, message = self._register_fn({
            "name": name, "port": port, "directory": directory,
            "start": start, "stop": stop,
        })
        if ok:
            self.query_one("#result", Static).update(f"[#74C767]✓[/#74C767] {message}")
            self.set_timer(0.9, self.dismiss)
        else:
            self.query_one("#result", Static).update(f"[#DD6E5A]{message}[/#DD6E5A]")

    def action_dismiss_modal(self) -> None:
        self.dismiss()


class LogViewerModal(ModalScreen):
    """Read-only tail of a project's log (runner.read_log_tail)."""

    BINDINGS = [("escape", "dismiss_modal", "Close")]

    DEFAULT_CSS = """
    LogViewerModal { align: center middle; }
    LogViewerModal #card {
        width: 100; height: 32; max-width: 96%; max-height: 90%;
        background: $surface-2;
        border: solid $amber-dim;
    }
    LogViewerModal .hh { padding: 1 2; border-bottom: solid $edge; }
    """

    def __init__(self, project_name: str, lines: list[str]) -> None:
        super().__init__()
        self._project_name = project_name
        self._lines = lines

    def compose(self):
        with Vertical(id="card"):
            yield Static(f"[b #F6C56E]Logs[/b #F6C56E]  [#9A8B6E]{self._project_name} · esc closes[/#9A8B6E]", classes="hh")
            log = RichLog(highlight=False, markup=False, wrap=True)
            yield log

    def on_mount(self) -> None:
        log = self.query_one(RichLog)
        if self._lines:
            for line in self._lines:
                log.write(line)
        else:
            log.write("(no log output yet)")

    def action_dismiss_modal(self) -> None:
        self.dismiss()


class DrillScreen(Screen):
    """Drill-down authority trace: press enter on a governance object to
    walk to a related object, esc to walk back — k9s's drill-and-escape
    model applied to Seshat's authority graph (receipt -> rule ->
    revocation). See seshat_tui/graph.py for GovernanceNode/Edge.

    A plain Screen, not a ModalScreen: this is a full context switch (the
    domain strip is hidden while drilling), not a dimmed overlay. Owns its
    own path stack internally so esc semantics are uniform — pop one hop,
    not dismiss-the-whole-drill — regardless of trace depth.
    """

    BINDINGS = [Binding("escape", "back", "Back", show=False)]

    def __init__(self, graph: GovernanceGraph, node: GovernanceNode) -> None:
        super().__init__()
        self._graph = graph
        self._stack: list[GovernanceNode] = [node]
        self._edges: list[Edge] = []

    def compose(self):
        yield Static("", id="drill-breadcrumb")
        with Horizontal(id="drill-work", classes="work"):
            with Vertical(id="drill-detail", classes="pane"):
                yield Static("", classes="pane-head", id="drill-detail-head")
                yield Static("", id="drill-detail-body")
            yield ListView(id="drill-edges")
        yield Static("[#E8AE52 b]↵[/#E8AE52 b] drill · [#E8AE52 b]esc[/#E8AE52 b] back", id="drill-footer")

    def on_mount(self) -> None:
        self._render_current()
        self.query_one("#drill-edges", ListView).focus()

    def _render_current(self) -> None:
        node = self._stack[-1]
        accent = node.accent or COLORS["amber_hi"]
        self.query_one("#drill-breadcrumb", Static).update(self._render_breadcrumb())
        self.query_one("#drill-detail-head", Static).update(
            f"[{accent} b]{node.glyph} {node.title}[/{accent} b]  [#9A8B6E]· {node.node_type}[/#9A8B6E]"
        )
        self.query_one("#drill-detail-body", Static).update(node.render_detail())

        self._edges = node.edges(self._graph)
        edge_list = self.query_one("#drill-edges", ListView)
        edge_list.clear()
        if not self._edges:
            # clear() defers the actual child removal to the next refresh
            # (same reason receipts.py's chain ListView uses
            # call_after_refresh below) — appending here too would race it
            # and risk a DuplicateIds error on the next render.
            edge_list.call_after_refresh(
                edge_list.append, ListItem(Static("[#9A8B6E](no further trace — esc to go back)[/#9A8B6E]"))
            )
        else:
            items = [
                ListItem(Static(f"{edge.target.glyph} {edge.label}"), id=f"edge-{idx}")
                for idx, edge in enumerate(self._edges)
            ]
            edge_list.call_after_refresh(self._populate_edges, edge_list, items)

    def _populate_edges(self, edge_list: ListView, items: list[ListItem]) -> None:
        edge_list.extend(items)
        edge_list.index = 0

    def _render_breadcrumb(self) -> str:
        nodes = self._stack
        shown: list[GovernanceNode | None]
        shown = list(nodes) if len(nodes) <= 4 else [nodes[0], None, nodes[-2], nodes[-1]]
        parts = []
        for node in shown:
            if node is None:
                parts.append("[#5F5340]…[/#5F5340]")
                continue
            is_current = node is nodes[-1]
            color = node.accent or COLORS["amber_hi"]
            style = "b" if is_current else "dim"
            title = node.title if is_current else _elide(node.title, 20)
            parts.append(f"[{color} {style}]{node.glyph} {title}[/{color} {style}]")
        arrow = " [#5F5340]→[/#5F5340] "
        return f"[#E8AE52]{EMBLEM}[/#E8AE52] [#9A8B6E]trace[/#9A8B6E]  " + arrow.join(parts)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id != "drill-edges" or event.item is None or not event.item.id:
            return
        if not event.item.id.startswith("edge-"):
            return
        event.stop()
        idx = int(event.item.id[len("edge-") :])
        edge = self._edges[idx]
        self._stack.append(edge.target)
        self._render_current()

    def action_back(self) -> None:
        self._stack.pop()
        if not self._stack:
            self.dismiss()
        else:
            self._render_current()


def _elide(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"
