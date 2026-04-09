# GitHub Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users scan their own GitHub repos, auto-extract project metadata from READMEs, detect local clones, and register projects via a confirmation table in Seshat.

**Architecture:** A new `github.py` module (`GitHubImporter` class) handles all GitHub API calls, README parsing, and local path detection. Three new routes in `seshat.py` expose this to the frontend. The GitHub token is stored in the Vault's shared store under key `__github_token__`. The UI adds an "Import from GitHub" button that opens a token-setup modal (first run) or the import table (subsequent runs).

**Tech Stack:** Python stdlib `urllib` for HTTP, existing `Vault` for token storage, existing Flask patterns, vanilla JS following `app.js` conventions.

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `github.py` | **Create** | `GitHubImporter` class — API calls, README extraction, local path detection |
| `tests/test_github.py` | **Create** | Unit tests for `GitHubImporter` |
| `seshat.py` | **Modify** | Import `GitHubImporter`, add 3 routes |
| `templates/index.html` | **Modify** | "Import from GitHub" button, token modal, import table modal |
| `static/app.js` | **Modify** | `openGitHubImport()`, `saveGitHubToken()`, `runGitHubScan()`, table render + import logic |

---

## Task 1: `GitHubImporter` — token + repo fetch

**Files:**
- Create: `github.py`
- Create: `tests/test_github.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_github.py
import pytest
from unittest.mock import patch, MagicMock
from github import GitHubImporter


@pytest.fixture
def importer():
    return GitHubImporter(token="ghp_test")


def _mock_response(data, status=200):
    m = MagicMock()
    m.status = status
    m.read.return_value = __import__("json").dumps(data).encode()
    m.__enter__ = lambda s: s
    m.__exit__ = MagicMock(return_value=False)
    return m


def test_validate_token_ok(importer):
    user_data = {"login": "rmichaelthomas"}
    with patch("urllib.request.urlopen", return_value=_mock_response(user_data)):
        result = importer.validate_token()
    assert result == {"ok": True, "login": "rmichaelthomas"}


def test_validate_token_bad(importer):
    with patch("urllib.request.urlopen", side_effect=Exception("401")):
        result = importer.validate_token()
    assert result["ok"] is False
    assert "error" in result


def test_fetch_repos_paginates(importer):
    page1 = [{"name": "repo-a", "full_name": "u/repo-a", "clone_url": "https://github.com/u/repo-a.git",
               "description": "A repo", "language": "Python", "topics": ["tool"],
               "fork": False, "pushed_at": "2026-01-01T00:00:00Z", "default_branch": "main"}]
    page2 = []
    responses = iter([_mock_response(page1), _mock_response(page2)])
    with patch("urllib.request.urlopen", side_effect=lambda *a, **k: next(responses)):
        repos = importer.fetch_repos()
    assert len(repos) == 1
    assert repos[0]["name"] == "repo-a"
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /Users/rmichaelthomas/seshat-repo
python3 -m pytest tests/test_github.py -v 2>&1 | head -20
```
Expected: `ModuleNotFoundError: No module named 'github'`

- [ ] **Step 3: Create `github.py` with token validation and repo fetch**

```python
# github.py
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
```

- [ ] **Step 4: Run tests**

```bash
python3 -m pytest tests/test_github.py::test_validate_token_ok tests/test_github.py::test_validate_token_bad tests/test_github.py::test_fetch_repos_paginates -v
```
Expected: all 3 PASS

- [ ] **Step 5: Commit**

```bash
git add github.py tests/test_github.py
git commit -m "feat: GitHubImporter with token validation and repo fetch"
```

---

## Task 2: Local path detection

**Files:**
- Modify: `github.py`
- Modify: `tests/test_github.py`

- [ ] **Step 1: Write failing tests**

```python
# Add to tests/test_github.py

def test_detect_local_path_found(importer, tmp_path):
    repo_dir = tmp_path / "my-repo"
    repo_dir.mkdir()
    git_dir = repo_dir / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text(
        "[remote \"origin\"]\n\turl = https://github.com/u/my-repo.git\n"
    )
    with patch("github._LOCAL_SEARCH_ROOTS", [tmp_path]):
        result = importer.detect_local_path("my-repo", "https://github.com/u/my-repo.git")
    assert result == str(repo_dir)


def test_detect_local_path_not_found(importer, tmp_path):
    with patch("github._LOCAL_SEARCH_ROOTS", [tmp_path]):
        result = importer.detect_local_path("nonexistent", "https://github.com/u/nonexistent.git")
    assert result is None


def test_detect_local_path_name_variations(importer, tmp_path):
    # repo name is "my-app" but cloned as "my_app"
    repo_dir = tmp_path / "my_app"
    repo_dir.mkdir()
    git_dir = repo_dir / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text(
        "[remote \"origin\"]\n\turl = https://github.com/u/my-app.git\n"
    )
    with patch("github._LOCAL_SEARCH_ROOTS", [tmp_path]):
        result = importer.detect_local_path("my-app", "https://github.com/u/my-app.git")
    assert result == str(repo_dir)
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python3 -m pytest tests/test_github.py::test_detect_local_path_found tests/test_github.py::test_detect_local_path_not_found tests/test_github.py::test_detect_local_path_name_variations -v
```
Expected: `AttributeError: 'GitHubImporter' object has no attribute 'detect_local_path'`

- [ ] **Step 3: Add `detect_local_path` to `github.py`**

Add this method to the `GitHubImporter` class:

```python
def detect_local_path(self, repo_name: str, clone_url: str) -> str | None:
    """Search common local directories for a clone of this repo."""
    # Build name variations to try (my-repo, my_repo, MyRepo)
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
            if config_file.exists() and clone_url.rstrip(".git") in config_file.read_text():
                candidates.append(candidate)
    if not candidates:
        return None
    # Return the most recently modified candidate
    return str(max(candidates, key=lambda p: p.stat().st_mtime))
```

- [ ] **Step 4: Run tests**

```bash
python3 -m pytest tests/test_github.py::test_detect_local_path_found tests/test_github.py::test_detect_local_path_not_found tests/test_github.py::test_detect_local_path_name_variations -v
```
Expected: all 3 PASS

- [ ] **Step 5: Commit**

```bash
git add github.py tests/test_github.py
git commit -m "feat: local path detection for GitHub repos"
```

---

## Task 3: README extraction

**Files:**
- Modify: `github.py`
- Modify: `tests/test_github.py`

- [ ] **Step 1: Write failing tests**

```python
# Add to tests/test_github.py
from base64 import b64encode

def _readme_response(text):
    encoded = b64encode(text.encode()).decode()
    return {"content": encoded + "\n", "encoding": "base64"}


def test_extract_port_from_env_line(importer):
    readme = "Run with:\n```\nPORT=6100 python3 app.py\n```"
    result = importer._extract_fields(readme)
    assert result["port"] == "6100"


def test_extract_port_from_localhost(importer):
    readme = "Visit http://localhost:8080 in your browser."
    result = importer._extract_fields(readme)
    assert result["port"] == "8080"


def test_extract_start_command_python(importer):
    readme = "## Running\n```\npython3 app.py\n```"
    result = importer._extract_fields(readme)
    assert result["start"] == "python3 app.py"


def test_extract_start_command_npm(importer):
    readme = "## Running\n```\nnpm start\n```"
    result = importer._extract_fields(readme)
    assert result["start"] == "npm start"


def test_extract_notes_first_paragraph(importer):
    readme = "# My App\n\nThis is a tool for managing things. It does stuff.\n\n## Install"
    result = importer._extract_fields(readme)
    assert result["notes"] == "This is a tool for managing things. It does stuff."


def test_extract_fields_missing(importer):
    readme = "# My App\n\nNo useful info here."
    result = importer._extract_fields(readme)
    assert result["port"] is None
    assert result["start"] is None


def test_fetch_readme_decodes(importer):
    readme_text = "# Hello\n\nport 3000"
    encoded = b64encode(readme_text.encode()).decode()
    api_response = {"content": encoded + "\n", "encoding": "base64"}
    with patch("urllib.request.urlopen", return_value=_mock_response(api_response)):
        result = importer.fetch_readme("owner/repo")
    assert "port 3000" in result


def test_fetch_readme_returns_none_on_404(importer):
    with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(
        None, 404, "Not Found", {}, None
    )):
        result = importer.fetch_readme("owner/repo")
    assert result is None
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python3 -m pytest tests/test_github.py -k "extract or readme" -v
```
Expected: `AttributeError: 'GitHubImporter' object has no attribute '_extract_fields'`

- [ ] **Step 3: Add README fetch and extraction to `github.py`**

Add `import urllib.error` to the imports at the top of `github.py`, then add these methods to `GitHubImporter`:

```python
_START_PATTERNS = re.compile(
    r"^\s*(python3?|npm|node|uvicorn|flask|yarn|cargo run|go run|\.\/gradlew|make)\b.*",
    re.MULTILINE,
)
_PORT_PATTERNS = [
    re.compile(r"PORT[=\s:]+(\d{4,5})"),
    re.compile(r"localhost:(\d{4,5})"),
    re.compile(r":\s*(\d{4,5})"),
]

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
        if para and not para.startswith("#") and not para.startswith("```"):
            notes = para[:300]
            break

    return {"port": port, "start": start, "notes": notes}
```

- [ ] **Step 4: Run tests**

```bash
python3 -m pytest tests/test_github.py -k "extract or readme" -v
```
Expected: all 8 PASS

- [ ] **Step 5: Commit**

```bash
git add github.py tests/test_github.py
git commit -m "feat: README extraction for port, start command, and notes"
```

---

## Task 4: Full scan method

**Files:**
- Modify: `github.py`
- Modify: `tests/test_github.py`

- [ ] **Step 1: Write failing test**

```python
# Add to tests/test_github.py

def test_scan_returns_structured_results(importer, tmp_path):
    repos = [{
        "name": "vault", "full_name": "u/vault",
        "clone_url": "https://github.com/u/vault.git",
        "description": "My vault app", "language": "Python",
        "topics": ["obsidian"], "fork": False,
        "pushed_at": "2026-01-01T00:00:00Z", "default_branch": "main",
    }]
    readme = "# Vault\n\nA vault tool.\n\n```\npython3 app.py\n```\n\nRuns on PORT=6100."
    registered_names = {"seshat"}

    with patch.object(importer, "fetch_repos", return_value=repos), \
         patch.object(importer, "fetch_readme", return_value=readme), \
         patch.object(importer, "detect_local_path", return_value="/Users/u/vault"):
        results = importer.scan(registered_names=registered_names)

    assert len(results) == 1
    r = results[0]
    assert r["name"] == "vault"
    assert r["local_path"] == "/Users/u/vault"
    assert r["port"] == "6100"
    assert r["start"] == "python3 app.py"
    assert r["tags"] == ["python", "obsidian"]
    assert r["registered"] is False
    assert r["is_fork"] is False
```

- [ ] **Step 2: Run test to confirm it fails**

```bash
python3 -m pytest tests/test_github.py::test_scan_returns_structured_results -v
```
Expected: `AttributeError: 'GitHubImporter' object has no attribute 'scan'`

- [ ] **Step 3: Add `scan` method to `github.py`**

```python
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
        extracted  = self._extract_fields(readme) if readme else {"port": None, "start": None, "notes": None}
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
            "tags":        sorted(tags),
            "notes":       notes[:300],
            "is_fork":     repo.get("fork", False),
            "pushed_at":   repo.get("pushed_at", ""),
            "registered":  name.lower() in {n.lower() for n in registered_names}
                           or (local_path is not None and local_path.lower() in
                               {n.lower() for n in registered_names}),
        })
    return results
```

- [ ] **Step 4: Run all tests**

```bash
python3 -m pytest tests/test_github.py -v
```
Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add github.py tests/test_github.py
git commit -m "feat: GitHubImporter.scan() — full repo scan with metadata extraction"
```

---

## Task 5: Backend routes

**Files:**
- Modify: `seshat.py`

- [ ] **Step 1: Add import and routes to `seshat.py`**

Add to the imports block (near the other module imports, around line 19):

```python
from github import GitHubImporter
```

Add these three routes after the existing vault routes (around line 535):

```python
# ── GitHub import ──────────────────────────────────────────────────────────


@app.route("/api/github/status", methods=["GET"])
def github_status():
    token = vault.get("__github_token__")
    return jsonify({"configured": token is not None})


@app.route("/api/github/token", methods=["POST"])
def github_save_token():
    data  = request.json or {}
    token = (data.get("token") or "").strip()
    if not token:
        return jsonify({"error": "token is required"}), 400
    result = GitHubImporter(token).validate_token()
    if not result["ok"]:
        return jsonify({"error": result["error"]}), 400
    vault.set("__github_token__", token)
    return jsonify({"ok": True, "login": result["login"]})


@app.route("/api/github/scan", methods=["GET"])
def github_scan():
    token = vault.get("__github_token__")
    if not token:
        return jsonify({"error": "GitHub token not configured"}), 400
    registered_names = {p["name"] for p in registry.list()}
    try:
        results = GitHubImporter(token).scan(registered_names=registered_names)
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
```

- [ ] **Step 2: Restart seshat and verify routes exist**

```bash
curl -s http://127.0.0.1:9000/api/github/status
```
Expected: `{"configured": false}`

- [ ] **Step 3: Commit**

```bash
git add seshat.py
git commit -m "feat: /api/github/status, /api/github/token, /api/github/scan routes"
```

---

## Task 6: Token setup modal (HTML + JS)

**Files:**
- Modify: `templates/index.html`
- Modify: `static/app.js`

- [ ] **Step 1: Add "Import from GitHub" button and token modal to `index.html`**

Add the button in the top nav, after the `+ Register Project` button (around line 25):

```html
<button class="btn btn-ghost" onclick="openGitHubImport()" title="Import from GitHub">⇩ GitHub</button>
```

Add the token modal before the closing `</body>` tag (before `<script src="/static/app.js">`):

```html
<div class="modal-overlay" id="githubTokenOverlay" style="display:none">
  <div class="modal" style="max-width:480px">
    <div class="modal-header">
      <div class="modal-title">Connect GitHub</div>
      <button class="icon-btn" onclick="closeGitHubTokenModal()">✕</button>
    </div>
    <div style="padding:0 24px 24px">
      <p style="font-size:13px;color:var(--text-muted);margin:0 0 16px">
        Create a Personal Access Token at GitHub with the <code>repo</code> scope
        (or <code>public_repo</code> for public repos only), then paste it below.
      </p>
      <label class="form-label">Personal Access Token</label>
      <input class="form-input" id="githubTokenInput" type="password"
             placeholder="ghp_..." style="margin-bottom:8px">
      <span id="githubTokenError" class="form-error"></span>
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px">
        <button class="btn btn-ghost" onclick="closeGitHubTokenModal()">Cancel</button>
        <button class="btn btn-primary" onclick="saveGitHubToken()">Test &amp; Save</button>
      </div>
    </div>
  </div>
</div>
```

- [ ] **Step 2: Add JS for token modal to `app.js`**

Add after the `restartRouterServices` function (around line 230):

```javascript
// ── GitHub import ──────────────────────────────────────────────────────────

async function openGitHubImport() {
  const res  = await fetch("/api/github/status");
  const data = await res.json();
  if (data.configured) {
    runGitHubScan();
  } else {
    $("githubTokenOverlay").style.display = "flex";
    $("githubTokenInput").value = "";
    $("githubTokenError").textContent = "";
  }
}

function closeGitHubTokenModal() {
  $("githubTokenOverlay").style.display = "none";
}

async function saveGitHubToken() {
  const token = $("githubTokenInput").value.trim();
  $("githubTokenError").textContent = "";
  if (!token) { $("githubTokenError").textContent = "Token is required."; return; }
  const res  = await fetch("/api/github/token", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token }),
  });
  const data = await res.json();
  if (!data.ok) {
    $("githubTokenError").textContent = data.error || "Invalid token.";
    return;
  }
  closeGitHubTokenModal();
  runGitHubScan();
}
```

- [ ] **Step 3: Verify modal renders**

Restart seshat, open `http://localhost:9000`, click "⇩ GitHub" — token modal should appear with input field and "Test & Save" button.

- [ ] **Step 4: Commit**

```bash
git add templates/index.html static/app.js
git commit -m "feat: GitHub token setup modal"
```

---

## Task 7: Import table modal

**Files:**
- Modify: `templates/index.html`
- Modify: `static/app.js`

- [ ] **Step 1: Add import table modal HTML to `index.html`**

Add after the token modal, before `<script src="/static/app.js">`:

```html
<div class="modal-overlay" id="githubImportOverlay" style="display:none">
  <div class="modal" style="max-width:900px;width:95vw;max-height:90vh;display:flex;flex-direction:column">
    <div class="modal-header" style="flex-shrink:0">
      <div class="modal-title">Import from GitHub</div>
      <button class="icon-btn" onclick="closeGitHubImportModal()">✕</button>
    </div>
    <div id="githubImportBanner" class="form-error" style="display:none;margin:0 24px 8px"></div>
    <div id="githubImportBody" style="flex:1;overflow-y:auto;padding:0 24px 8px">
      <div id="githubImportLoading" style="padding:40px;text-align:center;color:var(--text-muted)">
        Scanning your repos…
      </div>
      <table id="githubImportTable" style="display:none;width:100%;border-collapse:collapse;font-size:13px">
        <thead>
          <tr style="border-bottom:1px solid var(--border)">
            <th style="padding:8px 4px;text-align:left;width:32px">
              <input type="checkbox" id="githubSelectAll" onchange="githubToggleAll(this.checked)">
            </th>
            <th style="padding:8px 4px;text-align:left">Repo</th>
            <th style="padding:8px 4px;text-align:left">Local Path</th>
            <th style="padding:8px 4px;text-align:left;width:80px">Port</th>
            <th style="padding:8px 4px;text-align:left">Start Command</th>
            <th style="padding:8px 4px;text-align:left">Tags</th>
            <th style="padding:8px 4px;text-align:left;width:60px">Status</th>
          </tr>
        </thead>
        <tbody id="githubImportRows"></tbody>
      </table>
    </div>
    <div style="flex-shrink:0;padding:16px 24px;border-top:1px solid var(--border);display:flex;justify-content:flex-end;gap:8px">
      <button class="btn btn-ghost" onclick="closeGitHubImportModal()">Close</button>
      <button class="btn btn-primary" id="githubImportBtn" onclick="importSelectedRepos()" style="display:none">
        Import Selected
      </button>
    </div>
  </div>
</div>
```

- [ ] **Step 2: Add scan + table render JS to `app.js`**

Add after `saveGitHubToken()`:

```javascript
let _githubScanResults = [];

function closeGitHubImportModal() {
  $("githubImportOverlay").style.display = "none";
  _githubScanResults = [];
}

async function runGitHubScan() {
  $("githubImportOverlay").style.display = "flex";
  $("githubImportLoading").style.display = "block";
  $("githubImportTable").style.display  = "none";
  $("githubImportBtn").style.display    = "none";
  $("githubImportBanner").style.display = "none";

  try {
    const res  = await fetch("/api/github/scan");
    const data = await res.json();
    if (!res.ok) {
      $("githubImportBanner").textContent = data.error || "Scan failed.";
      $("githubImportBanner").style.display = "block";
      $("githubImportLoading").style.display = "none";
      return;
    }
    _githubScanResults = data;
    renderGitHubTable(data);
  } catch (e) {
    $("githubImportBanner").textContent = e.message;
    $("githubImportBanner").style.display = "block";
    $("githubImportLoading").style.display = "none";
  }
}

function renderGitHubTable(rows) {
  $("githubImportLoading").style.display = "none";
  $("githubImportTable").style.display   = "table";
  const newRows = rows.filter(r => !r.registered);
  $("githubImportBtn").style.display = newRows.length ? "" : "none";

  $("githubImportRows").innerHTML = rows.map((r, i) => {
    const grey     = r.registered ? "opacity:0.4;pointer-events:none" : "";
    const amber    = v => (!v ? "background:rgba(255,180,0,0.15)" : "");
    const checked  = !r.registered ? "checked" : "";
    const disabled = r.registered ? "disabled" : "";
    return `<tr data-idx="${i}" style="${grey};border-bottom:1px solid var(--border)">
      <td style="padding:6px 4px"><input type="checkbox" class="gh-check" data-idx="${i}" ${checked} ${disabled}></td>
      <td style="padding:6px 4px"><strong>${esc(r.name)}</strong>${r.is_fork ? ' <span style="font-size:11px;color:var(--text-muted)">(fork)</span>' : ""}</td>
      <td style="padding:6px 4px;${amber(r.local_path)}">
        <input class="form-input" style="font-size:12px;padding:2px 6px;width:200px" value="${esc(r.local_path||"")}" data-field="local_path" data-idx="${i}" ${disabled}>
      </td>
      <td style="padding:6px 4px;${amber(r.port)}">
        <input class="form-input" style="font-size:12px;padding:2px 6px;width:60px" value="${esc(r.port||"")}" data-field="port" data-idx="${i}" ${disabled}>
      </td>
      <td style="padding:6px 4px;${amber(r.start)}">
        <input class="form-input" style="font-size:12px;padding:2px 6px;width:180px" value="${esc(r.start||"")}" data-field="start" data-idx="${i}" ${disabled}>
      </td>
      <td style="padding:6px 4px">
        <input class="form-input" style="font-size:12px;padding:2px 6px;width:120px" value="${esc((r.tags||[]).join(", "))}" data-field="tags" data-idx="${i}" ${disabled}>
      </td>
      <td style="padding:6px 4px" id="gh-status-${i}">
        ${r.registered ? '<span style="font-size:11px;color:var(--text-muted)">Registered</span>' : '<span style="font-size:11px;color:var(--text-muted)">New</span>'}
      </td>
    </tr>`;
  }).join("");

  // Sync edits back to _githubScanResults
  $("githubImportRows").querySelectorAll("input[data-field]").forEach(inp => {
    inp.addEventListener("input", () => {
      const idx   = parseInt(inp.dataset.idx);
      const field = inp.dataset.field;
      _githubScanResults[idx][field] = inp.value;
    });
  });
}

function githubToggleAll(checked) {
  document.querySelectorAll(".gh-check:not(:disabled)").forEach(cb => cb.checked = checked);
}

async function importSelectedRepos() {
  const checked = [...document.querySelectorAll(".gh-check:checked:not(:disabled)")]
    .map(cb => parseInt(cb.dataset.idx));
  if (!checked.length) return;

  $("githubImportBtn").disabled = true;

  for (const idx of checked) {
    const r      = _githubScanResults[idx];
    const status = $(`gh-status-${idx}`);
    const tags   = typeof r.tags === "string"
      ? r.tags.split(",").map(t => t.trim()).filter(Boolean)
      : (r.tags || []);
    const port   = parseInt(r.port);

    if (!r.local_path || !r.port || !r.start) {
      status.innerHTML = '<span style="color:var(--error)">Missing fields</span>';
      continue;
    }
    if (isNaN(port)) {
      status.innerHTML = '<span style="color:var(--error)">Invalid port</span>';
      continue;
    }

    status.innerHTML = '⏳';
    const res  = await fetch("/api/projects", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name:      r.name,
        port:      r.port,
        directory: r.local_path,
        start:     r.start,
        tags,
        notes:     r.notes || "",
      }),
    });
    const data = await res.json();
    if (res.ok) {
      status.innerHTML = '<span style="color:var(--success,#4caf50)">✓ Imported</span>';
      _githubScanResults[idx].registered = true;
    } else {
      status.innerHTML = `<span style="color:var(--error)">${esc(data.error || "Error")}</span>`;
    }
  }

  $("githubImportBtn").disabled = false;
  await refresh();
}
```

- [ ] **Step 3: Verify end-to-end**

Restart seshat. Click "⇩ GitHub", enter a real GitHub token, click "Test & Save". The import table should appear with your repos, pre-filled fields, amber highlights on missing values, and greyed-out rows for already-registered projects. Edit a row, select it, click "Import Selected" — the project should appear in Seshat.

- [ ] **Step 4: Commit**

```bash
git add templates/index.html static/app.js
git commit -m "feat: GitHub import table modal with inline editing and batch import"
```

---

## Task 8: Push

- [ ] **Step 1: Run full test suite**

```bash
python3 -m pytest -v 2>&1 | tail -20
```
Expected: all tests pass (the pre-existing `test_setup_status_resolver_not_configured` failure is known and unrelated).

- [ ] **Step 2: Push**

```bash
git push
```
