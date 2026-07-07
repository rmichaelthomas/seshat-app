"""
seshat_tui.py — Seshat interactive TUI (Textual-based).

Launched through cli.py via _launch_tui(). Not a standalone entry point.
"""

import json
import uuid
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, DataTable, Static, TabbedContent, TabPane
from textual.reactive import reactive
from textual.binding import Binding
from textual import work

from registry import Registry
from runner import Runner
from vault import Vault
from scanner import Scanner
import deps as deps_module
import receipts as receipts_module

# ── Module instances ────────────────────────────────────────────────────────

registry = Registry()
runner   = Runner()
vault    = Vault()
scanner  = Scanner()

# ── Session identity ────────────────────────────────────────────────────────

SESSION_ID = f"tui_{uuid.uuid4().hex[:12]}"

# ── Color palette ───────────────────────────────────────────────────────────

COLORS = {
    "bg":          "#0c0c0f",
    "surface":     "#141418",
    "surface_2":   "#1e1e24",
    "surface_3":   "#252530",
    "border":      "#2a2a34",
    "text":        "#e2e2ea",
    "text_dim":    "#9494a4",
    "text_muted":  "#6b6b7a",
    "green":       "#22c55e",
    "red":         "#ef4444",
    "yellow":      "#f59e0b",
    "blue":        "#818cf8",
    "orange":      "#f97316",
    "purple":      "#a855f7",
}

STATUS_GLYPHS = {
    "running":  "●",
    "stopped":  "○",
    "conflict": "✗",
    "error":    "⚠",
    "degraded": "◐",
}

STATUS_COLORS = {
    "running":  COLORS["green"],
    "stopped":  COLORS["text_muted"],
    "conflict": COLORS["red"],
    "error":    COLORS["orange"],
    "degraded": COLORS["yellow"],
}

# ── Module-level helpers (shared with cli.py import) ───────────────────────

def _load_receipts(limit: int = 50) -> list:
    receipts_dir = Path.home() / ".seshat" / "receipts"
    if not receipts_dir.exists():
        return []
    files = sorted(receipts_dir.glob("*.json"), reverse=True)
    results = []
    for f in files:
        if len(results) >= limit:
            break
        try:
            results.append(json.loads(f.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    return results


def _shorten_path(path: str) -> str:
    home = str(Path.home())
    return ("~" + path[len(home):]) if path.startswith(home) else path


# ── TUI Application ─────────────────────────────────────────────────────────

class SeshatApp(App):
    """Seshat — local environmental agent harness."""

    CSS = """
    Screen {
        background: #0c0c0f;
    }
    DataTable {
        background: #0c0c0f;
        border: none;
    }
    DataTable > .datatable--header {
        background: #141418;
        color: #6b6b7a;
        text-style: bold;
    }
    DataTable > .datatable--cursor {
        background: #1e1e24;
        color: #e2e2ea;
    }
    #detail-panel {
        background: #141418;
        border-left: solid #2a2a34;
        padding: 1 2;
        width: 40;
    }
    #detail-panel.hidden { display: none; }
    .section-title {
        color: #6b6b7a;
        text-style: bold;
    }
    Footer {
        background: #141418;
        color: #6b6b7a;
    }
    """

    BINDINGS = [
        Binding("q",      "quit",            "Quit"),
        Binding("s",      "start_selected",  "Start"),
        Binding("x",      "stop_selected",   "Stop"),
        Binding("r",      "refresh",         "Refresh"),
        Binding("p",      "show_ports",      "Ports"),
        Binding("R",      "show_receipts",   "Receipts"),
        Binding("escape", "close_detail",    "Close"),
        Binding("?",      "show_help",       "Help"),
    ]

    TITLE = "Seshat"

    selected_project: reactive[str | None] = reactive(None)
    detail_open:      reactive[bool]       = reactive(False)
    active_tab:       reactive[str]        = reactive("projects")

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(id="tabs"):
            with TabPane("Projects", id="tab-projects"):
                yield DataTable(id="projects-table", cursor_type="row")
            with TabPane("Ports", id="tab-ports"):
                yield DataTable(id="ports-table", cursor_type="row")
            with TabPane("Receipts", id="tab-receipts"):
                yield DataTable(id="receipts-table", cursor_type="row")
        yield Static(id="detail-panel", classes="hidden")
        yield Footer()

    def on_mount(self) -> None:
        self._setup_projects_table()
        self._setup_ports_table()
        self._setup_receipts_table()
        self.refresh_data()
        self.set_interval(3, self.refresh_data)

    # ── Table setup ──────────────────────────────────────────────────────────

    def _setup_projects_table(self) -> None:
        table = self.query_one("#projects-table", DataTable)
        table.add_columns("", "Name", "Port", "Status", "Started by", "Directory")

    def _setup_ports_table(self) -> None:
        table = self.query_one("#ports-table", DataTable)
        table.add_columns("Port", "PID", "Kind", "Process")

    def _setup_receipts_table(self) -> None:
        table = self.query_one("#receipts-table", DataTable)
        table.add_columns("", "Time", "Action", "Target", "Actor")

    # ── Data refresh ─────────────────────────────────────────────────────────

    @work(thread=True)
    def refresh_data(self) -> None:
        scan     = scanner.scan()
        state    = registry.get_state()
        projects = registry.list()
        from cli import _build_project_view
        views = [_build_project_view(p, scan, state) for p in projects]
        self.call_from_thread(self._update_projects_table, views)
        self.call_from_thread(self._update_ports_table, scan, state)
        self.call_from_thread(self._update_receipts_table)

    def _update_projects_table(self, views: list) -> None:
        table = self.query_one("#projects-table", DataTable)
        table.clear()
        for view in views:
            vstatus    = view["composite_status"]
            glyph      = STATUS_GLYPHS.get(vstatus, "○")
            color      = STATUS_COLORS.get(vstatus, COLORS["text_muted"])
            started_by = view.get("started_by", "")
            attr_color = (
                COLORS["purple"] if (started_by and started_by.startswith("mcp_session_")) else
                COLORS["blue"]   if started_by == "dashboard" else
                COLORS["green"]  if (started_by and started_by.startswith("cli")) else
                COLORS["text_muted"]
            )
            table.add_row(
                f"[{color}]{glyph}[/{color}]",
                view["name"],
                str(view["port"]),
                f"[{color}]{vstatus}[/{color}]",
                f"[{attr_color}]{started_by or '—'}[/{attr_color}]",
                _shorten_path(view.get("directory", "")),
                key=view["name"],
            )

    def _update_ports_table(self, scan: dict, state: dict) -> None:
        table        = self.query_one("#ports-table", DataTable)
        port_to_proj = {p["port"]: p["name"] for p in registry.list()}
        managed_pids = {name: info.get("pid") for name, info in state.items() if info.get("pid")}
        table.clear()

        KIND_COLORS = {
            "seshat":   COLORS["blue"],
            "project":  COLORS["green"],
            "conflict": COLORS["red"],
            "orphan":   COLORS["orange"],
        }
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
            color = KIND_COLORS.get(kind, COLORS["text_muted"])
            label = project_name if project_name else info.get("name", "unknown")
            table.add_row(
                str(port), str(pid),
                f"[{color}]{kind}[/{color}]",
                label,
            )

    def _update_receipts_table(self) -> None:
        table = self.query_one("#receipts-table", DataTable)
        table.clear()
        rows = _load_receipts(limit=50)
        for r in rows:
            is_success = r.get("result", {}).get("status") == "success"
            glyph      = f"[{COLORS['green']}]✓[/{COLORS['green']}]" if is_success else f"[{COLORS['red']}]✗[/{COLORS['red']}]"
            ts         = r.get("timestamp", "")[:19].replace("T", " ")
            actor      = r.get("actor", {})
            short_id   = actor.get("session_id", "")[:14] or "—"
            target     = r.get("target", {})
            target_str = target.get("project") or target.get("group") or target.get("key") or "—"
            table.add_row(glyph, ts, r.get("action", ""), target_str, short_id)

    # ── Row selection and detail panel ───────────────────────────────────────

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "projects-table":
            return
        name = str(event.row_key.value)
        self.selected_project = name
        self.detail_open      = True
        self._render_detail(name)

    def _render_detail(self, name: str) -> None:
        panel   = self.query_one("#detail-panel", Static)
        project = registry.get(name)
        if not project:
            return
        scan  = scanner.scan()
        state = registry.get_state()
        from cli import _build_project_view
        view  = _build_project_view(project, scan, state)

        vstatus    = view["composite_status"]
        color      = STATUS_COLORS.get(vstatus, COLORS["text_muted"])
        glyph      = STATUS_GLYPHS.get(vstatus, "○")
        started_by = view.get("started_by", "—")
        pid        = view.get("pid", "—")

        lines = [
            f"[bold]{name}[/bold]  [cyan]:{view['port']}[/cyan]",
            "",
            f"[{COLORS['text_muted']}]Status[/{COLORS['text_muted']}]     [{color}]{glyph} {vstatus}[/{color}]",
            f"[{COLORS['text_muted']}]Started by[/{COLORS['text_muted']}] [{COLORS['purple']}]{started_by}[/{COLORS['purple']}]",
            f"[{COLORS['text_muted']}]PID[/{COLORS['text_muted']}]        [{COLORS['text_dim']}]{pid}[/{COLORS['text_dim']}]",
            f"[{COLORS['text_muted']}]Directory[/{COLORS['text_muted']}]  [{COLORS['text_muted']}]{_shorten_path(view.get('directory',''))}[/{COLORS['text_muted']}]",
        ]

        if view.get("recent_error"):
            err = view["recent_error"]
            lines += [
                "",
                f"[{COLORS['orange']}]Error[/{COLORS['orange']}]",
                f"  [{COLORS['text_dim']}]{err.get('message','')[:60]}[/{COLORS['text_dim']}]",
            ]

        dep_status = view.get("dep_status", [])
        if dep_status:
            lines += ["", f"[{COLORS['text_muted']}]Dependencies[/{COLORS['text_muted']}]"]
            for d in dep_status:
                dep_color = COLORS["green"] if d.get("status") == "connected" else COLORS["red"]
                lines.append(f"  [{dep_color}]●[/{dep_color}] {d.get('label', d.get('provider','?'))}")

        lines += [
            "",
            f"[{COLORS['text_muted']}][s] start  [x] stop  [Esc] close[/{COLORS['text_muted']}]",
        ]

        panel.update("\n".join(lines))
        panel.remove_class("hidden")

    # ── Actions ──────────────────────────────────────────────────────────────

    def action_close_detail(self) -> None:
        self.detail_open = False
        panel = self.query_one("#detail-panel", Static)
        panel.add_class("hidden")

    def action_refresh(self) -> None:
        self.refresh_data()

    def action_start_selected(self) -> None:
        if not self.selected_project:
            return
        self._start_project(self.selected_project)

    def action_stop_selected(self) -> None:
        if not self.selected_project:
            return
        self._stop_project(self.selected_project)

    def action_show_ports(self) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        tabs.active = "tab-ports"

    def action_show_receipts(self) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        tabs.active = "tab-receipts"

    def action_show_help(self) -> None:
        self.notify(
            "s=start  x=stop  p=ports  R=receipts  r=refresh  q=quit  ?=help",
            title="Keybindings",
            timeout=6,
        )

    @work(thread=True)
    def _start_project(self, name: str) -> None:
        env_before = receipts_module.snapshot()
        project    = registry.get(name)
        if not project:
            self.call_from_thread(self.notify, f"Project '{name}' not found.", severity="error")
            return
        scan = scanner.scan()
        if project["port"] in scan:
            self.call_from_thread(self.notify, f"Port {project['port']} in use.", severity="error")
            return
        try:
            extra_env = vault.resolve_for_project(name, project.get("env", []))
            pid       = runner.start(project, extra_env=extra_env)
            registry.set_pid(name, pid, started_by=SESSION_ID)
            self.call_from_thread(self.notify, f"{name} started (PID {pid}).")
            self.call_from_thread(self.refresh_data)
            result = {"status": "success", "pid": pid}
            receipts_module.emit(
                action="start_project",
                target={"project": name},
                result=result,
                env_before=env_before,
                session_id=SESSION_ID,
                actor_type="tui_session",
                agent_hint="tui",
            )
        except Exception as e:
            self.call_from_thread(self.notify, str(e), severity="error")
            receipts_module.emit(
                action="start_project",
                target={"project": name},
                result={"status": "failure", "error": str(e)},
                env_before=env_before,
                session_id=SESSION_ID,
                actor_type="tui_session",
                agent_hint="tui",
            )

    @work(thread=True)
    def _stop_project(self, name: str) -> None:
        env_before = receipts_module.snapshot()
        state      = registry.get_state()
        pid        = state.get(name, {}).get("pid")
        project    = registry.get(name)
        if not pid:
            self.call_from_thread(self.notify, f"{name} has no managed process.", severity="warning")
            return
        runner.stop(pid)
        registry.clear_pid(name)
        self.call_from_thread(self.notify, f"{name} stopped.")
        self.call_from_thread(self.refresh_data)
        result = {"status": "success", "stopped_pid": pid}
        receipts_module.emit(
            action="stop_project",
            target={"project": name, "port": project["port"] if project else 0},
            result=result,
            env_before=env_before,
            session_id=SESSION_ID,
            actor_type="tui_session",
            agent_hint="tui",
        )
