#!/usr/bin/env python3
"""
cli.py — Seshat CLI and TUI entry point.

One-shot commands via Click. Interactive TUI via Textual.
Peer entry point alongside seshat.py (Flask) and mcp_server.py (MCP).
All surfaces share the same module layer — no Flask dependency.

Usage:
  seshat                    → launch TUI
  seshat tui                → launch TUI (explicit)
  seshat status             → show all projects
  seshat start <name>       → start a project
  seshat stop <name>        → stop a project
  ...
"""

import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table
from rich.text import Text

from registry import Registry
from runner import Runner
from vault import Vault
from scanner import Scanner
import deps as deps_module
import agreements
import receipts as receipts_module

# ── Module instances ────────────────────────────────────────────────────────

registry = Registry()
runner   = Runner()
vault    = Vault()
scanner  = Scanner()

# ── Session identity ────────────────────────────────────────────────────────

SESSION_ID = f"cli_{uuid.uuid4().hex[:12]}"

console = Console()


def _emit(**kwargs) -> None:
    """Wrap receipts.emit(), injecting the current revocation_state so every
    CLI-emitted receipt carries it from one source (§7 invariant 4)."""
    receipts_module.emit(revocation_state=agreements.revocation_state(), **kwargs)


# ── Project view builder ────────────────────────────────────────────────────

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
            key   = f"{label}_URL" if label else None
            if key:
                resolved = vault.resolve_for_project(project_name, [key])
                if key in resolved:
                    d["url"] = resolved[key]
        enriched.append(d)
    return enriched


def _build_project_view(project: dict, scan: dict, state: dict) -> dict:
    """Merge registry + live scan + logs + deps into one view dict."""
    port        = project["port"]
    name        = project["name"]
    managed_pid = state.get(name, {}).get("pid")
    port_info   = scan.get(port)

    status    = "stopped"
    proc_data = {}

    if port_info:
        pid_on_port = port_info["pid"]
        if managed_pid and runner.is_running(managed_pid) and runner.owns_pid(managed_pid, pid_on_port):
            status = "running"
        else:
            status = "conflict"
        proc_data = {
            "pid":          port_info["pid"],
            "process_name": port_info.get("name", ""),
        }
    elif managed_pid and runner.is_running(managed_pid):
        status    = "running"
        proc_data = {"pid": managed_pid}

    dep_status = deps_module.get_cached(name) or []
    if status == "running" and any(d.get("status") == "disconnected" for d in dep_status):
        composite = "degraded"
    else:
        composite = status

    view = {**project, "status": status, "composite_status": composite, **proc_data}

    started_by = state.get(name, {}).get("started_by")
    if started_by:
        view["started_by"] = started_by

    recent_error = runner.find_recent_error(name)
    if recent_error:
        view["recent_error"] = recent_error

    view["dep_status"] = dep_status
    return view


# ── Color helpers ───────────────────────────────────────────────────────────

STATUS_COLORS = {
    "running":  "green",
    "stopped":  "dim",
    "conflict": "red",
    "error":    "yellow",
    "degraded": "yellow",
}

STATUS_GLYPHS = {
    "running":  "●",
    "stopped":  "○",
    "conflict": "✗",
    "error":    "⚠",
    "degraded": "◐",
}


def _status_text(status: str) -> Text:
    color = STATUS_COLORS.get(status, "dim")
    glyph = STATUS_GLYPHS.get(status, "○")
    return Text(f"{glyph} {status}", style=color)


def _attr_text(started_by: str | None) -> Text:
    if not started_by:
        return Text("—", style="dim")
    if started_by == "dashboard":
        return Text("dashboard", style="blue")
    if started_by == "cli":
        return Text("cli", style="green")
    if started_by.startswith("cli_"):
        short = started_by[4:12]
        return Text(f"cli:{short}", style="green")
    if started_by.startswith("mcp_session_"):
        short = started_by[12:20]
        return Text(f"agent:{short}", style="magenta")
    return Text(started_by, style="dim")


def _shorten_path(path: str) -> str:
    home = str(Path.home())
    if path.startswith(home):
        return "~" + path[len(home):]
    return path


# ── CLI entry point ─────────────────────────────────────────────────────────

@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx):
    """Seshat — local environmental agent harness.

    Run without arguments to launch the interactive TUI.
    """
    if ctx.invoked_subcommand is None:
        _launch_tui()


# ── Commands ────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("name", required=False)
def status(name):
    """Show project status. Pass a name for detail on one project."""
    scan  = scanner.scan()
    state = registry.get_state()

    if name:
        project = registry.get(name)
        if not project:
            console.print(f"[red]Project '{name}' not found.[/red]")
            sys.exit(1)
        view = _build_project_view(project, scan, state)
        _print_project_detail(view)
        return

    projects = registry.list()
    if not projects:
        console.print("[dim]No projects registered.[/dim]")
        return

    table = Table(show_header=True, header_style="dim", box=None, padding=(0, 2))
    table.add_column("", width=2)
    table.add_column("Name",       style="bold", min_width=16)
    table.add_column("Port",       style="cyan", min_width=6)
    table.add_column("Status",     min_width=10)
    table.add_column("Started by", min_width=14)
    table.add_column("Directory",  style="dim")

    for p in projects:
        view   = _build_project_view(p, scan, state)
        vstatus = view["composite_status"]
        color  = STATUS_COLORS.get(vstatus, "dim")
        glyph  = STATUS_GLYPHS.get(vstatus, "○")
        table.add_row(
            Text(glyph, style=color),
            view["name"],
            str(view["port"]),
            _status_text(vstatus),
            _attr_text(view.get("started_by")),
            _shorten_path(view.get("directory", "")),
        )

    console.print(table)


def _print_project_detail(view: dict) -> None:
    """Print single-project detail panel to terminal."""
    vstatus = view["composite_status"]
    console.print()
    console.print(f"[bold]{view['name']}[/bold]  [cyan]:{view['port']}[/cyan]")
    console.print(f"  Status:     {_status_text(vstatus)}")
    console.print(f"  Started by: {_attr_text(view.get('started_by'))}")
    if view.get("pid"):
        console.print(f"  PID:        [dim]{view['pid']}[/dim]")
    console.print(f"  Directory:  [dim]{view.get('directory', '—')}[/dim]")
    console.print(f"  Start cmd:  [dim]{view.get('start', '—')}[/dim]")
    if view.get("recent_error"):
        err = view["recent_error"]
        console.print(f"  [yellow]Error:[/yellow]      {err.get('message', '')}")
        if err.get("short"):
            console.print(f"              [dim]{err['short']}[/dim]")
    dep_status = view.get("dep_status", [])
    if dep_status:
        console.print("  Deps:")
        for d in dep_status:
            dep_color = {"connected": "green", "disconnected": "red"}.get(d.get("status", ""), "dim")
            console.print(f"    [{dep_color}]●[/{dep_color}] {d.get('label', d.get('provider', '?'))}")
    console.print()


@cli.command()
@click.argument("name", required=False)
@click.option("--group", "-g", default=None, help="Start a named group of projects.")
def start(name, group):
    """Start a project or group."""
    env_before = receipts_module.snapshot()

    if group:
        grp = registry.get_group(group)
        if not grp:
            console.print(f"[red]Group '{group}' not found.[/red]")
            sys.exit(1)
        scan  = scanner.scan()
        state = registry.get_state()
        results = []
        for proj_name in grp.get("projects", []):
            project = registry.get(proj_name)
            if not project:
                results.append({"name": proj_name, "error": "not found"})
                continue
            managed_pid = state.get(proj_name, {}).get("pid")
            if managed_pid and runner.is_running(managed_pid):
                console.print(f"[dim]{proj_name}[/dim] already running")
                results.append({"name": proj_name, "status": "already_running"})
                continue
            if project["port"] in scan:
                proc = scan[project["port"]]
                console.print(f"[red]{proj_name}[/red] port {project['port']} in use by '{proc['name']}'")
                results.append({"name": proj_name, "error": f"port {project['port']} in use"})
                continue
            try:
                extra_env = vault.resolve_for_project(proj_name, project.get("env", []))
                pid = runner.start(project, extra_env=extra_env)
                registry.set_pid(proj_name, pid, started_by=SESSION_ID)
                console.print(f"[green]✓[/green] {proj_name} started (PID {pid})")
                results.append({"name": proj_name, "status": "started", "pid": pid})
                time.sleep(0.4)
                scan = scanner.scan()
            except Exception as e:
                console.print(f"[red]✗[/red] {proj_name}: {e}")
                results.append({"name": proj_name, "error": str(e)})
        result = {"status": "success", "group": group, "results": results}
        _emit(
            action="start_group",
            target={"group": group},
            result=result,
            env_before=env_before,
            session_id=SESSION_ID,
            actor_type="cli_session",
            agent_hint=os.environ.get("MCP_AGENT_HINT", "cli"),
        )
        return

    if not name:
        console.print("[red]Provide a project name or --group.[/red]")
        sys.exit(1)

    project = registry.get(name)
    if not project:
        console.print(f"[red]Project '{name}' not found.[/red]")
        sys.exit(1)

    scan = scanner.scan()
    if project["port"] in scan:
        proc = scan[project["port"]]
        console.print(f"[red]Port {project['port']} is already in use by '{proc['name']}' (PID {proc['pid']}).[/red]")
        result = {"status": "failure", "error": f"port {project['port']} in use"}
        _emit(
            action="start_project",
            target={"project": name},
            result=result,
            env_before=env_before,
            session_id=SESSION_ID,
            actor_type="cli_session",
            agent_hint=os.environ.get("MCP_AGENT_HINT", "cli"),
        )
        sys.exit(1)

    try:
        extra_env = vault.resolve_for_project(name, project.get("env", []))
        pid = runner.start(project, extra_env=extra_env)
        registry.set_pid(name, pid, started_by=SESSION_ID)
        console.print(f"[green]✓[/green] {name} started (PID {pid})")
        result = {"status": "success", "pid": pid}
        _emit(
            action="start_project",
            target={"project": name, "port": project["port"]},
            result=result,
            env_before=env_before,
            session_id=SESSION_ID,
            actor_type="cli_session",
            agent_hint=os.environ.get("MCP_AGENT_HINT", "cli"),
        )
    except Exception as e:
        console.print(f"[red]✗[/red] {name}: {e}")
        result = {"status": "failure", "error": str(e)}
        _emit(
            action="start_project",
            target={"project": name},
            result=result,
            env_before=env_before,
            session_id=SESSION_ID,
            actor_type="cli_session",
            agent_hint=os.environ.get("MCP_AGENT_HINT", "cli"),
        )
        sys.exit(1)


@cli.command()
@click.argument("name", required=False)
@click.option("--group", "-g", default=None, help="Stop a named group of projects.")
def stop(name, group):
    """Stop a project or group."""
    env_before = receipts_module.snapshot()

    if group:
        grp = registry.get_group(group)
        if not grp:
            console.print(f"[red]Group '{group}' not found.[/red]")
            sys.exit(1)
        state   = registry.get_state()
        results = []
        for proj_name in grp.get("projects", []):
            pid = state.get(proj_name, {}).get("pid")
            if not pid:
                console.print(f"[dim]{proj_name}[/dim] not managed")
                results.append({"name": proj_name, "status": "not_managed"})
                continue
            runner.stop(pid)
            registry.clear_pid(proj_name)
            console.print(f"[green]✓[/green] {proj_name} stopped")
            results.append({"name": proj_name, "status": "stopped"})
        result = {"status": "success", "group": group, "results": results}
        _emit(
            action="stop_group",
            target={"group": group},
            result=result,
            env_before=env_before,
            session_id=SESSION_ID,
            actor_type="cli_session",
            agent_hint=os.environ.get("MCP_AGENT_HINT", "cli"),
        )
        return

    if not name:
        console.print("[red]Provide a project name or --group.[/red]")
        sys.exit(1)

    project = registry.get(name)
    if not project:
        console.print(f"[red]Project '{name}' not found.[/red]")
        sys.exit(1)

    state = registry.get_state()
    pid   = state.get(name, {}).get("pid")
    if not pid:
        console.print(f"[red]{name} has no managed process.[/red]")
        result = {"status": "failure", "error": "no managed process"}
        _emit(
            action="stop_project",
            target={"project": name},
            result=result,
            env_before=env_before,
            session_id=SESSION_ID,
            actor_type="cli_session",
            agent_hint=os.environ.get("MCP_AGENT_HINT", "cli"),
        )
        sys.exit(1)

    runner.stop(pid)
    registry.clear_pid(name)
    console.print(f"[green]✓[/green] {name} stopped (was PID {pid})")
    result = {"status": "success", "stopped_pid": pid}
    _emit(
        action="stop_project",
        target={"project": name, "port": project["port"]},
        result=result,
        env_before=env_before,
        session_id=SESSION_ID,
        actor_type="cli_session",
        agent_hint=os.environ.get("MCP_AGENT_HINT", "cli"),
    )


@cli.command(name="list")
def list_projects():
    """Alias for 'seshat status' — list all projects."""
    ctx = click.get_current_context()
    ctx.invoke(status)


@cli.command()
def ports():
    """Show all TCP listeners annotated by kind."""
    scan          = scanner.scan()
    state         = registry.get_state()
    port_to_proj  = {p["port"]: p["name"] for p in registry.list()}
    managed_pids  = {name: info.get("pid") for name, info in state.items() if info.get("pid")}

    table = Table(show_header=True, header_style="dim", box=None, padding=(0, 2))
    table.add_column("Port",    style="cyan",  min_width=6)
    table.add_column("PID",     style="dim",   min_width=8)
    table.add_column("Kind",    min_width=10)
    table.add_column("Process", style="dim")

    KIND_COLORS = {"seshat": "blue", "project": "green", "conflict": "red", "orphan": "yellow"}

    if not scan:
        console.print("[dim]No active listeners.[/dim]")
        return

    for port, info in sorted(scan.items()):
        pid          = info["pid"]
        project_name = port_to_proj.get(port)
        managed      = project_name and managed_pids.get(project_name) == pid
        if port == 9000:
            kind = "seshat"
        elif project_name and managed:
            kind = "project"
        elif project_name and not managed:
            kind = "conflict"
        else:
            kind = "orphan"

        kind_color = KIND_COLORS.get(kind, "dim")
        label      = project_name if project_name else info.get("name", "unknown")
        table.add_row(
            str(port),
            str(pid),
            Text(kind, style=kind_color),
            label,
        )

    console.print(table)


@cli.command()
def orphans():
    """Show unregistered processes on ports."""
    scan             = scanner.scan()
    registered_ports = {p["port"] for p in registry.list()} | {9000}

    rows = [
        (port, info)
        for port, info in sorted(scan.items())
        if port not in registered_ports
    ]

    if not rows:
        console.print("[dim]No orphaned processes.[/dim]")
        return

    table = Table(show_header=True, header_style="dim", box=None, padding=(0, 2))
    table.add_column("Port",    style="yellow", min_width=6)
    table.add_column("PID",     style="dim",    min_width=8)
    table.add_column("Process", style="dim")

    for port, info in rows:
        table.add_row(str(port), str(info["pid"]), info.get("name", "unknown"))

    console.print(table)


# ── Vault commands ──────────────────────────────────────────────────────────

@cli.group()
def vault_cmd():
    """Vault key management."""


@vault_cmd.command(name="list")
def vault_list():
    """List vault keys (names only — values are never shown)."""
    keys = vault.list_keys()
    if not keys:
        console.print("[dim]Vault is empty.[/dim]")
        return
    for key in sorted(keys):
        console.print(f"  [cyan]{key}[/cyan]")


@vault_cmd.command(name="set")
@click.argument("key")
@click.argument("value")
def vault_set(key, value):
    """Set a shared vault secret."""
    vault.set(key.strip().upper(), value)
    console.print(f"[green]✓[/green] Vault key [cyan]{key.strip().upper()}[/cyan] set.")


@vault_cmd.command(name="audit")
def vault_audit():
    """Cross-reference vault keys against project env declarations."""
    projects   = registry.list()
    audit_data = vault.audit(projects)

    if not audit_data:
        console.print("[dim]No vault audit data.[/dim]")
        return

    table = Table(show_header=True, header_style="dim", box=None, padding=(0, 2))
    table.add_column("Key",    style="cyan", min_width=24)
    table.add_column("Status", min_width=10)
    table.add_column("Projects")

    for entry in audit_data:
        key = entry.get("key", "")
        if entry.get("unused"):
            vstatus = "unused"
            projs  = "—"
        elif entry.get("missing_from"):
            vstatus = "missing"
            projs  = ", ".join(entry.get("declared_by", [])) or "—"
        else:
            vstatus = "ok"
            projs  = ", ".join(entry.get("declared_by", [])) or "—"
        s_color = {"ok": "green", "missing": "red", "unused": "dim"}.get(vstatus, "dim")
        table.add_row(key, Text(vstatus, style=s_color), projs)

    console.print(table)


cli.add_command(vault_cmd, name="vault")


# ── Agreement commands ──────────────────────────────────────────────────────

AGREEMENT_STARTER = """\
-- Seshat Agreement — agent permissions, deny-by-default.
-- Facts available: actor, action, scope (scope is "none" when the call has no project/group target).
-- No permit match = denied. A forbid always wins over a permit.

permit actor is "claude-code" and action is "start_project"
permit actor is "claude-code" and action is "stop_project"
permit actor is "claude-code" and action is "start_group"
permit actor is "claude-code" and action is "stop_group"
permit actor is "claude-code" and action is "register_project"

forbid action is "stop_orphan" because "orphan termination stays in the dashboard"
"""


@cli.group()
def agreement_cmd():
    """Agent-permission Agreement management (deny-by-default)."""


@agreement_cmd.command(name="init")
@click.option("--force", is_flag=True, default=False, help="Overwrite an existing Agreement file.")
def agreement_init(force):
    """Write the starter Agreement to ~/.seshat/agreement.limn."""
    path = agreements.AGREEMENT_PATH
    if path.exists() and not force:
        console.print(f"[yellow]Agreement already exists at {path}.[/yellow] Use --force to overwrite.")
        sys.exit(1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(AGREEMENT_STARTER)
    console.print(f"[green]✓[/green] Agreement written to [cyan]{path}[/cyan]")


@agreement_cmd.command(name="check")
@click.argument("action")
@click.option("--actor", default="claude-code", show_default=True, help="Actor to check.")
@click.option("--scope", default="none", show_default=True, help="Scope to check.")
def agreement_check(action, actor, scope):
    """Dry-run the Agreement decision for ACTION. Exit 0 on allow, 1 on deny."""
    decision = agreements.check_action(actor, action, scope)
    verdict = "[green]ALLOW[/green]" if decision.allowed else "[red]DENY[/red]"
    console.print(f"{verdict}  mode=[bold]{decision.mode}[/bold]")
    if decision.rule:
        console.print(f"  Rule:   [dim]{decision.rule}[/dim]")
    console.print(f"  Reason: {decision.reason}")
    sys.exit(0 if decision.allowed else 1)


@agreement_cmd.command(name="show")
def agreement_show():
    """Print the current Agreement file."""
    text = agreements.load_agreement()
    if text is None:
        console.print(
            f"[dim]No Agreement exists at {agreements.AGREEMENT_PATH}. "
            f"Run: seshat agreement init[/dim]"
        )
        return
    console.print(text)


cli.add_command(agreement_cmd, name="agreement")


# ── Receipts command ────────────────────────────────────────────────────────

@cli.group(invoke_without_command=True)
@click.option("--tail", is_flag=True, default=False, help="Live-follow new receipts.")
@click.option("--limit", default=20, show_default=True, help="Number of receipts to show.")
@click.option("--action", default=None, help="Filter by action name.")
@click.pass_context
def receipts(ctx, tail, limit, action):
    """Show or sync machine-action receipts from ~/.seshat/receipts/."""
    if ctx.invoked_subcommand is not None:
        return
    if tail:
        _tail_receipts(action)
        return
    _print_receipts(limit=limit, action_filter=action)


def _print_receipts(limit: int, action_filter: str | None) -> None:
    rows = receipts_module.load(limit=limit, action_filter=action_filter)
    if not rows:
        console.print("[dim]No receipts found.[/dim]")
        return

    table = Table(show_header=True, header_style="dim", box=None, padding=(0, 2))
    table.add_column("",       width=2)
    table.add_column("Time",   style="dim",     min_width=18)
    table.add_column("Action", style="bold",    min_width=20)
    table.add_column("Target", style="dim",     min_width=16)
    table.add_column("Actor",  style="magenta", min_width=14)

    for r in rows:
        is_success  = r.get("result", {}).get("status") == "success"
        glyph       = Text("✓", style="green") if is_success else Text("✗", style="red")
        ts          = r.get("timestamp", "")[:19].replace("T", " ")
        actor       = r.get("actor", {})
        session_id  = actor.get("session_id", "")
        short_id    = session_id[:14] if session_id else "—"
        target      = r.get("target", {})
        target_str  = target.get("project") or target.get("group") or target.get("key") or "—"
        table.add_row(glyph, ts, r.get("action", ""), target_str, short_id)

    console.print(table)


def _tail_receipts(action_filter: str | None) -> None:
    """Live-follow receipt files as they are written."""
    console.print("[dim]Tailing receipts… Ctrl-C to stop.[/dim]")
    seen = set(receipts_module.RECEIPTS_DIR.glob("*.json"))
    try:
        while True:
            time.sleep(1)
            current = set(receipts_module.RECEIPTS_DIR.glob("*.json"))
            new     = sorted(current - seen)
            for f in new:
                try:
                    r           = json.loads(f.read_text())
                    if action_filter and r.get("action") != action_filter:
                        continue
                    is_success  = r.get("result", {}).get("status") == "success"
                    glyph       = "[green]✓[/green]" if is_success else "[red]✗[/red]"
                    ts          = r.get("timestamp", "")[:19].replace("T", " ")
                    action      = r.get("action", "")
                    target      = r.get("target", {})
                    target_str  = target.get("project") or target.get("group") or "—"
                    console.print(f"{glyph} [dim]{ts}[/dim]  [bold]{action}[/bold]  [dim]{target_str}[/dim]")
                except (json.JSONDecodeError, OSError):
                    pass
            seen = current
    except KeyboardInterrupt:
        pass


LAST_SYNCED_PATH = receipts_module.RECEIPTS_DIR / ".last_synced"
RECEIPTS_API_DEFAULT = "https://liminate.dev"


def _read_last_synced() -> str | None:
    """Return the filename recorded in .last_synced, or None."""
    try:
        return LAST_SYNCED_PATH.read_text().strip() or None
    except FileNotFoundError:
        return None


def _write_last_synced(filename: str) -> None:
    """Record the filename of the last successfully synced receipt."""
    LAST_SYNCED_PATH.write_text(filename + "\n")


def _unsent_receipts() -> list[tuple[str, dict]]:
    """Return (filename, receipt_dict) pairs for all unsent receipts, in order."""
    last_synced = _read_last_synced()
    files = sorted(receipts_module.RECEIPTS_DIR.glob("*.json"))
    results = []
    past_marker = last_synced is None
    for f in files:
        if not past_marker:
            if f.name == last_synced:
                past_marker = True
            continue
        try:
            receipt = json.loads(f.read_text())
            results.append((f.name, receipt))
        except (json.JSONDecodeError, OSError):
            continue
    return results


@receipts.command(name="sync")
@click.option("--dry-run", is_flag=True, default=False, help="Show what would be synced without sending.")
def receipts_sync(dry_run):
    """Push unsent receipts to the Receipts API at liminate.dev."""
    import httpx

    api_key = vault.get("__receipts_api_key__")
    if not api_key and not dry_run:
        console.print(
            "[red]No Receipts API key configured.[/red]\n"
            "  Set one with: [cyan]seshat vault set __RECEIPTS_API_KEY__ <your-key>[/cyan]\n"
            "  Get a key at: [cyan]https://liminate.dev/keys[/cyan]"
        )
        sys.exit(1)

    unsent = _unsent_receipts()
    if not unsent:
        console.print("[dim]All receipts are synced.[/dim]")
        return

    console.print(f"[bold]{len(unsent)}[/bold] unsent receipt(s) found.")

    if dry_run:
        for filename, receipt in unsent:
            ts = receipt.get("timestamp", "")[:19].replace("T", " ")
            action = receipt.get("action", "")
            console.print(f"  [dim]{ts}[/dim]  [bold]{action}[/bold]  [dim]{filename}[/dim]")
        return

    api_base = os.environ.get("SESHAT_RECEIPTS_API", RECEIPTS_API_DEFAULT)
    url = f"{api_base}/api/v1/ingest"

    # Batch all unsent receipts into a single POST.
    payload = {
        "receipts": [r for _, r in unsent],
        "source": "seshat",
        "session_id": SESSION_ID,
    }

    try:
        resp = httpx.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Sync failed:[/red] HTTP {e.response.status_code}")
        try:
            detail = e.response.json().get("detail", str(e))
        except Exception:
            detail = str(e)
        console.print(f"  [dim]{detail}[/dim]")
        sys.exit(1)
    except httpx.RequestError as e:
        console.print(f"[red]Sync failed:[/red] {e}")
        sys.exit(1)

    # Record the last synced receipt.
    last_filename = unsent[-1][0]
    _write_last_synced(last_filename)

    result = resp.json()
    ingested = result.get("ingested", len(unsent))
    console.print(f"[green]✓[/green] {ingested} receipt(s) synced to {api_base}")


@receipts.command(name="verify")
def receipts_verify():
    """Verify the local receipt hash chain integrity."""
    files = sorted(receipts_module.RECEIPTS_DIR.glob("*.json"))
    if not files:
        console.print("[dim]No receipts to verify.[/dim]")
        return

    expected_previous: str | None = None
    total = 0
    broken_at: str | None = None

    for f in files:
        total += 1
        try:
            receipt = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            broken_at = f.name
            console.print(f"[red]✗[/red] {f.name} — unreadable")
            break

        # Check previous_hash linkage.
        actual_previous = receipt.get("previous_hash")
        if actual_previous != expected_previous:
            broken_at = f.name
            console.print(
                f"[red]✗[/red] {f.name} — chain break\n"
                f"  Expected previous_hash: [dim]{expected_previous}[/dim]\n"
                f"  Actual previous_hash:   [dim]{actual_previous}[/dim]"
            )
            break

        # Verify receipt_hash by recomputing.
        stored_hash = receipt.get("receipt_hash")
        verify_copy = {k: v for k, v in receipt.items() if k != "receipt_hash"}
        canonical = json.dumps(verify_copy, sort_keys=True, separators=(",", ":"))
        computed_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        if computed_hash != stored_hash:
            broken_at = f.name
            console.print(
                f"[red]✗[/red] {f.name} — hash mismatch (receipt was modified)\n"
                f"  Stored:   [dim]{stored_hash}[/dim]\n"
                f"  Computed: [dim]{computed_hash}[/dim]"
            )
            break

        expected_previous = stored_hash

    if broken_at is None:
        console.print(f"[green]✓[/green] Chain intact — {total} receipt(s) verified.")
    else:
        console.print(f"\n[yellow]Chain broken at receipt {total} of {len(files)}.[/yellow]")


# ── Revocations command ─────────────────────────────────────────────────────

REVOCATIONS_API_DEFAULT = "https://liminate.dev"


@cli.group()
def revocations_cmd():
    """Revocation registry management (~/.seshat/revocations.limn)."""


@revocations_cmd.command(name="show")
def revocations_show():
    """Print the current revocations file."""
    text = agreements.load_revocations()
    if text is None:
        console.print(
            f"[dim]No revocations file. It is written by `seshat revocations sync` "
            f"from the platform registry.[/dim]"
        )
        return

    error = agreements._validate_forbid_only(text)
    if error is not None:
        console.print(f"[yellow]Invalid revocations.limn: {error}[/yellow]")
    console.print(text)


@revocations_cmd.command(name="sync")
@click.option("--dry-run", is_flag=True, default=False, help="Show what would change without writing.")
def revocations_sync(dry_run):
    """Pull the current revocation set from the platform registry."""
    import httpx

    api_key = vault.get("__receipts_api_key__")
    if not api_key:
        console.print(
            "[red]No Receipts API key configured.[/red]\n"
            "  Set one with: [cyan]seshat vault set __RECEIPTS_API_KEY__ <your-key>[/cyan]\n"
            "  Get a key at: [cyan]https://liminate.dev/keys[/cyan]"
        )
        sys.exit(1)

    api_base = os.environ.get("SESHAT_RECEIPTS_API", REVOCATIONS_API_DEFAULT)
    url = f"{api_base}/api/v1/revocations"

    old_text = agreements.load_revocations()
    old_hash = hashlib.sha256(old_text.encode("utf-8")).hexdigest() if old_text is not None else None

    try:
        resp = httpx.get(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        # Fail-open: an unreachable/erroring registry leaves the last-known
        # revocations.limn in force. Never delete or blank it on transport error.
        console.print(
            f"[yellow]Warning:[/yellow] revocations sync failed (HTTP {e.response.status_code}); "
            f"local revocation set may be stale."
        )
        sys.exit(1)
    except httpx.RequestError as e:
        console.print(
            f"[yellow]Warning:[/yellow] revocations sync failed ({e}); "
            f"local revocation set may be stale."
        )
        sys.exit(1)

    data = resp.json()
    new_text = data.get("revocations_limn", "")
    # Self-computed, not the platform's claimed head_hash: this must use the
    # same method as agreements.revocation_state() (sha256 of actual content)
    # so the changed-content comparison against old_hash is apples-to-apples,
    # and so the receipt below can't be spoofed by a mismatched claimed hash.
    new_hash = hashlib.sha256(new_text.encode("utf-8")).hexdigest()

    if dry_run:
        line_count = len(new_text.splitlines())
        console.print(f"Would sync: [dim]{old_hash or '(none)'}[/dim] → [cyan]{new_hash}[/cyan]  ({line_count} line(s))")
        return

    changed = new_hash != old_hash

    agreements.REVOCATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    agreements.REVOCATIONS_PATH.write_text(new_text)

    agreements.LAST_SYNCED_REVOCATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    agreements.LAST_SYNCED_REVOCATIONS_PATH.write_text(datetime.now(timezone.utc).isoformat() + "\n")

    console.print(f"[green]✓[/green] revocations.limn synced from {api_base}")

    if changed:
        env = receipts_module.snapshot()
        _emit(
            action="apply_revocations",
            target={"head_hash": new_hash},
            result={"status": "success"},
            env_before=env,
            session_id=SESSION_ID,
            actor_type="cli_session",
            agent_hint=os.environ.get("MCP_AGENT_HINT", "cli"),
            env_after=env,
        )


cli.add_command(revocations_cmd, name="revocations")


# ── Serve and MCP commands ──────────────────────────────────────────────────

@cli.command()
@click.option("--port", default=9000, show_default=True, help="Port for the Flask dashboard.")
def serve(port):
    """Start the Seshat web dashboard."""
    console.print(f"[green]Starting Seshat dashboard at http://localhost:{port}[/green]")
    import seshat as seshat_app
    seshat_app.app.run(host="0.0.0.0", port=port, debug=False)


@cli.command()
def mcp():
    """Start the Seshat MCP server (stdio transport)."""
    Console(stderr=True).print("[dim]Starting Seshat MCP server (stdio)…[/dim]")
    import mcp_server
    mcp_server.mcp.run(transport="stdio")


# ── TUI command ─────────────────────────────────────────────────────────────

@cli.command()
def tui():
    """Launch the interactive TUI."""
    _launch_tui()


def _launch_tui():
    from seshat_tui import SeshatApp
    SeshatApp().run()


# ── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli()
