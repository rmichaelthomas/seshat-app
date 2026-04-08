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

    # ── Public hostname API ────────────────────────────────────────────────

    def all_hostnames(self) -> list[dict]:
        """Return [{project_name, hostname, port}] for all registered projects."""
        saved = self._load_hostnames()
        result = []
        for p in self.registry.list():
            name = p["name"]
            result.append({
                "project_name": name,
                "hostname":     saved.get(name) or _slugify(name),
                "port":         p.get("port"),
            })
        return result

    def set_hostname(self, project_name: str, hostname: str) -> dict:
        """Validate and persist a hostname override, then reload Caddy."""
        if not hostname.endswith(".seshat"):
            raise ValueError("invalid_hostname")
        subdomain = hostname[: -len(".seshat")]
        if not re.fullmatch(r"[a-z0-9]([a-z0-9\-]*[a-z0-9])?|[a-z0-9]", subdomain):
            raise ValueError("invalid_hostname")

        saved = self._load_hostnames()
        for proj, h in saved.items():
            if proj != project_name and h == hostname:
                raise ValueError("hostname_taken")

        saved[project_name] = hostname
        self._write_hostnames(saved)
        return self._reload_caddy()

    def reset_hostname(self, project_name: str) -> dict:
        """Remove hostname override and reload Caddy (reverts to auto-generated slug)."""
        saved = self._load_hostnames()
        saved.pop(project_name, None)
        self._write_hostnames(saved)
        return self._reload_caddy()

    def _reload_caddy(self) -> dict:
        """Signal Caddy to reload its configuration."""
        result = subprocess.run(
            ["caddy", "reload", "--config", str(CADDYFILE)],
            capture_output=True,
            text=True,
        )
        return {"ok": result.returncode == 0, "stderr": result.stderr}
