"""
registry.py — reads and writes:
  ~/.seshat/registry.yaml   project definitions (source of truth)
  ~/.seshat/state.json      ephemeral runtime state (PIDs Seshat started)
  ~/.seshat/groups.yaml     named launch groups
"""

import json
from pathlib import Path

import yaml

SESHAT_DIR    = Path.home() / ".seshat"
REGISTRY_FILE = SESHAT_DIR / "registry.yaml"
STATE_FILE    = SESHAT_DIR / "state.json"
GROUPS_FILE   = SESHAT_DIR / "groups.yaml"

_YAML_OPTS = dict(default_flow_style=False, allow_unicode=True, sort_keys=False)


class Registry:
    def __init__(self):
        SESHAT_DIR.mkdir(exist_ok=True)
        if not REGISTRY_FILE.exists():
            self._write({"services": []})
        if not STATE_FILE.exists():
            STATE_FILE.write_text("{}")

    # ── registry.yaml ──────────────────────────────────────────────────────

    def _read(self) -> dict:
        return yaml.safe_load(REGISTRY_FILE.read_text()) or {"services": []}

    def _write(self, data: dict) -> None:
        REGISTRY_FILE.write_text(yaml.dump(data, **_YAML_OPTS))

    def list(self) -> list:
        return self._read().get("services", [])

    def get(self, name: str) -> dict | None:
        return next((s for s in self.list() if s["name"] == name), None)

    def add(self, project: dict) -> dict:
        data     = self._read()
        services = data.get("services", [])

        if any(s["name"] == project["name"] for s in services):
            raise ValueError(f"A project named '{project['name']}' is already registered.")

        if any(s["port"] == project["port"] for s in services):
            owner = next(s for s in services if s["port"] == project["port"])
            raise ValueError(
                f"Port {project['port']} is already assigned to '{owner['name']}'."
            )

        services.append(project)
        data["services"] = services
        self._write(data)
        return project

    def update(self, name: str, updates: dict) -> dict:
        data     = self._read()
        services = data.get("services", [])

        for i, s in enumerate(services):
            if s["name"] == name:
                new_port = updates.get("port", s["port"])
                if new_port != s["port"]:
                    conflict = next(
                        (o for o in services if o["port"] == new_port and o["name"] != name),
                        None,
                    )
                    if conflict:
                        raise ValueError(
                            f"Port {new_port} is already assigned to '{conflict['name']}'."
                        )
                services[i] = {**s, **updates}
                data["services"] = services
                self._write(data)
                return services[i]

        raise ValueError(f"Project '{name}' not found.")

    def remove(self, name: str) -> None:
        data   = self._read()
        before = data.get("services", [])
        after  = [s for s in before if s["name"] != name]
        if len(after) == len(before):
            raise ValueError(f"Project '{name}' not found.")
        data["services"] = after
        self._write(data)
        # Remove this project from any groups
        self._remove_from_groups(name)

    # ── state.json ─────────────────────────────────────────────────────────

    def get_state(self) -> dict:
        return json.loads(STATE_FILE.read_text())

    def set_pid(self, name: str, pid: int, started_by: str | None = None) -> None:
        state = self.get_state()
        entry = {"pid": pid}
        if started_by:
            entry["started_by"] = started_by
        state[name] = entry
        STATE_FILE.write_text(json.dumps(state, indent=2))

    def clear_pid(self, name: str) -> None:
        state = self.get_state()
        state.pop(name, None)
        STATE_FILE.write_text(json.dumps(state, indent=2))

    # ── groups.yaml ────────────────────────────────────────────────────────

    def _read_groups(self) -> dict:
        if not GROUPS_FILE.exists():
            return {"groups": []}
        return yaml.safe_load(GROUPS_FILE.read_text()) or {"groups": []}

    def _write_groups(self, data: dict) -> None:
        GROUPS_FILE.write_text(yaml.dump(data, **_YAML_OPTS))

    def list_groups(self) -> list:
        return self._read_groups().get("groups", [])

    def get_group(self, name: str) -> dict | None:
        return next((g for g in self.list_groups() if g["name"] == name), None)

    def add_group(self, group: dict) -> dict:
        data   = self._read_groups()
        groups = data.get("groups", [])
        if any(g["name"] == group["name"] for g in groups):
            raise ValueError(f"A group named '{group['name']}' already exists.")
        groups.append(group)
        data["groups"] = groups
        self._write_groups(data)
        return group

    def remove_group(self, name: str) -> None:
        data   = self._read_groups()
        before = data.get("groups", [])
        after  = [g for g in before if g["name"] != name]
        if len(after) == len(before):
            raise ValueError(f"Group '{name}' not found.")
        data["groups"] = after
        self._write_groups(data)

    def _remove_from_groups(self, project_name: str) -> None:
        """Remove a project from all groups (called when a project is deleted)."""
        data   = self._read_groups()
        groups = data.get("groups", [])
        changed = False
        for g in groups:
            before = g.get("projects", [])
            after  = [p for p in before if p != project_name]
            if len(after) != len(before):
                g["projects"] = after
                changed = True
        if changed:
            data["groups"] = groups
            self._write_groups(data)
