"""
local_scanner.py — Local filesystem project discovery for Seshat.
"""
import json
import re
from pathlib import Path

from github import _PORT_PATTERNS, _START_PATTERNS

_SIGNAL_FILES = {
    "package.json", "pyproject.toml", "setup.py", "setup.cfg",
    "Cargo.toml", "go.mod", "Gemfile", "requirements.txt", "Makefile",
}


_ENV_FILES = {".env", ".env.local", ".env.example"}

# Pattern for -p PORT flag (e.g. "next dev -p 3100")
_FLAG_PORT_PATTERN = re.compile(r"-p\s+(\d{4,5})")


def _is_candidate(directory: Path) -> bool:
    """Return True if directory contains at least one signal file, .env file, or .git/."""
    try:
        for child in directory.iterdir():
            if child.name in _SIGNAL_FILES:
                return True
            if child.name in _ENV_FILES:
                return True
            if child.name == ".git" and child.is_dir():
                return True
            if child.suffix == ".xcodeproj" and child.is_dir():
                return True
    except PermissionError:
        return False
    return False


def _read(path: Path) -> str | None:
    """Read a file, return None on any error."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def _find_port(text: str) -> str | None:
    """Apply _PORT_PATTERNS (and -p flag) to text, return first match."""
    for pat in _PORT_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1)
    m = _FLAG_PORT_PATTERN.search(text)
    if m:
        return m.group(1)
    return None


def _find_start(text: str) -> str | None:
    """Apply _START_PATTERNS to text, return first match stripped."""
    m = _START_PATTERNS.search(text)
    return m.group(0).strip() if m else None


def _extract(project_dir: Path) -> dict:
    """Extract port and start command from files in project_dir."""
    port = None
    start = None

    # ── Port extraction ────────────────────────────────────────────────────
    # 1. .env files
    for env_name in (".env", ".env.local", ".env.example"):
        if port:
            break
        text = _read(project_dir / env_name)
        if text:
            m = re.search(r"^PORT=(\d{4,5})", text, re.MULTILINE)
            if m:
                port = m.group(1)

    # 2. package.json scripts
    if not port:
        text = _read(project_dir / "package.json")
        if text:
            try:
                pkg = json.loads(text)
                scripts = pkg.get("scripts", {})
                for key in ("dev", "start"):
                    val = scripts.get(key, "")
                    p = _find_port(val)
                    if p:
                        port = p
                        break
            except Exception:
                pass

    # 3. Makefile
    if not port:
        text = _read(project_dir / "Makefile")
        if text:
            port = _find_port(text)

    # 4. *.py files (first 200 lines each)
    if not port:
        for py_file in sorted(project_dir.glob("*.py"))[:5]:
            text = _read(py_file)
            if text:
                excerpt = "\n".join(text.splitlines()[:200])
                port = _find_port(excerpt)
                if port:
                    break

    # 5. README
    if not port:
        for readme_name in ("README.md", "README.rst", "README"):
            text = _read(project_dir / readme_name)
            if text:
                port = _find_port(text)
                if port:
                    break

    # ── Start command extraction ───────────────────────────────────────────
    # 1. package.json scripts.dev then scripts.start
    text = _read(project_dir / "package.json")
    if text:
        try:
            pkg = json.loads(text)
            scripts = pkg.get("scripts", {})
            for key in ("dev", "start"):
                val = scripts.get(key, "").strip()
                if val:
                    start = val
                    break
        except Exception:
            pass

    # 2. Makefile
    if not start:
        text = _read(project_dir / "Makefile")
        if text:
            start = _find_start(text)

    # 3. README code block
    if not start:
        for readme_name in ("README.md", "README.rst", "README"):
            text = _read(project_dir / readme_name)
            if text:
                code_blocks = re.findall(r"```[^\n]*\n(.*?)```", text, re.DOTALL)
                for block in code_blocks:
                    start = _find_start(block)
                    if start:
                        break
            if start:
                break

    return {"port": port, "start": start}


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

        lower_names = {n.lower() for n in registered_names}
        results = []
        for child in sorted(children):
            if not child.is_dir():
                continue
            if not _is_candidate(child):
                continue
            name = child.name
            registered = (
                name.lower() in lower_names
                or str(child).lower() in lower_names
            )
            extracted = _extract(child)
            results.append({
                "name":       name,
                "directory":  str(child),
                "port":       extracted["port"],
                "start":      extracted["start"],
                "registered": registered,
            })
        return results
