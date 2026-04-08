"""
router.py — local hostname management via Caddy + dnsmasq.
"""

import re
import subprocess
from pathlib import Path

import yaml

from registry import Registry, SESHAT_DIR

HOSTNAMES_FILE = SESHAT_DIR / "hostnames.yaml"
CADDYFILE      = SESHAT_DIR / "Caddyfile"

_YAML_OPTS = dict(default_flow_style=False, allow_unicode=True, sort_keys=True)


def _slugify(name: str) -> str:
    """Convert a project name to a .seshat hostname slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"{slug}.seshat"


class Router:

    def __init__(self, registry: Registry):
        self.registry = registry

    # ── Hostnames I/O ──────────────────────────────────────────────────────

    def _load_hostnames(self) -> dict[str, str]:
        """Return saved {project_name: hostname} mapping."""
        if not HOSTNAMES_FILE.exists():
            return {}
        data = yaml.safe_load(HOSTNAMES_FILE.read_text()) or {}
        return data.get("hostnames", {})

    def _write_hostnames(self, hostnames: dict[str, str]) -> None:
        """Persist hostname mapping to disk."""
        SESHAT_DIR.mkdir(exist_ok=True)
        HOSTNAMES_FILE.write_text(
            yaml.dump({"hostnames": hostnames}, **_YAML_OPTS)
        )
