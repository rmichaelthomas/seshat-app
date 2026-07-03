#!/usr/bin/env python3
"""
mcp_server.py — Seshat MCP server.

Exposes Seshat's local environment management as MCP tools and resources
for AI coding agents. Peer entry point alongside seshat.py (Flask dashboard).

Transport: stdio
Protocol: MCP (Model Context Protocol)
"""

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from registry import Registry
from scanner import Scanner
from runner import Runner
from vault import Vault
import deps as deps_module

try:
    import agreements
except ImportError as exc:
    raise ImportError(
        "Seshat MCP server requires the 'liminate' package for Agreement "
        "enforcement (deny-by-default agent permissions). Install it with: "
        "pip install 'liminate>=0.15.1,<0.16'. "
        f"Original error: {exc}"
    ) from exc

# ── Module instances (shared with Flask dashboard) ─────────────────────────

registry = Registry()
scanner = Scanner()
runner = Runner()
vault = Vault()

# ── MCP server ─────────────────────────────────────────────────────────────

mcp = FastMCP(
    "Seshat",
    instructions=(
        "Local environmental agent harness. "
        "Manages project registry, process lifecycle, secrets vault, "
        "port scanning, dependency health, and agent session tracking "
        "for the developer's local machine."
    ),
)

# ── Session identity ───────────────────────────────────────────────────────

SESSION_ID = f"mcp_session_{uuid.uuid4().hex[:12]}"

# ── Receipt storage ────────────────────────────────────────────────────────

RECEIPTS_DIR = Path.home() / ".seshat" / "receipts"
RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)


# ── Receipt helpers ────────────────────────────────────────────────────────


def _snapshot_before() -> dict:
    """Capture environment state before a tool action."""
    scan = scanner.scan()
    state = registry.get_state()
    return {
        "listening_ports": sorted(scan.keys()),
        "managed_projects": {
            name: {"pid": info.get("pid"), "started_by": info.get("started_by")}
            for name, info in state.items()
        },
    }


def _snapshot_after() -> dict:
    """Capture environment state after a tool action."""
    scan = scanner.scan()
    state = registry.get_state()
    return {
        "listening_ports": sorted(scan.keys()),
        "managed_projects": {
            name: {"pid": info.get("pid"), "started_by": info.get("started_by")}
            for name, info in state.items()
        },
    }


def _agreement_actor() -> str:
    """Agent-identity string used both for Agreement checks and receipt agent_hint.

    Single source of truth: the string checked against the Agreement and the
    agent_hint recorded in every receipt must never diverge (§8 invariant 3).
    """
    return os.environ.get("MCP_AGENT_HINT", "unknown-agent")


def _emit_receipt(
    action: str,
    target: dict,
    result: dict,
    env_before: dict,
    env_after: dict | None = None,
) -> None:
    """Write a machine-action Receipt to ~/.seshat/receipts/.

    Receipt schema (locked in §16 of addendum v1b):
      type, timestamp, actor, action, target, result,
      environment_before, environment_after

    `env_after` defaults to a fresh snapshot. Denial receipts pass the same
    snapshot used for `env_before`, since no action executed in that path.
    """
    if env_after is None:
        env_after = _snapshot_after()

    receipt = {
        "type": "machine_action",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "actor": {
            "type": "mcp_session",
            "session_id": SESSION_ID,
            "agent_hint": _agreement_actor(),
        },
        "action": action,
        "target": target,
        "result": result,
        "environment_before": env_before,
        "environment_after": env_after,
    }

    filename = (
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
        f"_{action}_{uuid.uuid4().hex[:8]}.json"
    )
    receipt_path = RECEIPTS_DIR / filename
    receipt_path.write_text(json.dumps(receipt, indent=2))


def _enforce(action: str, target: dict) -> str | None:
    """Evaluate the developer's Agreement for this call before it executes.

    Returns None when the Agreement permits the call. Returns a denial
    string (and logs a denial Receipt) when it does not — deny-by-default,
    per SES-Q4: no Agreement, no matching permit, or any evaluation error
    all deny, and a matching forbid always wins over a matching permit.
    """
    scope = target.get("project") or target.get("group")
    decision = agreements.check_action(_agreement_actor(), action, scope)
    if decision.allowed:
        return None

    env = _snapshot_before()
    result = {
        "status": "denied",
        "mode": decision.mode,
        "rule": decision.rule,
        "reason": decision.reason,
    }
    _emit_receipt(action, target, result, env, env_after=env)

    denial = f"DENIED by Agreement: {decision.reason}"
    if decision.rule is not None:
        denial += f" Rule: {decision.rule}"
    return denial


# ── Shared helpers ─────────────────────────────────────────────────────────


def _enrich_deps(project: dict, project_name: str) -> list:
    """Resolve vault-held URLs into dep configs before health-checking."""
    enriched = []
    for dep in project.get("dependencies", []):
        d = dict(dep)
        provider = d.get("provider", "").lower()
        if provider == "supabase" and not d.get("url"):
            resolved = vault.resolve_for_project(project_name, ["SUPABASE_URL"])
            if "SUPABASE_URL" in resolved:
                d["url"] = resolved["SUPABASE_URL"]
        elif provider == "postgres" and not d.get("url"):
            resolved = vault.resolve_for_project(project_name, ["DATABASE_URL"])
            if "DATABASE_URL" in resolved:
                d["url"] = resolved["DATABASE_URL"]
        elif provider in ("http", "api") and not d.get("url"):
            label = (d.get("label") or "").upper()
            key = f"{label}_URL" if label else None
            if key:
                resolved = vault.resolve_for_project(project_name, [key])
                if key in resolved:
                    d["url"] = resolved[key]
        enriched.append(d)
    return enriched


def _compute_composite_status(status: str, dep_results: list) -> str:
    """running + any dep disconnected = degraded."""
    if status != "running":
        return status
    if any(d.get("status") == "disconnected" for d in (dep_results or [])):
        return "degraded"
    return status


def _build_project_view(project: dict, scan: dict, state: dict) -> dict:
    """Merge registry data + live port scan + log errors + dep status into one view."""
    port = project["port"]
    name = project["name"]
    managed_pid = state.get(name, {}).get("pid")
    port_info = scan.get(port)

    status = "stopped"
    proc_data = {}

    if port_info:
        pid_on_port = port_info["pid"]
        if managed_pid and runner.is_running(managed_pid) and runner.owns_pid(managed_pid, pid_on_port):
            status = "running"
        else:
            status = "conflict"
        proc_data = {
            "pid": port_info["pid"],
            "process_name": port_info.get("name", ""),
            "process_cmd": port_info.get("cmdline", ""),
        }
    elif managed_pid and runner.is_running(managed_pid):
        status = "running"
        proc_data = {"pid": managed_pid}

    child_ports = []
    if managed_pid and status == "running":
        owned_pids = runner.child_pids(managed_pid) | {managed_pid}
        for scan_port, scan_info in scan.items():
            if scan_port != port and scan_info["pid"] in owned_pids:
                child_ports.append(scan_port)

    view = {**project, "status": status, **proc_data, "child_ports": sorted(child_ports)}

    started_by = state.get(name, {}).get("started_by")
    if started_by:
        view["started_by"] = started_by

    recent_error = runner.find_recent_error(name)
    if recent_error:
        view["recent_error"] = recent_error
        if status == "running":
            view["has_error"] = True

    dep_status = deps_module.get_cached(name) or []
    if not dep_status and project.get("dependencies"):
        enriched = _enrich_deps(project, name)
        deps_module.check_all_async(name, enriched)

    view["dep_status"] = dep_status
    view["composite_status"] = _compute_composite_status(status, dep_status)

    return view


# ── MCP tools ──────────────────────────────────────────────────────────────
# Every tool below calls _enforce() as its first statement, before any side
# effect. A 9th tool must do the same — it inherits the gate by convention,
# not by any structural guarantee.


@mcp.tool()
def start_project(name: str) -> str:
    """Start a registered project by name.

    Resolves vault secrets scoped to the project, starts the process,
    and records the PID with MCP session attribution.
    """
    denial = _enforce("start_project", {"project": name})
    if denial:
        return denial

    env_before = _snapshot_before()

    project = registry.get(name)
    if not project:
        result = {"status": "failure", "error": f"Project '{name}' not found"}
        _emit_receipt("start_project", {"project": name}, result, env_before)
        return json.dumps(result)

    scan = scanner.scan()
    if project["port"] in scan:
        proc = scan[project["port"]]
        result = {
            "status": "failure",
            "error": (
                f"Port {project['port']} is already in use by "
                f"'{proc['name']}' (PID {proc['pid']})"
            ),
        }
        _emit_receipt(
            "start_project",
            {"project": name, "port": project["port"]},
            result,
            env_before,
        )
        return json.dumps(result)

    try:
        extra_env = vault.resolve_for_project(name, project.get("env", []))
        pid = runner.start(project, extra_env=extra_env)
        registry.set_pid(name, pid, started_by=SESSION_ID)

        if project.get("dependencies"):
            enriched = _enrich_deps(project, name)
            deps_module.check_all_async(name, enriched)

        result = {"status": "success", "pid": pid}
        _emit_receipt(
            "start_project",
            {"project": name, "port": project["port"], "directory": project["directory"]},
            result,
            env_before,
        )
        return json.dumps(result)
    except (ValueError, OSError) as e:
        result = {"status": "failure", "error": str(e)}
        _emit_receipt("start_project", {"project": name}, result, env_before)
        return json.dumps(result)


@mcp.tool()
def stop_project(name: str) -> str:
    """Stop a running project by name."""
    denial = _enforce("stop_project", {"project": name})
    if denial:
        return denial

    env_before = _snapshot_before()

    project = registry.get(name)
    if not project:
        result = {"status": "failure", "error": f"Project '{name}' not found"}
        _emit_receipt("stop_project", {"project": name}, result, env_before)
        return json.dumps(result)

    state = registry.get_state()
    pid = state.get(name, {}).get("pid")
    if not pid:
        result = {"status": "failure", "error": "No managed process found"}
        _emit_receipt("stop_project", {"project": name}, result, env_before)
        return json.dumps(result)

    runner.stop(pid)
    registry.clear_pid(name)

    result = {"status": "success", "stopped_pid": pid}
    _emit_receipt(
        "stop_project",
        {"project": name, "port": project["port"]},
        result,
        env_before,
    )
    return json.dumps(result)


@mcp.tool()
def start_group(name: str) -> str:
    """Start all projects in a named group."""
    denial = _enforce("start_group", {"group": name})
    if denial:
        return denial

    env_before = _snapshot_before()

    group = registry.get_group(name)
    if not group:
        result = {"status": "failure", "error": f"Group '{name}' not found"}
        _emit_receipt("start_group", {"group": name}, result, env_before)
        return json.dumps(result)

    scan = scanner.scan()
    state = registry.get_state()
    results = []

    for proj_name in group.get("projects", []):
        project = registry.get(proj_name)
        if not project:
            results.append({"name": proj_name, "error": "Project not found"})
            continue

        managed_pid = state.get(proj_name, {}).get("pid")
        if managed_pid and runner.is_running(managed_pid):
            results.append({"name": proj_name, "status": "already_running"})
            continue

        if project["port"] in scan:
            proc = scan[project["port"]]
            results.append({
                "name": proj_name,
                "error": f"Port {project['port']} in use by '{proc['name']}'",
            })
            continue

        try:
            extra_env = vault.resolve_for_project(proj_name, project.get("env", []))
            pid = runner.start(project, extra_env=extra_env)
            registry.set_pid(proj_name, pid, started_by=SESSION_ID)
            results.append({"name": proj_name, "status": "started", "pid": pid})

            if project.get("dependencies"):
                enriched = _enrich_deps(project, proj_name)
                deps_module.check_all_async(proj_name, enriched)

            time.sleep(0.4)
            scan = scanner.scan()
        except Exception as e:
            results.append({"name": proj_name, "error": str(e)})

    result = {"status": "success", "group": name, "results": results}
    _emit_receipt("start_group", {"group": name}, result, env_before)
    return json.dumps(result)


@mcp.tool()
def stop_group(name: str) -> str:
    """Stop all projects in a named group."""
    denial = _enforce("stop_group", {"group": name})
    if denial:
        return denial

    env_before = _snapshot_before()

    group = registry.get_group(name)
    if not group:
        result = {"status": "failure", "error": f"Group '{name}' not found"}
        _emit_receipt("stop_group", {"group": name}, result, env_before)
        return json.dumps(result)

    state = registry.get_state()
    results = []

    for proj_name in group.get("projects", []):
        pid = state.get(proj_name, {}).get("pid")
        if not pid:
            results.append({"name": proj_name, "status": "not_managed"})
            continue
        runner.stop(pid)
        registry.clear_pid(proj_name)
        results.append({"name": proj_name, "status": "stopped"})

    result = {"status": "success", "group": name, "results": results}
    _emit_receipt("stop_group", {"group": name}, result, env_before)
    return json.dumps(result)


@mcp.tool()
def register_project(
    name: str,
    port: int,
    directory: str,
    start: str,
    stop: str = "",
    tags: list[str] | None = None,
    notes: str = "",
) -> str:
    """Register a new project in the Seshat registry.

    Args:
        name: Project name (must be unique)
        port: TCP port the project listens on
        directory: Absolute path to the project directory (~ allowed)
        start: Shell command to start the project
        stop: Optional shell command to stop the project
        tags: Optional list of tags for organization
        notes: Optional notes about the project
    """
    denial = _enforce("register_project", {"project": name})
    if denial:
        return denial

    env_before = _snapshot_before()

    project = {
        "name": name.strip(),
        "port": port,
        "scheme": "http",
        "directory": directory.strip(),
        "start": start.strip(),
        "stop": (stop or "").strip(),
        "url": f"http://localhost:{port}",
        "tags": tags or [],
        "notes": (notes or "").strip(),
        "dependencies": [],
        "env": [],
    }

    try:
        result_project = registry.add(project)
        result = {"status": "success", "project": result_project}
        _emit_receipt(
            "register_project",
            {"project": name, "port": port, "directory": directory},
            result,
            env_before,
        )
        return json.dumps(result)
    except ValueError as e:
        result = {"status": "failure", "error": str(e)}
        _emit_receipt(
            "register_project",
            {"project": name, "port": port},
            result,
            env_before,
        )
        return json.dumps(result)


@mcp.tool()
def stop_orphan(port: int) -> str:
    """Stop an unregistered process listening on a port."""
    denial = _enforce("stop_orphan", {"port": port})
    if denial:
        return denial

    env_before = _snapshot_before()

    scan = scanner.scan()
    if port not in scan:
        result = {"status": "failure", "error": f"No process found on port {port}"}
        _emit_receipt("stop_orphan", {"port": port}, result, env_before)
        return json.dumps(result)

    pid = scan[port]["pid"]
    process_name = scan[port].get("name", "unknown")
    runner.stop(pid)

    result = {"status": "success", "stopped_pid": pid, "process": process_name}
    _emit_receipt("stop_orphan", {"port": port, "pid": pid}, result, env_before)
    return json.dumps(result)


@mcp.tool()
def set_secret(key: str, value: str) -> str:
    """Store or update a shared secret in the vault.

    The secret is encrypted at rest (Keychain-backed Fernet).
    Secret values are never exposed through MCP resources —
    they are resolved at process start time via environment variables.
    """
    normalized_key = key.strip().upper()
    denial = _enforce("set_secret", {"key": normalized_key})
    if denial:
        return denial

    env_before = _snapshot_before()

    vault.set(key.strip().upper(), value)

    result = {"status": "success", "key": key.strip().upper()}
    _emit_receipt("set_secret", {"key": key.strip().upper()}, result, env_before)
    return json.dumps(result)


@mcp.tool()
def set_project_override(project: str, key: str, value: str) -> str:
    """Set a project-specific secret override in the vault.

    Overrides take precedence over shared secrets when resolving
    environment variables for this project at start time.
    """
    normalized_key = key.strip().upper()
    denial = _enforce("set_project_override", {"project": project, "key": normalized_key})
    if denial:
        return denial

    env_before = _snapshot_before()

    if not registry.get(project):
        result = {"status": "failure", "error": f"Project '{project}' not found"}
        _emit_receipt(
            "set_project_override",
            {"project": project, "key": key.strip().upper()},
            result,
            env_before,
        )
        return json.dumps(result)

    vault.set_override(project, key.strip().upper(), value)

    result = {"status": "success", "project": project, "key": key.strip().upper()}
    _emit_receipt(
        "set_project_override",
        {"project": project, "key": key.strip().upper()},
        result,
        env_before,
    )
    return json.dumps(result)


# ── MCP resources ──────────────────────────────────────────────────────────


@mcp.resource("seshat://projects")
def resource_projects() -> str:
    """All registered projects with live composite status, dep health, and recent errors."""
    projects = registry.list()
    scan = scanner.scan()
    state = registry.get_state()
    views = [_build_project_view(p, scan, state) for p in projects]
    return json.dumps(views, indent=2)


@mcp.resource("seshat://project/{name}")
def resource_project(name: str) -> str:
    """Single project detail with live status."""
    project = registry.get(name)
    if not project:
        return json.dumps({"error": f"Project '{name}' not found"})
    scan = scanner.scan()
    state = registry.get_state()
    return json.dumps(_build_project_view(project, scan, state), indent=2)


@mcp.resource("seshat://listeners")
def resource_listeners() -> str:
    """All TCP listeners on this machine, annotated by kind (project / seshat / conflict / orphan)."""
    scan = scanner.scan()
    state = registry.get_state()
    port_to_project = {p["port"]: p["name"] for p in registry.list()}
    managed_pids = {
        name: info.get("pid")
        for name, info in state.items()
        if info.get("pid")
    }

    rows = []
    for port, info in sorted(scan.items()):
        pid = info["pid"]
        project_name = port_to_project.get(port)
        managed = project_name and managed_pids.get(project_name) == pid
        if port == 9000:
            kind = "seshat"
        elif project_name and managed:
            kind = "project"
        elif project_name and not managed:
            kind = "conflict"
        else:
            kind = "orphan"
        rows.append({
            "port": port,
            "pid": pid,
            "name": info.get("name", ""),
            "cmdline": info.get("cmdline", ""),
            "kind": kind,
            "project_name": project_name,
        })
    return json.dumps(rows, indent=2)


@mcp.resource("seshat://orphans")
def resource_orphans() -> str:
    """Unregistered processes on ports."""
    scan = scanner.scan()
    registered_ports = {p["port"] for p in registry.list()}
    registered_ports.add(9000)

    orphans = [
        {
            "port": port,
            "pid": info["pid"],
            "name": info.get("name", "unknown"),
            "cmdline": info.get("cmdline", ""),
        }
        for port, info in sorted(scan.items())
        if port not in registered_ports
    ]
    return json.dumps(orphans, indent=2)


@mcp.resource("seshat://groups")
def resource_groups() -> str:
    """Named project groups and their members."""
    return json.dumps(registry.list_groups(), indent=2)


@mcp.resource("seshat://vault/audit")
def resource_vault_audit() -> str:
    """Cross-reference of vault keys vs. project env declarations (missing/unused).

    Does NOT expose secret values. Shows only key names,
    which projects declare them, and whether they are present or missing.
    """
    return json.dumps(vault.audit(registry.list()), indent=2)


@mcp.resource("seshat://project/{name}/logs")
def resource_project_logs(name: str) -> str:
    """Recent log output and most recent error for a project."""
    if not registry.get(name):
        return json.dumps({"error": f"Project '{name}' not found"})
    lines = runner.read_log_tail(name, n=150)
    error = runner.find_recent_error(name)
    return json.dumps({"lines": lines, "recent_error": error}, indent=2)


@mcp.resource("seshat://project/{name}/deps")
def resource_project_deps(name: str) -> str:
    """Dependency health results for a project."""
    project = registry.get(name)
    if not project:
        return json.dumps({"error": f"Project '{name}' not found"})

    dep_status = deps_module.get_cached(name) or []
    if not dep_status and project.get("dependencies"):
        enriched = _enrich_deps(project, name)
        deps_module.check_all_async(name, enriched)
        dep_status = []

    return json.dumps(dep_status, indent=2)


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
