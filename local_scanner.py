"""
local_scanner.py — Local filesystem project discovery for Seshat.
"""
from pathlib import Path

from github import _PORT_PATTERNS, _START_PATTERNS

_SIGNAL_FILES = {
    "package.json", "pyproject.toml", "setup.py", "setup.cfg",
    "Cargo.toml", "go.mod", "Gemfile", "requirements.txt", "Makefile",
}


def _is_candidate(directory: Path) -> bool:
    """Return True if the directory contains at least one signal file or .git/."""
    try:
        for child in directory.iterdir():
            if child.name in _SIGNAL_FILES:
                return True
            if child.name == ".git" and child.is_dir():
                return True
            if child.suffix == ".xcodeproj" and child.is_dir():
                return True
    except PermissionError:
        return False
    return False


class LocalScanner:

    def scan(self, directory: str, registered_names: set[str]) -> list[dict]:
        """
        Walk one level deep into directory, return project candidates.
        Raises ValueError if directory does not exist or is unreadable.
        """
        root = Path(directory).expanduser().resolve()
        if not root.exists():
            raise ValueError(f"Directory not found: {directory}")
        try:
            children = list(root.iterdir())
        except PermissionError:
            raise ValueError(f"Cannot read directory: {directory}")

        results = []
        for child in sorted(children):
            if not child.is_dir():
                continue
            if not _is_candidate(child):
                continue
            name = child.name
            registered = (
                name.lower() in {n.lower() for n in registered_names}
                or str(child).lower() in {n.lower() for n in registered_names}
            )
            results.append({
                "name":       name,
                "directory":  str(child),
                "port":       None,
                "start":      None,
                "registered": registered,
            })
        return results
