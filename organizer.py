"""
organizer.py — folder map, recommended structure, safe migration, rollback.
"""

import re
import shutil       # used in migrate() and rollback()
import subprocess   # used in _git_verify()
from datetime import datetime, timezone  # used in migrate()
from pathlib import Path

import yaml

from registry import Registry, SESHAT_DIR

MOVES_FILE = SESHAT_DIR / "moves.log"

_YAML_OPTS = dict(default_flow_style=False, allow_unicode=True, sort_keys=False)

TAG_DIRS = {
    "infrastructure": "infrastructure",
    "games":          "games",
    "creative":       "creative",
    "civic":          "civic",
    "rag":            "infrastructure",
}


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


class Organizer:

    def __init__(self, registry: Registry):
        self.registry = registry

    # ── History ────────────────────────────────────────────────────────────

    def load_history(self, project_name: str | None = None) -> list[dict]:
        moves = self._load_moves()
        if project_name:
            moves = [m for m in moves if m["project"] == project_name]
        return list(reversed(moves))

    # ── Moves log I/O ──────────────────────────────────────────────────────

    def _load_moves(self) -> list[dict]:
        if not MOVES_FILE.exists():
            return []
        data = yaml.safe_load(MOVES_FILE.read_text()) or {}
        return data.get("moves", [])

    def _append_move(self, record: dict) -> None:
        moves = self._load_moves()
        moves.append(record)
        self._write_moves(moves)

    def _write_moves(self, moves: list[dict]) -> None:
        SESHAT_DIR.mkdir(exist_ok=True)
        MOVES_FILE.write_text(yaml.dump({"moves": moves}, **_YAML_OPTS))
