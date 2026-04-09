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

        # Start command — look in fenced code blocks first, then bare lines
        start = None
        code_blocks = re.findall(r"```[^\n]*\n(.*?)```", readme, re.DOTALL)
        for block in code_blocks:
            m = _START_PATTERNS.search(block)
            if m:
                start = m.group(0).strip()
                break
        if not start:
            m = _START_PATTERNS.search(readme)
            if m:
                start = m.group(0).strip()

        # Notes — first non-heading, non-empty paragraph
        notes = None
        for para in re.split(r"\n{2,}", readme):
            para = para.strip()
            if para and not para.startswith("#") and not para.startswith("```") and not para.startswith("!") and not para.startswith("[!["):
                notes = para[:300]
                break

        return {"port": port, "start": start, "notes": notes}

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
