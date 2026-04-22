"""
runner.py — starts and stops project processes, captures logs to ~/.seshat/logs/.
"""

import os
import re
import signal
import subprocess
from datetime import datetime
from pathlib import Path

import psutil

LOG_DIR = Path.home() / ".seshat" / "logs"

# Patterns that indicate an error in log output
_ERROR_PATTERNS = [
    "Traceback",
    "Error:",
    "Exception:",
    " Error ",
    "FATAL",
    "CRITICAL",
    "SyntaxError",
    "TypeError",
    "ValueError",
    "KeyError",
    "AttributeError",
    "ImportError",
    "NameError",
    "RuntimeError",
    "AssertionError",
    "UnhandledPromiseRejectionWarning",
    "ENOENT",
    "EADDRINUSE",
    "ECONNREFUSED",
    "panic:",
    "fatal error:",
]

_IGNORE_PATTERNS = [
    "DeprecationWarning",
    "ExperimentalWarning",
    "No such file or directory: '.env'",
]

_PY_FILE_RE = re.compile(r'File "([^"]+)", line (\d+)')
_JS_FILE_RE = re.compile(r'\bat .+? \((.+?):(\d+):\d+\)')


class Runner:

    # ── Helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _has_vite_invocation(directory: Path, cmd: str) -> bool:
        """Return True if the project uses Vite and the start command invokes it.

        Checks both the start command (for 'vite' or 'npm run dev') and the
        project directory (for a vite.config.* file) so we only rewrite
        commands for genuine Vite projects.
        """
        if not re.search(r"\bnpm run dev\b|\bvite\b", cmd):
            return False
        vite_config_names = ("vite.config.js", "vite.config.ts", "vite.config.mjs", "vite.config.cjs")
        return any((directory / name).exists() for name in vite_config_names)

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def start(self, project: dict, extra_env: dict | None = None) -> int:
        """
        Start a project. Stdout and stderr are captured to
        ~/.seshat/logs/<name>.log. Vault-resolved env vars are injected
        via extra_env. Returns the child PID.
        """
        directory = Path(project["directory"]).expanduser().resolve()
        if not directory.exists():
            raise ValueError(
                f"Directory not found: {project['directory']}\n"
                f"(Resolved to: {directory})"
            )

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOG_DIR / f"{project['name']}.log"

        # Build environment: inherit OS env, force unbuffered Python output,
        # inject the Seshat-configured port, then layer in vault secrets.
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        start_cmd = project["start"]
        if project.get("port"):
            port = project["port"]
            if self._has_vite_invocation(directory, start_cmd):
                # Vite projects: pass port via CLI flag so Vite (the user-facing
                # process Caddy proxies to) owns the configured port. The backend
                # process falls back to its own default port rather than competing.
                start_cmd = re.sub(r"\bnpm run dev\b", f"npm run dev -- --port {port}", start_cmd)
                start_cmd = re.sub(r"(?<!\w)vite(?!\s+--port)(?!\w)", f"vite --port {port}", start_cmd)
            else:
                env["PORT"] = str(port)
        if extra_env:
            env.update(extra_env)

        separator = (
            f"\n--- Started {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---\n"
            f"cmd: {start_cmd}\n"
        )
        with open(log_path, "a") as f:
            f.write(separator)

        log_file = open(log_path, "a")
        proc = subprocess.Popen(
            start_cmd,
            shell=True,
            cwd=directory,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
            env=env,
        )
        log_file.close()   # parent closes; child keeps writing
        return proc.pid

    def stop(self, pid: int) -> None:
        """Gracefully stop a process and its children."""
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
            return
        except (ProcessLookupError, PermissionError):
            pass
        except Exception:
            pass
        try:
            psutil.Process(pid).terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    def is_running(self, pid: int) -> bool:
        """Return True if the process is alive and not a zombie."""
        try:
            proc = psutil.Process(pid)
            return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False

    def owns_pid(self, managed_pid: int, other_pid: int) -> bool:
        """Return True if `other_pid` is `managed_pid` or one of its descendants.

        Required because `shell=True` makes the managed PID a shell wrapper,
        while the real server (npm → node, python → gunicorn, etc.) is a
        descendant and is the one that actually binds the port.
        """
        if managed_pid == other_pid:
            return True
        try:
            parent = psutil.Process(managed_pid)
            for child in parent.children(recursive=True):
                if child.pid == other_pid:
                    return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        return False

    # ── Logs ───────────────────────────────────────────────────────────────

    def log_path(self, project_name: str) -> Path:
        return LOG_DIR / f"{project_name}.log"

    def read_log_tail(self, project_name: str, n: int = 120) -> list[str]:
        """Return the last n lines of the most recent run."""
        path = self.log_path(project_name)
        if not path.exists():
            return []

        lines = path.read_text(errors="replace").splitlines()

        # Scope to current run (lines after the last separator)
        last_sep = 0
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].startswith("--- Started"):
                last_sep = i
                break

        run_lines = lines[last_sep:]
        return run_lines[-n:] if len(run_lines) > n else run_lines

    def find_recent_error(self, project_name: str) -> dict | None:
        """
        Scan the current run's log for the most recent error line.
        Returns {message, context, file_ref, short} or None.
        """
        lines = self.read_log_tail(project_name, n=300)
        if not lines:
            return None

        for i in range(len(lines) - 1, -1, -1):
            line = lines[i]
            if not any(pat in line for pat in _ERROR_PATTERNS):
                continue
            if any(pat in line for pat in _IGNORE_PATTERNS):
                continue

            ctx_start = max(0, i - 3)
            ctx_end   = min(len(lines), i + 6)
            file_ref  = _extract_file_ref(lines, i)

            short = None
            if file_ref:
                short = f"{Path(file_ref['path']).name}:{file_ref['line']}"

            return {
                "message":  line.strip(),
                "context":  lines[ctx_start:ctx_end],
                "file_ref": file_ref,
                "short":    short,
            }

        return None


def _extract_file_ref(lines: list[str], error_idx: int) -> dict | None:
    """Try to find a file:line reference near an error."""
    for j in range(error_idx, max(-1, error_idx - 15), -1):
        m = _PY_FILE_RE.search(lines[j]) or _JS_FILE_RE.search(lines[j])
        if m:
            return {"path": m.group(1), "line": int(m.group(2))}
    return None
