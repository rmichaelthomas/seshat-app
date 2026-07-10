"""palette.py — command palette Provider.

Built on Textual's native command palette (confirmed in the §1.5 scan:
textual.command.CommandPalette, bound to ctrl+p by default via
App.COMMAND_PALETTE_BINDING, extensible through App.COMMANDS). This gives
fuzzy filtering for free instead of hand-rolling a bespoke ModalScreen.
A `:` binding on the App also opens it (action_command_palette), matching
the design's dual launch keys.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from textual.command import DiscoveryHit, Hit, Hits, Provider


@dataclass
class PaletteCommand:
    domain: str
    glyph: str
    name: str
    key: str
    action: Callable[[], None]


class DomainCommandProvider(Provider):
    """Lists every action across all six domains, fuzzy-filtered.

    This is the surface that resolves TUI-Q2's "rare/cross-domain actions"
    concern: anything ambiguous as a direct keybinding is still reachable
    here by name.
    """

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        commands: list[PaletteCommand] = getattr(self.app, "palette_commands", [])
        for cmd in commands:
            text = f"{cmd.domain}: {cmd.name}"
            score = matcher.match(text)
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(text),
                    cmd.action,
                    help=f"{cmd.domain} · key {cmd.key}",
                )

    async def discover(self) -> Hits:
        commands: list[PaletteCommand] = getattr(self.app, "palette_commands", [])
        for cmd in commands:
            yield DiscoveryHit(
                f"{cmd.domain}: {cmd.name}",
                cmd.action,
                help=f"{cmd.domain} · key {cmd.key}",
            )
