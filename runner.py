"""
runner.py — starts and stops project processes.
"""

import os
import signal
import subprocess
from pathlib import Path

import psutil


class Runner:
    def start(self, project: dict) -> int:
        """
        Start a project using its configured start command.
        Returns the PID of the launched process.
        Raises ValueError if the directory does not exist.
        """
        directory = Path(project["directory"]).expanduser().resolve()
        if not directory.exists():
            raise ValueError(
                f"Directory not found: {project['directory']}\n"
                f"(Resolved to: {directory})"
            )

        proc = subprocess.Popen(
            project["start"],
            shell=True,
            cwd=directory,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,   # own process group → clean group kill
        )
        return proc.pid

    def stop(self, pid: int) -> None:
        """
        Gracefully stop a process and its children.
        Sends SIGTERM to the process group; falls back to direct terminate.
        """
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
            return
        except (ProcessLookupError, PermissionError):
            pass   # process already gone or not in a killable group
        except Exception:
            pass

        # Fallback: target the process directly
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
