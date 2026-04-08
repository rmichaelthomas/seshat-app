#!/usr/bin/env python3
"""
Seshat — Local project registry and control room.
Runs at http://localhost:9000
"""

import subprocess
import time
from pathlib import Path

from flask import Flask, jsonify, request, render_template

from registry import Registry
from scanner import Scanner
from runner import Runner

app = Flask(__name__)

registry = Registry()
scanner  = Scanner()
runner   = Runner()


# ── Helpers ────────────────────────────────────────────────────────────────


def build_project_view(project: dict, scan: dict, state: dict) -> dict:
    """Merge registry data + live port scan + log errors into one view object."""
    port        = project["port"]
    name        = project["name"]
    managed_pid = state.get(name, {}).get("pid")
    port_info   = scan.get(port)

    status    = "stopped"
    proc_data = {}

    if port_info:
        pid_on_port = port_info["pid"]
        if managed_pid and pid_on_port == managed_pid and runner.is_running(managed_pid):
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

    view = {**project, "status": status, **proc_data}

    # Attach most recent error from logs (if any)
    recent_error = runner.find_recent_error(name)
    if recent_error:
        view["recent_error"] = recent_error
        # Running project with a logged error → flag it
        if status == "running":
            view["has_error"] = True

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

    port    = int(data["port"])
    project = {
        "name":         data["name"].strip(),
        "port":         port,
        "directory":    data["directory"].strip(),
        "start":        data["start"].strip(),
        "stop":         data.get("stop", "").strip(),
        "url":          data.get("url") or f"http://localhost:{port}",
        "tags":         data.get("tags") or [],
        "notes":        data.get("notes", "").strip(),
        "dependencies": data.get("dependencies") or [],
        "env":          data.get("env") or [],
    }
    try:
        return jsonify(registry.add(project)), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 409


@app.route("/api/projects/<name>", methods=["PUT"])
def update_project(name):
    data = request.json or {}
    try:
        return jsonify(registry.update(name, data))
    except ValueError as e:
        return jsonify({"error": str(e)}), 404


@app.route("/api/projects/<name>", methods=["DELETE"])
def remove_project(name):
    try:
        registry.remove(name)
        registry.clear_pid(name)
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404


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
        pid = runner.start(project)
        registry.set_pid(name, pid)
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
        registry.set_pid(project["name"], scan[port]["pid"])
        return jsonify(result), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 409


# ── Groups ─────────────────────────────────────────────────────────────────


@app.route("/api/groups", methods=["GET"])
def get_groups():
    return jsonify(registry.list_groups())


@app.route("/api/groups", methods=["POST"])
def add_group():
    data = request.json or {}
    name     = (data.get("name") or "").strip()
    projects = data.get("projects") or []

    if not name:
        return jsonify({"error": "Missing required field: name"}), 400
    if not isinstance(projects, list):
        return jsonify({"error": "'projects' must be a list"}), 400

    # Validate that all named projects exist
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

        # Already running → skip
        managed_pid = state.get(proj_name, {}).get("pid")
        if managed_pid and runner.is_running(managed_pid):
            results.append({"name": proj_name, "status": "already_running"})
            continue

        # Port conflict → skip
        if project["port"] in scan:
            proc = scan[project["port"]]
            results.append({
                "name":  proj_name,
                "error": f"Port {project['port']} in use by '{proc['name']}'"
            })
            continue

        try:
            pid = runner.start(project)
            registry.set_pid(proj_name, pid)
            results.append({"name": proj_name, "status": "started", "pid": pid})
            # Brief pause so this process can bind its port before the next starts
            time.sleep(0.4)
            scan = scanner.scan()   # refresh after each start
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
        elif mode == "browser":
            subprocess.Popen(["open", path])
        elif mode == "editor":
            # Open a specific file in the default editor
            subprocess.Popen(["open", expanded])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Entry point ────────────────────────────────────────────────────────────


if __name__ == "__main__":
    print("⊕  Seshat is running at http://localhost:9000")
    app.run(host="127.0.0.1", port=9000, debug=False)
