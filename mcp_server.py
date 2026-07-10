#!/usr/bin/env python3
"""
mcp_server.py — Seshat MCP server.

Exposes Seshat's local environment management as MCP tools and resources
for AI coding agents. Peer entry point alongside seshat.py (Flask dashboard).

Transport: stdio
Protocol: MCP (Model Context Protocol)
"""

import functools
import hashlib
import inspect
import json
import os
import time
import uuid

from mcp.server.fastmcp import FastMCP

from registry import Registry
from scanner import Scanner
from runner import Runner
from vault import Vault
import amendment_diff
import deps as deps_module
import receipts
import identity
import invariant_check

try:
    import agreements
except ImportError as exc:
    raise ImportError(
        "Seshat MCP server requires the 'liminate' package for Agreement "
        "enforcement (deny-by-default agent permissions). Install it with: "
        "pip install 'liminate>=0.16.0,<0.17'. "
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


def _verified_identity() -> "identity.VerifiedIdentity | None":
    """The single source of truth for whether this call carries a verified
    identity token — _agreement_actor() and _emit() both call this so they
    can never disagree about the verdict (§8 invariant 3, extended to the
    identity plane)."""
    token = os.environ.get("SESHAT_IDENTITY_TOKEN")
    if not token:
        return None
    return identity.verify(token)


def _agreement_actor() -> str:
    """Agent-identity string used both for Agreement checks and receipt agent_hint.

    Single source of truth: the string checked against the Agreement and the
    agent_hint recorded in every receipt must never diverge (§8 invariant 3).

    Identity-plane Stage 1: when SESHAT_IDENTITY_TOKEN is present and
    verifies, this returns the token's verified identifier — provable, not
    self-declared. check_action() independently re-verifies the same token
    itself (never trusts this return value as proof of anything); this
    function only supplies the display/lookup string used when no token is
    present or verification fails, in which case it falls back to the
    existing self-declared MCP_AGENT_HINT (F-02 acute): any process can
    set that to any string, so receipts.emit() stamps
    actor.identity_verified: false unconditionally in that case.
    """
    verified = _verified_identity()
    if verified is not None:
        return verified.identifier
    return os.environ.get("MCP_AGENT_HINT", "unknown-agent")


def _emit(**kwargs) -> dict:
    """Wrap receipts.emit(), injecting revocation_state, agreement_hash,
    identity_verified, and (post-action) the Invariant verification block
    so every MCP-emitted receipt carries them from one source (§7
    invariant 4). Returns the written receipt dict (receipts.emit()'s
    return value)."""
    env_after = kwargs.get("env_after") or receipts.snapshot()
    kwargs["env_after"] = env_after
    return receipts.emit(
        revocation_state=agreements.revocation_state(),
        agreement_hash=agreements.agreement_hash(),
        invariant=invariant_check.run_verification(env_after),
        identity_verified=_verified_identity() is not None,
        **kwargs,
    )


def _enforce(action: str, target: dict) -> str | None:
    """Evaluate the developer's Agreement for this call before it executes.

    Returns None when the Agreement permits the call. Returns a denial
    string (and logs a denial Receipt) when it does not — deny-by-default,
    per SES-Q4: no Agreement, no matching permit, or any evaluation error
    all deny, and a matching forbid always wins over a matching permit.

    Identity-plane Stage 1: SESHAT_IDENTITY_TOKEN, if present, is passed to
    check_action's token parameter — it independently verifies the token
    (never trusting _agreement_actor()'s already-computed string as proof)
    and denies with mode="identity-invalid" on failure.
    """
    scope = target.get("project") or target.get("group")
    token = os.environ.get("SESHAT_IDENTITY_TOKEN")
    decision = agreements.check_action(_agreement_actor(), action, scope, token=token)
    if decision.allowed:
        return None

    env = receipts.snapshot()
    result = {
        "status": "denied",
        "mode": decision.mode,
        "rule": decision.rule,
        "reason": decision.reason,
    }
    _emit(
        action=action,
        target=target,
        result=result,
        env_before=env,
        session_id=SESSION_ID,
        actor_type="mcp_session",
        agent_hint=_agreement_actor(),
        env_after=env,
    )

    denial = f"DENIED by Agreement: {decision.reason}"
    if decision.rule is not None:
        denial += f" Rule: {decision.rule}"
    return denial


_ENFORCED_MARKER = "_seshat_enforced_tool"


def _enforced_tool(action: str, target_fn):
    """Register an MCP tool with a structural (not convention-based)
    enforcement gate (F-11). Every tool wrapped here calls _enforce()
    before its body runs, no matter what the body does or omits — there
    is no path from an MCP call into the tool's logic that skips it.

    target_fn receives the tool call's bound arguments (a dict, defaults
    applied) and must return the `target` dict _enforce() expects, e.g.
    `lambda a: {"project": a["name"]}`.

    Also stamps the wrapped function so _assert_all_tools_enforced() can
    catch a tool that bypasses this decorator entirely (a bare
    @mcp.tool()) at import time, rather than serving it unenforced.
    """
    def decorator(fn):
        signature = inspect.signature(fn)

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def wrapper(*args, **kwargs):
                bound = signature.bind(*args, **kwargs)
                bound.apply_defaults()
                denial = _enforce(action, target_fn(bound.arguments))
                if denial:
                    return denial
                return await fn(*args, **kwargs)
        else:
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                bound = signature.bind(*args, **kwargs)
                bound.apply_defaults()
                denial = _enforce(action, target_fn(bound.arguments))
                if denial:
                    return denial
                return fn(*args, **kwargs)

        setattr(wrapper, _ENFORCED_MARKER, True)
        return mcp.tool()(wrapper)

    return decorator


def _assert_all_tools_enforced() -> None:
    """Fail at import time — not silently — if any registered MCP tool
    skipped the _enforced_tool gate. A structural check, not a review
    convention: this runs unconditionally when this module loads."""
    unenforced = sorted(
        tool.name for tool in mcp._tool_manager.list_tools()
        if not getattr(tool.fn, _ENFORCED_MARKER, False)
    )
    if unenforced:
        raise RuntimeError(
            f"MCP tool(s) registered without the _enforced_tool gate: "
            f"{unenforced}. Use @_enforced_tool(action, target_fn) instead "
            "of a bare @mcp.tool()."
        )


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
# Every tool below is registered via @_enforced_tool(...), which calls
# _enforce() before the tool body runs — structurally (F-11), not by
# convention. _assert_all_tools_enforced() at the bottom of this module
# audits that every registered tool actually went through it.


@_enforced_tool("start_project", lambda a: {"project": a["name"]})
def start_project(name: str) -> str:
    """Start a registered project by name.

    Resolves vault secrets scoped to the project, starts the process,
    and records the PID with MCP session attribution.
    """
    env_before = receipts.snapshot()

    project = registry.get(name)
    if not project:
        result = {"status": "failure", "error": f"Project '{name}' not found"}
        _emit(
            action="start_project",
            target={"project": name},
            result=result,
            env_before=env_before,
            session_id=SESSION_ID,
            actor_type="mcp_session",
            agent_hint=_agreement_actor(),
        )
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
        _emit(
            action="start_project",
            target={"project": name, "port": project["port"]},
            result=result,
            env_before=env_before,
            session_id=SESSION_ID,
            actor_type="mcp_session",
            agent_hint=_agreement_actor(),
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
        _emit(
            action="start_project",
            target={"project": name, "port": project["port"], "directory": project["directory"]},
            result=result,
            env_before=env_before,
            session_id=SESSION_ID,
            actor_type="mcp_session",
            agent_hint=_agreement_actor(),
        )
        return json.dumps(result)
    except (ValueError, OSError) as e:
        result = {"status": "failure", "error": str(e)}
        _emit(
            action="start_project",
            target={"project": name},
            result=result,
            env_before=env_before,
            session_id=SESSION_ID,
            actor_type="mcp_session",
            agent_hint=_agreement_actor(),
        )
        return json.dumps(result)


@_enforced_tool("stop_project", lambda a: {"project": a["name"]})
def stop_project(name: str) -> str:
    """Stop a running project by name."""
    env_before = receipts.snapshot()

    project = registry.get(name)
    if not project:
        result = {"status": "failure", "error": f"Project '{name}' not found"}
        _emit(
            action="stop_project",
            target={"project": name},
            result=result,
            env_before=env_before,
            session_id=SESSION_ID,
            actor_type="mcp_session",
            agent_hint=_agreement_actor(),
        )
        return json.dumps(result)

    state = registry.get_state()
    pid = state.get(name, {}).get("pid")
    if not pid:
        result = {"status": "failure", "error": "No managed process found"}
        _emit(
            action="stop_project",
            target={"project": name},
            result=result,
            env_before=env_before,
            session_id=SESSION_ID,
            actor_type="mcp_session",
            agent_hint=_agreement_actor(),
        )
        return json.dumps(result)

    runner.stop(pid)
    registry.clear_pid(name)

    result = {"status": "success", "stopped_pid": pid}
    _emit(
        action="stop_project",
        target={"project": name, "port": project["port"]},
        result=result,
        env_before=env_before,
        session_id=SESSION_ID,
        actor_type="mcp_session",
        agent_hint=_agreement_actor(),
    )
    return json.dumps(result)


@_enforced_tool("start_group", lambda a: {"group": a["name"]})
def start_group(name: str) -> str:
    """Start all projects in a named group."""
    env_before = receipts.snapshot()

    group = registry.get_group(name)
    if not group:
        result = {"status": "failure", "error": f"Group '{name}' not found"}
        _emit(
            action="start_group",
            target={"group": name},
            result=result,
            env_before=env_before,
            session_id=SESSION_ID,
            actor_type="mcp_session",
            agent_hint=_agreement_actor(),
        )
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
    _emit(
        action="start_group",
        target={"group": name},
        result=result,
        env_before=env_before,
        session_id=SESSION_ID,
        actor_type="mcp_session",
        agent_hint=_agreement_actor(),
    )
    return json.dumps(result)


@_enforced_tool("stop_group", lambda a: {"group": a["name"]})
def stop_group(name: str) -> str:
    """Stop all projects in a named group."""
    env_before = receipts.snapshot()

    group = registry.get_group(name)
    if not group:
        result = {"status": "failure", "error": f"Group '{name}' not found"}
        _emit(
            action="stop_group",
            target={"group": name},
            result=result,
            env_before=env_before,
            session_id=SESSION_ID,
            actor_type="mcp_session",
            agent_hint=_agreement_actor(),
        )
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
    _emit(
        action="stop_group",
        target={"group": name},
        result=result,
        env_before=env_before,
        session_id=SESSION_ID,
        actor_type="mcp_session",
        agent_hint=_agreement_actor(),
    )
    return json.dumps(result)


@_enforced_tool("register_project", lambda a: {"project": a["name"]})
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
    env_before = receipts.snapshot()

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
        _emit(
            action="register_project",
            target={"project": name, "port": port, "directory": directory},
            result=result,
            env_before=env_before,
            session_id=SESSION_ID,
            actor_type="mcp_session",
            agent_hint=_agreement_actor(),
        )
        return json.dumps(result)
    except ValueError as e:
        result = {"status": "failure", "error": str(e)}
        _emit(
            action="register_project",
            target={"project": name, "port": port},
            result=result,
            env_before=env_before,
            session_id=SESSION_ID,
            actor_type="mcp_session",
            agent_hint=_agreement_actor(),
        )
        return json.dumps(result)


@_enforced_tool("stop_orphan", lambda a: {"port": a["port"]})
def stop_orphan(port: int) -> str:
    """Stop an unregistered process listening on a port."""
    env_before = receipts.snapshot()

    scan = scanner.scan()
    if port not in scan:
        result = {"status": "failure", "error": f"No process found on port {port}"}
        _emit(
            action="stop_orphan",
            target={"port": port},
            result=result,
            env_before=env_before,
            session_id=SESSION_ID,
            actor_type="mcp_session",
            agent_hint=_agreement_actor(),
        )
        return json.dumps(result)

    pid = scan[port]["pid"]
    process_name = scan[port].get("name", "unknown")
    runner.stop(pid)

    result = {"status": "success", "stopped_pid": pid, "process": process_name}
    _emit(
        action="stop_orphan",
        target={"port": port, "pid": pid},
        result=result,
        env_before=env_before,
        session_id=SESSION_ID,
        actor_type="mcp_session",
        agent_hint=_agreement_actor(),
    )
    return json.dumps(result)


@_enforced_tool("set_secret", lambda a: {"key": a["key"].strip().upper()})
def set_secret(key: str, value: str) -> str:
    """Store or update a shared secret in the vault.

    The secret is always encrypted at rest (Keychain-backed Fernet) — if
    the required crypto packages aren't installed, storing fails outright
    (F-06) rather than ever writing an unencrypted vault. Secret values
    are never exposed through MCP resources — they are resolved at
    process start time via environment variables.
    """
    env_before = receipts.snapshot()

    vault.set(key.strip().upper(), value)

    result = {"status": "success", "key": key.strip().upper()}
    _emit(
        action="set_secret",
        target={"key": key.strip().upper()},
        result=result,
        env_before=env_before,
        session_id=SESSION_ID,
        actor_type="mcp_session",
        agent_hint=_agreement_actor(),
    )
    return json.dumps(result)


@_enforced_tool(
    "set_project_override",
    lambda a: {"project": a["project"], "key": a["key"].strip().upper()},
)
def set_project_override(project: str, key: str, value: str) -> str:
    """Set a project-specific secret override in the vault.

    Overrides take precedence over shared secrets when resolving
    environment variables for this project at start time.
    """
    env_before = receipts.snapshot()

    if not registry.get(project):
        result = {"status": "failure", "error": f"Project '{project}' not found"}
        _emit(
            action="set_project_override",
            target={"project": project, "key": key.strip().upper()},
            result=result,
            env_before=env_before,
            session_id=SESSION_ID,
            actor_type="mcp_session",
            agent_hint=_agreement_actor(),
        )
        return json.dumps(result)

    vault.set_override(project, key.strip().upper(), value)

    result = {"status": "success", "project": project, "key": key.strip().upper()}
    _emit(
        action="set_project_override",
        target={"project": project, "key": key.strip().upper()},
        result=result,
        env_before=env_before,
        session_id=SESSION_ID,
        actor_type="mcp_session",
        agent_hint=_agreement_actor(),
    )
    return json.dumps(result)


@_enforced_tool(
    "amend_agreement",
    lambda a: {"additions": a.get("additions") or [], "removals": a.get("removals") or []},
)
def amend_agreement(additions: list[str] | None = None, removals: list[str] | None = None) -> str:
    """Propose an amendment to the Agreement (TI-Q7, v1.0k §55).

    `additions` and `removals` are each full canonical statement lines
    (e.g. 'forbid action is "wipe_disk"') to add to or remove from the
    Agreement. The proposed change is classified (monotonic / de-escalating
    / entrenched-violation) and recorded as a proposal receipt.

    This tool NEVER writes ~/.seshat/agreement.limn — it only proposes. A
    human must run `seshat agreement amend --apply <receipt_id>` at the
    terminal to enact it (TI-Q6c: only a human writes the Agreement).
    """
    additions = additions or []
    removals = removals or []

    env_before = receipts.snapshot()

    before_src = agreements.load_agreement() or ""
    hash_before = agreements.agreement_hash()

    after_src = amendment_diff.apply_delta(before_src, additions, removals)
    hash_after = hashlib.sha256(after_src.encode("utf-8")).hexdigest()

    proposed_delta = amendment_diff.diff_statements(before_src, after_src)
    classification = amendment_diff.classify_amendment(
        before_src, after_src, agreements.entrenched_keys()
    )

    target = {
        "agreement_hash_before": hash_before,
        "agreement_hash_after": hash_after,
        "additions": additions,
        "removals": removals,
        "proposed_delta": proposed_delta,
    }
    result: dict = {
        "status": "proposed",
        "classification": classification["class"],
    }
    if classification.get("violations"):
        result["violations"] = [list(v) for v in classification["violations"]]

    receipt = _emit(
        action="amend_agreement",
        target=target,
        result=result,
        env_before=env_before,
        session_id=SESSION_ID,
        actor_type="mcp_session",
        agent_hint=_agreement_actor(),
    )

    response: dict = {
        "status": "proposed",
        "receipt_id": receipt["receipt_hash"],
        "classification": classification["class"],
        "next_step": (
            f"A human must run `seshat agreement amend --apply {receipt['receipt_hash']}` "
            "at the terminal to apply this amendment. This tool never writes the Agreement."
        ),
    }
    if classification.get("violations"):
        response["violations"] = [list(v) for v in classification["violations"]]
    return json.dumps(response)


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


# F-11: run once at import time — fails loudly, refusing to even import
# this module (let alone start serving), if any tool above skipped the
# structural enforcement gate.
_assert_all_tools_enforced()


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
