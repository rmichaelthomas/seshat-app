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
