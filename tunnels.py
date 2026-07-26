"""
tunnels.py — public tunnel management via a single shared ngrok agent.

ngrok's free tier permits only one simultaneous agent session (ERR_NGROK_108),
so a per-project `ngrok http <port>` cannot work: the second tunnelled project
would be refused. Instead Seshat generates one config file holding every
project's endpoint and runs a single `ngrok start --all` — mirroring the way
router.py generates one Caddyfile and runs a single Caddy.

Projects opt in by declaring a `tunnel:` block in ~/.seshat/registry.yaml:

    - name: vault-mcp
      port: 6150
      tunnel:
        provider: ngrok                      # optional, defaults to ngrok
        domain: abc.ngrok-free.dev           # optional; omit for an ephemeral URL

Credentials are deliberately not written here. Seshat's config carries only
endpoints; the authtoken stays in ngrok's own config, which is layered in as a
second --config argument at launch.
"""

import json
import os
import re
import shutil
import signal
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen

import yaml

from registry import Registry, SESHAT_DIR

NGROK_CONFIG = SESHAT_DIR / "ngrok.yml"
PID_FILE     = SESHAT_DIR / "ngrok.pid"
LOG_DIR      = SESHAT_DIR / "logs"
NGROK_API    = "http://localhost:4040/api/tunnels"

# ngrok's own config, holding the authtoken. Location varies by platform and
# install age; we pass whichever exists so Seshat never has to store the token.
_DEFAULT_CONFIG_CANDIDATES = (
    Path.home() / "Library" / "Application Support" / "ngrok" / "ngrok.yml",
    Path.home() / ".config" / "ngrok" / "ngrok.yml",
    Path.home() / ".ngrok2" / "ngrok.yml",
)

_YAML_OPTS = dict(default_flow_style=False, allow_unicode=True, sort_keys=False)


def _endpoint_name(name: str) -> str:
    """Convert a project name to an ngrok endpoint name."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _default_config_path() -> Path | None:
    """Return ngrok's own config file (the one holding the authtoken), if any."""
    env_path = os.environ.get("NGROK_CONFIG")
    if env_path and Path(env_path).exists():
        return Path(env_path)
    for candidate in _DEFAULT_CONFIG_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


class TunnelManager:

    def __init__(self, registry: Registry):
        self.registry = registry

    # ── Discovery ──────────────────────────────────────────────────────────

    def tunneled_projects(self) -> list[dict]:
        """Return [{name, port, provider, domain}] for every ngrok-tunnelled project."""
        result = []
        for p in self.registry.list():
            tunnel = p.get("tunnel")
            if not tunnel or not p.get("port"):
                continue
            provider = (tunnel.get("provider") or "ngrok").lower()
            if provider not in ("ngrok", "ngrok-stable", "ngrok-edge"):
                continue
            result.append({
                "name":     p["name"],
                "port":     p["port"],
                "provider": provider,
                "domain":   tunnel.get("domain"),
            })
        return result

    # ── Config generation ──────────────────────────────────────────────────

    def _generate_config(self) -> str:
        """Build ngrok agent config (v3) from all tunnelled projects."""
        projects = self.tunneled_projects()
        if not projects:
            return ""

        endpoints = []
        for p in projects:
            endpoint = {
                "name":     _endpoint_name(p["name"]),
                "upstream": {"url": p["port"]},
            }
            domain = p.get("domain")
            if domain:
                # Accept a bare hostname or a full URL; ngrok wants a URL.
                endpoint["url"] = domain if "://" in domain else f"https://{domain}"
            endpoints.append(endpoint)

        return yaml.dump({"version": "3", "endpoints": endpoints}, **_YAML_OPTS)

    def _write_config(self) -> Path:
        """Persist the generated endpoint config. Never contains credentials."""
        SESHAT_DIR.mkdir(exist_ok=True)
        NGROK_CONFIG.write_text(self._generate_config())
        return NGROK_CONFIG

    # ── Agent lifecycle ────────────────────────────────────────────────────

    def _managed_pid(self) -> int | None:
        try:
            return int(PID_FILE.read_text().strip())
        except (FileNotFoundError, ValueError):
            return None

    def is_running(self) -> bool:
        """True if the shared agent is alive and answering on its local API."""
        try:
            with urlopen(Request(NGROK_API, headers={"User-Agent": "Seshat/1.0"}),
                         timeout=2):
                return True
        except Exception:
            return False

    def start(self) -> dict:
        """Write the config and launch the shared agent. Idempotent."""
        projects = self.tunneled_projects()
        if not projects:
            return {"ok": True, "endpoints": 0, "status": "no_tunnels"}

        if self.is_running():
            if self._managed_pid() is None:
                # Someone started ngrok by hand. Free tier allows one agent
                # session, so Seshat can't start its own alongside it — and
                # stop() deliberately won't kill a process it didn't start.
                return {
                    "ok": False,
                    "status": "foreign_agent",
                    "error": "an ngrok agent is already running that Seshat did not "
                             "start; stop it and retry so Seshat can manage the tunnels",
                }
            return {"ok": True, "endpoints": len(projects), "status": "already_running"}

        if not shutil.which("ngrok"):
            return {"ok": False, "error": "ngrok not installed (brew install ngrok)"}

        config = self._write_config()
        argv = ["ngrok", "start", "--all", "--log=stdout", "--log-format=logfmt"]
        default_config = _default_config_path()
        if default_config:
            argv += ["--config", str(default_config)]
        argv += ["--config", str(config)]

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOG_DIR / "ngrok.log"
        with open(log_path, "a") as f:
            f.write(f"\n--- Started {datetime.now():%Y-%m-%d %H:%M:%S} ---\n"
                    f"cmd: {' '.join(argv)}\n")

        log_file = open(log_path, "a")
        try:
            proc = subprocess.Popen(
                argv,
                stdout=log_file,
                stderr=log_file,
                start_new_session=True,
            )
        finally:
            log_file.close()   # parent closes; child keeps writing

        PID_FILE.write_text(str(proc.pid))
        return {"ok": True, "pid": proc.pid, "endpoints": len(projects),
                "status": "started", "log": str(log_path)}

    def stop(self) -> dict:
        """Stop the shared agent, if Seshat started one."""
        pid = self._managed_pid()
        if pid is None:
            return {"ok": True, "status": "not_managed"}
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass   # already gone, or not ours — clearing the pidfile is still right
        PID_FILE.unlink(missing_ok=True)
        return {"ok": True, "status": "stopped", "stopped_pid": pid}

    def ensure(self) -> dict:
        """Bring the agent in line with the registry, restarting only if needed.

        This is the project-start path: if the agent is already serving exactly
        the endpoints the registry declares, leave it alone rather than dropping
        other projects' live tunnels for no reason.
        """
        desired = self._generate_config()
        if not desired:
            return {"ok": True, "endpoints": 0, "status": "no_tunnels"}

        current = NGROK_CONFIG.read_text() if NGROK_CONFIG.exists() else ""
        if current == desired and self.is_running() and self._managed_pid():
            return {"ok": True, "status": "already_running",
                    "endpoints": len(self.tunneled_projects())}
        return self.reload()

    def reload(self) -> dict:
        """Re-sync the running agent with the registry.

        ngrok has no CLI hot-reload, so a config change means restarting the
        agent — briefly dropping open tunnels. Called after any registry change
        that adds, removes, or edits a tunnel.
        """
        projects = self.tunneled_projects()
        self._write_config()

        if not projects:
            # Nothing left to serve — don't hold the account's one agent session.
            if self.is_running():
                self.stop()
            return {"ok": True, "endpoints": 0, "status": "no_tunnels"}

        if self.is_running():
            self.stop()
        return self.start()

    # ── Status ─────────────────────────────────────────────────────────────

    def status(self) -> dict:
        """Report every declared endpoint against what the agent is actually serving.

        A project declared in the registry but absent from the live agent is
        reported disconnected — the case that previously showed green.
        """
        live = _live_tunnels()
        running = live is not None
        by_port = {_addr_port(t): t for t in (live or [])}

        endpoints = []
        for p in self.tunneled_projects():
            tunnel = by_port.get(p["port"])
            if tunnel:
                endpoints.append({
                    "name":       p["name"],
                    "port":       p["port"],
                    "status":     "connected",
                    "public_url": tunnel.get("public_url", ""),
                })
            else:
                endpoints.append({
                    "name":       p["name"],
                    "port":       p["port"],
                    "status":     "disconnected",
                    "public_url": "",
                    "detail":     ("ngrok agent not running" if not running
                                   else "declared but not served by the agent"),
                })

        return {"running": running, "endpoints": endpoints}


# ── ngrok local API helpers (shared with deps.py) ───────────────────────────


def _live_tunnels() -> list | None:
    """Return the agent's live tunnels, or None if the agent isn't reachable."""
    try:
        req = Request(NGROK_API, headers={"User-Agent": "Seshat/1.0"})
        with urlopen(req, timeout=2) as resp:
            return json.loads(resp.read()).get("tunnels", [])
    except Exception:
        return None


def _addr_port(tunnel: dict) -> int | None:
    """Extract the upstream port from a tunnel's config.addr."""
    addr = (tunnel.get("config") or {}).get("addr", "")
    m = re.search(r":(\d+)\s*$", addr)
    return int(m.group(1)) if m else None
