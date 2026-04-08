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

    # ── Folder map ─────────────────────────────────────────────────────────────

    def folder_map(self) -> list[dict]:
        projects = self.registry.list()
        groups: dict[str, list] = {}
        for p in projects:
            path   = Path(p["directory"]).expanduser().resolve()
            parent = str(path.parent)
            groups.setdefault(parent, []).append({
                "name":      p["name"],
                "port":      p["port"],
                "tags":      p.get("tags", []),
                "directory": str(path),
            })
        return [{"parent": k, "projects": v} for k, v in sorted(groups.items())]

    # ── Recommendations ────────────────────────────────────────────────────────

    def recommend_structure(self, root: str = "~/Projects") -> list[dict]:
        root_path = Path(root).expanduser()
        result = []
        for p in self.registry.list():
            current  = str(Path(p["directory"]).expanduser().resolve())
            slug     = _slugify(p["name"])
            subdir   = next(
                (TAG_DIRS[t] for t in (p.get("tags") or []) if t in TAG_DIRS),
                "misc",
            )
            suggested = str(root_path / subdir / slug)
            result.append({
                "project_name": p["name"],
                "current":      current,
                "suggested":    suggested,
                "slug":         slug,
            })
        return result

    # ── Migration ──────────────────────────────────────────────────────────────

    def migrate(self, project_name: str, destination: str, force: bool = False) -> dict:
        project = self.registry.get(project_name)
        if not project:
            raise ValueError(f"Project '{project_name}' not found.")

        state = self.registry.get_state()
        if project_name in state and not force:
            return {"warning": "project_running"}

        current = Path(project["directory"]).expanduser().resolve()
        dest    = Path(destination).expanduser().resolve()

        if dest.exists():
            raise ValueError(f"Destination already exists: {dest}")
        if not dest.parent.exists():
            raise ValueError(f"Parent directory does not exist: {dest.parent}")

        shutil.move(str(current), str(dest))

        self.registry.update(project_name, {"directory": str(dest)})

        git_result    = self._git_verify(str(dest))
        health_result = self._health_check({**project, "directory": str(dest)})

        now     = datetime.now(timezone.utc)
        move_id = f"{now.strftime('%Y%m%d-%H%M%S')}-{_slugify(project_name)}"
        self._append_move({
            "id":              move_id,
            "project":         project_name,
            "from":            str(current),
            "to":              str(dest),
            "timestamp":       now.isoformat(),
            "git_verified":    git_result["ok"],
            "health_verified": health_result["ok"],
            "rolled_back":     False,
        })

        return {
            "ok":            True,
            "move_id":       move_id,
            "git_result":    git_result,
            "health_result": health_result,
        }

    # ── Git verification (stub — replaced in Task 5) ───────────────────────────

    def _git_verify(self, directory: str) -> dict:
        return {"ok": True}

    # ── Health check (stub — replaced in Task 6) ──────────────────────────────

    def _health_check(self, project: dict) -> dict:
        return {"ok": True, "check_type": "unknown"}
