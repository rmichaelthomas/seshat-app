"""domains/revocations.py — Revocations domain: forbid-only list + sync status.

Read-only per the resolved trust-boundary conflict (2026-07-09): `y` shows
sync status only. The TUI never calls the real sync path — that would
write ~/.seshat/revocations.limn, one of the three protected enforcement
files. Actual syncing stays CLI-only (`seshat revocations sync`).
"""

from __future__ import annotations

from textual import work
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Static, TabPane

import agreements
import receipts as receipts_module

from ..colors import COLORS
from ..data import denial_count_for_rule, summarize_agreement_rules, sync_freshness
from ..graph import RevocationNode
from ..palette import PaletteCommand
from ..widgets import EmptyState
from .agreements import _condition_text

_FRESHNESS_STYLE = {"fresh": COLORS["green"], "stale": COLORS["orange"], "never": COLORS["red"]}
_FRESHNESS_LABEL = {"fresh": "synced", "stale": "stale sync", "never": "never synced"}


class RevocationsDomainMixin:
    def compose_revocations(self):
        with TabPane("⊘ Revocations", id="tab-revocations"):
            yield Static(
                "[#9A8B6E]actions[/#9A8B6E]  "
                "[#C3B492][#E8AE52 b]y[/#E8AE52 b] sync status[/#C3B492]",
                id="revocations-cmdstrip", classes="cmdstrip",
            )
            yield Vertical(id="revocations-body")

    def on_mount_revocations(self) -> None:
        self._revocations_cache: dict[str, dict] = {}
        self._revocations_built = False
        self._revocations_detailed_key: str | None = None

    def get_revocations_palette_commands(self) -> list[PaletteCommand]:
        return [
            PaletteCommand("revocations", "⊘", "View sync status", "y", self.action_revocations_sync_info),
        ]

    def get_revocations_help(self) -> list[tuple[str, str]]:
        return [
            ("y", "view sync status (sync itself is CLI-only)"),
            ("↵", "inspect revocation"),
        ]

    @work(thread=True, group="revocations-refresh", exclusive=True)
    def refresh_revocations(self) -> None:
        text = agreements.load_revocations()
        state = agreements.revocation_state()
        recent = receipts_module.load(limit=200) if text is not None else []
        self.call_from_thread(self._apply_revocations_data, text, state, recent)

    def _apply_revocations_data(self, text: str | None, state: dict | None, recent: list) -> None:
        body = self.query_one("#revocations-body", Vertical)

        if text is None:
            if getattr(self, "_revocations_built", False):
                body.remove_children()
                self._revocations_built = False
            elif body.children:
                return
            body.mount(EmptyState(
                "No revocations synced",
                "Revocations subtract authority from the Agreement — a platform-issued "
                "kill order that always wins over a matching permit.",
                [("pull the current revocation set", "seshat revocations sync")],
                glyph="⊘",
            ))
            return

        rules = summarize_agreement_rules(text)
        freshness = sync_freshness(state.get("last_checked") if state else None)
        self._revocations_state = state
        self._revocations_recent = recent

        style = _FRESHNESS_STYLE[freshness]
        label = _FRESHNESS_LABEL[freshness]
        head_text = (
            f"[b]{len(rules)} revocations[/b] [#9A8B6E]· forbid-only[/#9A8B6E]"
            f"          [{style}]●[/{style}] {label}"
        )

        if getattr(self, "_revocations_built", False):
            self.query_one("#revocations-pane .pane-head", Static).update(head_text)
            table = self.query_one("#revocations-table", DataTable)
            table.clear()
        else:
            body.remove_children()
            head = Static(head_text, classes="pane-head")
            table = DataTable(id="revocations-table", cursor_type="row")
            table.add_column("#", width=3)
            table.add_column("VERB", width=7)
            table.add_column("CONDITION", width=24)
            table.add_column("DENIALS", width=8)
            pane = Vertical(head, table, id="revocations-pane", classes="pane")
            detail = Vertical(Static("[#9A8B6E]select a revocation[/#9A8B6E]"), id="revocations-detail", classes="detail")
            body.mount(Horizontal(pane, detail, id="revocations-work", classes="work"))
            self._revocations_built = True

        self._revocations_cache = {}
        for idx, r in enumerate(rules):
            row_id = f"R{idx + 1}"
            if "error" in r:
                table.add_row(row_id, "[#DD6E5A]error[/#DD6E5A]", r["error"], "", key=row_id)
                continue
            denials = denial_count_for_rule(r["canonical"], recent)
            table.add_row(
                f"[#DD6E5A]{row_id}[/#DD6E5A]",
                "[#DD6E5A b]forbid[/#DD6E5A b]",
                f"[#E8AE52]{_condition_text(r['canonical'], r['verb'])}[/#E8AE52]",
                f"[#DD6E5A]{denials}[/#DD6E5A]" if denials else "[#9A8B6E]0[/#9A8B6E]",
                key=row_id,
            )
            self._revocations_cache[row_id] = {**r, "_denials": denials}

    def handle_revocations_row_selected(self, event: DataTable.RowSelected) -> None:
        row_id = str(event.row_key.value)
        rule = self._revocations_cache.get(row_id)
        detail = self.query_one("#revocations-detail", Vertical)
        if not rule or "error" in rule:
            detail.remove_children()
            detail.mount(Static("[#9A8B6E]select a revocation[/#9A8B6E]"))
            self._revocations_detailed_key = None
            return

        if row_id == self._revocations_detailed_key:
            node = RevocationNode(rule["canonical"], rule.get("verb"), rule.get("window", "unbounded"))
            self.push_drill(node)
            return
        self._revocations_detailed_key = row_id

        detail.remove_children()
        state = getattr(self, "_revocations_state", None) or {}
        lines = [
            f"[b]Revocation {row_id}[/b]",
            f"[#DD6E5A b]forbid[/#DD6E5A b]  [#9A8B6E]· {rule['window']} · platform[/#9A8B6E]",
            "",
            "[#9A8B6E b]CANONICAL[/#9A8B6E b]",
            rule["canonical"],
            "",
            "[#9A8B6E b]SYNC[/#9A8B6E b]",
            f"head      [#63C6BE]{(state.get('head_hash') or '—')[:16]}…[/#63C6BE]" if state.get("head_hash") else "head      —",
            f"checked   {state.get('last_checked') or 'never'}",
            "",
            "[#9A8B6E b]ENFORCEMENT[/#9A8B6E b]",
            f"denials   [#DD6E5A]{rule.get('_denials', 0)}[/#DD6E5A]",
        ]
        detail.mount(Static("\n".join(lines)))
        detail.mount(Static("[#E8AE52 b]↵[/#E8AE52 b] trace authority", classes="cta-block"))

    def action_revocations_sync_info(self) -> None:
        if self._current_domain() != "revocations":
            return
        state = getattr(self, "_revocations_state", None)
        if not state:
            self.notify("No revocations synced yet. Run: seshat revocations sync", severity="warning")
            return
        freshness = sync_freshness(state.get("last_checked"))
        self.notify(
            f"{_FRESHNESS_LABEL[freshness]} · last checked {state.get('last_checked') or 'never'}\n"
            f"Sync itself is CLI-only: seshat revocations sync",
            title="Revocations sync status",
        )
