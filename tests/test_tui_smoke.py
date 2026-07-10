# tests/test_tui_smoke.py
"""Headless smoke tests for the six-domain TUI (seshat_tui/).

Uses Textual's own App.run_test() harness (no real terminal needed). These
tests read this machine's actual ~/.seshat/ state the same way a developer
running `seshat tui` would — narrower isolation than the dashboard endpoint
tests, but enough to catch the two classes of bug this rewrite kept
producing: widgets mounted before their parent is attached (MountError /
DuplicateIds), and markup/method-name mistakes that only surface once
Textual actually paints a frame (bare-word color tags, a helper method
accidentally named _render() and shadowing Widget._render()).

The write-guard test is the one that matters most going forward: it fails
loudly if any future change makes the TUI write to an enforcement file.
"""
import asyncio

import pytest

from pathlib import Path

from seshat_tui import SeshatApp

DOMAINS = ["projects", "agreements", "receipts", "invariant", "revocations", "vault"]


def test_boots_and_visits_every_domain():
    async def run():
        app = SeshatApp()
        async with app.run_test() as pilot:
            await pilot.press("space")  # dismiss boot splash
            await pilot.pause()
            for domain in DOMAINS:
                app.action_jump_domain(domain)
                await pilot.pause()
                assert app._current_domain() == domain

    asyncio.run(run())


def test_help_and_palette_overlays_open_and_close():
    async def run():
        app = SeshatApp()
        async with app.run_test() as pilot:
            await pilot.press("space")
            await pilot.pause()

            app.action_show_help()
            await pilot.pause()
            assert len(app.screen_stack) == 2
            await pilot.press("escape")
            await pilot.pause()
            assert len(app.screen_stack) == 1

            app.action_command_palette()
            await pilot.pause()
            assert len(app.screen_stack) == 2
            await pilot.press("escape")
            await pilot.pause()

    asyncio.run(run())


def test_never_writes_enforcement_files(monkeypatch):
    """Locks in Consistency Invariant #1: the TUI reads and evaluates the
    Agreement/Revocations/Invariant surfaces, it never writes them."""
    import agreements

    guarded = {agreements.AGREEMENT_PATH, agreements.REVOCATIONS_PATH, agreements.INVARIANT_PATH}
    original_write_text = Path.write_text

    def guarded_write_text(self, *args, **kwargs):
        assert self not in guarded, f"TUI wrote to a protected enforcement file: {self}"
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", guarded_write_text)

    async def run():
        app = SeshatApp()
        async with app.run_test() as pilot:
            await pilot.press("space")
            await pilot.pause()
            for domain in DOMAINS:
                app.action_jump_domain(domain)
                await pilot.pause()

    asyncio.run(run())


def test_drill_screen_pushes_renders_and_walks_back():
    from seshat_tui.graph import GovernanceGraph, ReceiptNode, RuleNode
    from seshat_tui.screens import DrillScreen

    async def run():
        app = SeshatApp()
        async with app.run_test() as pilot:
            await pilot.press("space")
            await pilot.pause()

            rule_canonical = "forbid action is stop_orphan"
            receipt = {
                "receipt_hash": "a" * 40,
                "previous_hash": None,
                "timestamp": "2026-07-10T12:00:00+00:00",
                "actor": {"session_id": "tui_abc", "agent_hint": "tui"},
                "action": "stop_orphan",
                "target": {"port": 4321},
                "result": {"status": "denied", "mode": "forbidden", "rule": rule_canonical, "reason": "no."},
                "environment_after": {"listening_ports": [4321]},
            }
            graph = GovernanceGraph(
                receipts=[receipt],
                agreement_rules=[],
                revocation_rules=[{"canonical": rule_canonical, "verb": "forbid", "window": "active"}],
            )
            app.governance_graph = graph
            app.push_screen(DrillScreen(graph, ReceiptNode(receipt)))
            await pilot.pause()
            assert len(app.screen_stack) == 2
            drill = app.screen_stack[-1]
            assert isinstance(drill, DrillScreen)
            assert "receipt" in str(drill.query_one("#drill-breadcrumb").content)

            await pilot.press("enter")
            await pilot.pause()
            assert len(drill._stack) == 2
            assert isinstance(drill._stack[-1], RuleNode)

            await pilot.press("escape")
            await pilot.pause()
            assert len(drill._stack) == 1
            assert len(app.screen_stack) == 2

            await pilot.press("escape")
            await pilot.pause()
            assert len(app.screen_stack) == 1

    asyncio.run(run())


def test_push_drill_builds_graph_lazily_and_pushes_screen():
    from seshat_tui.graph import GovernanceNode
    from seshat_tui.screens import DrillScreen

    async def run():
        app = SeshatApp()
        async with app.run_test() as pilot:
            await pilot.press("space")
            await pilot.pause()
            assert app.governance_graph is not None  # populated by on_main_mount

            leaf = GovernanceNode("receipt", "◈", None, "receipt deadbeef…")
            leaf.render_detail = lambda: "detail"
            leaf.edges = lambda graph: []
            app.push_drill(leaf)
            await pilot.pause()
            assert isinstance(app.screen_stack[-1], DrillScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert len(app.screen_stack) == 1

    asyncio.run(run())
