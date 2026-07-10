"""app.py — SeshatApp: six-domain shell, boot splash, palette, help, CLI echo.

Launched through cli.py via _launch_tui(). Not a standalone entry point.

Trust boundary: this module and its domains/ never write to
~/.seshat/agreement.limn, ~/.seshat/revocations.limn, or
~/.seshat/invariant.limn. Registry/state/vault/receipts writes are the
same operational writes the TUI has always made (start/stop/register),
unchanged from the three-tab app this replaces.
"""

from __future__ import annotations

import uuid

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Static, TabbedContent

import agreements
import receipts as receipts_module
from registry import Registry
from scanner import Scanner
from vault import Vault

from .colors import DOMAIN_ACCENTS, DOMAIN_GLYPHS, EMBLEM
from .domains.agreements import AgreementsDomainMixin
from .domains.invariant import InvariantDomainMixin
from .domains.projects import ProjectsDomainMixin
from .domains.receipts import ReceiptsDomainMixin
from .domains.revocations import RevocationsDomainMixin
from .domains.vault import VaultDomainMixin
from .palette import DomainCommandProvider
from .screens import BootSplashScreen, HelpOverlayScreen
from .widgets import CliEcho

_registry = Registry()
_vault = Vault()
_scanner = Scanner()

DOMAIN_ORDER = ["projects", "agreements", "receipts", "invariant", "revocations", "vault"]
DOMAIN_LABELS = {
    "projects": "Projects", "agreements": "Agreements", "receipts": "Receipts",
    "invariant": "Invariant", "revocations": "Revocations", "vault": "Vault",
}


class MainScreen(Screen):
    """Default screen. Overrides tab/shift+tab for domain-cycling — modal
    screens (DryRunModal, RegisterModal, ...) are unaffected and keep the
    inherited Screen default (tab moves between their Input fields)."""

    BINDINGS = [
        Binding("tab", "cycle_domain(1)", "Next domain", show=False),
        Binding("shift+tab", "cycle_domain(-1)", "Prev domain", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield from self.app.compose_body()

    def on_mount(self) -> None:
        self.app.on_main_mount()

    def action_cycle_domain(self, direction: int) -> None:
        self.app.cycle_domain(direction)


class SeshatApp(
    App,
    ProjectsDomainMixin,
    AgreementsDomainMixin,
    ReceiptsDomainMixin,
    InvariantDomainMixin,
    RevocationsDomainMixin,
    VaultDomainMixin,
):
    """Seshat — local environmental agent harness."""

    TITLE = "Seshat"
    CSS_PATH = "app.tcss"
    COMMANDS = App.COMMANDS | {DomainCommandProvider}

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("1", "jump_domain('projects')", "Projects", show=False),
        Binding("2", "jump_domain('agreements')", "Agreements", show=False),
        Binding("3", "jump_domain('receipts')", "Receipts", show=False),
        Binding("4", "jump_domain('invariant')", "Invariant", show=False),
        Binding("5", "jump_domain('revocations')", "Revocations", show=False),
        Binding("6", "jump_domain('vault')", "Vault", show=False),
        Binding("colon", "command_palette", "Palette", show=False),
        Binding("question_mark", "show_help", "Help", show=False),
        Binding("slash", "toggle_filter", "Filter", show=False),
        Binding("escape", "close_overlay", "Close", show=False),
        Binding("s", "projects_start", "Start", show=False),
        Binding("x", "projects_stop", "Stop", show=False),
        Binding("a", "projects_start_group", "Start group", show=False),
        Binding("o", "projects_logs", "Logs", show=False),
        Binding("n", "projects_register", "Register", show=False),
        Binding("c", "agreements_dryrun", "Dry-run", show=False),
        Binding("e", "edit_current", "Edit", show=False),
        Binding("f", "receipts_follow", "Follow", show=False),
        Binding("v", "receipts_verify", "Verify", show=False),
        Binding("y", "sync_current", "Sync", show=False),
        Binding("r", "vault_reveal", "Reveal", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.session_id = f"tui_{uuid.uuid4().hex[:12]}"
        self.emblem = EMBLEM
        self.palette_commands: list = []
        self._echo_timer = None

    def get_default_screen(self) -> Screen:
        return MainScreen(id="_default")

    def get_theme_variable_defaults(self) -> dict[str, str]:
        # Registers the warm-amber tokens as global $variables so every
        # widget's DEFAULT_CSS can reference them (e.g. BootSplashScreen,
        # pushed before MainScreen composes) — a top-level `$name: value;`
        # declaration in app.tcss only resolves within that same
        # stylesheet, not across other widget classes' own CSS.
        from .colors import COLORS
        return {name.replace("_", "-"): value for name, value in COLORS.items()}

    def on_mount(self) -> None:
        checks = self._collect_boot_checks()
        self.push_screen(BootSplashScreen(checks, self.emblem))

    def _collect_boot_checks(self) -> list[tuple[str, str]]:
        projects = _registry.list()
        agreement_text = agreements.load_agreement()
        agreement_detail = "no agreement" if agreement_text is None else "deny-by-default"
        try:
            from .domains.receipts import _verify_chain
            intact, total, _ = _verify_chain()
            receipts_detail = f"chain {'intact' if intact else 'BROKEN'} · {total} hashed"
        except Exception:
            receipts_detail = "unavailable"
        rev_state = agreements.revocation_state()
        rev_detail = "not synced" if rev_state is None else f"checked {rev_state.get('last_checked') or 'never'}"
        summary = _vault.summary()
        vault_detail = f"{'encrypted' if summary.get('encrypted') else 'unencrypted'} · {summary.get('key_count', 0)} keys"
        return [
            ("registry", f"{len(projects)} projects"),
            ("agreement", agreement_detail),
            ("receipts", receipts_detail),
            ("revocations", rev_detail),
            ("vault", vault_detail),
        ]

    def on_main_mount(self) -> None:
        for name in DOMAIN_ORDER:
            getattr(self, f"on_mount_{name}")()
        self.palette_commands = self._collect_palette_commands()
        self.refresh_projects()
        self.refresh_agreements()
        self.refresh_receipts()
        self.refresh_invariant()
        self.refresh_revocations()
        self.refresh_vault()
        self.set_interval(3, self.refresh_projects)

    def compose_body(self) -> ComposeResult:
        yield Static(self._render_topbar(), id="topbar")
        with TabbedContent(id="domains"):
            yield from self.compose_projects()
            yield from self.compose_agreements()
            yield from self.compose_receipts()
            yield from self.compose_invariant()
            yield from self.compose_revocations()
            yield from self.compose_vault()
        yield CliEcho(id="global-echo")
        yield Static(self._render_statusbar(), id="statusbar")

    def _render_topbar(self) -> str:
        short = self.session_id.replace("tui_", "")[:8]
        host_up = 9000 in _scanner.scan()
        host_color = "#74C767" if host_up else "#5F5340"
        return (
            f"[#E8AE52]{self.emblem}[/#E8AE52] [b #F6C56E]SESHAT[/b #F6C56E]   "
            f"[{host_color}]●[/{host_color}] [#9A8B6E]localhost:9000[/#9A8B6E]"
            + " " * 4 +
            f"[#9A8B6E]tui · {short}[/#9A8B6E]"
        )

    # Compact primary hints per domain — the subset of get_X_help() worth a
    # dedicated status-bar slot (mirrors the reference's per-domain STATUS()).
    _STATUS_PRIMARY_HINTS = {
        "projects": [("s", "start"), ("x", "stop")],
        "agreements": [("c", "check"), ("e", "editor")],
        "receipts": [("f", "follow"), ("v", "verify"), ("y", "sync")],
        "invariant": [("e", "editor")],
        "revocations": [("y", "sync")],
        "vault": [("r", "reveal")],
    }

    def _render_statusbar(self) -> str:
        domain = self._current_domain()
        hints = [("tab", "domain"), ("↑↓", "move")]
        hints += self._STATUS_PRIMARY_HINTS.get(domain, [])
        hints += [(":", "palette"), ("?", "help"), ("q", "quit")]
        text = "  ".join(f"[#E8AE52 b]{k}[/#E8AE52 b] {label}" for k, label in hints)
        return f"{text}          [#E8AE52]{self.emblem}[/#E8AE52]"

    def _refresh_statusbar(self) -> None:
        try:
            self.query_one("#statusbar", Static).update(self._render_statusbar())
        except Exception:
            pass

    # ── Domain navigation ────────────────────────────────────────────────

    def _current_domain(self) -> str:
        try:
            active = self.query_one("#domains", TabbedContent).active
        except Exception:
            return "projects"
        return (active or "tab-projects")[len("tab-"):]

    def action_jump_domain(self, name: str) -> None:
        self.query_one("#domains", TabbedContent).active = f"tab-{name}"

    def cycle_domain(self, direction: int) -> None:
        current = self._current_domain()
        idx = DOMAIN_ORDER.index(current) if current in DOMAIN_ORDER else 0
        self.action_jump_domain(DOMAIN_ORDER[(idx + direction) % len(DOMAIN_ORDER)])

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        name = event.pane.id[len("tab-"):] if event.pane.id else ""
        refresh = {
            "agreements": self.refresh_agreements,
            "invariant": self.refresh_invariant,
            "revocations": self.refresh_revocations,
            "vault": self.refresh_vault,
        }.get(name)
        if refresh:
            refresh()
        self._refresh_statusbar()

    # ── Row-selection dispatch (one handler; multiple domains use DataTable) ──

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        handler = {
            "projects-table": self.handle_projects_row_selected,
            "agreements-table": self.handle_agreements_row_selected,
            "invariant-table": self.handle_invariant_row_selected,
            "revocations-table": self.handle_revocations_row_selected,
            "vault-table": self.handle_vault_row_selected,
        }.get(event.data_table.id)
        if handler:
            handler(event)

    # ── Palette ──────────────────────────────────────────────────────────

    def _collect_palette_commands(self) -> list:
        commands = []
        for name in DOMAIN_ORDER:
            raw = getattr(self, f"get_{name}_palette_commands")()
            for cmd in raw:
                commands.append(self._wrap_palette_command(name, cmd))
        return commands

    def _wrap_palette_command(self, domain: str, cmd):
        original_action = cmd.action

        def wrapped() -> None:
            self.action_jump_domain(domain)
            original_action()

        cmd.action = wrapped
        return cmd

    # ── Help overlay ─────────────────────────────────────────────────────

    def action_show_help(self) -> None:
        domain = self._current_domain()
        glyph = DOMAIN_GLYPHS.get(domain, "")
        accent = DOMAIN_ACCENTS.get(domain)
        label_text = f"{glyph} {DOMAIN_LABELS[domain]}"
        domain_label = f"[{accent}]{label_text}[/{accent}]" if accent else label_text
        this_domain = getattr(self, f"get_{domain}_help")()
        groups = [
            ("This domain", this_domain),
            ("Navigation", [
                ("tab", "next domain"), ("1–6", "jump to domain"),
                ("↑↓", "move selection"), ("esc", "back / close"),
            ]),
            ("Global", [
                (":", "command palette"), ("/", "filter current list"),
                ("?", "this help"), ("q", "quit"),
            ]),
        ]
        self.push_screen(HelpOverlayScreen(domain_label, groups))

    def action_close_overlay(self) -> None:
        if len(self.screen_stack) > 1:
            self.pop_screen()

    # ── Filter toggle ────────────────────────────────────────────────────

    def action_toggle_filter(self) -> None:
        domain = self._current_domain()
        input_id = {"projects": "#projects-filter-input", "vault": "#vault-filter-input"}.get(domain)
        if not input_id:
            return
        try:
            box = self.query_one(input_id)
        except Exception:
            return
        box.toggle_class("-visible")
        if box.has_class("-visible"):
            box.focus()

    # ── Cross-domain key dispatch (e, y) ─────────────────────────────────

    def action_edit_current(self) -> None:
        domain = self._current_domain()
        if domain == "agreements":
            self.action_agreements_edit()
        elif domain == "invariant":
            self.action_invariant_edit()

    def action_sync_current(self) -> None:
        domain = self._current_domain()
        if domain == "receipts":
            self.action_receipts_sync()
        elif domain == "revocations":
            self.action_revocations_sync_info()

    # ── CLI echo ─────────────────────────────────────────────────────────

    def _show_echo(self, domain: str, command: str, note: str = "") -> None:
        echo = self.query_one("#global-echo", CliEcho)
        echo.show(command, note)
        if self._echo_timer:
            self._echo_timer.stop()
        self._echo_timer = self.set_timer(8, echo.hide)
