"""domains/agreements.py — Agreements domain: rule list + revocations overlay + dry-run.

Read-only. Verb/window come from agreements._verb_of / _temporal_window
(never re-derived here). The dry-run evaluates via agreements.check_action()
and persists nothing.
"""

from __future__ import annotations

import os
import subprocess

from textual import work
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Static, TabPane

import agreements

from ..colors import COLORS
from ..data import denial_count_for_rule, summarize_agreement_rules
from ..graph import RuleNode
from ..palette import PaletteCommand
from ..screens import DryRunModal
from ..widgets import EmptyState, FilterInput, Rail


def _condition_text(canonical: str, verb: str | None) -> str:
    if not verb:
        return canonical
    marker = f"{verb} "
    return canonical.split(marker, 1)[-1] if marker in canonical else canonical


class AgreementsDomainMixin:
    def compose_agreements(self):
        with TabPane("≡ Agreements", id="tab-agreements"):
            yield Static(
                "[#9A8B6E]actions[/#9A8B6E]  "
                "[#F6C56E][#F6C56E b]c[/#F6C56E b] dry-run check[/#F6C56E]  "
                "[#C3B492][#E8AE52 b]e[/#E8AE52 b] open in editor[/#C3B492]",
                id="agreements-cmdstrip", classes="cmdstrip",
            )
            yield Vertical(id="agreements-body")

    def on_mount_agreements(self) -> None:
        self.agreements_view = "all"
        self._agreements_rules_cache: dict[str, dict] = {}
        self._agreements_built = False
        self._agreements_detailed_key: str | None = None

    def get_agreements_palette_commands(self) -> list[PaletteCommand]:
        return [
            PaletteCommand("agreements", "≡", "Dry-run check an action", "c", self.action_agreements_dryrun),
            PaletteCommand("agreements", "≡", "Open agreement.limn in editor", "e", self.action_agreements_edit),
        ]

    def get_agreements_help(self) -> list[tuple[str, str]]:
        return [
            ("c", "dry-run check (actor / action / scope)"),
            ("e", "open agreement.limn in $EDITOR"),
            ("↵", "inspect rule"),
        ]

    @work(thread=True, group="agreements-refresh", exclusive=True)
    def refresh_agreements(self) -> None:
        text = agreements.load_agreement()
        revocations_text = agreements.load_revocations()
        recent = None
        if text is not None:
            import receipts as receipts_module
            recent = receipts_module.load(limit=200)
        self.call_from_thread(self._apply_agreements_data, text, revocations_text, recent)

    def _apply_agreements_data(self, text: str | None, revocations_text: str | None, recent: list | None) -> None:
        body = self.query_one("#agreements-body", Vertical)

        if text is None:
            if getattr(self, "_agreements_built", False):
                body.remove_children()
                self._agreements_built = False
            elif body.children:
                return  # empty state already showing
            body.mount(EmptyState(
                "No Agreement governs this machine",
                "Agents are acting without deny-by-default enforcement. An Agreement defines "
                "what each actor may and may not do.",
                [
                    ("initialize a starter Agreement", "seshat agreement init"),
                    ("or install an existing file", "seshat agreement install <path>"),
                ],
                glyph="≡",
            ))
            return

        rules = summarize_agreement_rules(text)
        revocation_rules = summarize_agreement_rules(revocations_text) if revocations_text else []
        self._agreements_rules_all = rules
        self._agreements_rules_revocations = revocation_rules
        self._agreements_recent = recent or []

        deny_default = " · deny-by-default"
        overlay = f" · {len(revocation_rules)} revocations overlay" if revocation_rules else ""
        head_text = f"[b]{len(rules)} rules[/b][#9A8B6E]{deny_default}{overlay}[/#9A8B6E]"
        rail_sections = [("View", [
            ("all", "All rules", str(len(rules) + len(revocation_rules)), ""),
            ("rules", "Agreement rules", str(len(rules)), ""),
            ("revocations", "Revocations", str(len(revocation_rules)), "r" if revocation_rules else ""),
        ])]

        if getattr(self, "_agreements_built", False):
            self.query_one("#agreements-pane .pane-head", Static).update(head_text)
            self.query_one("#agreements-rail", Rail).build(rail_sections, getattr(self, "agreements_view", "all"))
            self._render_agreements_table()
            return

        body.remove_children()
        head = Static(head_text, classes="pane-head")
        table = DataTable(id="agreements-table", cursor_type="row")
        table.add_column("#", width=3)
        table.add_column("VERB", width=7)
        table.add_column("CONDITION", width=24)
        table.add_column("WINDOW", width=9)

        rail = Rail(on_change=self._set_agreements_view, id="agreements-rail")
        pane = Vertical(head, table, id="agreements-pane", classes="pane")
        detail = Vertical(Static("[#9A8B6E]select a rule[/#9A8B6E]"), id="agreements-detail", classes="detail")
        # Children are passed to the containers' constructors above (not
        # mounted after the fact) so they exist before the Horizontal below
        # is attached — Rail.build() below still needs a deferred call,
        # since it mounts its own children dynamically from data.
        body.mount(Horizontal(rail, pane, detail, id="agreements-work", classes="work"))
        self._agreements_built = True

        rail.call_after_refresh(rail.build, rail_sections, getattr(self, "agreements_view", "all"))
        self.call_after_refresh(self._render_agreements_table)

    def _render_agreements_table(self) -> None:
        try:
            table = self.query_one("#agreements-table", DataTable)
        except Exception:
            return
        table.clear()
        self._agreements_rules_cache = {}

        view = getattr(self, "agreements_view", "all")
        rules = getattr(self, "_agreements_rules_all", [])
        revocations_rules = getattr(self, "_agreements_rules_revocations", [])

        rows: list[tuple[str, dict, bool]] = []
        if view in ("all", "rules"):
            for idx, r in enumerate(rules):
                rows.append((str(r.get("line", idx + 1)), r, False))
        if view in ("all", "revocations"):
            for idx, r in enumerate(revocations_rules):
                rows.append((f"R{idx + 1}", r, True))

        for row_id, rule, is_revocation in rows:
            if "error" in rule:
                table.add_row(row_id, "[#DD6E5A]error[/#DD6E5A]", rule["error"], "", key=row_id)
                continue
            verb = rule["verb"] or "?"
            verb_style = f"{COLORS['green']} b" if verb == "permit" else f"{COLORS['red']} b"
            win = rule["window"]
            win_style = COLORS["green"] if win == "active" else COLORS["text_3"]
            table.add_row(
                f"[#DD6E5A]{row_id}[/#DD6E5A]" if is_revocation else f"[#9A8B6E]{row_id}[/#9A8B6E]",
                f"[{verb_style}]{verb}[/{verb_style}]",
                f"[#E8AE52]{_condition_text(rule['canonical'], rule['verb'])}[/#E8AE52]",
                f"[{win_style}]{win}[/{win_style}]",
                key=row_id,
            )
            self._agreements_rules_cache[row_id] = {**rule, "_revocation": is_revocation}

    def _set_agreements_view(self, key: str) -> None:
        self.agreements_view = key
        self._render_agreements_table()

    def handle_agreements_row_selected(self, event: DataTable.RowSelected) -> None:
        row_id = str(event.row_key.value)
        rule = self._agreements_rules_cache.get(row_id)
        detail = self.query_one("#agreements-detail", Vertical)
        if not rule or "error" in (rule or {}):
            detail.remove_children()
            detail.mount(Static("[#9A8B6E]select a rule[/#9A8B6E]"))
            self._agreements_detailed_key = None
            return

        if row_id == self._agreements_detailed_key:
            node = RuleNode(
                rule["canonical"], rule.get("verb"), rule.get("window", "unbounded"),
                is_revocation=rule.get("_revocation", False),
            )
            self.push_drill(node)
            return
        self._agreements_detailed_key = row_id

        detail.remove_children()
        verb = rule["verb"] or "?"
        verb_style = f"{COLORS['green']} b" if verb == "permit" else f"{COLORS['red']} b"
        lines = [
            f"[b]Rule {row_id}[/b]",
            f"[{verb_style}]{verb}[/{verb_style}]  [#9A8B6E]· {rule['window']} · "
            f"{'revocation' if rule.get('_revocation') else 'agreement'}[/#9A8B6E]",
            "",
            "[#9A8B6E b]CANONICAL[/#9A8B6E b]",
            rule["canonical"],
        ]
        if rule.get("_revocation"):
            denials = denial_count_for_rule(rule["canonical"], getattr(self, "_agreements_recent", []))
            lines += ["", "[#9A8B6E b]ENFORCEMENT[/#9A8B6E b]", f"denials   [#DD6E5A]{denials}[/#DD6E5A]"]
        detail.mount(Static("\n".join(lines)))
        detail.mount(Static(
            "[#E8AE52 b]c[/#E8AE52 b] dry-run this rule\n[#E8AE52 b]e[/#E8AE52 b] edit agreement\n"
            "[#E8AE52 b]↵[/#E8AE52 b] trace authority",
            classes="cta-block",
        ))

    # ── Actions ──────────────────────────────────────────────────────────

    def action_agreements_dryrun(self) -> None:
        if self._current_domain() != "agreements":
            return
        self.push_screen(DryRunModal(self._run_dryrun))

    def _run_dryrun(self, actor: str, action: str, scope: str | None):
        decision = agreements.check_action(actor, action, scope)
        scope_flag = f" --scope {scope}" if scope else ""
        self._show_echo("agreements", f"seshat agreement check {action} --actor {actor}{scope_flag}", "dry-run — no receipt")
        return decision

    def action_agreements_edit(self) -> None:
        if self._current_domain() != "agreements":
            return
        self._open_in_editor(agreements.AGREEMENT_PATH)

    def _open_in_editor(self, path) -> None:
        if not path.exists():
            self.notify(f"{path} does not exist yet.", severity="warning")
            return
        editor = os.environ.get("EDITOR", "vi")
        with self.suspend():
            subprocess.run([editor, str(path)])
        self.refresh_agreements()
