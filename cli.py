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

import base64
import difflib
import hashlib
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import click
from cryptography.hazmat.primitives.asymmetric import ed25519
from rich.console import Console
from rich.table import Table
from rich.text import Text

from registry import Registry
from runner import Runner
from vault import RECEIPTS_API_KEY_VAULT_KEY, Vault
from scanner import Scanner
import amendment_diff
import deps as deps_module
import agreements
import receipts as receipts_module
import invariant_check
import identity

# ── Module instances ────────────────────────────────────────────────────────

registry = Registry()
runner   = Runner()
vault    = Vault()
scanner  = Scanner()

# ── Session identity ────────────────────────────────────────────────────────

SESSION_ID = f"cli_{uuid.uuid4().hex[:12]}"

console = Console()


def _emit(**kwargs) -> dict:
    """Wrap receipts.emit(), injecting revocation_state, agreement_hash, and
    (post-action) the Invariant verification block so every CLI-emitted
    receipt carries them from one source (§7 invariant 4). Returns the
    written receipt dict (receipts.emit()'s return value)."""
    env_after = kwargs.get("env_after") or receipts_module.snapshot()
    kwargs["env_after"] = env_after
    return receipts_module.emit(
        revocation_state=agreements.revocation_state(),
        agreement_hash=agreements.agreement_hash(),
        invariant=invariant_check.run_verification(env_after),
        **kwargs,
    )


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


def _validate_agreement_source(source: str) -> list:
    """Run SOURCE through the interpreter and return blocking errors (empty
    list = valid). Unbound-reference "errors" ("I can't find 'X'") are
    expected here — an Agreement references facts (actor/action/scope)
    supplied only at enforcement time — so they are never blocking."""
    import liminate

    result = liminate.run(source)
    return [
        r for r in result.results
        if r.status.name in ("ERROR_PARSE", "ERROR_SEMANTIC")
        and not (r.status.name == "ERROR_SEMANTIC" and r.message and "I can't find" in r.message)
    ]


# ── Agreement lint ──────────────────────────────────────────────────────────
#
# The gap this closes: `permit action is "start_projct"` — one transposed
# letter — is a perfectly valid Liminate program that silently means
# deny-forever, and a silently-denying Agreement looks exactly like a
# working one. Enforcement cannot catch this (a permit that never matches
# is indistinguishable from a permit that shouldn't), so it is caught here,
# before the file reaches the enforcement surface.
#
# Lint is ADVISORY at enforcement time: check_action neither calls nor
# knows about any of this. Zero runtime coupling.

# A fact reference is a bare name in comparison position. Matches the
# subject of `is` / `includes` / `not includes`, which is every shape a
# condition can take against a fact.
_LINT_FACT_RE = re.compile(r"\b([a-z][a-z0-9_-]*)\s+(?:is\b|(?:not\s+)?includes\b)")
_LINT_ACTION_LITERAL_RE = re.compile(r'\baction\s+is\s+"([^"]*)"')
_LINT_SCOPE_LITERAL_RE = re.compile(r'\bscope\s+is\s+"([^"]*)"')
_LINT_REMEMBER_RE = re.compile(r"^remember\s+an?\s+\S+\s+called\s+([\w-]+)")
_LINT_QUOTED_RE = re.compile(r'"[^"]*"')

# scope's sentinel when a call has no project/group target — always valid,
# never a registry name.
_LINT_SCOPE_SENTINEL = "none"


class LintFinding:
    """One lint result. `severity` is "error" (blocks) or "warning" (informs)."""

    def __init__(self, severity: str, line: int | None, message: str, suggestion: str | None = None):
        self.severity = severity
        self.line = line
        self.message = message
        self.suggestion = suggestion

    def __repr__(self) -> str:
        return f"LintFinding({self.severity!r}, line={self.line}, {self.message!r})"


def _lint_known_actions() -> set:
    """Registered MCP action names, read LIVE from the server module.

    Never a hardcoded list: a literal copy here would drift the moment a
    tool is added or renamed, and a linter that is confidently wrong about
    the tool vocabulary is worse than no linter. Imported lazily so the
    ordinary CLI path never pays for loading the MCP stack.
    """
    try:
        import mcp_server
        return mcp_server.enforced_actions()
    except Exception:
        return set()


def _lint_known_scopes() -> set:
    """Registered project and group names. Best-effort: an unreadable
    registry yields an empty set, which only suppresses scope warnings —
    it never manufactures an error."""
    names = set()
    try:
        names.update(p["name"] for p in registry.list() if "name" in p)
        names.update(g["name"] for g in registry.list_groups() if "name" in g)
    except Exception:
        pass
    return names


def _lint_statements(source: str):
    """Yield (line_number, statement) for each real statement.

    parse_statements() skips blank and '#' lines but NOT Liminate's '--'
    comments, which it would classify as verb 'other' — so those are
    filtered here rather than reported as unparseable. The temporal prefix
    is stripped before parsing via identity._strip_temporal_prefix (the
    same helper agreements._revoked_actor_identifiers uses), since
    `starting "..." permit ...` would otherwise parse as verb 'other' and
    hide a real permit from every check below.
    """
    for lineno, raw in enumerate(source.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("--") or stripped.startswith("#"):
            continue
        remainder = identity._strip_temporal_prefix(stripped)
        parsed = amendment_diff.parse_statements(remainder)
        if parsed:
            yield lineno, parsed[0]


def _lint_derationalized(statement: dict) -> str:
    """The statement text with any `because "..."` rationale removed.

    The rationale is free prose, so scanning it would invent facts and
    actions that aren't there — `because "action is unclear"` must not
    read as a reference to the `action` fact, nor as an action literal."""
    text = statement["raw"]
    if statement.get("rationale") is not None:
        m = amendment_diff._BECAUSE_RE.search(text)
        if m:
            text = text[: m.start()]
    return text


def lint_agreement(source: str, *, known_actions=None, known_scopes=None) -> list:
    """Return LintFindings for SOURCE, most structural first.

    Errors (block an install, exit 1):
      - any parse/semantic error the interpreter reports
      - a reference to a fact name that cannot resolve at enforcement time

    Warnings (informational, exit 0):
      - `action is "<literal>"` naming no registered MCP tool
      - `scope is "<literal>"` naming no registered project or group
      - no permit statements at all — the Agreement denies everything
    """
    if known_actions is None:
        known_actions = _lint_known_actions()
    if known_scopes is None:
        known_scopes = _lint_known_scopes()

    findings = []

    for r in _validate_agreement_source(source):
        loc = getattr(r, "line", None)
        findings.append(LintFinding("error", loc, r.message or "interpreter error"))

    statements = list(_lint_statements(source))

    # A name bound by a `remember` inside the Agreement itself resolves at
    # enforcement time just as the harness-supplied facts do, so it counts
    # as known — otherwise a self-contained Agreement would lint as broken.
    defined = set()
    for _lineno, stmt in statements:
        m = _LINT_REMEMBER_RE.match(stmt["raw"].strip())
        if m:
            defined.add(m.group(1))
    known_facts = agreements.KNOWN_FACTS | defined

    has_permit = False
    for lineno, stmt in statements:
        if stmt["verb"] == "permit":
            has_permit = True
        if stmt["verb"] not in ("permit", "forbid", "require"):
            continue

        conditions = _lint_derationalized(stmt)
        # Quoted literals are blanked for the FACT scan only: a string like
        # "start_project" must not read as a bare name in comparison
        # position. The literal scans below need the quotes intact.
        bare = _LINT_QUOTED_RE.sub('""', conditions)

        for fact in _LINT_FACT_RE.findall(bare):
            if fact in known_facts:
                continue
            suggestion = difflib.get_close_matches(fact, sorted(known_facts), n=1, cutoff=0.6)
            findings.append(LintFinding(
                "error", lineno,
                f'unknown fact "{fact}" — it cannot resolve at enforcement time',
                f'Did you mean "{suggestion[0]}"?' if suggestion else None,
            ))

        for literal in _LINT_ACTION_LITERAL_RE.findall(conditions):
            if not known_actions or literal in known_actions:
                continue
            suggestion = difflib.get_close_matches(literal, sorted(known_actions), n=1, cutoff=0.6)
            findings.append(LintFinding(
                "warning", lineno,
                f'action "{literal}" is not a registered MCP tool — this rule can never match',
                f'Did you mean "{suggestion[0]}"?' if suggestion else None,
            ))

        for literal in _LINT_SCOPE_LITERAL_RE.findall(conditions):
            if literal == _LINT_SCOPE_SENTINEL or not known_scopes or literal in known_scopes:
                continue
            suggestion = difflib.get_close_matches(literal, sorted(known_scopes), n=1, cutoff=0.6)
            findings.append(LintFinding(
                "warning", lineno,
                f'scope "{literal}" names no registered project or group',
                f'Did you mean "{suggestion[0]}"?' if suggestion else None,
            ))

    if statements and not has_permit:
        findings.append(LintFinding(
            "warning", None,
            "no permit statements — this Agreement denies everything (deny-by-default)",
        ))

    return findings


def _print_lint_findings(findings: list) -> None:
    for f in findings:
        colour = "red" if f.severity == "error" else "yellow"
        loc = f"line {f.line}: " if f.line else ""
        console.print(f"  [{colour}]{f.severity}[/{colour}]  {loc}{f.message}")
        if f.suggestion:
            console.print(f"         [dim]{f.suggestion}[/dim]")


def _print_validation_errors(blocking: list) -> None:
    for r in blocking:
        loc = f"line {r.line}: " if getattr(r, "line", None) else ""
        console.print(f"  [red]{loc}{r.message}[/red]")


def _find_receipt_by_hash(receipt_hash: str) -> dict | None:
    """Scan ~/.seshat/receipts/ for the receipt whose receipt_hash matches.
    Newest first, since a duplicate hash cannot occur (receipts.emit() chains
    on it), but a fresh receipt is the more likely lookup."""
    for f in sorted(receipts_module.RECEIPTS_DIR.glob("*.json"), reverse=True):
        try:
            r = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if r.get("receipt_hash") == receipt_hash:
            return r
    return None


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

    console.print(f"\n  [dim]Need to turn an existing policy into an Agreement? Try [cyan]liminate.dev/translate[/cyan][/dim]")


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


@agreement_cmd.command(name="lint")
@click.option(
    "--path", "path", default=None,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="Lint this file instead of the installed Agreement.",
)
def agreement_lint(path):
    """Check an Agreement for typos and unresolvable references.

    Catches what enforcement cannot: `permit action is "start_projct"` is a
    valid program that silently means deny-forever, and a silently-denying
    Agreement is indistinguishable from a working one at runtime.

    Exit 0 when clean or warnings-only, 1 when errors are found — so this
    is usable as a CI gate.
    """
    if path is not None:
        try:
            source = Path(path).read_text()
        except UnicodeDecodeError as exc:
            console.print(f"[red]Could not read {path} as text.[/red] ({exc})")
            sys.exit(1)
        label = str(path)
    else:
        source = agreements.load_agreement()
        if source is None:
            console.print(
                f"[dim]No Agreement exists at {agreements.AGREEMENT_PATH}. "
                f"Run: seshat agreement init[/dim]"
            )
            return
        label = str(agreements.AGREEMENT_PATH)

    findings = lint_agreement(source)
    errors = [f for f in findings if f.severity == "error"]
    warnings = [f for f in findings if f.severity == "warning"]

    if not findings:
        console.print(f"[green]✓[/green] {label} — no issues found.")
        return

    _print_lint_findings(findings)
    summary = f"{len(errors)} error(s), {len(warnings)} warning(s)"
    console.print(f"\n  [{'red' if errors else 'yellow'}]{summary}[/]")
    sys.exit(1 if errors else 0)


@agreement_cmd.command(name="install")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, readable=True))
@click.option("--force", is_flag=True, default=False, help="Overwrite an existing Agreement file.")
def agreement_install(path, force):
    """Validate a Liminate Agreement file and install it to ~/.seshat/agreement.limn.

    Reads PATH, checks it parses cleanly through the interpreter (a broken
    Agreement is never installed), and writes it to the enforcement surface.
    This is a human action at the terminal — it is deliberately not gated by
    the Agreement itself (that gate applies to agent MCP calls, not to you).
    """
    dest = agreements.AGREEMENT_PATH
    if dest.exists() and not force:
        console.print(f"[yellow]Agreement already exists at {dest}.[/yellow] Use --force to overwrite.")
        sys.exit(1)

    try:
        source = Path(path).read_text()
    except UnicodeDecodeError as exc:
        console.print(f"[red]Could not read {path} as text — not installed.[/red] ({exc})")
        sys.exit(1)

    # Validate through the interpreter before writing. Any parse/semantic error
    # blocks the install — a broken Agreement must never reach the enforcement
    # surface.
    blocking = _validate_agreement_source(source)
    if blocking:
        console.print(f"[red]Agreement did not validate — not installed.[/red]")
        _print_validation_errors(blocking)
        sys.exit(1)

    # Lint after validation. Errors block, extending the existing rule that
    # a broken Agreement never reaches the enforcement surface — a rule
    # referencing a fact that cannot resolve IS broken, it just fails
    # silently instead of loudly. Warnings print but never block: a typo'd
    # action is a suspicion about intent, not a defect in the file.
    findings = lint_agreement(source)
    lint_errors = [f for f in findings if f.severity == "error"]
    if lint_errors:
        console.print("[red]Agreement failed lint — not installed.[/red]")
        _print_lint_findings(lint_errors)
        sys.exit(1)
    if findings:
        _print_lint_findings(findings)

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(source)
    console.print(f"[green]✓[/green] Agreement installed to [cyan]{dest}[/cyan]")


@agreement_cmd.command(name="amend")
@click.option(
    "--apply", "receipt_id", required=True,
    help="Apply a previously proposed amend_agreement receipt by its receipt id (receipt_hash).",
)
@click.option(
    "--allow-deescalation", is_flag=True, default=False,
    help="Required to apply a de-escalating (privilege-granting) amendment.",
)
def agreement_amend(receipt_id, allow_deescalation):
    """Apply a previously proposed amend_agreement receipt to the live Agreement.

    Human terminal action (TI-Q6c) — only a human writes agreement.limn; the
    amend_agreement MCP tool only proposes. This command always re-derives
    the classification against the CURRENT Agreement and entrenched.limn at
    apply time — it never trusts the proposal receipt's stored (harness-
    attested) classification, since either file may have changed since the
    proposal was made.
    """
    proposal = _find_receipt_by_hash(receipt_id)
    if proposal is None:
        console.print(f"[red]No receipt found with id {receipt_id}.[/red]")
        sys.exit(1)
    if proposal.get("action") != "amend_agreement":
        console.print(
            f"[red]Receipt {receipt_id} is not an amend_agreement proposal "
            f"(action={proposal.get('action')!r}).[/red]"
        )
        sys.exit(1)

    target = proposal.get("target") or {}
    additions = target.get("additions", [])
    removals = target.get("removals", [])

    current_src = agreements.load_agreement() or ""
    hash_before = agreements.agreement_hash()

    after_src = amendment_diff.apply_delta(current_src, additions, removals)
    hash_after = hashlib.sha256(after_src.encode("utf-8")).hexdigest()

    classification = amendment_diff.classify_amendment(
        current_src, after_src, agreements.entrenched_keys()
    )
    cls = classification["class"]

    if cls == "entrenched-violation":
        console.print(
            "[red]Refused: this amendment touches an entrenched rule and cannot be applied.[/red]"
        )
        for verb, subject in classification["violations"]:
            console.print(f"  [red]entrenched: {verb} {subject}[/red]")
        sys.exit(1)

    if cls == "de-escalating" and not allow_deescalation:
        console.print(
            "[yellow]This amendment removes or loosens a restriction (de-escalating).[/yellow]\n"
            "  Re-run with [cyan]--allow-deescalation[/cyan] to apply it."
        )
        sys.exit(1)

    # Validate through the interpreter before writing — a broken Agreement
    # never reaches the enforcement surface (mirrors `agreement install`).
    blocking = _validate_agreement_source(after_src)
    if blocking:
        console.print("[red]Amended Agreement did not validate — not applied.[/red]")
        _print_validation_errors(blocking)
        sys.exit(1)

    env_before = receipts_module.snapshot()
    agreements.AGREEMENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    agreements.AGREEMENT_PATH.write_text(after_src)

    _emit(
        action="apply_amendment",
        target={
            "proposal_receipt_id": receipt_id,
            "agreement_hash_before": hash_before,
            "agreement_hash_after": hash_after,
            "proposed_delta": amendment_diff.diff_statements(current_src, after_src),
        },
        result={
            "status": "applied",
            "classification": cls,
            "applied_by": os.environ.get("USER") or os.environ.get("USERNAME") or "unknown",
        },
        env_before=env_before,
        session_id=SESSION_ID,
        actor_type="cli_session",
        agent_hint=os.environ.get("MCP_AGENT_HINT", "cli"),
    )

    console.print(
        f"[green]✓[/green] Amendment applied ([bold]{cls}[/bold]). "
        f"Agreement written to [cyan]{agreements.AGREEMENT_PATH}[/cyan]"
    )


cli.add_command(agreement_cmd, name="agreement")


# ── Teams commands ─────────────────────────────────────────────────────────

TEAMS_STARTER = """\
-- Seshat Teams — actor membership, resolved by the harness at enforcement time.
-- Convention: `<team>-members` lists actor names; `<team>-parents` lists
-- parent team names. The harness computes the transitive closure and binds
-- it as the `actor-teams` fact. remember-statements only — nothing else
-- belongs in this file.

remember a list called engineering-members with "claude-code"
remember a list called engineering-parents
"""


def _validate_teams_source(source: str) -> list:
    """Teams files are self-contained programs — unlike Agreements, they
    reference no enforcement-time facts, so NO unbound-reference
    forgiveness applies. Any parse/semantic/runtime error blocks install.
    Additionally, only `remember` statements are allowed: a deontic or
    mutation verb in teams.limn would execute on every resolution run."""
    import liminate

    result = liminate.run(source, enter_phase2=False, auto_confirm_amber=True)
    blocking = [
        r for r in result.results
        if r.status.name in ("ERROR_PARSE", "ERROR_SEMANTIC", "ERROR_RUNTIME",
                             "AMBER_PRECEDENCE", "AMBER_AMBIGUITY")
    ]
    for r in result.results:
        verb = agreements._verb_of(r.canonical)
        if verb is not None and verb != "remember" and r not in blocking:
            blocking.append(r)   # non-remember statement — report it
    return blocking


def _print_teams_validation_errors(blocking: list) -> None:
    """Like _print_validation_errors, but synthesizes a message for a
    non-remember statement — those results carry no interpreter error
    message of their own (they executed fine; they just don't belong)."""
    for r in blocking:
        loc = f"line {r.line}: " if getattr(r, "line", None) else ""
        message = r.message or (
            f"only remember statements are allowed in teams.limn, got: {r.canonical}"
        )
        console.print(f"  [red]{loc}{message}[/red]")


@cli.group()
def teams_cmd():
    """Actor-membership teams feeding the `actor-teams` enforcement fact."""


@teams_cmd.command(name="init")
@click.option("--force", is_flag=True, default=False, help="Overwrite an existing teams file.")
def teams_init(force):
    """Write the starter teams file to ~/.seshat/teams.limn."""
    path = agreements.TEAMS_PATH
    if path.exists() and not force:
        console.print(f"[yellow]Teams file already exists at {path}.[/yellow] Use --force to overwrite.")
        sys.exit(1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEAMS_STARTER)
    console.print(f"[green]✓[/green] Teams written to [cyan]{path}[/cyan]")


@teams_cmd.command(name="show")
def teams_show():
    """Print the current teams file."""
    text = agreements.load_teams()
    if text is None:
        console.print(
            f"[dim]No teams file exists at {agreements.TEAMS_PATH}. "
            f"Run: seshat teams init[/dim]"
        )
        return
    console.print(text)


@teams_cmd.command(name="install")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, readable=True))
@click.option("--force", is_flag=True, default=False, help="Overwrite an existing teams file.")
def teams_install(path, force):
    """Validate a Liminate teams file and install it to ~/.seshat/teams.limn.

    Reads PATH, checks it runs cleanly through the interpreter and contains
    only `remember` statements (a broken or deontic teams file is never
    installed), and writes it to the enforcement surface. This is a human
    action at the terminal — like `agreement install`, it is deliberately
    not gated by the Agreement itself; the filesystem boundary is the
    protection. No agent-reachable path writes this file.
    """
    dest = agreements.TEAMS_PATH
    if dest.exists() and not force:
        console.print(f"[yellow]Teams file already exists at {dest}.[/yellow] Use --force to overwrite.")
        sys.exit(1)

    try:
        source = Path(path).read_text()
    except UnicodeDecodeError as exc:
        console.print(f"[red]Could not read {path} as text — not installed.[/red] ({exc})")
        sys.exit(1)

    blocking = _validate_teams_source(source)
    if blocking:
        console.print(f"[red]Teams file did not validate — not installed.[/red]")
        _print_teams_validation_errors(blocking)
        sys.exit(1)

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(source)
    console.print(f"[green]✓[/green] Teams installed to [cyan]{dest}[/cyan]")


@teams_cmd.command(name="check")
@click.argument("actor")
def teams_check(actor):
    """Print the resolved transitive team closure for ACTOR.

    The diagnostic for team resolution: a missing or broken teams.limn
    fails safe to an empty closure (grants fail closed), which is silent
    at enforcement time — this is where you see it.
    """
    resolved = agreements.resolve_teams(actor)
    if not resolved:
        console.print(f"[dim]{actor} belongs to no teams.[/dim]")
        return
    for team in resolved:
        console.print(team)


cli.add_command(teams_cmd, name="teams")


# ── Invariant commands ──────────────────────────────────────────────────────

INVARIANT_STARTER = """\
-- Seshat Invariant — post-action verification claims.
-- These check environment correctness AFTER a permitted action runs.
-- Unlike the Agreement (which grants/denies permission), these verify state.
-- Claims that fail are recorded on the receipt; they do NOT block actions.

-- Example: require that the vault project stays reachable.
-- require source is "environment" and status is "ok"
"""


@cli.group()
def invariant_cmd():
    """Invariant post-action verification management (~/.seshat/invariant.limn)."""


@invariant_cmd.command(name="init")
@click.option("--force", is_flag=True, default=False, help="Overwrite an existing Invariant contract.")
def invariant_init(force):
    """Write the starter Invariant contract to ~/.seshat/invariant.limn."""
    path = agreements.INVARIANT_PATH
    if path.exists() and not force:
        console.print(f"[yellow]Invariant contract already exists at {path}.[/yellow] Use --force to overwrite.")
        sys.exit(1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(INVARIANT_STARTER)
    console.print(f"[green]✓[/green] Invariant contract written to [cyan]{path}[/cyan]")


@invariant_cmd.command(name="show")
def invariant_show():
    """Print the current Invariant contract."""
    text = agreements.load_invariant()
    if text is None:
        console.print(
            f"[dim]No Invariant contract exists at {agreements.INVARIANT_PATH}. "
            f"Run: seshat invariant init[/dim]"
        )
        return
    console.print(text)


@invariant_cmd.command(name="check")
def invariant_check_cmd():
    """Run Invariant verification against the current environment and print per-claim outcomes."""
    if agreements.load_invariant() is None:
        console.print(
            f"[dim]No Invariant contract exists at {agreements.INVARIANT_PATH}. "
            f"Run: seshat invariant init[/dim]"
        )
        return

    block = invariant_check.run_verification(receipts_module.snapshot())
    if block is None:
        console.print("[yellow]Invariant harness unavailable (liminate-invariant not installed).[/yellow]")
        return

    if "error" in block:
        console.print(f"[red]Invariant harness error:[/red] {block['error']}")
        return

    table = Table(title="Invariant verification")
    table.add_column("Claim")
    table.add_column("Status")
    table.add_column("Cycles")
    table.add_column("Escalation reason")
    style_by_status = {"verified": "green", "corrected": "cyan", "escalated": "red"}
    for claim in block["claims"]:
        status = claim["status"]
        style = style_by_status.get(status, "white")
        table.add_row(
            claim["name"],
            f"[{style}]{status}[/{style}]",
            str(claim["cycles"]),
            claim["escalation_reason"] or "",
        )
    console.print(table)
    console.print(f"converged={block['converged']}  total_cycles={block['total_cycles']}")


cli.add_command(invariant_cmd, name="invariant")


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

    # Sync hint — surface the platform when receipts accumulate
    unsent = _count_unsent()
    if unsent > 0:
        api_key = vault.get(RECEIPTS_API_KEY_VAULT_KEY)
        if api_key:
            console.print(f"\n  [dim]{unsent} receipt(s) not synced. Run [cyan]seshat receipts sync[/cyan] to push to liminate.dev.[/dim]")
        else:
            console.print(f"\n  [dim]{unsent} receipt(s) local only. Run [cyan]seshat receipts sync[/cyan] to back up to liminate.dev (free).[/dim]")


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


def _count_unsent() -> int:
    """Count unsent receipts without parsing JSON — just file count past the marker."""
    last_synced = _read_last_synced()
    files = sorted(receipts_module.RECEIPTS_DIR.glob("*.json"))
    if last_synced is None:
        return len(files)
    past_marker = False
    count = 0
    for f in files:
        if not past_marker:
            if f.name == last_synced:
                past_marker = True
            continue
        count += 1
    return count


@receipts.command(name="sync")
@click.option("--dry-run", is_flag=True, default=False, help="Show what would be synced without sending.")
def receipts_sync(dry_run):
    """Push unsent receipts to the Receipts API at liminate.dev."""
    import httpx

    api_key = vault.get(RECEIPTS_API_KEY_VAULT_KEY)
    if not api_key and not dry_run:
        console.print(
            "[red]No Receipts API key configured.[/red]\n"
            f"  Set one with: [cyan]seshat vault set {RECEIPTS_API_KEY_VAULT_KEY} <your-key>[/cyan]\n"
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

    # Batch all unsent receipts into a single POST. chain_anchor is
    # additive (F-01 B2) — the local head hash + count, so the platform
    # can, in future, pin it per install and let `receipts verify` compare
    # against a remote anchor too. Not yet verified server-side; today it
    # only closes the local-truncation gap (receipts_verify's own
    # .chain_head check), but sending it now means no client change is
    # needed once the platform adds pinning.
    payload = {
        "receipts": [r for _, r in unsent],
        "source": "seshat",
        "session_id": SESSION_ID,
        "chain_anchor": receipts_module._read_chain_head(),
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
@click.option(
    "--pubkey", "pubkey_hex", default=None,
    help="Verify version-3 receipts against this hex Ed25519 public key "
         "instead of the local Keychain — the auditor's path. Works on a "
         "machine that has never held the private key.",
)
def receipts_verify(pubkey_hex):
    """Verify the local receipt hash chain integrity.

    Each receipt is verified according to its receipt_version:

    \b
    - version 3 (ID-Q4 Phase 2): Ed25519 signature check — against the
      local Keychain-backed receipt public key, or, with --pubkey,
      against a supplied public key. The first receipts independently
      verifiable by a third party, not just the emitting machine.
    - version 2: HMAC recompute (F-01), unchanged.
    - absent, before any keyed (version >= 2) receipt: legacy
      plain-sha256, link-verified only, unchanged.
    - absent, after a keyed receipt: forgery, hard fail (unchanged).

    The chain only ever moves forward: an unversioned receipt appearing
    after a keyed one is forgery, not nostalgia, and a version-2 receipt
    appearing after a version-3 one is a downgrade, not a fork — both hard
    fail. With --pubkey, version-2 and unversioned receipts cannot be
    verified by this method (no HMAC key; wrong scheme) and are reported
    as unverifiable rather than as failures — only their chain linkage is
    checked. After the link-walk, the disk state is compared against the
    persisted .chain_head anchor to catch tail-truncation, which a
    self-consistent-but-shorter chain can't reveal on its own.
    """
    files = sorted(receipts_module.RECEIPTS_DIR.glob("*.json"))
    if not files:
        console.print("[dim]No receipts to verify.[/dim]")
        return

    pubkey = None
    if pubkey_hex is not None:
        try:
            pubkey = ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex))
        except Exception as e:
            console.print(f"[red]Invalid --pubkey:[/red] {e}")
            sys.exit(1)

    expected_previous: str | None = None
    total = 0
    broken_at: str | None = None
    highest_version_seen = 0  # 0 = no keyed (version >= 2) receipt seen yet
    legacy_count = 0
    unverifiable_count = 0

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

        stored_hash = receipt.get("receipt_hash")
        verify_copy = {k: v for k, v in receipt.items() if k != "receipt_hash"}
        canonical = json.dumps(verify_copy, sort_keys=True, separators=(",", ":"))

        version = receipt.get("receipt_version")

        if version is not None and version >= 3:
            highest_version_seen = max(highest_version_seen, version)
            if pubkey is not None:
                verifying_key = pubkey
            else:
                try:
                    verifying_key = receipts_module._require_receipt_public_key()
                except receipts_module.ReceiptKeyUnavailableError as exc:
                    broken_at = f.name
                    console.print(f"[red]✗[/red] {f.name} — cannot verify: {exc}")
                    break
            try:
                verifying_key.verify(bytes.fromhex(stored_hash), canonical.encode("utf-8"))
            except Exception:
                broken_at = f.name
                console.print(
                    f"[red]✗[/red] {f.name} — signature verification failed "
                    "(receipt was modified)"
                )
                break

        elif version is not None and version == 2:
            if highest_version_seen >= 3:
                # The chain only ever moves forward — a version-2 receipt
                # can never legitimately follow a version-3 one.
                broken_at = f.name
                console.print(
                    f"[red]✗[/red] {f.name} — downgrade detected: a "
                    "version-2 receipt appears after the chain had already "
                    "graduated to version-3 (Ed25519-signed) receipts"
                )
                break
            highest_version_seen = max(highest_version_seen, 2)
            if pubkey is not None:
                unverifiable_count += 1
                console.print(
                    f"[yellow]?[/yellow] {f.name} — unverifiable-by-this-method: "
                    "a version-2, HMAC-keyed receipt has no Ed25519 signature "
                    "to check against --pubkey. Only chain linkage was checked."
                )
            else:
                try:
                    computed_hash = receipts_module._keyed_hash(canonical)
                except receipts_module.ReceiptKeyUnavailableError as exc:
                    broken_at = f.name
                    console.print(f"[red]✗[/red] {f.name} — cannot verify: {exc}")
                    break
                if computed_hash != stored_hash:
                    broken_at = f.name
                    console.print(
                        f"[red]✗[/red] {f.name} — hash mismatch (receipt was modified)\n"
                        f"  Stored:   [dim]{stored_hash}[/dim]\n"
                        f"  Computed: [dim]{computed_hash}[/dim]"
                    )
                    break

        elif highest_version_seen >= 2:
            # An unversioned receipt can never legitimately appear after
            # the chain already graduated to keyed receipts — the chain
            # never downgrades. Treat as forgery, not legacy.
            broken_at = f.name
            console.print(
                f"[red]✗[/red] {f.name} — possible forgery: unkeyed receipt "
                "appears after the chain had already graduated to keyed "
                "receipts"
            )
            break

        else:
            legacy_count += 1
            if pubkey is not None:
                unverifiable_count += 1
                console.print(
                    f"[yellow]?[/yellow] {f.name} — unverifiable-by-this-method: "
                    "a legacy, unkeyed (plain sha256) receipt has no Ed25519 "
                    "signature to check against --pubkey. Only chain linkage "
                    "was checked."
                )
            else:
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

    if broken_at is not None:
        console.print(f"\n[yellow]Chain broken at receipt {total} of {len(files)}.[/yellow]")
        return

    if legacy_count:
        console.print(
            f"[yellow]⚠[/yellow] {legacy_count} receipt(s) verified via the legacy "
            "unkeyed method (written before chain keying) — link-verified "
            "only, not forgery-resistant."
        )

    if unverifiable_count:
        console.print(
            f"[yellow]⚠[/yellow] {unverifiable_count} receipt(s) are "
            "unverifiable-by-this-method under --pubkey (version-2 HMAC or "
            "legacy plain-hash) — chain linkage was checked, signatures "
            "were not."
        )

    # Anchor check — catches tail-truncation, which the link-walk alone
    # cannot: a truncated chain is still perfectly self-consistent, just
    # shorter. No anchor yet (fresh install, or a legacy chain that has
    # never had a keyed receipt emitted against it) means there is nothing
    # to compare against — that gap closes as soon as the anchor bootstraps
    # from the next emit().
    anchor = receipts_module._read_chain_head()
    if anchor is not None:
        anchor_head = anchor.get("head_hash")
        anchor_count = anchor.get("count", 0)
        if anchor_count != total or anchor_head != expected_previous:
            console.print(
                f"[red]✗[/red] Truncation detected — the chain anchor "
                f"recorded {anchor_count} receipt(s) ending at "
                f"[dim]{anchor_head}[/dim], but only {total} receipt(s) "
                f"ending at [dim]{expected_previous}[/dim] are present on "
                "disk. Receipts were deleted, or never returned after "
                "being written."
            )
            return

    console.print(f"[green]✓[/green] Chain intact — {total} receipt(s) verified.")


@receipts.group(name="keys")
def receipts_keys_cmd():
    """Inspect this install's Ed25519 receipt signing key (public half only)."""


@receipts_keys_cmd.command(name="show")
def receipts_keys_show():
    """Print the receipt signing public key.

    This is the value to hand a third party — an auditor, an enterprise
    SIEM — for independent, cross-org verification of every version-3
    receipt this install emits (via `seshat receipts verify --pubkey`),
    without ever handing over a forging key (ID-Q4 Phase 2's whole
    point). Never prints the private key.
    """
    try:
        pub_hex = receipts_module.receipt_public_key_hex()
    except receipts_module.ReceiptKeyUnavailableError as e:
        console.print(f"[red]Cannot read the receipt public key:[/red] {e}")
        sys.exit(1)

    console.print(pub_hex)
    console.print(
        "\n  [dim]This is the RECEIPT PUBLIC key for this install — safe to share. "
        "Hand it to a third party so they can independently verify receipts this "
        "install emits, without ever needing this install's private key.[/dim]"
    )


@receipts_keys_cmd.command(name="export")
@click.option("--out", "out_path", required=True, type=click.Path(), help="File to write the public key to.")
def receipts_keys_export(out_path):
    """Write the receipt public key to --out, for transmission to a third party."""
    try:
        pub_hex = receipts_module.receipt_public_key_hex()
    except receipts_module.ReceiptKeyUnavailableError as e:
        console.print(f"[red]Cannot read the receipt public key:[/red] {e}")
        sys.exit(1)

    Path(out_path).write_text(pub_hex + "\n")
    console.print(f"[green]✓[/green] Wrote receipt public key to [bold]{out_path}[/bold]")


RECEIPTS_KEYS_REGISTER_PATH = "/api/v1/receipts/keys"


@receipts_keys_cmd.command(name="register")
@click.option("--dry-run", is_flag=True, default=False, help="Show what would be sent without sending.")
@click.option(
    "--rotate", is_flag=True, default=False,
    help="Sign the current public key's raw bytes with the current private key, "
         "proving possession — for re-registering after the platform holds a "
         "stale key for this account.",
)
def receipts_keys_register(dry_run, rotate):
    """Register this install's receipt public key with the platform.

    Sends the key this install already has — idempotent, and normally needs
    no signature. If the platform responds 403, it holds a different key on
    file for this account (a different machine registered it, or this
    install's Keychain key changed since); this command does not attempt to
    resolve that automatically. Pass --rotate to prove current possession by
    signing the public key with its own private key — the deliberate,
    explicit path for re-registering over a stale platform-side key. This
    build does not generate new keypairs; an install has one receipt
    keypair.
    """
    import httpx

    try:
        pub_hex = receipts_module.receipt_public_key_hex()
    except receipts_module.ReceiptKeyUnavailableError as e:
        console.print(f"[red]Cannot read the receipt public key:[/red] {e}")
        sys.exit(1)

    api_key = vault.get(RECEIPTS_API_KEY_VAULT_KEY)
    if not api_key and not dry_run:
        console.print(
            "[red]No Receipts API key configured.[/red]\n"
            f"  Set one with: [cyan]seshat vault set {RECEIPTS_API_KEY_VAULT_KEY} <your-key>[/cyan]\n"
            "  Get a key at: [cyan]https://liminate.dev/keys[/cyan]"
        )
        sys.exit(1)

    api_base = os.environ.get("SESHAT_RECEIPTS_API", RECEIPTS_API_DEFAULT)
    url = f"{api_base}{RECEIPTS_KEYS_REGISTER_PATH}"

    payload = {"public_key": pub_hex}
    if rotate:
        private_key = receipts_module._receipt_signing_key()
        signature = private_key.sign(bytes.fromhex(pub_hex))
        payload["signature"] = signature.hex()

    if dry_run:
        suffix = " [dim](with a proof-of-possession signature)[/dim]" if rotate else ""
        console.print(f"Would register: [cyan]{pub_hex}[/cyan]{suffix} → {url}")
        return

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
        try:
            detail = e.response.json().get("detail", str(e))
        except Exception:
            detail = str(e)

        if e.response.status_code == 403 and not rotate:
            console.print(
                "[red]Registration rejected (403):[/red] the platform has a key on "
                "file for this account that does not match this install's key.\n"
                "  This means either this account was registered from a different "
                "machine, or this install's Keychain key changed.\n"
                "  If this install's key is the correct one, retry with "
                "[cyan]seshat receipts keys register --rotate[/cyan] to prove "
                "current possession with a signature."
            )
        else:
            console.print(f"[red]Registration failed:[/red] HTTP {e.response.status_code}")
            console.print(f"  [dim]{detail}[/dim]")
        sys.exit(1)
    except httpx.RequestError as e:
        console.print(f"[red]Registration failed:[/red] {e}")
        sys.exit(1)

    console.print(f"[green]✓[/green] Registered receipt public key with {api_base}")


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


def _write_revocations(new_text: str) -> None:
    """The one place revocations.limn is ever written — whether the
    content came from a remote `revocations sync` fetch or a direct local
    `identity revoke` edit. Also refreshes the F-07 freshness marker
    (.last_synced_revocations): the write itself IS the freshest possible
    confirmation the content is current, whether it came from a sync or a
    human's own hand at the terminal just now. Skipping this for a local
    edit would make `identity revoke` self-defeating — an
    unsynced-before-now revocations.limn would trigger a blanket
    stale-deny for every action (F-07), not just the revoked identity.
    """
    agreements.REVOCATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    agreements.REVOCATIONS_PATH.write_text(new_text)
    agreements.LAST_SYNCED_REVOCATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    agreements.LAST_SYNCED_REVOCATIONS_PATH.write_text(datetime.now(timezone.utc).isoformat() + "\n")


@revocations_cmd.command(name="sync")
@click.option("--dry-run", is_flag=True, default=False, help="Show what would change without writing.")
def revocations_sync(dry_run):
    """Pull the current revocation set from the platform registry."""
    import httpx

    api_key = vault.get(RECEIPTS_API_KEY_VAULT_KEY)
    if not api_key:
        console.print(
            "[red]No Receipts API key configured.[/red]\n"
            f"  Set one with: [cyan]seshat vault set {RECEIPTS_API_KEY_VAULT_KEY} <your-key>[/cyan]\n"
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

    _write_revocations(new_text)

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


# ── Entrenchment command (TI-Q7, v1.0k §57) ─────────────────────────────────
#
# A distinct, higher-ceremony surface from `agreement` — entrenchment
# protects specific (verb, subject) keys from ever being amended, even by a
# human `agreement amend --apply`. entrenched.limn lives outside the
# amendment surface entirely and is mutated only here, never by any
# agent-reachable code path (mirrors TI-Q6c for agreement.limn).

def _entrenched_line(verb: str, subject: str) -> str:
    """A minimal canonical statement line whose parsed (verb, subject) is
    exactly (verb, subject) — the parser's verb-subject pattern requires
    predicate content after the subject, hence the trailing 'is protected'."""
    ts = datetime.now(timezone.utc).isoformat()
    return f'{verb} {subject} is protected because "entrenched via seshat entrench on {ts}"'


def _write_entrenched_lines(lines: list[str]) -> None:
    agreements.ENTRENCHED_PATH.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(lines)
    if content and not content.endswith("\n"):
        content += "\n"
    agreements.ENTRENCHED_PATH.write_text(content)


@cli.group(name="entrench")
def entrench_cmd():
    """Manage entrenched (protected) Agreement rules (~/.seshat/entrenched.limn)."""


@entrench_cmd.command(name="show")
def entrench_show():
    """List currently entrenched (verb, subject) keys."""
    keys = agreements.entrenched_keys()
    if not keys:
        console.print(f"[dim]No entrenched rules at {agreements.ENTRENCHED_PATH}.[/dim]")
        return
    for verb, subject in sorted(keys):
        console.print(f"  [bold]{verb}[/bold] {subject}")


@entrench_cmd.command(name="add")
@click.argument("verb")
@click.argument("subject")
def entrench_add(verb, subject):
    """Protect (VERB, SUBJECT) from ever being amended, even by a human --apply.

    Requires typed confirmation — type the exact key back to confirm.
    """
    key_str = f"{verb} {subject}"
    if (verb, subject) in agreements.entrenched_keys():
        console.print(f"[yellow]{key_str} is already entrenched.[/yellow]")
        return

    console.print(
        f"[bold]This will permanently protect '{key_str}' from amendment[/bold] "
        "(even a human `agreement amend --apply` will refuse to touch it)."
    )
    typed = click.prompt(f"Type '{key_str}' to confirm")
    if typed != key_str:
        console.print("[red]Confirmation did not match — aborted. Nothing was entrenched.[/red]")
        sys.exit(1)

    lines = (agreements.load_entrenched() or "").splitlines()
    lines.append(_entrenched_line(verb, subject))
    _write_entrenched_lines(lines)

    env = receipts_module.snapshot()
    _emit(
        action="entrench",
        target={"operation": "add", "verb": verb, "subject": subject},
        result={"status": "success"},
        env_before=env,
        session_id=SESSION_ID,
        actor_type="cli_session",
        agent_hint=os.environ.get("MCP_AGENT_HINT", "cli"),
        env_after=env,
    )
    console.print(f"[green]✓[/green] Entrenched: [bold]{key_str}[/bold]")


@entrench_cmd.command(name="remove")
@click.argument("verb")
@click.argument("subject")
def entrench_remove(verb, subject):
    """Un-protect (VERB, SUBJECT). Security-critical — requires typed confirmation.

    Unentrenching is the direction that matters most to get deliberately
    right: a mistaken removal silently reopens a rule to amendment.
    """
    key_str = f"{verb} {subject}"
    if (verb, subject) not in agreements.entrenched_keys():
        console.print(f"[yellow]{key_str} is not currently entrenched.[/yellow]")
        return

    console.print(
        f"[bold red]This will remove entrenchment protection from '{key_str}'.[/bold red] "
        "It will become amendable again."
    )
    typed = click.prompt(f"Type '{key_str}' to confirm removal")
    if typed != key_str:
        console.print("[red]Confirmation did not match — aborted. Nothing was changed.[/red]")
        sys.exit(1)

    remaining = [
        s["raw"] for s in amendment_diff.parse_statements(agreements.load_entrenched() or "")
        if (s["verb"], s["subject"]) != (verb, subject)
    ]
    _write_entrenched_lines(remaining)

    env = receipts_module.snapshot()
    _emit(
        action="entrench",
        target={"operation": "remove", "verb": verb, "subject": subject},
        result={"status": "success"},
        env_before=env,
        session_id=SESSION_ID,
        actor_type="cli_session",
        agent_hint=os.environ.get("MCP_AGENT_HINT", "cli"),
        env_after=env,
    )
    console.print(f"[green]✓[/green] Un-entrenched: [bold]{key_str}[/bold]")


cli.add_command(entrench_cmd, name="entrench")


# ── Identity command (identity-plane arc; ID-Q4 Phase 1: Ed25519) ──────────
#
# Human-only surface — mint requires the root key and issues authority, so
# it must never be an MCP tool (§9.4). Minted-identity metadata lives under
# ~/.seshat/identity/, never in the four enforcement files (§9.1).

def _identity_meta_path(identifier: str) -> Path:
    return identity.IDENTITY_DIR / f"{identifier}.json"


def _write_identity_meta(path: Path, meta: dict) -> None:
    """Persist identity metadata (including, since ID-Q4 Phase 1, the
    holder's live Ed25519 private key) to PATH, created mode 0o600 from
    the moment it exists — this file is a human-surface secret store, and
    unlike the token alone, a holder private key is a forging credential
    if leaked. Uses os.open with an explicit mode rather than
    write-then-chmod so there is no window where the file is
    world-readable."""
    identity.IDENTITY_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(meta, indent=2))


def _find_holder_private_key(token: str) -> str | None:
    """Look up the current holder's private key for TOKEN by scanning
    ~/.seshat/identity/*.json for a persisted mint/attenuate --as record
    whose token field matches exactly. None if not found — e.g. a legacy
    HS256-macaroon token (which has no holder key at all), or an
    EdDSA-chain token minted/attenuated outside this CLI's own
    bookkeeping (attenuate() will then refuse with a clear error rather
    than silently proceeding unkeyed)."""
    identity.IDENTITY_DIR.mkdir(parents=True, exist_ok=True)
    for f in identity.IDENTITY_DIR.glob("*.json"):
        try:
            meta = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if meta.get("token") == token:
            return meta.get("holder_private_key")
    return None


@cli.group(name="identity")
def identity_cmd():
    """Mint and inspect agent identity tokens (Ed25519 capability tokens)."""


@identity_cmd.command(name="mint")
@click.argument("agent")
@click.option("--caveat", "caveats", multiple=True, help="A Liminate caveat line (repeatable).")
@click.option(
    "--ttl", "ttl_hours", type=float, default=None,
    help=f"Token lifetime in hours (default: {identity.DEFAULT_TTL_HOURS}). Pass 0 for an already-expired token.",
)
@click.option(
    "--until", "until_date", default=None,
    help="Explicit absolute expiry date (YYYY-MM-DD), overriding --ttl.",
)
def identity_mint(agent, caveats, ttl_hours, until_date):
    """Mint a new identity token for AGENT and print it.

    The printed token is what SESHAT_IDENTITY_TOKEN should carry for that
    agent's MCP session. This command never writes to agreement.limn,
    revocations.limn, invariant.limn, or entrenched.limn.

    Short-lived by default (identity-plane Stage 3): unless --until is
    given, the token expires --ttl hours from now (24h by default). Note:
    --until is a human-intuitive flag name ("valid until this date"), but
    the caveat it builds uses the Liminate keyword `starting`, not
    `until` — `until` would make a forbid go INERT after the date, which
    is backwards for "the token has expired". See identity.mint's
    docstring for the mechanism.

    ID-Q4 Phase 1: the token is an Ed25519 next-key block chain, signed by
    the Keychain-backed root key. A freshly generated holder private key
    is persisted alongside the token (0o600) so a later `identity
    attenuate` at the terminal can narrow it without ever touching the
    root key — never printed here.
    """
    caveat_list = list(caveats)
    if until_date:
        caveat_list.append(f'starting "{until_date}" forbid actor is "{agent}"')

    mint_kwargs = {}
    if ttl_hours is not None:
        mint_kwargs["ttl_hours"] = ttl_hours

    try:
        token, holder_private_key = identity.mint(agent, caveats=caveat_list, **mint_kwargs)
    except identity.IllegalCaveatError as e:
        console.print(f"[red]Refused to mint — illegal caveat:[/red] {e}")
        sys.exit(1)

    verified = identity.verify(token)
    meta = {
        "identifier": agent,
        "caveats": verified.caveats if verified else caveat_list,
        "minted_at": datetime.now(timezone.utc).isoformat(),
        "token": token,
        "holder_private_key": holder_private_key,
    }
    _write_identity_meta(_identity_meta_path(agent), meta)

    console.print(f"[green]✓[/green] Minted identity token for [bold]{agent}[/bold]:\n")
    console.print(token)
    console.print(f"\n  [dim]Set SESHAT_IDENTITY_TOKEN to this value for {agent}'s MCP session.[/dim]")


@identity_cmd.command(name="list")
def identity_list():
    """List minted identities."""
    identity.IDENTITY_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(identity.IDENTITY_DIR.glob("*.json"))
    if not files:
        console.print("[dim]No identities minted yet. Run: seshat identity mint <agent>[/dim]")
        return
    for f in files:
        meta = json.loads(f.read_text())
        console.print(
            f"  [bold]{meta['identifier']}[/bold]  "
            f"[dim]{len(meta.get('caveats', []))} caveat(s), minted {meta.get('minted_at', '?')}[/dim]"
        )


@identity_cmd.command(name="show")
@click.argument("agent")
def identity_show(agent):
    """Show a minted identity's caveats and token."""
    path = _identity_meta_path(agent)
    if not path.exists():
        console.print(f"[red]No identity minted for '{agent}'.[/red]")
        sys.exit(1)
    meta = json.loads(path.read_text())
    console.print(f"[bold]{meta['identifier']}[/bold]")
    console.print(f"  Minted: [dim]{meta.get('minted_at', '?')}[/dim]")
    console.print("  Caveats:")
    if meta.get("caveats"):
        for c in meta["caveats"]:
            console.print(f"    [dim]{c}[/dim]")
    else:
        console.print("    [dim](none)[/dim]")
    console.print(f"  Token:  [dim]{meta['token']}[/dim]")


@identity_cmd.command(name="attenuate")
@click.argument("token")
@click.option("--caveat", "caveats", multiple=True, help="A forbid-only Liminate caveat line (repeatable).")
@click.option("--as", "delegate_to", default=None, help="Delegate the narrowed token to a named sub-agent.")
def identity_attenuate(token, caveats, delegate_to):
    """Narrow TOKEN by appending forbid-only caveats and print the result.

    TOKEN can only ever be narrowed, never broadened — see
    identity.attenuate's docstring. This command never writes to
    agreement.limn, revocations.limn, invariant.limn, or entrenched.limn.
    If --as is given, the resulting child token's metadata is persisted
    under ~/.seshat/identity/<child>.json (listable via `identity list`),
    matching `identity mint`'s behavior.

    ID-Q4 Phase 1: for an Ed25519 token, the current holder's private key
    is looked up from this install's own ~/.seshat/identity/*.json
    bookkeeping (whichever record's `token` matches TOKEN exactly) and
    used to sign the new block — the root private key is never read. A
    legacy HS256-macaroon TOKEN attenuates via the unchanged root-key
    path instead; no holder key is needed or looked up for it.
    """
    holder_private_key = _find_holder_private_key(token)
    try:
        new_token, new_holder_private_key = identity.attenuate(
            token, list(caveats), delegate_to=delegate_to, holder_private_key=holder_private_key,
        )
    except identity.IllegalCaveatError as e:
        console.print(f"[red]Refused to attenuate:[/red] {e}")
        sys.exit(1)

    console.print(f"[green]✓[/green] Narrowed identity token:\n")
    console.print(new_token)

    if delegate_to:
        verified = identity.verify(new_token)
        meta = {
            "identifier": delegate_to,
            "caveats": verified.caveats if verified else list(caveats),
            "minted_at": datetime.now(timezone.utc).isoformat(),
            "token": new_token,
            "holder_private_key": new_holder_private_key,
        }
        _write_identity_meta(_identity_meta_path(delegate_to), meta)
        console.print(f"\n  [dim]Set SESHAT_IDENTITY_TOKEN to this value for {delegate_to}'s MCP session.[/dim]")


@identity_cmd.command(name="inspect")
@click.argument("token")
def identity_inspect(token):
    """Decode and verify TOKEN, showing its identifier, caveats, and
    delegation path.

    Attempts full verification first; if that fails, still shows whatever
    can be decoded structurally, clearly labeled as unverified — mirrors
    common JWT-debugger behavior, useful for diagnosing a bad token.
    """
    verified = identity.verify(token)
    if verified is not None:
        console.print(f"[bold]{verified.identifier}[/bold]  [green](verified)[/green]")
        if verified.delegation_path:
            console.print(f"  Delegation path: [dim]{' -> '.join(verified.delegation_path)}[/dim]")
            console.print(f"  Delegation depth: [dim]{len(verified.delegation_path) - 1}[/dim]")
        else:
            console.print("  Delegation path: [dim](none — not delegated)[/dim]")
        console.print("  Caveats:")
        if verified.caveats:
            for c in verified.caveats:
                console.print(f"    [dim]{c}[/dim]")
        else:
            console.print("    [dim](none)[/dim]")
        return

    console.print("[red]Token does not verify[/red] — forged, tampered, or carrying an illegal caveat.")
    try:
        header_b64, payload_b64, _sig_b64 = token.split(".")
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)))
        console.print("  [yellow]Decoded (UNVERIFIED — do not trust this data):[/yellow]")
        console.print(f"    identifier: [dim]{payload.get('identifier', '?')}[/dim]")
        console.print(f"    caveats:    [dim]{payload.get('caveats', [])}[/dim]")
    except Exception:
        console.print("  [dim]Could not decode token structure at all.[/dim]")


@identity_cmd.command(name="revoke")
@click.argument("identifier", required=False)
@click.option(
    "--token", "token_to_revoke", default=None,
    help="Revoke a single token by its nonce, rather than a whole identifier.",
)
def identity_revoke(identifier, token_to_revoke):
    """Revoke IDENTIFIER (or, with --token, a single token by nonce).

    Appends `forbid actor is "<identifier>"` to revocations.limn — human-
    only, mirroring the mint/attenuate/revoke authority boundary (mint
    issues, attenuate narrows and is agent-reachable since it can only
    narrow, revoke kills and is human-only like mint). Routes through the
    same REVOCATIONS_PATH writer `seshat revocations sync` uses — there is
    exactly one place this file is ever written.

    Revoking the root identifier of a delegated token kills every
    descendant delegated from it; revoking a mid-path hop kills that hop
    and everything delegated below it — the delegation path recorded in
    the token makes this automatic (identity-plane Stage 3).
    """
    if token_to_revoke is not None:
        verified = identity.verify(token_to_revoke)
        if verified is None or not verified.token_nonce:
            console.print("[red]Cannot revoke: token does not verify or carries no nonce.[/red]")
            sys.exit(1)
        target_id = verified.token_nonce
    elif identifier:
        target_id = identifier
    else:
        console.print("[red]Provide an identifier or --token <token>.[/red]")
        sys.exit(1)

    line = f'forbid actor is "{target_id}"'
    current = agreements.load_revocations() or ""
    if line in [l.strip() for l in current.splitlines()]:
        console.print(f"[yellow]'{target_id}' is already revoked.[/yellow]")
        return

    new_text = current
    if new_text and not new_text.endswith("\n"):
        new_text += "\n"
    new_text += line + "\n"

    validation_error = agreements._validate_forbid_only(new_text)
    if validation_error is not None:
        console.print(f"[red]Refusing to write revocations.limn:[/red] {validation_error}")
        sys.exit(1)

    _write_revocations(new_text)
    console.print(f"[green]✓[/green] Revoked: [bold]{target_id}[/bold]")


@identity_cmd.group(name="keys")
def identity_keys_cmd():
    """Inspect this install's Ed25519 root signing key (public half only)."""


@identity_keys_cmd.command(name="show")
def identity_keys_show():
    """Print the root public key.

    This is the value to hand a third party — an auditor, an enterprise
    SIEM — for independent, cross-org verification of every token this
    install mints, without ever handing over a forging key (ID-Q4 Phase
    1's whole point). Never prints the private key.
    """
    try:
        pub_hex = identity.root_public_key_hex()
    except identity.IdentityKeyUnavailableError as e:
        console.print(f"[red]Cannot read the root public key:[/red] {e}")
        sys.exit(1)

    console.print(pub_hex)
    console.print(
        "\n  [dim]This is the ROOT PUBLIC key for this install — safe to share. "
        "Hand it to a third party so they can independently verify tokens this "
        "install mints, without ever needing this install's private key.[/dim]"
    )


@identity_keys_cmd.command(name="export")
@click.option("--out", "out_path", required=True, type=click.Path(), help="File to write the public key to.")
def identity_keys_export(out_path):
    """Write the root public key to --out, for transmission to a third party."""
    try:
        pub_hex = identity.root_public_key_hex()
    except identity.IdentityKeyUnavailableError as e:
        console.print(f"[red]Cannot read the root public key:[/red] {e}")
        sys.exit(1)

    Path(out_path).write_text(pub_hex + "\n")
    console.print(f"[green]✓[/green] Wrote root public key to [bold]{out_path}[/bold]")


cli.add_command(identity_cmd, name="identity")


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
