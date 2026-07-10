"""domains/invariant.py — Invariant domain: last-run summary + claim list.

Reads the last `invariant` block off of existing receipts only. Never
calls invariant_check.run_verification() on load/view (Failure Mode #5) —
that would run a fresh verification just from opening the domain.
"""

from __future__ import annotations

from textual import work
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Static, TabPane

import agreements
import receipts as receipts_module

from ..colors import COLORS
from ..data import last_invariant_block
from ..graph import ClaimNode
from ..palette import PaletteCommand
from ..widgets import EmptyState

_STATUS_STYLE = {"verified": COLORS["green"], "corrected": COLORS["cyan"], "escalated": COLORS["red"]}
_STATUS_GLYPH = {"verified": "●", "corrected": "◐", "escalated": "▲"}


class InvariantDomainMixin:
    def compose_invariant(self):
        with TabPane("◇ Invariant", id="tab-invariant"):
            yield Static(
                "[#9A8B6E]actions[/#9A8B6E]  "
                "[#C3B492][#E8AE52 b]e[/#E8AE52 b] open contract[/#C3B492]",
                id="invariant-cmdstrip", classes="cmdstrip",
            )
            yield Vertical(id="invariant-body")

    def on_mount_invariant(self) -> None:
        self._invariant_claims_cache: dict[str, dict] = {}
        self._invariant_state: str | None = None
        self._invariant_detailed_key: str | None = None

    def get_invariant_palette_commands(self) -> list[PaletteCommand]:
        return [
            PaletteCommand("invariant", "◇", "Open invariant.limn in editor", "e", self.action_invariant_edit),
        ]

    def get_invariant_help(self) -> list[tuple[str, str]]:
        return [
            ("e", "open invariant.limn in $EDITOR"),
            ("↵", "inspect claim"),
        ]

    @work(thread=True, group="invariant-refresh", exclusive=True)
    def refresh_invariant(self) -> None:
        contract = agreements.load_invariant()
        block, source_receipt = (None, None)
        if contract is not None:
            recent = receipts_module.load(limit=200)
            block, source_receipt = last_invariant_block(recent)
        self.call_from_thread(self._apply_invariant_data, contract, block, source_receipt)

    def _apply_invariant_data(self, contract: str | None, block: dict | None, source_receipt: dict | None) -> None:
        body = self.query_one("#invariant-body", Vertical)
        prior_state = getattr(self, "_invariant_state", None)

        if contract is None:
            if prior_state != "none":
                body.remove_children()
                body.mount(EmptyState(
                    "No Invariant contract governs this machine",
                    "Invariant verifies environment correctness AFTER a permitted action runs. "
                    "Failed claims are recorded on the receipt; they never block the action.",
                    [("write a starter verification contract", "seshat invariant init")],
                    glyph="◇",
                ))
                self._invariant_state = "none"
            return

        if block is None:
            if prior_state != "no-verification":
                body.remove_children()
                body.mount(EmptyState(
                    "No verification has run yet",
                    "Invariant runs automatically after permitted CLI/MCP actions once "
                    "~/.seshat/invariant.limn exists. Nothing has triggered it yet.",
                    [("run a check by hand", "seshat invariant check")],
                    glyph="◇",
                ))
                self._invariant_state = "no-verification"
            return

        self._invariant_claims_cache = {}
        self._invariant_source_receipt = source_receipt

        converged = "converged" if block.get("converged") else "did not converge"
        cycles = block.get("total_cycles", 0)
        version = block.get("harness_version") or "?"
        head_text = (
            f"[b]Last verification[/b] [#9A8B6E]· {converged} · {cycles} cycle(s)[/#9A8B6E]"
            f"          [#9A8B6E]harness v{version}[/#9A8B6E]"
        )

        if prior_state == "populated":
            self.query_one("#invariant-pane .pane-head", Static).update(head_text)
            table = self.query_one("#invariant-table", DataTable)
            table.clear()
        else:
            body.remove_children()
            head = Static(head_text, classes="pane-head")
            table = DataTable(id="invariant-table", cursor_type="row")
            table.add_column("CLAIM", width=34)
            table.add_column("STATUS", width=14)
            pane = Vertical(head, table, id="invariant-pane", classes="pane")
            detail = Vertical(Static("[#9A8B6E]select a claim[/#9A8B6E]"), id="invariant-detail", classes="detail")
            body.mount(Horizontal(pane, detail, id="invariant-work", classes="work"))
            self._invariant_state = "populated"

        for claim in block.get("claims", []):
            status = claim.get("status", "?")
            style = _STATUS_STYLE.get(status, COLORS["text_3"])
            glyph = _STATUS_GLYPH.get(status, "○")
            key = claim.get("name", "")
            table.add_row(key, f"[{style}]{glyph} {status}[/{style}]", key=key)
            self._invariant_claims_cache[key] = claim

    def handle_invariant_row_selected(self, event: DataTable.RowSelected) -> None:
        key = str(event.row_key.value)
        claim = self._invariant_claims_cache.get(key)
        detail = self.query_one("#invariant-detail", Vertical)
        if not claim:
            detail.remove_children()
            detail.mount(Static("[#9A8B6E]select a claim[/#9A8B6E]"))
            self._invariant_detailed_key = None
            return

        receipt = getattr(self, "_invariant_source_receipt", None)
        if key == self._invariant_detailed_key:
            if receipt:
                self.push_drill(ClaimNode(claim, receipt))
            return
        self._invariant_detailed_key = key

        status = claim.get("status", "?")
        style = _STATUS_STYLE.get(status, COLORS["text_3"])
        detail.remove_children()
        lines = [
            f"[b]{claim.get('name', '')}[/b]",
            f"[{style}]{_STATUS_GLYPH.get(status, '○')} {status}[/{style}]",
        ]
        if claim.get("escalation_reason"):
            lines += ["", "[#9A8B6E b]ESCALATION[/#9A8B6E b]", claim["escalation_reason"]]
        if receipt:
            env = receipt.get("environment_after", {})
            lines += ["", "[#9A8B6E b]SNAPSHOT (FROM RECEIPT)[/#9A8B6E b]",
                      f"listening_ports: {env.get('listening_ports', [])}"]
            lines += ["", "[#9A8B6E b]FROM RECEIPT[/#9A8B6E b]",
                      f"hash    [#63C6BE]{receipt.get('receipt_hash', '')[:16]}…[/#63C6BE]",
                      f"cycle   {claim.get('cycles', '?')} of {receipt.get('invariant', {}).get('total_cycles', '?')}"]
        detail.mount(Static("\n".join(lines)))
        detail.mount(Static("[#E8AE52 b]↵[/#E8AE52 b] trace to receipt", classes="cta-block"))

    def action_invariant_edit(self) -> None:
        if self._current_domain() != "invariant":
            return
        self._open_in_editor(agreements.INVARIANT_PATH)
