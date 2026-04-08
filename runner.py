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

# Patterns that are noisy and should be ignored even if they match above
_IGNORE_PATTERNS = [
    "DeprecationWarning",
    "ExperimentalWarning",
    "No such file or directory: '.env'",
]

_PY_FILE_RE = re.compile(r'File "([^"]+)", line (\d+)')
_JS_FILE_RE = re.compile(r'\bat .+? \((.+?):(\d+):\d+\)')


class Runner:

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def start(self, project: dict) -> int:
        """
        Start a project. Stdout and stderr are captured to
        ~/.seshat/logs/<name>.log. Returns the child PID.
        """
        directory = Path(project["directory"]).expanduser().resolve()
        if not directory.exists():
            raise ValueError(
                f"Directory not found: {project['directory']}\n"
                f"(Resolved to: {directory})"
            )

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOG_DIR / f"{project['name']}.log"

        separator = (
            f"\n--- Started {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---\n"
            f"cmd: {project['start']}\n"
        )
        with open(log_path, "a") as f:
            f.write(separator)

        log_file = open(log_path, "a")
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}

        proc = subprocess.Popen(
            project["start"],
            shell=True,
            cwd=directory,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,   # own process group → clean group kill
            env=env,
        )
        log_file.close()   # parent closes its copy; child keeps writing
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

    # ── Logs ───────────────────────────────────────────────────────────────

    def log_path(self, project_name: str) -> Path:
        return LOG_DIR / f"{project_name}.log"

    def read_log_tail(self, project_name: str, n: int = 120) -> list[str]:
        """Return the last n lines of the most recent run."""
        path = self.log_path(project_name)
        if not path.exists():
            return []

        lines = path.read_text(errors="replace").splitlines()

        # Find the last "--- Started" separator → scope to current run
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
        Returns a dict with message, context, file_ref, and short label,
        or None if no error is found.
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
