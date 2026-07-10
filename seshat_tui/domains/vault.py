"""domains/vault.py — Vault domain: key list, audit status, reveal.

§5's prose text grants only "reuse existing Vault read methods" — despite
the reference's command strip showing 'n add key' / 'i import .env', the
prompt's own conflict-resolution rule (reference governs appearance,
prompt governs behavior) means this domain does not write. Vault.set /
set_override / import_dotenv are never called here; only list_keys, get,
get_overrides, audit, summary. 'r reveal' is the one real action.
"""

from __future__ import annotations

from textual import work
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Static, TabPane

from registry import Registry
from vault import Vault

from ..colors import COLORS
from ..palette import PaletteCommand
from ..widgets import EmptyState, FilterInput, Rail

_vault = Vault()
_registry = Registry()

_AUDIT_STYLE = {"ok": COLORS["green"], "missing": COLORS["red"], "unused": COLORS["text_3"]}
_AUDIT_GLYPH = {"ok": "●", "missing": "✗", "unused": "○"}


def _audit_status(entry: dict) -> str:
    if entry.get("unused"):
        return "unused"
    if entry.get("missing_from"):
        return "missing"
    return "ok"


class VaultDomainMixin:
    def compose_vault(self):
        with TabPane("⚿ Vault", id="tab-vault"):
            yield Static(
                "[#9A8B6E]actions[/#9A8B6E]  "
                "[#F6C56E][#F6C56E b]r[/#F6C56E b] reveal[/#F6C56E]",
                id="vault-cmdstrip", classes="cmdstrip",
            )
            yield Vertical(id="vault-body")

    def on_mount_vault(self) -> None:
        self.vault_view = "all"
        self.vault_query = ""
        self.vault_revealed: set[str] = set()
        self._vault_audit_cache: dict[str, dict] = {}
        self._vault_built = False

    def get_vault_palette_commands(self) -> list[PaletteCommand]:
        return [
            PaletteCommand("vault", "⚿", "Reveal selected key's value", "r", self.action_vault_reveal),
        ]

    def get_vault_help(self) -> list[tuple[str, str]]:
        return [
            ("r", "reveal selected key's value"),
            ("/", "filter list"),
            ("↵", "inspect key"),
        ]

    @work(thread=True, group="vault-refresh", exclusive=True)
    def refresh_vault(self) -> None:
        keys = _vault.list_keys()
        projects = _registry.list()
        audit = _vault.audit(projects)
        summary = _vault.summary()
        self.call_from_thread(self._apply_vault_data, keys, audit, summary)

    def _apply_vault_data(self, keys: list, audit: list, summary: dict) -> None:
        body = self.query_one("#vault-body", Vertical)

        if not keys:
            if getattr(self, "_vault_built", False):
                body.remove_children()
                self._vault_built = False
            elif body.children:
                return
            body.mount(EmptyState(
                "Vault is empty",
                "The vault holds shared secrets (API keys, database URLs) that projects "
                "resolve at start time, encrypted at rest.",
                [("set the first key", "seshat vault set <KEY> <value>")],
                glyph="⚿",
            ))
            return

        self._vault_audit_cache = {e["key"]: e for e in audit}
        self._vault_audit_full = audit
        n_overrides = sum(1 for e in audit if e.get("overridden_by"))

        enc = "encrypted" if summary.get("encrypted") else "[#DD6E5A]unencrypted fallback[/#DD6E5A]"
        head_text = f"[b]{len(keys)} keys[/b] [#9A8B6E]· {enc}[/#9A8B6E]"
        rail_sections = [("View", [
            ("all", "All keys", str(len(keys)), ""),
            ("overrides", "Overrides", str(n_overrides), ""),
            ("audit", "Audit issues", str(sum(1 for e in audit if _audit_status(e) != "ok")), "r"),
        ])]

        if getattr(self, "_vault_built", False):
            self.query_one("#vault-pane .pane-head", Static).update(head_text)
            self.query_one("#vault-rail", Rail).build(rail_sections, getattr(self, "vault_view", "all"))
            self._render_vault_table()
            return

        body.remove_children()
        head = Static(head_text, classes="pane-head")
        table = DataTable(id="vault-table", cursor_type="row")
        table.add_columns("key", "audit", "used by")

        rail = Rail(on_change=self._set_vault_view, id="vault-rail")
        pane = Vertical(head, FilterInput(on_change=self._set_vault_query, id="vault-filter-input"), table,
                         id="vault-pane", classes="pane")
        detail = Vertical(Static("[#9A8B6E]select a key[/#9A8B6E]"), id="vault-detail", classes="detail")
        body.mount(Horizontal(rail, pane, detail, id="vault-work", classes="work"))
        self._vault_built = True

        rail.call_after_refresh(rail.build, rail_sections, getattr(self, "vault_view", "all"))
        self.call_after_refresh(self._render_vault_table)

    def _render_vault_table(self) -> None:
        try:
            table = self.query_one("#vault-table", DataTable)
        except Exception:
            return
        table.clear()
        view = getattr(self, "vault_view", "all")
        query = getattr(self, "vault_query", "")
        for entry in getattr(self, "_vault_audit_full", []):
            status = _audit_status(entry)
            if view == "overrides" and not entry.get("overridden_by"):
                continue
            if view == "audit" and status == "ok":
                continue
            if query and query.lower() not in entry["key"].lower():
                continue
            style = _AUDIT_STYLE[status]
            glyph = _AUDIT_GLYPH[status]
            used_by = ", ".join(entry.get("declared_by", [])) or "—"
            table.add_row(
                entry["key"],
                f"[{style}]{glyph} {status}[/{style}]",
                used_by,
                key=entry["key"],
            )

    def _set_vault_view(self, key: str) -> None:
        self.vault_view = key
        self._render_vault_table()

    def _set_vault_query(self, query: str) -> None:
        self.vault_query = query
        self._render_vault_table()

    def handle_vault_row_selected(self, event: DataTable.RowSelected) -> None:
        self.vault_selected = str(event.row_key.value)
        self._render_vault_detail(self.vault_selected)

    def _render_vault_detail(self, key: str) -> None:
        entry = self._vault_audit_cache.get(key)
        detail = self.query_one("#vault-detail", Vertical)
        detail.remove_children()
        if not entry:
            detail.mount(Static("[#9A8B6E]select a key[/#9A8B6E]"))
            return
        status = _audit_status(entry)
        style = _AUDIT_STYLE[status]
        revealed = key in self.vault_revealed
        value_line = _vault.get(key) or "" if revealed else "•" * 16
        lines = [
            f"[b]{key}[/b]",
            f"[{style}]{_AUDIT_GLYPH[status]} {status}[/{style}]  [#9A8B6E]· shared[/#9A8B6E]",
            "",
            "[#9A8B6E b]VALUE[/#9A8B6E b]",
            value_line,
            "",
            "[#9A8B6E b]USED BY[/#9A8B6E b]",
        ]
        for proj in entry.get("declared_by", []):
            tag = "[#E8AE52]override[/#E8AE52]" if proj in entry.get("overridden_by", []) else "shared"
            lines.append(f"{proj}   {tag}")
        if not entry.get("declared_by"):
            lines.append("[#9A8B6E]—[/#9A8B6E]")
        detail.mount(Static("\n".join(lines)))
        detail.mount(Static("[#E8AE52 b]r[/#E8AE52 b] reveal value", classes="cta-block"))

    def action_vault_reveal(self) -> None:
        if self._current_domain() != "vault" or not getattr(self, "vault_selected", None):
            return
        key = self.vault_selected
        if key in self.vault_revealed:
            self.vault_revealed.discard(key)
        else:
            self.vault_revealed.add(key)
        self._render_vault_detail(key)
