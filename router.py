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

    # ── Caddyfile ──────────────────────────────────────────────────────────

    def _generate_caddyfile(self) -> str:
        """Build Caddyfile content from all registered projects."""
        blocks = []
        for h in self.all_hostnames():
            if h["port"] is not None:
                blocks.append(
                    f"{h['hostname']} {{\n    reverse_proxy localhost:{h['port']}\n}}"
                )
        return "\n\n".join(blocks) + ("\n" if blocks else "")

    def _reload_caddy(self) -> dict:
        """Write Caddyfile and reload (or start) Caddy."""
        content = self._generate_caddyfile()
        if not content:
            return {"ok": True}
        SESHAT_DIR.mkdir(exist_ok=True)
        CADDYFILE.write_text(content)

        which = subprocess.run(["which", "caddy"], capture_output=True, timeout=5)
        if which.returncode != 0:
            return {"ok": False, "error": "caddy not installed"}

        running = subprocess.run(["pgrep", "-x", "caddy"], capture_output=True, timeout=5)
        if running.returncode == 0:
            r = subprocess.run(
                ["caddy", "reload", "--config", str(CADDYFILE)],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode == 0:
                return {"ok": True}
            return {"ok": False, "error": r.stderr.strip() or "(no output)"}
        else:
            r = subprocess.run(
                ["caddy", "start", "--config", str(CADDYFILE)],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode == 0:
                return {"ok": True}
            return {"ok": False, "error": r.stderr.strip() or "(no output)"}

    # ── Setup ──────────────────────────────────────────────────────────────

    def configure_dnsmasq(self) -> dict:
        """Append *.seshat wildcard to dnsmasq config and restart the service."""
        r = subprocess.run(["brew", "--prefix"], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return {"ok": False, "error": "brew not found"}
        prefix = Path(r.stdout.strip())
        # Try standard brew location first, fall back to prefix root
        for candidate in (prefix / "etc" / "dnsmasq.conf", prefix / "dnsmasq.conf"):
            if candidate.exists():
                conf_path = candidate
                break
        else:
            return {"ok": False, "error": f"dnsmasq config not found under {prefix}"}

        line = "address=/.seshat/127.0.0.1"
        content = conf_path.read_text()
        if line not in content:
            conf_path.write_text(content.rstrip() + f"\n{line}\n")

        r2 = subprocess.run(
            ["brew", "services", "restart", "dnsmasq"],
            capture_output=True, text=True, timeout=30,
        )
        if r2.returncode != 0:
            return {"ok": False, "error": r2.stderr.strip() or "(no output)"}
        return {"ok": True}

    def start_caddy(self) -> dict:
        """Generate Caddyfile and start (or reload) Caddy."""
        return self._reload_caddy()

    def setup_status(self) -> dict:
        """Check whether the full routing stack is installed and configured."""
        caddy_installed    = subprocess.run(["which", "caddy"],   capture_output=True, timeout=5).returncode == 0
        dnsmasq_installed  = subprocess.run(["which", "dnsmasq"], capture_output=True, timeout=5).returncode == 0
        caddy_running      = subprocess.run(["pgrep", "-x", "caddy"],   capture_output=True, timeout=5).returncode == 0
        dnsmasq_running    = subprocess.run(["pgrep", "-x", "dnsmasq"], capture_output=True, timeout=5).returncode == 0
        resolver_configured = Path("/etc/resolver/seshat").exists()
        caddyfile_exists   = CADDYFILE.exists()
        # If no hostnames have ports assigned, Caddy doesn't need to run yet.
        has_routes = any(h["port"] is not None for h in self.all_hostnames())
        caddy_running = caddy_running or not has_routes
        return {
            "caddy_installed":     caddy_installed,
            "dnsmasq_installed":   dnsmasq_installed,
            "caddy_running":       caddy_running,
            "dnsmasq_running":     dnsmasq_running,
            "resolver_configured": resolver_configured,
            "caddyfile_exists":    caddyfile_exists,
        }
