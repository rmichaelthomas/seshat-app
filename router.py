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
RESOLVER_FILE  = Path("/etc/resolver/seshat")

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
                    f"{h['hostname']} {{\n    tls internal\n    reverse_proxy localhost:{h['port']}\n}}"
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
        try:
            if running.returncode == 0:
                r = subprocess.run(
                    ["caddy", "reload", "--config", str(CADDYFILE)],
                    capture_output=True, text=True, timeout=15,
                )
            else:
                r = subprocess.run(
                    ["caddy", "start", "--config", str(CADDYFILE)],
                    capture_output=True, text=True, timeout=15,
                )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "caddy timed out — it may already be starting"}
        if r.returncode == 0:
            return {"ok": True}
        return {"ok": False, "error": r.stderr.strip() or "(no output)"}

    # ── Setup ──────────────────────────────────────────────────────────────

    @staticmethod
    def _run_as_admin(shell_cmd: str, timeout: int = 60) -> dict:
        """Run a shell command with macOS admin privileges via osascript."""
        # Include common brew paths so commands are found when running as admin
        path = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        escaped = shell_cmd.replace("\\", "\\\\").replace('"', '\\"')
        script = f'do shell script "PATH={path} {escaped}" with administrator privileges'
        try:
            r = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "timed out waiting for admin privileges"}
        if r.returncode != 0:
            err = r.stderr.strip() or r.stdout.strip() or "(no output)"
            return {"ok": False, "error": err}
        return {"ok": True}

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

        return self._run_as_admin("brew services restart dnsmasq")

    def configure_resolver(self) -> dict:
        """Create /etc/resolver/seshat to route .seshat DNS queries to dnsmasq."""
        if RESOLVER_FILE.exists():
            return {"ok": True}
        return self._run_as_admin(
            'mkdir -p /etc/resolver && printf "nameserver 127.0.0.1\\n" > /etc/resolver/seshat'
        )

    def trust_caddy_ca(self) -> dict:
        """Install Caddy's local CA into the system keychain so browsers trust it."""
        return self._run_as_admin("caddy trust")

    def start_caddy(self) -> dict:
        """Generate Caddyfile and start (or reload) Caddy."""
        return self._reload_caddy()

    def setup_status(self) -> dict:
        """Check whether the full routing stack is installed and configured."""
        caddy_installed    = subprocess.run(["which", "caddy"],   capture_output=True, timeout=5).returncode == 0
        dnsmasq_installed  = subprocess.run(["which", "dnsmasq"], capture_output=True, timeout=5).returncode == 0
        caddy_running      = subprocess.run(["pgrep", "-x", "caddy"],   capture_output=True, timeout=5).returncode == 0
        dnsmasq_running    = subprocess.run(["pgrep", "-x", "dnsmasq"], capture_output=True, timeout=5).returncode == 0
        resolver_configured = RESOLVER_FILE.exists()
        caddyfile_exists    = CADDYFILE.exists()
        # Caddy's local CA lives here once 'caddy trust' has been run
        caddy_ca_trusted    = Path.home().joinpath(
            ".local/share/caddy/pki/authorities/local/root.crt"
        ).exists()
        # If no hostnames have ports assigned, Caddy doesn't need to run yet.
        has_routes = any(h["port"] is not None for h in self.all_hostnames())
        caddy_running = caddy_running or not has_routes
        return {
            "caddy_installed":     caddy_installed,
            "dnsmasq_installed":   dnsmasq_installed,
            "caddy_running":       caddy_running,
            "dnsmasq_running":     dnsmasq_running,
            "resolver_configured": resolver_configured,
            "caddy_ca_trusted":    caddy_ca_trusted,
            "caddyfile_exists":    caddyfile_exists,
        }
