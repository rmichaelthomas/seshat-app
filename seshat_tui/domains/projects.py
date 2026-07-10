"""domains/projects.py — Projects domain: table + rail + detail.

Ports/orphans/conflicts are folded in here as a status filter (per §4 of
the build prompt) rather than kept as a separate domain — orphan listeners
render as synthetic rows so visibility is preserved without a seventh tab.
"""

from __future__ import annotations

import webbrowser

from textual import work
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Static, TabPane

import receipts as receipts_module
from registry import Registry
from runner import Runner
from scanner import Scanner
from vault import Vault

from ..colors import COLORS, STATUS_COLORS, STATUS_GLYPHS
from ..data import build_sparkline, shorten_path
from ..palette import PaletteCommand
from ..screens import LogViewerModal, RegisterModal
from ..widgets import FilterInput, Rail

STATUS_GLYPHS = {**STATUS_GLYPHS, "orphan": "▲"}
STATUS_COLORS = {**STATUS_COLORS, "orphan": COLORS["orange"]}

_registry = Registry()
_runner = Runner()
_scanner = Scanner()
_vault = Vault()


class ProjectsDomainMixin:
    """Mixed into SeshatApp. Relies on App-level helpers: self._current_domain(),
    self._show_help(...), self.session_id, self.emblem."""

    def compose_projects(self):
        with TabPane("◈ Projects", id="tab-projects"):
            yield Static(self._projects_cmdstrip(), id="projects-cmdstrip", classes="cmdstrip")
            with Horizontal(id="projects-work", classes="work"):
                yield Rail(on_change=self._set_projects_filter, id="projects-rail")
                with Vertical(id="projects-pane", classes="pane"):
                    yield Static("", id="projects-panehead", classes="pane-head")
                    yield FilterInput(on_change=self._set_projects_query, id="projects-filter-input")
                    yield DataTable(id="projects-table", cursor_type="row")
                with Vertical(id="projects-detail", classes="detail"):
                    yield Static("[#9A8B6E]select a project[/#9A8B6E]")

    @staticmethod
    def _projects_cmdstrip() -> str:
        return (
            "[#9A8B6E]actions[/#9A8B6E]  "
            "[#F6C56E][#F6C56E b]s[/#F6C56E b] start[/#F6C56E]  "
            "[#C3B492][#E8AE52 b]x[/#E8AE52 b] stop[/#C3B492]  "
            "[#C3B492][#E8AE52 b]a[/#E8AE52 b] start group[/#C3B492]  "
            "[#C3B492][#E8AE52 b]o[/#E8AE52 b] logs[/#C3B492]  "
            "[#C3B492][#E8AE52 b]n[/#E8AE52 b] register[/#C3B492]"
        )

    def on_mount_projects(self) -> None:
        table = self.query_one("#projects-table", DataTable)
        # Status dot lives inside the NAME cell (not a separate column) —
        # matches the verified reference layout and keeps total column
        # width bounded so the pane never needs a horizontal scrollbar.
        table.add_column("NAME", width=16)
        table.add_column("PORT", width=6)
        table.add_column("STATUS", width=9)
        table.add_column("STARTED BY", width=12)
        self.projects_filter = "all"
        self.projects_query = ""
        self.projects_selected = None
        self._projects_view_cache: dict[str, dict] = {}

    def get_projects_palette_commands(self) -> list[PaletteCommand]:
        return [
            PaletteCommand("projects", "◈", "Start selected project", "s", self.action_projects_start),
            PaletteCommand("projects", "◈", "Stop selected project", "x", self.action_projects_stop),
            PaletteCommand("projects", "◈", "Start a group", "a", self.action_projects_start_group),
            PaletteCommand("projects", "◈", "View logs of selected project", "o", self.action_projects_logs),
            PaletteCommand("projects", "◈", "Register a new project", "n", self.action_projects_register),
            PaletteCommand("projects", "◈", "Open selected project in browser", "", self.action_projects_open),
        ]

    def get_projects_help(self) -> list[tuple[str, str]]:
        return [
            ("s", "start selected project"),
            ("x", "stop selected project"),
            ("a", "start a group"),
            ("o", "view logs"),
            ("n", "register a project"),
            ("/", "filter list"),
            ("↵", "open detail (open in browser: via palette)"),
        ]

    # ── Data refresh ─────────────────────────────────────────────────────

    @work(thread=True, group="projects-refresh", exclusive=True)
    def refresh_projects(self) -> None:
        scan = _scanner.scan()
        state = _registry.get_state()
        projects = _registry.list()
        from cli import _build_project_view
        views = [_build_project_view(p, scan, state) for p in projects]

        registered_ports = {p["port"] for p in projects} | {9000}
        orphans = []
        for port, info in sorted(scan.items()):
            if port in registered_ports:
                continue
            orphans.append({
                "name": info.get("name", "unknown"),
                "port": port,
                "composite_status": "orphan",
                "pid": info["pid"],
                "directory": "",
                "started_by": None,
                "dep_status": [],
                "_orphan": True,
            })

        groups = _registry.list_groups()
        recent = receipts_module.load(limit=100)
        self.call_from_thread(self._apply_projects_data, views, orphans, groups, recent)

    def _apply_projects_data(self, views: list, orphans: list, groups: list, recent: list) -> None:
        combined = views + orphans
        self._projects_view_cache = {v["name"]: v for v in combined}
        self._projects_groups = groups

        running  = sum(1 for v in combined if v["composite_status"] == "running")
        stopped  = sum(1 for v in combined if v["composite_status"] == "stopped")
        conflict = sum(1 for v in combined if v["composite_status"] == "conflict")
        orphan_n = len(orphans)

        status_rows = [
            ("all", "All", str(len(combined)), ""),
            ("running", "Running", str(running), "g"),
            ("stopped", "Stopped", str(stopped), ""),
            ("conflict", "Conflicts", str(conflict), "r"),
            ("orphan", "Orphans", str(orphan_n), "o"),
        ]
        group_rows = []
        for g in groups:
            names = set(g.get("projects", []))
            g_running = sum(1 for v in combined if v["name"] in names and v["composite_status"] == "running")
            group_rows.append((f"group:{g['name']}", g["name"], str(len(names)), "b"))

        rail = self.query_one("#projects-rail", Rail)
        current_filter = getattr(self, "projects_filter", "all")
        rail.build([("Status", status_rows), ("Groups", group_rows)], current_filter)

        self._render_projects_table()

        spark = build_sparkline(recent)
        head = self.query_one("#projects-panehead", Static)
        head.update(
            f"[b]{len(combined)} projects[/b] [#9A8B6E]· {running} running[/#9A8B6E]"
            f"          [#74C767]{spark}[/#74C767]"
        )

    def _filtered_projects_rows(self) -> list[dict]:
        combined = list(self._projects_view_cache.values())
        flt = getattr(self, "projects_filter", "all")
        if flt.startswith("group:"):
            group_name = flt[len("group:"):]
            group = next((g for g in getattr(self, "_projects_groups", []) if g["name"] == group_name), None)
            names = set(group.get("projects", [])) if group else set()
            combined = [v for v in combined if v["name"] in names]
        elif flt != "all":
            combined = [v for v in combined if v["composite_status"] == flt]

        query = getattr(self, "projects_query", "")
        if query:
            q = query.lower()
            combined = [v for v in combined if q in v["name"].lower()]
        return combined

    def _render_projects_table(self) -> None:
        table = self.query_one("#projects-table", DataTable)
        table.clear()
        for view in self._filtered_projects_rows():
            vstatus = view["composite_status"]
            glyph = STATUS_GLYPHS.get(vstatus, "○")
            color = STATUS_COLORS.get(vstatus, COLORS["text_3"])
            started_by = view.get("started_by") or ""
            attr_color = (
                COLORS["purple"] if started_by.startswith("mcp_session_") else
                COLORS["blue"] if started_by == "dashboard" else
                COLORS["amber"] if started_by.startswith(("cli", "tui")) else
                COLORS["text_3"]
            )
            table.add_row(
                f"[{color}]{glyph}[/{color}] {view['name']}",
                str(view["port"]),
                f"[{color}]{vstatus}[/{color}]",
                f"[{attr_color}]{started_by or '—'}[/{attr_color}]",
                key=view["name"],
            )

    def _set_projects_filter(self, key: str) -> None:
        self.projects_filter = key
        self._render_projects_table()

    def _set_projects_query(self, query: str) -> None:
        self.projects_query = query
        self._render_projects_table()

    # ── Selection / detail ──────────────────────────────────────────────

    def handle_projects_row_selected(self, event: DataTable.RowSelected) -> None:
        name = str(event.row_key.value)
        if name == getattr(self, "projects_selected", None):
            # Enter again on the row already showing in the detail panel —
            # matches the CTA's "↵ open in browser" (a fresh selection just
            # shows detail; DataTable itself owns Enter, so "open" only
            # makes sense as a second press on the same row).
            self.action_projects_open()
            return
        self.projects_selected = name
        self._render_projects_detail(name)

    def _render_projects_detail(self, name: str) -> None:
        view = self._projects_view_cache.get(name)
        panel = self.query_one("#projects-detail", Vertical)
        panel.remove_children()
        if not view:
            panel.mount(Static("[#9A8B6E]select a project[/#9A8B6E]"))
            return

        vstatus = view["composite_status"]
        color = STATUS_COLORS.get(vstatus, COLORS["text_3"])
        glyph = STATUS_GLYPHS.get(vstatus, "○")
        started_by = view.get("started_by")
        meta = f"[{color}]{glyph} {vstatus}[/{color}]"
        if started_by:
            meta += f"  [#9A8B6E]·[/#9A8B6E] [#6EA8C4]{started_by}[/#6EA8C4]"
        lines = [
            f"[b]{view['name']}[/b]",
            meta,
            "",
            "[#9A8B6E b]CONFIG[/#9A8B6E b]",
            f"port      [#63C6BE]{view['port']}[/#63C6BE]",
            f"pid       [#C3B492]{view.get('pid', '—')}[/#C3B492]",
        ]
        if view.get("directory"):
            lines.append(f"directory [#9A8B6E]{shorten_path(view['directory'])}[/#9A8B6E]")
        if view.get("recent_error"):
            err = view["recent_error"]
            lines += ["", "[#E8A052 b]Error[/#E8A052 b]", f"  [#9A8B6E]{err.get('message', '')[:70]}[/#9A8B6E]"]
        dep_status = view.get("dep_status", [])
        if dep_status:
            lines += ["", "[#9A8B6E b]DEPENDENCIES[/#9A8B6E b]"]
            for d in dep_status:
                dep_color = COLORS["green"] if d.get("status") == "connected" else COLORS["red"]
                lines.append(f"  [{dep_color}]●[/{dep_color}] {d.get('label', d.get('provider', '?'))}")

        panel.mount(Static("\n".join(lines)))

        if not view.get("_orphan"):
            cta_lines = []
            if vstatus == "running":
                cta_lines.append("[#E8AE52 b]x[/#E8AE52 b] stop this project")
            else:
                cta_lines.append("[#E8AE52 b]s[/#E8AE52 b] start this project")
            cta_lines.append("[#E8AE52 b]o[/#E8AE52 b] tail its logs")
            if view.get("port"):
                cta_lines.append("[#E8AE52 b]↵[/#E8AE52 b] open in browser")
            panel.mount(Static("\n".join(cta_lines), classes="cta-block"))

    # ── Actions ──────────────────────────────────────────────────────────

    def action_projects_start(self) -> None:
        if self._current_domain() != "projects" or not getattr(self, "projects_selected", None):
            return
        self._projects_start(self.projects_selected)

    def action_projects_stop(self) -> None:
        if self._current_domain() != "projects" or not getattr(self, "projects_selected", None):
            return
        self._projects_stop(self.projects_selected)

    def action_projects_open(self) -> None:
        if self._current_domain() != "projects" or not getattr(self, "projects_selected", None):
            return
        view = self._projects_view_cache.get(self.projects_selected)
        if view and view.get("port") and not view.get("_orphan"):
            webbrowser.open(f"http://localhost:{view['port']}")

    def action_projects_logs(self) -> None:
        if self._current_domain() != "projects" or not getattr(self, "projects_selected", None):
            return
        name = self.projects_selected
        lines = _runner.read_log_tail(name, n=200)
        self.push_screen(LogViewerModal(name, lines))

    def action_projects_register(self) -> None:
        if self._current_domain() != "projects":
            return
        self.push_screen(RegisterModal(self._do_register))

    def action_projects_start_group(self) -> None:
        if self._current_domain() != "projects":
            return
        flt = getattr(self, "projects_filter", "all")
        groups = getattr(self, "_projects_groups", [])
        target = None
        if flt.startswith("group:"):
            target = flt[len("group:"):]
        elif len(groups) == 1:
            target = groups[0]["name"]
        if not target:
            self.notify("Select a group in the rail first, or use the palette.", severity="warning")
            return
        self._projects_start_group(target)

    def _do_register(self, fields: dict) -> tuple[bool, str]:
        env_before = receipts_module.snapshot()
        project = {
            "name": fields["name"],
            "port": fields["port"],
            "scheme": "http",
            "directory": fields["directory"],
            "start": fields["start"],
            "stop": fields.get("stop", ""),
            "url": f"http://localhost:{fields['port']}",
            "tags": [],
            "notes": "",
            "dependencies": [],
            "env": [],
        }
        try:
            result_project = _registry.add(project)
        except ValueError as e:
            receipts_module.emit(
                action="register_project",
                target={"project": fields["name"], "port": fields["port"]},
                result={"status": "failure", "error": str(e)},
                env_before=env_before,
                session_id=self.session_id,
                actor_type="tui_session",
                agent_hint="tui",
            )
            return False, str(e)

        receipts_module.emit(
            action="register_project",
            target={"project": fields["name"], "port": fields["port"], "directory": fields["directory"]},
            result={"status": "success", "project": result_project},
            env_before=env_before,
            session_id=self.session_id,
            actor_type="tui_session",
            agent_hint="tui",
        )
        self.refresh_projects()
        self._show_echo("projects", f"seshat register (via TUI: {fields['name']})", "receipt emitted")
        return True, f"{fields['name']} registered."

    @work(thread=True)
    def _projects_start(self, name: str) -> None:
        env_before = receipts_module.snapshot()
        project = _registry.get(name)
        if not project:
            self.call_from_thread(self.notify, f"Project '{name}' not found.", severity="error")
            return
        scan = _scanner.scan()
        if project["port"] in scan:
            self.call_from_thread(self.notify, f"Port {project['port']} in use.", severity="error")
            return
        try:
            extra_env = _vault.resolve_for_project(name, project.get("env", []))
            pid = _runner.start(project, extra_env=extra_env)
            _registry.set_pid(name, pid, started_by=self.session_id)
            self.call_from_thread(self.notify, f"{name} started (PID {pid}).")
            self.call_from_thread(self.refresh_projects)
            self.call_from_thread(self._show_echo, "projects", f"seshat start {name}", "receipt emitted")
            receipts_module.emit(
                action="start_project", target={"project": name}, result={"status": "success", "pid": pid},
                env_before=env_before, session_id=self.session_id, actor_type="tui_session", agent_hint="tui",
            )
        except Exception as e:
            self.call_from_thread(self.notify, str(e), severity="error")
            receipts_module.emit(
                action="start_project", target={"project": name}, result={"status": "failure", "error": str(e)},
                env_before=env_before, session_id=self.session_id, actor_type="tui_session", agent_hint="tui",
            )

    @work(thread=True)
    def _projects_stop(self, name: str) -> None:
        env_before = receipts_module.snapshot()
        state = _registry.get_state()
        pid = state.get(name, {}).get("pid")
        project = _registry.get(name)
        if not pid:
            self.call_from_thread(self.notify, f"{name} has no managed process.", severity="warning")
            return
        _runner.stop(pid)
        _registry.clear_pid(name)
        self.call_from_thread(self.notify, f"{name} stopped.")
        self.call_from_thread(self.refresh_projects)
        self.call_from_thread(self._show_echo, "projects", f"seshat stop {name}", "receipt emitted")
        receipts_module.emit(
            action="stop_project",
            target={"project": name, "port": project["port"] if project else 0},
            result={"status": "success", "stopped_pid": pid},
            env_before=env_before, session_id=self.session_id, actor_type="tui_session", agent_hint="tui",
        )

    @work(thread=True)
    def _projects_start_group(self, group_name: str) -> None:
        env_before = receipts_module.snapshot()
        grp = _registry.get_group(group_name)
        if not grp:
            self.call_from_thread(self.notify, f"Group '{group_name}' not found.", severity="error")
            return
        scan = _scanner.scan()
        state = _registry.get_state()
        results = []
        for proj_name in grp.get("projects", []):
            project = _registry.get(proj_name)
            if not project:
                results.append({"name": proj_name, "error": "not found"})
                continue
            managed_pid = state.get(proj_name, {}).get("pid")
            if managed_pid and _runner.is_running(managed_pid):
                results.append({"name": proj_name, "status": "already_running"})
                continue
            if project["port"] in scan:
                results.append({"name": proj_name, "error": f"port {project['port']} in use"})
                continue
            try:
                extra_env = _vault.resolve_for_project(proj_name, project.get("env", []))
                pid = _runner.start(project, extra_env=extra_env)
                _registry.set_pid(proj_name, pid, started_by=self.session_id)
                results.append({"name": proj_name, "status": "started", "pid": pid})
                scan = _scanner.scan()
            except Exception as e:
                results.append({"name": proj_name, "error": str(e)})

        self.call_from_thread(self.notify, f"Group '{group_name}' start: {len(results)} project(s) processed.")
        self.call_from_thread(self.refresh_projects)
        self.call_from_thread(self._show_echo, "projects", f"seshat start --group {group_name}", "receipt emitted")
        receipts_module.emit(
            action="start_group", target={"group": group_name},
            result={"status": "success", "group": group_name, "results": results},
            env_before=env_before, session_id=self.session_id, actor_type="tui_session", agent_hint="tui",
        )
