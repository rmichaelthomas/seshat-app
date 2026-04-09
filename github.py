"""
github.py — GitHub API client for Seshat's import feature.
"""
import json
import re
import urllib.request
import urllib.error
from base64 import b64decode
from pathlib import Path

_API = "https://api.github.com"
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
            qs = "&".join(f"{k}={v}" for k, v in params.items())
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
            if not page_data:
                break
            repos.extend(page_data)
            if len(page_data) < 100:
                break
            page += 1
        return repos
