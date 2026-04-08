#!/usr/bin/env python3
"""
Seshat — Local project registry and control room.
Runs at http://localhost:9000
"""

import subprocess
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
    """Merge registry data with live runtime state into a single view object."""
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

    return {**project, "status": status, **proc_data}


# ── Routes — dashboard ─────────────────────────────────────────────────────


@app.route("/")
def index():
    return render_template("index.html")


# ── Routes — projects ──────────────────────────────────────────────────────


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


# ── Routes — start / stop ──────────────────────────────────────────────────


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
                f"Stop that process or change this project's port."
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


# ── Routes — orphans ───────────────────────────────────────────────────────


@app.route("/api/orphans", methods=["GET"])
def get_orphans():
    scan             = scanner.scan()
    registered_ports = {p["port"] for p in registry.list()}
    registered_ports.add(9000)   # never flag Seshat itself

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
    """Promote an orphan process to a registered project."""
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


# ── Routes — open in Finder / Terminal / Browser ───────────────────────────


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
            subprocess.Popen(["open", path])   # path is a URL here
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Entry point ────────────────────────────────────────────────────────────


if __name__ == "__main__":
    print("⊕  Seshat is running at http://localhost:9000")
    app.run(host="127.0.0.1", port=9000, debug=False)
