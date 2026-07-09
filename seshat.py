#!/usr/bin/env python3
"""
Seshat — Local project registry and control room.
Runs at http://localhost:9000
"""

import json
import subprocess
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request, render_template

from registry  import Registry
from scanner   import Scanner
from runner    import Runner
from vault     import Vault
from organizer import Organizer
from router    import Router
from github        import GitHubImporter
from local_scanner import LocalScanner
import deps as deps_module
import agreements
import invariant_check
import liminate

app = Flask(__name__)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

registry = Registry()
scanner  = Scanner()
runner   = Runner()
vault     = Vault()
organizer = Organizer(registry)
router   = Router(registry)

RECEIPTS_DIR = Path.home() / ".seshat" / "receipts"


# ── Helpers ────────────────────────────────────────────────────────────────


def _enrich_deps_with_vault(deps_config: list, project_name: str) -> list:
    """Resolve vault-held URLs into dep configs before health-checking.

    Supports:
      - supabase  → SUPABASE_URL
      - postgres  → DATABASE_URL
      - http/api  → <LABEL>_URL  (e.g. label="stripe" → STRIPE_URL)
    """
    enriched = []
    for dep in deps_config:
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


def _compute_composite_status(status: str, dep_results: list) -> str:
    """Compute composite status: running + any dep disconnected = degraded."""
    if status != "running":
        return status
    if any(d.get("status") == "disconnected" for d in (dep_results or [])):
        return "degraded"
    return status


def _summarize_agreement_rules(text: str) -> list:
    """Parse `.limn` Agreement/revocations text and enumerate each rule.

    No facts are injected — this is enumeration, not evaluation of a
    specific (actor, action, scope). Mirrors the composition pattern
    `agreements.check_action` uses (see agreements.py), but without the
    `remember actor/action/scope` prelude. Verb and temporal-window
    extraction always defer to `agreements._verb_of` /
    `agreements._temporal_window` — never reimplemented here, so this stays
    consistent with the enforcement path. Read-only; never writes to
    ~/.seshat/.
    """
    try:
        result = liminate.run(text, enter_phase2=False, auto_confirm_amber=True)
    except Exception as e:
        return [{"error": str(e)}]

    rules = []
    for r in result.results:
        rule = {
            "canonical": r.canonical,
            "verb":      agreements._verb_of(r.canonical),
            "window":    agreements._temporal_window(r.canonical),
        }
        if r.line is not None:
            rule["line"] = r.line
        rules.append(rule)
    return rules


def build_project_view(project: dict, scan: dict, state: dict) -> dict:
    """Merge registry data + live port scan + log errors + dep status into one view object."""
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
            "process_cmd":  port_info.get("cmdline", ""),
        }
    elif managed_pid and runner.is_running(managed_pid):
        status    = "running"
        proc_data = {"pid": managed_pid}

    # Detect secondary ports held by child processes (e.g. backend alongside Vite)
    child_ports = []
    if managed_pid and status == "running":
        owned_pids = runner.child_pids(managed_pid) | {managed_pid}
        for scan_port, scan_info in scan.items():
            if scan_port != port and scan_info["pid"] in owned_pids:
                child_ports.append(scan_port)

    view = {**project, "status": status, **proc_data, "child_ports": sorted(child_ports)}

    # Include started_by attribution from state
    started_by = state.get(name, {}).get("started_by")
    if started_by:
        view["started_by"] = started_by

    # Attach most recent error from logs
    recent_error = runner.find_recent_error(name)
    if recent_error:
        view["recent_error"] = recent_error
        if status == "running":
            view["has_error"] = True

    # Attach dep status from cache; kick off async check if cache is cold
    dep_status = deps_module.get_cached(name) or []
    if not dep_status and project.get("dependencies"):
        enriched = _enrich_deps_with_vault(project.get("dependencies", []), name)
        deps_module.check_all_async(name, enriched)

    view["dep_status"]       = dep_status
    view["composite_status"] = _compute_composite_status(status, dep_status)

    return view


# ── Dashboard ──────────────────────────────────────────────────────────────


@app.route("/")
def index():
    return render_template("index.html")


# ── Projects ───────────────────────────────────────────────────────────────


@app.route("/api/projects", methods=["GET"])
def get_projects():
    projects = registry.list()
    scan     = scanner.scan()
    state    = registry.get_state()
    return jsonify([build_project_view(p, scan, state) for p in projects])


@app.route("/api/projects", methods=["POST"])
def add_project():
    data = request.json or {}
    for field in ("name", "port", "directory", "start"):
        if not data.get(field):
            return jsonify({"error": f"Missing required field: {field}"}), 400

    port   = int(data["port"])
    scheme = (data.get("scheme") or "http").lower()
    if scheme not in ("http", "https"):
        return jsonify({"error": "scheme must be 'http' or 'https'"}), 400
    project = {
        "name":         data["name"].strip(),
        "port":         port,
        "scheme":       scheme,
        "directory":    data["directory"].strip(),
        "start":        data["start"].strip(),
        "stop":         data.get("stop", "").strip(),
        "url":          data.get("url") or f"{scheme}://localhost:{port}",
        "tags":         data.get("tags") or [],
        "notes":        data.get("notes", "").strip(),
        "dependencies": data.get("dependencies") or [],
        "env":          data.get("env") or [],
    }
    source = data.get("source")
    if isinstance(source, dict) and source.get("type") == "github" and source.get("full_name"):
        project["source"] = {
            "type":      "github",
            "full_name": source["full_name"],
            "scraped":   source.get("scraped") or {},
        }
    try:
        result = registry.add(project)
    except ValueError as e:
        return jsonify({"error": str(e)}), 409
    try:
        router._reload_caddy()
    except Exception:
        pass  # Caddy reload failure is non-fatal for registration
    return jsonify(result), 201


@app.route("/api/projects/<name>", methods=["PUT"])
def update_project(name):
    data = request.json or {}
    if "scheme" in data:
        scheme = (data.get("scheme") or "http").lower()
        if scheme not in ("http", "https"):
            return jsonify({"error": "scheme must be 'http' or 'https'"}), 400
        data["scheme"] = scheme
    try:
        result = registry.update(name, data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    if "port" in data or "scheme" in data:
        try:
            router._reload_caddy()
        except Exception:
            pass
    return jsonify(result)


@app.route("/api/projects/<name>", methods=["DELETE"])
def remove_project(name):
    try:
        registry.remove(name)
        registry.clear_pid(name)
        vault.clear_project(name)       # remove any vault overrides
        deps_module.invalidate(name)    # clear dep cache
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    try:
        router._reload_caddy()
    except Exception:
        pass  # Caddy reload failure is non-fatal for removal
    return jsonify({"ok": True})


# ── Start / Stop ───────────────────────────────────────────────────────────


@app.route("/api/projects/<name>/start", methods=["POST"])
def start_project(name):
    project = registry.get(name)
    if not project:
        return jsonify({"error": f"Project '{name}' not found"}), 404

    scan = scanner.scan()
    if project["port"] in scan:
        proc = scan[project["port"]]
        return jsonify({
            "error": (
                f"Port {project['port']} is already in use by "
                f"'{proc['name']}' (PID {proc['pid']}). "
                f"Stop that process or reassign this project's port."
            )
        }), 409

    try:
        extra_env = vault.resolve_for_project(name, project.get("env", []))
        pid       = runner.start(project, extra_env=extra_env)
        registry.set_pid(name, pid, started_by="dashboard")
        # Kick off first dep check asynchronously once the project is live
        if project.get("dependencies"):
            enriched = _enrich_deps_with_vault(project.get("dependencies", []), name)
            deps_module.check_all_async(name, enriched)
        return jsonify({"ok": True, "pid": pid})
    except (ValueError, OSError) as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/projects/<name>/stop", methods=["POST"])
def stop_project(name):
    project = registry.get(name)
    if not project:
        return jsonify({"error": f"Project '{name}' not found"}), 404

    pid = registry.get_state().get(name, {}).get("pid")
    if not pid:
        return jsonify({
            "error": "No managed process found. "
                     "This project may have been started outside Seshat."
        }), 404

    runner.stop(pid)
    registry.clear_pid(name)
    return jsonify({"ok": True})


# ── Logs ───────────────────────────────────────────────────────────────────


@app.route("/api/projects/<name>/logs", methods=["GET"])
def get_project_logs(name):
    if not registry.get(name):
        return jsonify({"error": f"Project '{name}' not found"}), 404
    lines = runner.read_log_tail(name, n=150)
    error = runner.find_recent_error(name)
    return jsonify({"lines": lines, "recent_error": error})


# ── Dependency health (force-refresh) ──────────────────────────────────────


@app.route("/api/projects/<name>/deps", methods=["GET"])
def get_project_deps(name):
    project = registry.get(name)
    if not project:
        return jsonify({"error": f"Project '{name}' not found"}), 404
    enriched = _enrich_deps_with_vault(project.get("dependencies", []), name)
    results  = deps_module.check_all(name, enriched)
    return jsonify(results)


# ── Orphans ────────────────────────────────────────────────────────────────


@app.route("/api/orphans", methods=["GET"])
def get_orphans():
    scan             = scanner.scan()
    registered_ports = {p["port"] for p in registry.list()}
    registered_ports.add(9000)

    return jsonify([
        {
            "port":    port,
            "pid":     info["pid"],
            "name":    info.get("name", "unknown"),
            "cmdline": info.get("cmdline", ""),
        }
        for port, info in sorted(scan.items())
        if port not in registered_ports
    ])


@app.route("/api/listeners", methods=["GET"])
def get_listeners():
    """All TCP listeners on this machine, annotated by role."""
    scan             = scanner.scan()
    state            = registry.get_state()
    port_to_project  = {p["port"]: p["name"] for p in registry.list()}
    managed_pids     = {name: info.get("pid") for name, info in state.items() if info.get("pid")}

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
            "port":         port,
            "pid":          pid,
            "name":         info.get("name", ""),
            "cmdline":      info.get("cmdline", ""),
            "kind":         kind,
            "project_name": project_name,
        })
    return jsonify(rows)


@app.route("/api/listeners/<int:port>/stop", methods=["POST"])
def stop_listener(port):
    scan = scanner.scan()
    if port not in scan:
        return jsonify({"error": f"No process found on port {port}"}), 404
    runner.stop(scan[port]["pid"])
    # If this port belongs to a managed project, clear its pid from state
    for p in registry.list():
        if p["port"] == port:
            registry.clear_pid(p["name"])
            break
    return jsonify({"ok": True})


@app.route("/api/orphans/<int:port>/stop", methods=["POST"])
def stop_orphan(port):
    scan = scanner.scan()
    if port not in scan:
        return jsonify({"error": f"No process found on port {port}"}), 404
    runner.stop(scan[port]["pid"])
    return jsonify({"ok": True})


@app.route("/api/orphans/<int:port>/register", methods=["POST"])
def register_orphan(port):
    data = request.json or {}
    scan = scanner.scan()
    if port not in scan:
        return jsonify({"error": f"No process found on port {port}"}), 404

    for field in ("name", "directory", "start"):
        if not data.get(field):
            return jsonify({"error": f"Missing required field: {field}"}), 400

    project = {
        "name":         data["name"].strip(),
        "port":         port,
        "directory":    data["directory"].strip(),
        "start":        data["start"].strip(),
        "stop":         data.get("stop", "").strip(),
        "url":          data.get("url") or f"http://localhost:{port}",
        "tags":         data.get("tags") or [],
        "notes":        data.get("notes", "").strip(),
        "dependencies": [],
        "env":          [],
    }
    try:
        result = registry.add(project)
        registry.set_pid(project["name"], scan[port]["pid"], started_by="dashboard")
        return jsonify(result), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 409


# ── Groups ─────────────────────────────────────────────────────────────────


@app.route("/api/groups", methods=["GET"])
def get_groups():
    return jsonify(registry.list_groups())


@app.route("/api/groups", methods=["POST"])
def add_group():
    data     = request.json or {}
    name     = (data.get("name") or "").strip()
    projects = data.get("projects") or []

    if not name:
        return jsonify({"error": "Missing required field: name"}), 400
    if not isinstance(projects, list):
        return jsonify({"error": "'projects' must be a list"}), 400

    unknown = [p for p in projects if not registry.get(p)]
    if unknown:
        return jsonify({"error": f"Unknown projects: {', '.join(unknown)}"}), 400

    try:
        return jsonify(registry.add_group({"name": name, "projects": projects})), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 409


@app.route("/api/groups/<name>", methods=["DELETE"])
def remove_group(name):
    try:
        registry.remove_group(name)
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404


@app.route("/api/groups/<name>/start", methods=["POST"])
def start_group(name):
    group = registry.get_group(name)
    if not group:
        return jsonify({"error": f"Group '{name}' not found"}), 404

    scan    = scanner.scan()
    state   = registry.get_state()
    results = []

    for proj_name in group.get("projects", []):
        project = registry.get(proj_name)
        if not project:
            results.append({"name": proj_name, "error": "Project not found in registry"})
            continue

        managed_pid = state.get(proj_name, {}).get("pid")
        if managed_pid and runner.is_running(managed_pid):
            results.append({"name": proj_name, "status": "already_running"})
            continue

        if project["port"] in scan:
            proc = scan[project["port"]]
            results.append({
                "name":  proj_name,
                "error": f"Port {project['port']} in use by '{proc['name']}'"
            })
            continue

        try:
            extra_env = vault.resolve_for_project(proj_name, project.get("env", []))
            pid       = runner.start(project, extra_env=extra_env)
            registry.set_pid(proj_name, pid, started_by="dashboard")
            results.append({"name": proj_name, "status": "started", "pid": pid})
            if project.get("dependencies"):
                enriched = _enrich_deps_with_vault(project.get("dependencies", []), proj_name)
                deps_module.check_all_async(proj_name, enriched)
            time.sleep(0.4)
            scan = scanner.scan()
        except Exception as e:
            results.append({"name": proj_name, "error": str(e)})

    return jsonify({"group": name, "results": results})


@app.route("/api/groups/<name>/stop", methods=["POST"])
def stop_group(name):
    group = registry.get_group(name)
    if not group:
        return jsonify({"error": f"Group '{name}' not found"}), 404

    state   = registry.get_state()
    results = []

    for proj_name in group.get("projects", []):
        pid = state.get(proj_name, {}).get("pid")
        if not pid:
            results.append({"name": proj_name, "status": "not_managed"})
            continue
        runner.stop(pid)
        registry.clear_pid(proj_name)
        results.append({"name": proj_name, "status": "stopped"})

    return jsonify({"group": name, "results": results})


# ── Vault — summary & keys ─────────────────────────────────────────────────


@app.route("/api/vault", methods=["GET"])
def get_vault_summary():
    return jsonify(vault.summary())


@app.route("/api/vault/install-deps", methods=["POST"])
def vault_install_deps():
    """Install keyring + cryptography. Seshat must be manually restarted to activate."""
    import sys
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "keyring", "cryptography"],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        return jsonify({"ok": False, "error": result.stderr.strip() or result.stdout.strip()}), 500
    return jsonify({"ok": True})


@app.route("/api/vault/keys", methods=["GET"])
def list_vault_keys():
    return jsonify(vault.list_keys())


@app.route("/api/vault/keys/<key>", methods=["GET"])
def get_vault_key(key):
    value = vault.get(key)
    if value is None:
        return jsonify({"error": f"Key '{key}' not found in vault"}), 404
    return jsonify({"key": key, "value": value})


@app.route("/api/vault/keys", methods=["POST"])
def set_vault_key():
    data  = request.json or {}
    key   = (data.get("key") or "").strip().upper()
    value = data.get("value", "")
    if not key:
        return jsonify({"error": "Missing field: key"}), 400
    vault.set(key, value)
    return jsonify({"ok": True, "key": key})


@app.route("/api/vault/keys/<key>", methods=["DELETE"])
def delete_vault_key(key):
    vault.delete(key)
    return jsonify({"ok": True})


# ── Vault — overrides ──────────────────────────────────────────────────────


@app.route("/api/vault/overrides/<project>", methods=["GET"])
def get_project_overrides(project):
    # Return key names only (not values) for the status display
    return jsonify({"keys": sorted(vault.get_overrides(project).keys())})


@app.route("/api/vault/overrides/<project>/<key>", methods=["GET"])
def get_override_value(project, key):
    overrides = vault.get_overrides(project)
    if key not in overrides:
        return jsonify({"error": f"No override for '{key}' in project '{project}'"}), 404
    return jsonify({"key": key, "value": overrides[key]})


@app.route("/api/vault/overrides/<project>", methods=["POST"])
def set_project_override(project):
    data  = request.json or {}
    key   = (data.get("key") or "").strip().upper()
    value = data.get("value", "")
    if not key:
        return jsonify({"error": "Missing field: key"}), 400
    vault.set_override(project, key, value)
    return jsonify({"ok": True})


@app.route("/api/vault/overrides/<project>/<key>", methods=["DELETE"])
def delete_project_override(project, key):
    vault.delete_override(project, key)
    return jsonify({"ok": True})


# ── Vault — audit & import ─────────────────────────────────────────────────


@app.route("/api/vault/audit", methods=["GET"])
def vault_audit():
    return jsonify(vault.audit(registry.list()))


@app.route("/api/vault/import", methods=["POST"])
def import_dotenv():
    data    = request.json or {}
    content = data.get("content", "").strip()
    project = data.get("project") or None   # None = import to shared

    if not content:
        return jsonify({"error": "No .env content provided"}), 400

    imported = vault.import_dotenv(content, project)
    return jsonify({
        "ok":      True,
        "count":   len(imported),
        "keys":    sorted(imported.keys()),
        "project": project,
    })


# ── Organize ───────────────────────────────────────────────────────────────


@app.route("/api/organize/map", methods=["GET"])
def get_folder_map():
    return jsonify(organizer.folder_map())


@app.route("/api/organize/recommendations", methods=["GET"])
def get_recommendations():
    root = request.args.get("root", "~/Projects")
    return jsonify(organizer.recommend_structure(root))


@app.route("/api/organize/migrate", methods=["POST"])
def migrate_project():
    data        = request.json or {}
    project     = (data.get("project") or "").strip()
    destination = (data.get("destination") or "").strip()
    force       = bool(data.get("force", False))

    if not project or not destination:
        return jsonify({"error": "Missing required fields: project, destination"}), 400

    try:
        result = organizer.migrate(project, destination, force=force)
        if "warning" in result:
            return jsonify(result), 200
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/organize/history", methods=["GET"])
def get_move_history():
    return jsonify(organizer.load_history())


@app.route("/api/organize/history/<project_name>", methods=["GET"])
def get_project_move_history(project_name):
    return jsonify(organizer.load_history(project_name))


@app.route("/api/organize/rollback", methods=["POST"])
def rollback_move():
    data    = request.json or {}
    move_id = (data.get("move_id") or "").strip()
    if not move_id:
        return jsonify({"error": "Missing required field: move_id"}), 400
    try:
        return jsonify(organizer.rollback(move_id))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


# ── Router ─────────────────────────────────────────────────────────────────


@app.route("/api/router/status", methods=["GET"])
def get_router_status():
    return jsonify(router.setup_status())


@app.route("/api/router/setup/dnsmasq", methods=["POST"])
def setup_dnsmasq():
    return jsonify(router.configure_dnsmasq())


@app.route("/api/router/setup/resolver", methods=["POST"])
def setup_resolver():
    return jsonify(router.configure_resolver())


@app.route("/api/router/setup/caddy-start", methods=["POST"])
def setup_caddy_start():
    return jsonify(router.start_caddy())


@app.route("/api/router/setup/caddy-trust", methods=["POST"])
def setup_caddy_trust():
    return jsonify(router.trust_caddy_ca())


@app.route("/api/router/hostnames", methods=["GET"])
def get_hostnames():
    return jsonify(router.all_hostnames())


@app.route("/api/router/hostnames/<project>", methods=["PUT"])
def set_hostname(project):
    data     = request.json or {}
    hostname = (data.get("hostname") or "").strip()
    if not hostname:
        return jsonify({"error": "Missing required field: hostname"}), 400
    try:
        return jsonify(router.set_hostname(project, hostname))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/router/hostnames/<project>", methods=["DELETE"])
def reset_hostname(project):
    return jsonify(router.reset_hostname(project))


@app.route("/api/router/reload", methods=["POST"])
def reload_caddy():
    return jsonify(router._reload_caddy())


# ── GitHub import ──────────────────────────────────────────────────────────


@app.route("/api/github/status", methods=["GET"])
def github_status():
    token = vault.get("__github_token__")
    return jsonify({"configured": token is not None})


@app.route("/api/github/token", methods=["POST"])
def github_save_token():
    data  = request.json or {}
    token = (data.get("token") or "").strip()
    if not token:
        return jsonify({"error": "token is required"}), 400
    result = GitHubImporter(token).validate_token()
    if not result["ok"]:
        return jsonify({"error": result["error"]}), 400
    vault.set("__github_token__", token)
    return jsonify({"ok": True, "login": result["login"]})


_REFRESHABLE_FIELDS = ("port", "start", "notes", "tags")


def _scraped_snapshot(scan_result: dict) -> dict:
    return {f: scan_result.get(f) for f in _REFRESHABLE_FIELDS}


def _merge_refresh(project: dict, new_scrape: dict) -> tuple[dict, list[str]]:
    """Merge new scraped values into project, preserving user edits.

    Returns (updates_dict, changed_field_names). A field is overwritten only if
    the project's current value still equals the previously-scraped baseline.
    The scraped baseline is always updated to the latest scrape.
    """
    source  = project.get("source") or {}
    baseline = source.get("scraped") or {}
    updates  = {}
    changed  = []
    for f in _REFRESHABLE_FIELDS:
        new_val = new_scrape.get(f)
        old_val = project.get(f)
        base    = baseline.get(f)
        if old_val == base and new_val != old_val:
            updates[f] = new_val
            changed.append(f)
    new_source = {
        **source,
        "type":      "github",
        "scraped":   _scraped_snapshot(new_scrape),
    }
    updates["source"] = new_source
    return updates, changed


@app.route("/api/projects/<name>/refresh", methods=["POST"])
def refresh_project_source(name):
    project = registry.get(name)
    if not project:
        return jsonify({"error": f"Project '{name}' not found"}), 404
    source = project.get("source") or {}
    if source.get("type") != "github" or not source.get("full_name"):
        return jsonify({"error": "Project is not linked to a GitHub source"}), 400
    token = vault.get("__github_token__")
    if not token:
        return jsonify({"error": "GitHub token not configured"}), 400
    try:
        new_scrape = GitHubImporter(token).scan_one(source["full_name"])
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    updates, changed = _merge_refresh(project, new_scrape)
    try:
        result = registry.update(name, updates)
    except ValueError as e:
        return jsonify({"error": str(e)}), 409
    if "port" in changed:
        try:
            router._reload_caddy()
        except Exception:
            pass
    return jsonify({"ok": True, "changed": changed, "project": result})


@app.route("/api/projects/<name>/link", methods=["POST"])
def link_project_source(name):
    project = registry.get(name)
    if not project:
        return jsonify({"error": f"Project '{name}' not found"}), 404
    data      = request.json or {}
    full_name = (data.get("full_name") or "").strip()
    if not full_name or "/" not in full_name:
        return jsonify({"error": "full_name (owner/repo) is required"}), 400
    token = vault.get("__github_token__")
    if not token:
        return jsonify({"error": "GitHub token not configured"}), 400
    try:
        GitHubImporter(token).fetch_repo(full_name)
    except Exception as e:
        return jsonify({"error": f"Could not access repo: {e}"}), 400
    new_source = {
        "type":      "github",
        "full_name": full_name,
        "scraped":   {f: project.get(f) for f in _REFRESHABLE_FIELDS},
    }
    try:
        result = registry.update(name, {"source": new_source})
    except ValueError as e:
        return jsonify({"error": str(e)}), 409
    return jsonify({"ok": True, "project": result})


@app.route("/api/github/scan", methods=["GET"])
def github_scan():
    token = vault.get("__github_token__")
    if not token:
        return jsonify({"error": "GitHub token not configured"}), 400
    registered_names = {p["name"] for p in registry.list()}
    try:
        results = GitHubImporter(token).scan(registered_names=registered_names)
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Local project discovery ────────────────────────────────────────────────


@app.route("/api/local-scan", methods=["POST"])
def local_scan():
    data      = request.json or {}
    directory = (data.get("directory") or "").strip()
    if not directory:
        return jsonify({"error": "directory is required"}), 400
    projects = registry.list()
    registered_names = {p["name"] for p in projects} | {p["directory"] for p in projects}
    try:
        results = LocalScanner().scan(directory, registered_names=registered_names)
        return jsonify(results)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Open in Finder / Terminal / Browser ────────────────────────────────────


@app.route("/api/open", methods=["POST"])
def open_path():
    data = request.json or {}
    path = data.get("path", "").strip()
    mode = data.get("mode", "finder")

    if not path:
        return jsonify({"error": "No path provided"}), 400

    expanded = str(Path(path).expanduser().resolve())

    try:
        if mode == "finder":
            subprocess.Popen(["open", expanded])
        elif mode == "terminal":
            script = f'tell application "Terminal" to do script "cd {expanded}"'
            subprocess.Popen(["osascript", "-e", script])
        elif mode in ("browser", "editor"):
            subprocess.Popen(["open", path])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Receipts ───────────────────────────────────────────────────────────────


@app.route("/api/receipts", methods=["GET"])
def get_receipts():
    limit          = min(int(request.args.get("limit", 50)), 200)
    action_filter  = request.args.get("action")
    session_filter = request.args.get("session")

    if not RECEIPTS_DIR.exists():
        return jsonify([])

    files    = sorted(RECEIPTS_DIR.glob("*.json"), reverse=True)
    receipts = []
    for f in files:
        if len(receipts) >= limit:
            break
        try:
            receipt = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if action_filter and receipt.get("action") != action_filter:
            continue
        if session_filter and receipt.get("actor", {}).get("session_id") != session_filter:
            continue
        receipts.append(receipt)

    return jsonify(receipts)


@app.route("/api/receipts/stats", methods=["GET"])
def get_receipts_stats():
    if not RECEIPTS_DIR.exists():
        return jsonify({"total": 0, "sessions": [], "actions": {}})

    files    = sorted(RECEIPTS_DIR.glob("*.json"), reverse=True)
    sessions = set()
    actions  = {}
    total    = 0

    for f in files:
        try:
            receipt = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        total += 1
        session_id = receipt.get("actor", {}).get("session_id")
        if session_id:
            sessions.add(session_id)
        action = receipt.get("action", "unknown")
        actions[action] = actions.get(action, 0) + 1

    return jsonify({
        "total":    total,
        "sessions": sorted(sessions),
        "actions":  actions,
    })


# ── Agreement ──────────────────────────────────────────────────────────────


@app.route("/api/agreement", methods=["GET"])
def get_agreement():
    """Return the current Agreement: raw text, existence, and a parsed rule
    summary. Read-only — never writes to ~/.seshat/."""
    text = agreements.load_agreement()
    if text is None:
        return jsonify({"exists": False, "text": None, "rules": []})
    rules = _summarize_agreement_rules(text)
    return jsonify({"exists": True, "text": text, "rules": rules})


@app.route("/api/agreement/check", methods=["POST"])
def agreement_check():
    """Evaluate a hypothetical (actor, action, scope) against the live Agreement
    WITHOUT executing anything or writing any receipt. Pure read-only evaluation
    for the dashboard's dry-run strip."""
    data   = request.json or {}
    actor  = (data.get("actor") or "").strip()
    action = (data.get("action") or "").strip()
    scope  = data.get("scope")
    scope  = scope.strip() if isinstance(scope, str) and scope.strip() else None
    if not actor or not action:
        return jsonify({"error": "actor and action are required"}), 400
    decision = agreements.check_action(actor, action, scope)
    return jsonify({
        "allowed": decision.allowed,
        "mode":    decision.mode,
        "rule":    decision.rule,
        "reason":  decision.reason,
    })


# ── Revocations ────────────────────────────────────────────────────────────


@app.route("/api/revocations", methods=["GET"])
def get_revocations():
    """Return the current platform revocations overlay: raw text, existence,
    forbid-only rule summary, and sync state. Read-only."""
    text = agreements.load_revocations()
    state = agreements.revocation_state()  # None if no revocations file
    if text is None:
        return jsonify({"exists": False, "text": None, "rules": [], "sync": None})
    rules = _summarize_agreement_rules(text)  # revocations are forbid-only
    return jsonify({"exists": True, "text": text, "rules": rules, "sync": state})


# ── Invariant ──────────────────────────────────────────────────────────────


@app.route("/api/invariant", methods=["GET"])
def get_invariant():
    """Return the Invariant verification contract text and existence. Read-only.
    Does NOT run verification (that happens post-action in the receipt path)."""
    text = agreements.load_invariant()
    return jsonify({"exists": text is not None, "text": text})


@app.route("/api/invariant/last-run", methods=["GET"])
def get_invariant_last_run():
    """Return the most recent receipt carrying an `invariant` block, walked
    newest-first over RECEIPTS_DIR (same glob/sort pattern as get_receipts()).
    Read-only — never runs verification itself; that only happens on the
    action path via invariant_check.run_verification."""
    if not RECEIPTS_DIR.exists():
        return jsonify({"exists": False})

    files = sorted(RECEIPTS_DIR.glob("*.json"), reverse=True)
    for f in files:
        try:
            receipt = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        invariant_block = receipt.get("invariant")
        if invariant_block is not None:
            return jsonify({
                "exists":           True,
                "invariant":        invariant_block,
                "receipt_timestamp": receipt.get("timestamp"),
                "receipt_hash":      receipt.get("receipt_hash"),
            })

    return jsonify({"exists": False})


# ── Background dep checker ─────────────────────────────────────────────────


def _dep_checker_loop() -> None:
    """Daemon thread: re-check deps for all running projects every 30 seconds."""
    time.sleep(10)          # brief warm-up pause before first sweep
    while True:
        try:
            state = registry.get_state()
            for project in registry.list():
                if not project.get("dependencies"):
                    continue
                name        = project["name"]
                managed_pid = state.get(name, {}).get("pid")
                if managed_pid and runner.is_running(managed_pid):
                    enriched = _enrich_deps_with_vault(project["dependencies"], name)
                    deps_module.check_all(name, enriched)
        except Exception:
            pass            # never crash the daemon
        time.sleep(30)


# ── Entry point ────────────────────────────────────────────────────────────


if __name__ == "__main__":
    _checker = threading.Thread(target=_dep_checker_loop, daemon=True, name="dep-checker")
    _checker.start()
    threading.Thread(target=router._reload_caddy, daemon=True, name="caddy-boot").start()
    print("⊕  Seshat is running at http://localhost:9000")
    app.run(host="127.0.0.1", port=9000, debug=False)
