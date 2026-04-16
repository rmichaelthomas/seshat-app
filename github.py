"""
github.py — GitHub API client for Seshat's import feature.
"""
import json
import re
import urllib.request
import urllib.error
from base64 import b64decode
from pathlib import Path
from urllib.parse import urlencode

_API = "https://api.github.com"
_START_PATTERNS = re.compile(
    r"^\s*(python3?|npm|node|uvicorn|flask|yarn|cargo run|go run|\.\/gradlew|make\s+(?:run|start|serve|dev|up))\b.*",
    re.MULTILINE,
)
_SETUP_COMMANDS = re.compile(
    r"^\s*("
    r"npm\s+(?:install|ci|init|link|uninstall)"
    r"|yarn\s+(?:add|install)"
    r"|pip3?\s+install"
    r"|cargo\s+build"
    r"|go\s+(?:get|install)"
    r"|make\s+(?:install|build)"
    r"|bundle\s+install"
    r")\b",
    re.MULTILINE,
)
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)", re.MULTILINE)

_RUN_HEADINGS = re.compile(
    r"(?:run|start|usage|develop|launch|getting.started|quick.start)",
    re.IGNORECASE,
)
_SETUP_HEADINGS = re.compile(
    r"(?:setup|install|prerequisites|requirements|build|dependencies)",
    re.IGNORECASE,
)


def _split_sections(readme: str) -> list[tuple[str, str]]:
    """Split README into (heading, body) tuples. Preamble gets heading ''."""
    splits = list(_HEADING_RE.finditer(readme))
    sections: list[tuple[str, str]] = []
    if not splits:
        return [("", readme)]
    # Preamble before first heading
    if splits[0].start() > 0:
        sections.append(("", readme[: splits[0].start()]))
    for i, m in enumerate(splits):
        heading = m.group(2).strip()
        start = m.end()
        end = splits[i + 1].start() if i + 1 < len(splits) else len(readme)
        sections.append((heading, readme[start:end]))
    return sections


def _find_commands_in_text(text: str) -> list[str]:
    """Find all start-pattern matches in text, excluding setup commands.
    Searches fenced code blocks first, then bare lines."""
    commands: list[str] = []
    seen = set()
    code_blocks = re.findall(r"```[^\n]*\n(.*?)```", text, re.DOTALL)
    for block in code_blocks:
        for m in _START_PATTERNS.finditer(block):
            cmd = m.group(0).strip()
            if not _SETUP_COMMANDS.match(cmd) and cmd not in seen:
                commands.append(cmd)
                seen.add(cmd)
    # Also search bare lines (outside code blocks)
    bare = re.sub(r"```[^\n]*\n.*?```", "", text, flags=re.DOTALL)
    for m in _START_PATTERNS.finditer(bare):
        cmd = m.group(0).strip()
        if not _SETUP_COMMANDS.match(cmd) and cmd not in seen:
            commands.append(cmd)
            seen.add(cmd)
    return commands


def _extract_start_commands(readme: str) -> list[str]:
    """Extract all start commands from a README using section-aware parsing."""
    sections = _split_sections(readme)

    run_cmds: list[str] = []
    unclassified_cmds: list[str] = []
    setup_cmds: list[str] = []

    for heading, body in sections:
        cmds = _find_commands_in_text(body)
        if not cmds:
            continue
        if _RUN_HEADINGS.search(heading):
            run_cmds.extend(cmds)
        elif _SETUP_HEADINGS.search(heading):
            setup_cmds.extend(cmds)
        else:
            unclassified_cmds.extend(cmds)

    return run_cmds or unclassified_cmds or setup_cmds


_PORT_PATTERNS = [
    re.compile(r"PORT[=\s:]+(\d{4,5})"),
    re.compile(r"localhost:(\d{4,5})"),
    re.compile(r"(?i)(?:port|listen|bind)[=:\s]+(\d{4,5})"),
]
_LOCAL_SEARCH_ROOTS = [
    Path.home(),
    Path.home() / "Projects",
    Path.home() / "Developer",
    Path.home() / "Code",
    Path.home() / "dev",
    Path.home() / "src",
]


class GitHubImporter:
    def __init__(self, token: str):
        self._token = token

    def _get(self, path: str, params: dict | None = None) -> object:
        """Make an authenticated GET request; return parsed JSON."""
        url = f"{_API}{path}"
        if params:
            qs = urlencode(params)
            url = f"{url}?{qs}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    def validate_token(self) -> dict:
        """Test token by fetching the authenticated user."""
        try:
            data = self._get("/user")
            return {"ok": True, "login": data.get("login", "")}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def detect_local_path(self, repo_name: str, clone_url: str) -> str | None:
        """Search common local directories for a clone of this repo."""
        # Build name variations to try (my-repo, my_repo, myrepo)
        variations = {
            repo_name,
            repo_name.replace("-", "_"),
            repo_name.replace("_", "-"),
            repo_name.replace("-", "").replace("_", ""),
        }
        candidates = []
        for root in _LOCAL_SEARCH_ROOTS:
            if not root.exists():
                continue
            for name in variations:
                candidate = root / name
                config_file = candidate / ".git" / "config"
                if config_file.exists():
                    config_text = config_file.read_text(encoding="utf-8", errors="replace")
                    https_url = clone_url.removesuffix(".git")
                    # Derive SSH form: https://github.com/owner/repo -> git@github.com:owner/repo
                    ssh_url = clone_url.removesuffix(".git").replace("https://github.com/", "git@github.com:", 1)
                    if https_url in config_text or ssh_url in config_text:
                        candidates.append(candidate)
        if not candidates:
            return None
        # Return the most recently modified candidate
        return str(max(candidates, key=lambda p: p.stat().st_mtime))

    def fetch_readme(self, full_name: str) -> str | None:
        """Fetch and decode the README for a repo. Returns None if not found."""
        try:
            data = self._get(f"/repos/{full_name}/readme")
            if data.get("encoding") == "base64":
                return b64decode(data["content"]).decode("utf-8", errors="replace")
            return data.get("content")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
        except Exception:
            return None

    def _extract_fields(self, readme: str) -> dict:
        """Extract port, start command, and notes from README text."""
        # Port
        port = None
        for pattern in _PORT_PATTERNS:
            m = pattern.search(readme)
            if m:
                port = m.group(1)
                break

        # Start commands — section-aware extraction
        commands = _extract_start_commands(readme)
        start = commands[0] if commands else None

        # Notes — first non-heading, non-empty paragraph
        notes = None
        for para in re.split(r"\n{2,}", readme):
            para = para.strip()
            if para and not para.startswith("#") and not para.startswith("```") and not para.startswith("!") and not para.startswith("[!["):
                notes = para[:300]
                break

        return {"port": port, "start": start, "start_all": commands, "notes": notes}

    def fetch_repos(self) -> list[dict]:
        """Fetch all repos owned by the authenticated user (paginated)."""
        repos, page = [], 1
        while True:
            page_data = self._get("/user/repos", {
                "affiliation": "owner",
                "sort": "pushed",
                "direction": "desc",
                "per_page": "100",
                "page": str(page),
            })
            if not isinstance(page_data, list):
                raise TypeError(f"Expected list from GitHub API, got {type(page_data).__name__}: {page_data!r}")
            if not page_data:
                break
            repos.extend(page_data)
            if len(page_data) < 100:
                break
            page += 1
        return repos

    def scan(self, registered_names: set[str] | None = None) -> list[dict]:
        """
        Fetch all owned repos, detect local paths, extract metadata.
        Returns a list of dicts ready for the frontend import table.
        """
        registered_names = registered_names or set()
        repos = self.fetch_repos()
        results = []
        for repo in repos:
            name       = repo["name"]
            full_name  = repo["full_name"]
            clone_url  = repo["clone_url"]
            readme     = self.fetch_readme(full_name)
            extracted  = self._extract_fields(readme) if readme else {"port": None, "start": None, "start_all": [], "notes": None}
            local_path = self.detect_local_path(name, clone_url)
            tags = list({(repo.get("language") or "").lower()} | set(repo.get("topics") or []))
            tags = [t for t in tags if t]  # remove empty strings
            description = repo.get("description") or ""
            notes = extracted["notes"] or description or ""
            results.append({
                "name":        name,
                "full_name":   full_name,
                "clone_url":   clone_url,
                "local_path":  local_path,
                "port":        extracted["port"],
                "start":       extracted["start"],
                "start_all":   extracted["start_all"],
                "tags":        sorted(tags),
                "notes":       notes[:300],
                "is_fork":     repo.get("fork", False),
                "pushed_at":   repo.get("pushed_at", ""),
                "registered":  name.lower() in {n.lower() for n in registered_names}
                               or (local_path is not None and local_path.lower() in
                                   {n.lower() for n in registered_names}),
            })
        return results
