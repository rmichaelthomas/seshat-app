"""seshat_tui — Seshat interactive TUI (Textual-based, six-domain).

Launched through cli.py via _launch_tui(). Not a standalone entry point.
"""

from .app import SeshatApp

__all__ = ["SeshatApp"]
