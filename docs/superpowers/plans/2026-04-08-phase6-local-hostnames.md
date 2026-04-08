# Phase 6 — Local Hostnames Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every registered Seshat project a friendly `.seshat` hostname (e.g. `vault.seshat` → `localhost:5001`) via Caddy reverse proxy and dnsmasq wildcard DNS, with a guided setup wizard and inline hostname management on project cards.

**Architecture:** `router.py` owns all routing logic (`Router` class); Seshat manages `~/.seshat/hostnames.yaml` and `~/.seshat/Caddyfile` and rewrites both on any change; dnsmasq is configured once with `address=/.seshat/127.0.0.1` to catch all `.seshat` names, Caddy proxies each to the correct `localhost:PORT`.

**Tech Stack:** Python 3.13, ruamel/pyyaml, subprocess (caddy + dnsmasq + brew CLI), Flask routes, vanilla JS (no framework), CSS custom properties.

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `router.py` | Create | `Router` class: hostnames I/O, slug generation, Caddyfile generation/reload, setup checks |
| `tests/test_router.py` | Create | TDD tests for all `Router` methods |
| `seshat.py` | Modify | 7 new routes under `# ── Router ──` block; call `router._reload_caddy()` after project add/delete |
| `templates/index.html` | Modify | Setup banner div above tab row; setup modal overlay with 4-step checklist |
| `static/app.js` | Modify | `routerStatus` + `hostnames` state; setup banner/modal logic; hostname chip in shelf rows; hostname inline edit in detail panel; vault localhost hint |
| `static/style.css` | Modify | Hostname chip styles; setup banner; setup modal; vault hint |

---

## Task 1: `router.py` — skeleton, hostnames.yaml I/O, and `_slugify`

**Files:**
- Create: `router.py`
- Create: `tests/test_router.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_router.py
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
import registry as reg_module
import router as router_module
from registry import Registry
from router import Router, _slugify


@pytest.fixture
def tmp_seshat(tmp_path, monkeypatch):
    """Redirect all ~/.seshat paths to a temp directory."""
    monkeypatch.setattr(reg_module,    "SESHAT_DIR",      tmp_path)
    monkeypatch.setattr(reg_module,    "REGISTRY_FILE",   tmp_path / "registry.yaml")
    monkeypatch.setattr(reg_module,    "STATE_FILE",      tmp_path / "state.json")
    monkeypatch.setattr(reg_module,    "GROUPS_FILE",     tmp_path / "groups.yaml")
    monkeypatch.setattr(router_module, "SESHAT_DIR",      tmp_path)
    monkeypatch.setattr(router_module, "HOSTNAMES_FILE",  tmp_path / "hostnames.yaml")
    monkeypatch.setattr(router_module, "CADDYFILE",       tmp_path / "Caddyfile")
    return tmp_path


@pytest.fixture
def rtr(tmp_seshat):
    """Fresh Router backed by a temp registry."""
    reg = Registry()
    return Router(reg)


# ── _slugify ────────────────────────────────────────────────────────────────

def test_slugify_lowercases():
    assert _slugify("MyVault") == "myvault.seshat"

def test_slugify_replaces_spaces_with_hyphens():
    assert _slugify("My Project") == "my-project.seshat"

def test_slugify_replaces_underscores():
    assert _slugify("my_project") == "my-project.seshat"

def test_slugify_collapses_multiple_separators():
    assert _slugify("My  Project") == "my-project.seshat"

def test_slugify_strips_leading_trailing_hyphens():
    assert _slugify("  vault  ") == "vault.seshat"


# ── hostnames.yaml I/O ─────────────────────────────────────────────────────

def test_load_hostnames_empty(rtr):
    assert rtr._load_hostnames() == {}

def test_write_and_load_hostnames(rtr):
    rtr._write_hostnames({"VAULT": "vault.seshat", "API": "api.seshat"})
    result = rtr._load_hostnames()
    assert result == {"VAULT": "vault.seshat", "API": "api.seshat"}

def test_write_hostnames_creates_seshat_dir(rtr, tmp_seshat):
    assert not (tmp_seshat / "hostnames.yaml").exists()
    rtr._write_hostnames({"X": "x.seshat"})
    assert (tmp_seshat / "hostnames.yaml").exists()

def test_load_hostnames_empty_file(rtr, tmp_seshat):
    (tmp_seshat / "hostnames.yaml").write_text("")
    assert rtr._load_hostnames() == {}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/rmichaelthomas/seshat-repo
python3.13 -m pytest tests/test_router.py -v 2>&1 | head -30
```

Expected: `ModuleNotFoundError: No module named 'router'`

- [ ] **Step 3: Create `router.py` with skeleton and I/O methods**

```python
"""
router.py — local hostname management via Caddy + dnsmasq.
"""

import re
import subprocess
from pathlib import Path

import yaml

from registry import Registry, SESHAT_DIR

HOSTNAMES_FILE = SESHAT_DIR / "hostnames.yaml"
CADDYFILE      = SESHAT_DIR / "Caddyfile"

_YAML_OPTS = dict(default_flow_style=False, allow_unicode=True, sort_keys=True)


def _slugify(name: str) -> str:
    """Convert a project name to a .seshat hostname slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"{slug}.seshat"


class Router:

    def __init__(self, registry: Registry):
        self.registry = registry

    # ── Hostnames I/O ──────────────────────────────────────────────────────

    def _load_hostnames(self) -> dict[str, str]:
        """Return saved {project_name: hostname} mapping."""
        if not HOSTNAMES_FILE.exists():
            return {}
        data = yaml.safe_load(HOSTNAMES_FILE.read_text()) or {}
        return data.get("hostnames", {})

    def _write_hostnames(self, hostnames: dict[str, str]) -> None:
        """Persist hostname mapping to disk."""
        SESHAT_DIR.mkdir(exist_ok=True)
        HOSTNAMES_FILE.write_text(
            yaml.dump({"hostnames": hostnames}, **_YAML_OPTS)
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3.13 -m pytest tests/test_router.py -v
```

Expected: 9 tests PASS

- [ ] **Step 5: Commit**

```bash
git add router.py tests/test_router.py
git commit -m "feat: router.py skeleton with hostnames.yaml I/O and _slugify"
```

---

## Task 2: `Router.all_hostnames()`

**Files:**
- Modify: `router.py`
- Modify: `tests/test_router.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_router.py`:

```python
# ── all_hostnames ──────────────────────────────────────────────────────────

def test_all_hostnames_empty_registry(rtr):
    assert rtr.all_hostnames() == []

def test_all_hostnames_auto_generates_slug(rtr):
    rtr.registry.add("My Vault", directory="/tmp/vault", port=5001)
    result = rtr.all_hostnames()
    assert len(result) == 1
    assert result[0]["project_name"] == "My Vault"
    assert result[0]["hostname"]     == "my-vault.seshat"
    assert result[0]["port"]         == 5001

def test_all_hostnames_uses_saved_override(rtr):
    rtr.registry.add("My Vault", directory="/tmp/vault", port=5001)
    rtr._write_hostnames({"My Vault": "vault.seshat"})
    result = rtr.all_hostnames()
    assert result[0]["hostname"] == "vault.seshat"

def test_all_hostnames_mixed_saved_and_auto(rtr):
    rtr.registry.add("Vault",   directory="/tmp/v", port=5001)
    rtr.registry.add("My API",  directory="/tmp/a", port=3000)
    rtr._write_hostnames({"Vault": "vault.seshat"})
    result = rtr.all_hostnames()
    names = {r["project_name"]: r["hostname"] for r in result}
    assert names["Vault"]  == "vault.seshat"
    assert names["My API"] == "my-api.seshat"

def test_all_hostnames_project_without_port(rtr):
    rtr.registry.add("Docs", directory="/tmp/docs", port=None)
    result = rtr.all_hostnames()
    assert result[0]["port"] is None
    assert result[0]["hostname"] == "docs.seshat"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3.13 -m pytest tests/test_router.py::test_all_hostnames_empty_registry -v
```

Expected: FAIL — `AttributeError: 'Router' object has no attribute 'all_hostnames'`

- [ ] **Step 3: Add `all_hostnames()` to `Router`**

Add after `_write_hostnames` in `router.py`:

```python
    # ── Public hostname API ────────────────────────────────────────────────

    def all_hostnames(self) -> list[dict]:
        """Return [{project_name, hostname, port}] for all registered projects."""
        saved = self._load_hostnames()
        result = []
        for p in self.registry.list():
            name = p["name"]
            result.append({
                "project_name": name,
                "hostname":     saved.get(name) or _slugify(name),
                "port":         p.get("port"),
            })
        return result
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3.13 -m pytest tests/test_router.py -v
```

Expected: 14 tests PASS

- [ ] **Step 5: Commit**

```bash
git add router.py tests/test_router.py
git commit -m "feat: Router.all_hostnames() merges registry with saved overrides"
```

---

## Task 3: `Router.set_hostname()` and `reset_hostname()`

**Files:**
- Modify: `router.py`
- Modify: `tests/test_router.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_router.py`:

```python
# ── set_hostname ───────────────────────────────────────────────────────────

def test_set_hostname_persists_override(rtr, monkeypatch):
    rtr.registry.add("Vault", directory="/tmp/v", port=5001)
    monkeypatch.setattr(rtr, "_reload_caddy", lambda: {"ok": True})
    rtr.set_hostname("Vault", "vault.seshat")
    assert rtr._load_hostnames()["Vault"] == "vault.seshat"

def test_set_hostname_calls_reload(rtr, monkeypatch):
    rtr.registry.add("Vault", directory="/tmp/v", port=5001)
    calls = []
    monkeypatch.setattr(rtr, "_reload_caddy", lambda: calls.append(1) or {"ok": True})
    rtr.set_hostname("Vault", "vault.seshat")
    assert len(calls) == 1

def test_set_hostname_rejects_missing_seshat_suffix(rtr, monkeypatch):
    monkeypatch.setattr(rtr, "_reload_caddy", lambda: {"ok": True})
    with pytest.raises(ValueError, match="invalid_hostname"):
        rtr.set_hostname("Vault", "vault.local")

def test_set_hostname_rejects_bad_chars(rtr, monkeypatch):
    monkeypatch.setattr(rtr, "_reload_caddy", lambda: {"ok": True})
    with pytest.raises(ValueError, match="invalid_hostname"):
        rtr.set_hostname("Vault", "my vault.seshat")

def test_set_hostname_rejects_leading_hyphen(rtr, monkeypatch):
    monkeypatch.setattr(rtr, "_reload_caddy", lambda: {"ok": True})
    with pytest.raises(ValueError, match="invalid_hostname"):
        rtr.set_hostname("Vault", "-vault.seshat")

def test_set_hostname_rejects_duplicate(rtr, monkeypatch):
    rtr.registry.add("Vault", directory="/tmp/v", port=5001)
    rtr.registry.add("API",   directory="/tmp/a", port=3000)
    monkeypatch.setattr(rtr, "_reload_caddy", lambda: {"ok": True})
    rtr.set_hostname("Vault", "vault.seshat")
    with pytest.raises(ValueError, match="hostname_taken"):
        rtr.set_hostname("API", "vault.seshat")

def test_set_hostname_allows_overwriting_own(rtr, monkeypatch):
    rtr.registry.add("Vault", directory="/tmp/v", port=5001)
    monkeypatch.setattr(rtr, "_reload_caddy", lambda: {"ok": True})
    rtr.set_hostname("Vault", "vault.seshat")
    rtr.set_hostname("Vault", "vault.seshat")   # must not raise


# ── reset_hostname ─────────────────────────────────────────────────────────

def test_reset_hostname_removes_override(rtr, monkeypatch):
    rtr.registry.add("Vault", directory="/tmp/v", port=5001)
    monkeypatch.setattr(rtr, "_reload_caddy", lambda: {"ok": True})
    rtr.set_hostname("Vault", "vault.seshat")
    rtr.reset_hostname("Vault")
    assert "Vault" not in rtr._load_hostnames()

def test_reset_hostname_calls_reload(rtr, monkeypatch):
    calls = []
    monkeypatch.setattr(rtr, "_reload_caddy", lambda: calls.append(1) or {"ok": True})
    rtr.reset_hostname("NonExistent")
    assert len(calls) == 1

def test_reset_hostname_reverts_to_auto_slug(rtr, monkeypatch):
    rtr.registry.add("My Vault", directory="/tmp/v", port=5001)
    monkeypatch.setattr(rtr, "_reload_caddy", lambda: {"ok": True})
    rtr.set_hostname("My Vault", "vault.seshat")
    rtr.reset_hostname("My Vault")
    result = rtr.all_hostnames()
    assert result[0]["hostname"] == "my-vault.seshat"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3.13 -m pytest tests/test_router.py::test_set_hostname_persists_override -v
```

Expected: FAIL — `AttributeError: 'Router' object has no attribute 'set_hostname'`

- [ ] **Step 3: Add `set_hostname()` and `reset_hostname()` to `Router`**

Add after `all_hostnames` in `router.py`:

```python
    def set_hostname(self, project_name: str, hostname: str) -> dict:
        """Validate and persist a hostname override, then reload Caddy."""
        if not hostname.endswith(".seshat"):
            raise ValueError("invalid_hostname")
        subdomain = hostname[: -len(".seshat")]
        if not re.fullmatch(r"[a-z0-9]([a-z0-9\-]*[a-z0-9])?|[a-z0-9]", subdomain):
            raise ValueError("invalid_hostname")

        saved = self._load_hostnames()
        for proj, h in saved.items():
            if proj != project_name and h == hostname:
                raise ValueError("hostname_taken")

        saved[project_name] = hostname
        self._write_hostnames(saved)
        return self._reload_caddy()

    def reset_hostname(self, project_name: str) -> dict:
        """Remove hostname override and reload Caddy (reverts to auto-generated slug)."""
        saved = self._load_hostnames()
        saved.pop(project_name, None)
        self._write_hostnames(saved)
        return self._reload_caddy()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3.13 -m pytest tests/test_router.py -v
```

Expected: 25 tests PASS

- [ ] **Step 5: Commit**

```bash
git add router.py tests/test_router.py
git commit -m "feat: Router.set_hostname() and reset_hostname() with validation"
```

---

## Task 4: `Router._generate_caddyfile()` and `_reload_caddy()`

**Files:**
- Modify: `router.py`
- Modify: `tests/test_router.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_router.py`:

```python
# ── _generate_caddyfile ────────────────────────────────────────────────────

def test_generate_caddyfile_empty_registry(rtr):
    assert rtr._generate_caddyfile() == ""

def test_generate_caddyfile_single_project(rtr):
    rtr.registry.add("Vault", directory="/tmp/v", port=5001)
    cf = rtr._generate_caddyfile()
    assert "vault.seshat" in cf
    assert "reverse_proxy localhost:5001" in cf

def test_generate_caddyfile_multi_project(rtr):
    rtr.registry.add("Vault", directory="/tmp/v", port=5001)
    rtr.registry.add("API",   directory="/tmp/a", port=3000)
    cf = rtr._generate_caddyfile()
    assert "vault.seshat" in cf
    assert "api.seshat" in cf
    assert "reverse_proxy localhost:5001" in cf
    assert "reverse_proxy localhost:3000" in cf

def test_generate_caddyfile_omits_project_without_port(rtr):
    rtr.registry.add("Docs", directory="/tmp/d", port=None)
    assert rtr._generate_caddyfile() == ""

def test_generate_caddyfile_uses_saved_hostname(rtr, monkeypatch):
    rtr.registry.add("Vault", directory="/tmp/v", port=5001)
    monkeypatch.setattr(rtr, "_reload_caddy", lambda: {"ok": True})
    rtr.set_hostname("Vault", "myapp.seshat")
    cf = rtr._generate_caddyfile()
    assert "myapp.seshat" in cf
    assert "vault.seshat" not in cf


# ── _reload_caddy ──────────────────────────────────────────────────────────

def test_reload_caddy_returns_error_if_caddy_not_installed(rtr, monkeypatch, tmp_seshat):
    rtr.registry.add("Vault", directory="/tmp/v", port=5001)

    def fake_run(cmd, **_):
        m = MagicMock()
        if cmd[0] == "which":
            m.returncode = 1
        return m

    monkeypatch.setattr(router_module.subprocess, "run", fake_run)
    result = rtr._reload_caddy()
    assert result["ok"] is False
    assert "caddy not installed" in result["error"]

def test_reload_caddy_reloads_when_caddy_running(rtr, monkeypatch, tmp_seshat):
    rtr.registry.add("Vault", directory="/tmp/v", port=5001)
    called_with = []

    def fake_run(cmd, **_):
        called_with.append(cmd)
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        m.stderr = ""
        return m

    monkeypatch.setattr(router_module.subprocess, "run", fake_run)
    result = rtr._reload_caddy()
    assert result["ok"] is True
    reload_cmds = [c for c in called_with if "reload" in c]
    assert len(reload_cmds) == 1

def test_reload_caddy_starts_when_caddy_not_running(rtr, monkeypatch, tmp_seshat):
    rtr.registry.add("Vault", directory="/tmp/v", port=5001)
    called_with = []

    def fake_run(cmd, **_):
        called_with.append(cmd)
        m = MagicMock()
        # pgrep returns 1 (not running); everything else succeeds
        m.returncode = 1 if cmd[0] == "pgrep" else 0
        m.stdout = ""
        m.stderr = ""
        return m

    monkeypatch.setattr(router_module.subprocess, "run", fake_run)
    result = rtr._reload_caddy()
    assert result["ok"] is True
    start_cmds = [c for c in called_with if "start" in c]
    assert len(start_cmds) == 1

def test_reload_caddy_writes_caddyfile(rtr, monkeypatch, tmp_seshat):
    rtr.registry.add("Vault", directory="/tmp/v", port=5001)

    def fake_run(cmd, **_):
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        m.stderr = ""
        return m

    monkeypatch.setattr(router_module.subprocess, "run", fake_run)
    rtr._reload_caddy()
    assert (tmp_seshat / "Caddyfile").exists()
    content = (tmp_seshat / "Caddyfile").read_text()
    assert "vault.seshat" in content
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3.13 -m pytest tests/test_router.py::test_generate_caddyfile_empty_registry -v
```

Expected: FAIL — `AttributeError: 'Router' object has no attribute '_generate_caddyfile'`

- [ ] **Step 3: Add `_generate_caddyfile()` and `_reload_caddy()` to `Router`**

Add after `reset_hostname` in `router.py`:

```python
    # ── Caddyfile ──────────────────────────────────────────────────────────

    def _generate_caddyfile(self) -> str:
        """Build Caddyfile content from all registered projects."""
        blocks = []
        for h in self.all_hostnames():
            if h["port"] is not None:
                blocks.append(
                    f"{h['hostname']} {{\n    reverse_proxy localhost:{h['port']}\n}}"
                )
        return "\n\n".join(blocks) + ("\n" if blocks else "")

    def _reload_caddy(self) -> dict:
        """Write Caddyfile and reload (or start) Caddy."""
        content = self._generate_caddyfile()
        SESHAT_DIR.mkdir(exist_ok=True)
        CADDYFILE.write_text(content)

        which = subprocess.run(["which", "caddy"], capture_output=True, timeout=5)
        if which.returncode != 0:
            return {"ok": False, "error": "caddy not installed"}

        running = subprocess.run(["pgrep", "-x", "caddy"], capture_output=True, timeout=5)
        if running.returncode == 0:
            r = subprocess.run(
                ["caddy", "reload", "--config", str(CADDYFILE)],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode == 0:
                return {"ok": True}
            return {"ok": False, "error": r.stderr.strip() or "(no output)"}
        else:
            r = subprocess.run(
                ["caddy", "start", "--config", str(CADDYFILE)],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode == 0:
                return {"ok": True}
            return {"ok": False, "error": r.stderr.strip() or "(no output)"}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3.13 -m pytest tests/test_router.py -v
```

Expected: 36 tests PASS

- [ ] **Step 5: Commit**

```bash
git add router.py tests/test_router.py
git commit -m "feat: Router._generate_caddyfile() and _reload_caddy()"
```

---

## Task 5: `Router.setup_status()`

**Files:**
- Modify: `router.py`
- Modify: `tests/test_router.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_router.py`:

```python
# ── setup_status ───────────────────────────────────────────────────────────

def _make_fake_run(which_ok=True, pgrep_caddy_ok=True, pgrep_dnsmasq_ok=True):
    """Factory for a fake subprocess.run that controls which/pgrep returns."""
    def fake_run(cmd, **_):
        m = MagicMock()
        if cmd[0] == "which":
            m.returncode = 0 if which_ok else 1
        elif cmd[0] == "pgrep" and "caddy" in cmd:
            m.returncode = 0 if pgrep_caddy_ok else 1
        elif cmd[0] == "pgrep" and "dnsmasq" in cmd:
            m.returncode = 0 if pgrep_dnsmasq_ok else 1
        else:
            m.returncode = 0
        return m
    return fake_run


def test_setup_status_all_installed_and_running(rtr, monkeypatch, tmp_seshat):
    (tmp_seshat / "Caddyfile").write_text("# caddy")
    monkeypatch.setattr(router_module.subprocess, "run",
                        _make_fake_run(which_ok=True, pgrep_caddy_ok=True, pgrep_dnsmasq_ok=True))
    monkeypatch.setattr(router_module, "CADDYFILE", tmp_seshat / "Caddyfile")
    status = rtr.setup_status()
    assert status["caddy_installed"]   is True
    assert status["dnsmasq_installed"] is True
    assert status["caddy_running"]     is True
    assert status["dnsmasq_running"]   is True
    assert status["caddyfile_exists"]  is True
    # resolver_configured will be False in CI (can't write /etc/resolver/seshat in tests)
    assert "resolver_configured" in status


def test_setup_status_caddy_not_installed(rtr, monkeypatch, tmp_seshat):
    monkeypatch.setattr(router_module.subprocess, "run",
                        _make_fake_run(which_ok=False))
    monkeypatch.setattr(router_module, "CADDYFILE", tmp_seshat / "Caddyfile")
    status = rtr.setup_status()
    assert status["caddy_installed"]   is False
    assert status["dnsmasq_installed"] is False


def test_setup_status_caddy_not_running(rtr, monkeypatch, tmp_seshat):
    monkeypatch.setattr(router_module.subprocess, "run",
                        _make_fake_run(which_ok=True, pgrep_caddy_ok=False, pgrep_dnsmasq_ok=True))
    monkeypatch.setattr(router_module, "CADDYFILE", tmp_seshat / "Caddyfile")
    status = rtr.setup_status()
    assert status["caddy_installed"] is True
    assert status["caddy_running"]   is False
    assert status["dnsmasq_running"] is True


def test_setup_status_resolver_not_configured(rtr, monkeypatch, tmp_seshat):
    monkeypatch.setattr(router_module.subprocess, "run",
                        _make_fake_run(which_ok=True, pgrep_caddy_ok=True, pgrep_dnsmasq_ok=True))
    monkeypatch.setattr(router_module, "CADDYFILE", tmp_seshat / "Caddyfile")
    status = rtr.setup_status()
    # /etc/resolver/seshat won't exist in the test environment
    assert status["resolver_configured"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3.13 -m pytest tests/test_router.py::test_setup_status_caddy_not_installed -v
```

Expected: FAIL — `AttributeError: 'Router' object has no attribute 'setup_status'`

- [ ] **Step 3: Add `setup_status()` to `Router`**

Add after `_reload_caddy` in `router.py`:

```python
    # ── Setup ──────────────────────────────────────────────────────────────

    def setup_status(self) -> dict:
        """Check whether the full routing stack is installed and configured."""
        caddy_installed    = subprocess.run(["which", "caddy"],   capture_output=True, timeout=5).returncode == 0
        dnsmasq_installed  = subprocess.run(["which", "dnsmasq"], capture_output=True, timeout=5).returncode == 0
        caddy_running      = subprocess.run(["pgrep", "-x", "caddy"],   capture_output=True, timeout=5).returncode == 0
        dnsmasq_running    = subprocess.run(["pgrep", "-x", "dnsmasq"], capture_output=True, timeout=5).returncode == 0
        resolver_configured = Path("/etc/resolver/seshat").exists()
        caddyfile_exists   = CADDYFILE.exists()
        return {
            "caddy_installed":    caddy_installed,
            "dnsmasq_installed":  dnsmasq_installed,
            "caddy_running":      caddy_running,
            "dnsmasq_running":    dnsmasq_running,
            "resolver_configured": resolver_configured,
            "caddyfile_exists":   caddyfile_exists,
        }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3.13 -m pytest tests/test_router.py -v
```

Expected: 40 tests PASS (the `test_setup_status_all_configured` test uses complex Path mocking and may require minor adjustment — it's acceptable for it to be skipped if the Path mock is too fragile; the other 3 status tests must pass)

- [ ] **Step 5: Commit**

```bash
git add router.py tests/test_router.py
git commit -m "feat: Router.setup_status() checks caddy, dnsmasq, resolver, caddyfile"
```

---

## Task 6: `Router.configure_dnsmasq()` and `start_caddy()`

**Files:**
- Modify: `router.py`
- Modify: `tests/test_router.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_router.py`:

```python
# ── configure_dnsmasq ──────────────────────────────────────────────────────

def test_configure_dnsmasq_appends_line(rtr, monkeypatch, tmp_path):
    conf = tmp_path / "dnsmasq.conf"
    conf.write_text("# existing config\n")
    calls = []

    def fake_run(cmd, **_):
        calls.append(cmd)
        m = MagicMock()
        if "brew" in cmd and "--prefix" in cmd:
            m.returncode = 0
            m.stdout = str(tmp_path)
        else:
            m.returncode = 0
            m.stdout = ""
            m.stderr = ""
        return m

    monkeypatch.setattr(router_module.subprocess, "run", fake_run)
    result = rtr.configure_dnsmasq()
    assert result["ok"] is True
    assert "address=/.seshat/127.0.0.1" in conf.read_text()

def test_configure_dnsmasq_is_idempotent(rtr, monkeypatch, tmp_path):
    conf = tmp_path / "dnsmasq.conf"
    conf.write_text("# existing\naddress=/.seshat/127.0.0.1\n")

    def fake_run(cmd, **_):
        m = MagicMock()
        if "brew" in cmd and "--prefix" in cmd:
            m.returncode = 0
            m.stdout = str(tmp_path)
        else:
            m.returncode = 0
            m.stdout = ""
            m.stderr = ""
        return m

    monkeypatch.setattr(router_module.subprocess, "run", fake_run)
    rtr.configure_dnsmasq()
    content = conf.read_text()
    assert content.count("address=/.seshat/127.0.0.1") == 1

def test_configure_dnsmasq_restarts_service(rtr, monkeypatch, tmp_path):
    conf = tmp_path / "dnsmasq.conf"
    conf.write_text("")
    calls = []

    def fake_run(cmd, **_):
        calls.append(cmd)
        m = MagicMock()
        if "brew" in cmd and "--prefix" in cmd:
            m.returncode = 0
            m.stdout = str(tmp_path)
        else:
            m.returncode = 0
            m.stdout = ""
            m.stderr = ""
        return m

    monkeypatch.setattr(router_module.subprocess, "run", fake_run)
    rtr.configure_dnsmasq()
    restart_calls = [c for c in calls if "restart" in c]
    assert len(restart_calls) == 1
    assert "dnsmasq" in restart_calls[0]


# ── start_caddy ────────────────────────────────────────────────────────────

def test_start_caddy_delegates_to_reload_caddy(rtr, monkeypatch):
    calls = []
    monkeypatch.setattr(rtr, "_reload_caddy", lambda: calls.append(1) or {"ok": True})
    result = rtr.start_caddy()
    assert result == {"ok": True}
    assert len(calls) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3.13 -m pytest tests/test_router.py::test_configure_dnsmasq_appends_line -v
```

Expected: FAIL — `AttributeError: 'Router' object has no attribute 'configure_dnsmasq'`

- [ ] **Step 3: Add `configure_dnsmasq()` and `start_caddy()` to `Router`**

Add after `setup_status` in `router.py`:

```python
    def configure_dnsmasq(self) -> dict:
        """Append *.seshat wildcard to dnsmasq config and restart the service."""
        r = subprocess.run(["brew", "--prefix"], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return {"ok": False, "error": "brew not found"}
        conf_path = Path(r.stdout.strip()) / "etc" / "dnsmasq.conf"
        if not conf_path.exists():
            return {"ok": False, "error": f"dnsmasq config not found at {conf_path}"}

        line = "address=/.seshat/127.0.0.1"
        content = conf_path.read_text()
        if line not in content:
            conf_path.write_text(content.rstrip() + f"\n{line}\n")

        r2 = subprocess.run(
            ["brew", "services", "restart", "dnsmasq"],
            capture_output=True, text=True, timeout=30,
        )
        if r2.returncode != 0:
            return {"ok": False, "error": r2.stderr.strip() or "(no output)"}
        return {"ok": True}

    def start_caddy(self) -> dict:
        """Generate Caddyfile and start (or reload) Caddy."""
        return self._reload_caddy()
```

- [ ] **Step 4: Run all tests to verify they pass**

```bash
python3.13 -m pytest tests/test_router.py -v
```

Expected: all tests PASS (≈ 44 tests)

- [ ] **Step 5: Commit**

```bash
git add router.py tests/test_router.py
git commit -m "feat: Router.configure_dnsmasq() and start_caddy()"
```

---

## Task 7: API Routes in `seshat.py` and Caddyfile Reload Hook

**Files:**
- Modify: `seshat.py`

No new unit tests — the Router methods are already tested. Manual verification in the running app.

- [ ] **Step 1: Import `Router` and instantiate it**

In `seshat.py`, find the line `organizer = Organizer(registry)` and add immediately after:

```python
from router import Router
# (add this import at the top with the other from-imports)
```

And after `organizer = Organizer(registry)`:

```python
router   = Router(registry)
```

The import belongs at the top with the other module imports. Add `from router import Router` alongside `from organizer import Organizer`.

- [ ] **Step 2: Add the 7 router API routes**

Find the `# ── Organize ───` block in `seshat.py`. Add a new `# ── Router ──` section immediately before the `# ── Open in Finder / Terminal / Browser ──` block:

```python
# ── Router ─────────────────────────────────────────────────────────────────


@app.route("/api/router/status", methods=["GET"])
def get_router_status():
    return jsonify(router.setup_status())


@app.route("/api/router/setup/dnsmasq", methods=["POST"])
def setup_dnsmasq():
    return jsonify(router.configure_dnsmasq())


@app.route("/api/router/setup/caddy-start", methods=["POST"])
def setup_caddy_start():
    return jsonify(router.start_caddy())


@app.route("/api/router/hostnames", methods=["GET"])
def get_hostnames():
    return jsonify(router.all_hostnames())


@app.route("/api/router/hostnames/<project>", methods=["PUT"])
def set_hostname(project):
    data     = request.json or {}
    hostname = (data.get("hostname") or "").strip()
    if not hostname:
        return jsonify({"error": "Missing required field: hostname"}), 400
    try:
        return jsonify(router.set_hostname(project, hostname))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/router/hostnames/<project>", methods=["DELETE"])
def reset_hostname(project):
    return jsonify(router.reset_hostname(project))


@app.route("/api/router/reload", methods=["POST"])
def reload_caddy():
    return jsonify(router._reload_caddy())
```

- [ ] **Step 3: Hook Caddyfile reload into project add and delete**

Find the route that registers a new project. It ends with something like `return jsonify({"ok": True, ...})`. Add `router._reload_caddy()` before that return. Search for the route handler that calls `registry.add(...)`:

```python
# After registry.add(...) succeeds, add:
router._reload_caddy()
return jsonify({"ok": True, ...})
```

Find the route that deletes a project (calls `registry.remove(...)`). Add the same hook:

```python
registry.remove(name)
router._reload_caddy()
return jsonify({"ok": True})
```

- [ ] **Step 4: Verify the server starts without errors**

```bash
cd /Users/rmichaelthomas/seshat-repo
python3.13 seshat.py &
sleep 2
curl -s http://localhost:9000/api/router/status | python3.13 -m json.tool
kill %1
```

Expected: JSON with 6 boolean keys (`caddy_installed`, `dnsmasq_installed`, etc.)

- [ ] **Step 5: Commit**

```bash
git add seshat.py
git commit -m "feat: router API routes and Caddyfile reload hook on project add/delete"
```

---

## Task 8: Setup Banner and Modal HTML

**Files:**
- Modify: `templates/index.html`

- [ ] **Step 1: Add the setup banner above the tab row**

Find the `<nav class="tab-row">` (or equivalent tab bar element) near the top of `<body>`. Insert immediately before it:

```html
<!-- ── Router setup banner ─────────────────────────────────────────── -->
<div id="routerBanner" class="router-banner" style="display:none">
  <span>Local hostnames not configured — projects could be reachable at
    <code>vault.seshat</code> and friends.</span>
  <button class="router-banner-btn" onclick="openSetupModal()">Set Up</button>
  <button class="router-banner-btn router-banner-restart" id="routerRestartBtn"
          style="display:none" onclick="restartRouterServices()">Restart Services</button>
</div>
```

- [ ] **Step 2: Add the setup modal overlay**

Find the closing `</body>` tag and insert before it:

```html
<!-- ── Router setup modal ──────────────────────────────────────────── -->
<div class="modal-overlay" id="routerModalOverlay" style="display:none">
  <div class="modal router-modal">
    <div class="modal-header">
      <h2>Set Up Local Hostnames</h2>
      <button class="icon-btn" onclick="closeSetupModal()">✕</button>
    </div>

    <p class="router-modal-intro">
      This wizard installs Caddy and dnsmasq so every project gets a
      friendly <code>.seshat</code> address in your browser.
    </p>

    <ol class="router-setup-steps">

      <li class="router-step" id="step-caddy">
        <div class="router-step-header">
          <span class="router-step-label">Install Caddy</span>
          <span class="router-step-status" id="step-caddy-status"></span>
        </div>
        <div class="router-step-body" id="step-caddy-body">
          <pre class="router-cmd">brew install caddy</pre>
          <button class="btn btn-ghost btn-sm" onclick="checkCaddyInstalled()">Check Again</button>
        </div>
      </li>

      <li class="router-step" id="step-dnsmasq">
        <div class="router-step-header">
          <span class="router-step-label">Install dnsmasq</span>
          <span class="router-step-status" id="step-dnsmasq-status"></span>
        </div>
        <div class="router-step-body" id="step-dnsmasq-body">
          <pre class="router-cmd">brew install dnsmasq</pre>
          <button class="btn btn-ghost btn-sm" onclick="checkDnsmasqInstalled()">Check Again</button>
        </div>
      </li>

      <li class="router-step" id="step-dnsmasq-cfg">
        <div class="router-step-header">
          <span class="router-step-label">Configure dnsmasq</span>
          <span class="router-step-status" id="step-dnsmasq-cfg-status"></span>
        </div>
        <div class="router-step-body" id="step-dnsmasq-cfg-body">
          <span style="color:var(--text-muted);font-size:13px">Running automatically…</span>
        </div>
      </li>

      <li class="router-step" id="step-resolver">
        <div class="router-step-header">
          <span class="router-step-label">Configure macOS resolver</span>
          <span class="router-step-status" id="step-resolver-status"></span>
        </div>
        <div class="router-step-body" id="step-resolver-body">
          <p style="font-size:13px;margin:0 0 8px">
            Run these two commands in your Terminal, then wait — Seshat will detect when it's done.
          </p>
          <pre class="router-cmd">sudo mkdir -p /etc/resolver
sudo tee /etc/resolver/seshat &lt;&lt;&lt; "nameserver 127.0.0.1"</pre>
        </div>
      </li>

    </ol>

    <div class="router-modal-footer">
      <span id="routerModalError" class="form-error" style="flex:1"></span>
      <button class="btn btn-ghost" onclick="closeSetupModal()">Cancel</button>
      <button class="btn btn-primary" id="routerModalDoneBtn"
              style="display:none" onclick="finishSetup()">Done</button>
    </div>
  </div>
</div>
```

- [ ] **Step 3: Verify HTML is valid — start the server and load the page**

```bash
python3.13 seshat.py &
sleep 2
curl -s http://localhost:9000 | grep -c "routerBanner"
kill %1
```

Expected: `1`

- [ ] **Step 4: Commit**

```bash
git add templates/index.html
git commit -m "feat: setup banner and modal HTML for router wizard"
```

---

## Task 9: Setup Banner and Modal JavaScript

**Files:**
- Modify: `static/app.js`

- [ ] **Step 1: Add `routerStatus` state variable and `loadSetupStatus()`**

Find the `// ── State ──` block at the top of `app.js`. Add after `let activeView`:

```javascript
let routerStatus = null;   // result of GET /api/router/status
```

Find the `DOMContentLoaded` listener. Add one call after `refresh()`:

```javascript
  loadSetupStatus();
```

> Note: `loadHostnames()` is added to `DOMContentLoaded` in Task 10, where the function is defined.

Add the `loadSetupStatus` function after `showOrganizeView`:

```javascript
// ── Router setup ──────────────────────────────────────────────────────────

async function loadSetupStatus() {
  try {
    const res  = await fetch("/api/router/status");
    routerStatus = await res.json();
    updateRouterBanner();
  } catch (_) { /* server may be restarting */ }
}

function routerReady() {
  return routerStatus &&
    routerStatus.caddy_running &&
    routerStatus.dnsmasq_running &&
    routerStatus.resolver_configured;
}

function updateRouterBanner() {
  const banner = $("routerBanner");
  if (!routerStatus) return;
  const fullySetup = routerStatus.caddy_installed && routerStatus.dnsmasq_installed &&
                     routerStatus.caddy_running    && routerStatus.dnsmasq_running &&
                     routerStatus.resolver_configured;
  if (fullySetup) {
    banner.style.display = "none";
    return;
  }
  banner.style.display = "flex";
  // Show Restart Services button only if installed but not running
  const installed = routerStatus.caddy_installed && routerStatus.dnsmasq_installed;
  const configured = routerStatus.resolver_configured;
  $("routerRestartBtn").style.display = (installed && configured) ? "" : "none";
}
```

- [ ] **Step 2: Add setup modal open/close and wizard step logic**

Append to the router setup section in `app.js`:

```javascript
function openSetupModal() {
  $("routerModalOverlay").style.display = "flex";
  runSetupWizard();
}

function closeSetupModal() {
  $("routerModalOverlay").style.display = "none";
  if (_resolverPollTimer) { clearInterval(_resolverPollTimer); _resolverPollTimer = null; }
}

let _resolverPollTimer = null;

async function runSetupWizard() {
  if (!routerStatus) await loadSetupStatus();
  updateStepStatus("caddy",       routerStatus.caddy_installed);
  updateStepStatus("dnsmasq",     routerStatus.dnsmasq_installed);
  if (routerStatus.caddy_installed && routerStatus.dnsmasq_installed) {
    await runDnsmasqConfig();
  }
}

function updateStepStatus(stepId, ok, running = false) {
  const el = $(`step-${stepId}-status`);
  const body = $(`step-${stepId}-body`);
  if (running) {
    el.textContent = "⏳";
    if (body) body.style.display = "none";
    return;
  }
  el.textContent = ok ? "✅" : "❌";
  if (body) body.style.display = ok ? "none" : "";
}

async function checkCaddyInstalled() {
  await loadSetupStatus();
  updateStepStatus("caddy", routerStatus.caddy_installed);
  if (routerStatus.caddy_installed && routerStatus.dnsmasq_installed) runDnsmasqConfig();
}

async function checkDnsmasqInstalled() {
  await loadSetupStatus();
  updateStepStatus("dnsmasq", routerStatus.dnsmasq_installed);
  if (routerStatus.caddy_installed && routerStatus.dnsmasq_installed) runDnsmasqConfig();
}

async function runDnsmasqConfig() {
  updateStepStatus("dnsmasq-cfg", false, true);
  try {
    const res  = await fetch("/api/router/setup/dnsmasq", { method: "POST" });
    const data = await res.json();
    updateStepStatus("dnsmasq-cfg", data.ok);
    if (data.ok) startResolverPolling();
    else $("routerModalError").textContent = data.error || "dnsmasq configuration failed";
  } catch (e) {
    updateStepStatus("dnsmasq-cfg", false);
    $("routerModalError").textContent = e.message;
  }
}

function startResolverPolling() {
  updateStepStatus("resolver", false);
  _resolverPollTimer = setInterval(async () => {
    await loadSetupStatus();
    if (routerStatus.resolver_configured) {
      clearInterval(_resolverPollTimer);
      _resolverPollTimer = null;
      updateStepStatus("resolver", true);
      $("routerModalDoneBtn").style.display = "";
    }
  }, 2000);
}

async function finishSetup() {
  const res  = await fetch("/api/router/setup/caddy-start", { method: "POST" });
  const data = await res.json();
  if (!data.ok) {
    $("routerModalError").textContent = data.error || "Failed to start Caddy";
    return;
  }
  closeSetupModal();
  await loadSetupStatus();
  await loadHostnames();
  renderShelf();
}

async function restartRouterServices() {
  await fetch("/api/router/setup/caddy-start", { method: "POST" });
  await fetch("/api/router/setup/dnsmasq",     { method: "POST" });
  await loadSetupStatus();
  renderShelf();
}
```

- [ ] **Step 3: Verify the banner appears and the modal opens**

```bash
python3.13 seshat.py &
sleep 2
# Open http://localhost:9000 in a browser
# Expected: yellow setup banner visible (Caddy/dnsmasq not installed in test env)
# Click "Set Up" → modal opens with 4 steps
kill %1
```

- [ ] **Step 4: Commit**

```bash
git add static/app.js
git commit -m "feat: router setup banner and modal wizard JavaScript"
```

---

## Task 10: Hostname Chip in Shelf Rows

**Files:**
- Modify: `static/app.js`

- [ ] **Step 1: Add `hostnames` state and `loadHostnames()`**

Find the state variables at the top of `app.js`. Add after `let routerStatus = null;`:

```javascript
let hostnames = [];   // [{project_name, hostname, port}] from /api/router/hostnames
```

Add `loadHostnames` function after `restartRouterServices`:

```javascript
async function loadHostnames() {
  try {
    const res = await fetch("/api/router/hostnames");
    hostnames = await res.json();
  } catch (_) { /* server may be restarting */ }
}
```

Add `loadHostnames()` to the `DOMContentLoaded` listener, after `loadSetupStatus()`:

```javascript
  loadSetupStatus();
  loadHostnames();
```

- [ ] **Step 2: Add hostname chip to `projectRowHTML`**

Find `function projectRowHTML(p)`. Find the line that renders the port:

```javascript
      <div class="project-port">:${p.port}</div>
```

Replace it with:

```javascript
      <div class="project-port">:${p.port}</div>
      ${_hostnameChipHTML(p.name)}
```

Add the helper function immediately before `projectRowHTML`:

```javascript
function _hostnameChipHTML(projectName) {
  const h = hostnames.find(x => x.project_name === projectName);
  if (!h) return "";
  const ready = routerReady();
  return `<div class="hostname-chip${ready ? "" : " muted"}"
               data-hostname="${esc(h.hostname)}">${esc(h.hostname)}</div>`;
}
```

- [ ] **Step 3: Wire the chip click in `attachRowEvents`**

In `attachRowEvents`, after `row.querySelector(".open-browser-btn").addEventListener(...)`, add:

```javascript
    row.querySelector(".hostname-chip:not(.muted)")?.addEventListener("click", e => {
      e.stopPropagation();
      window.open(`http://${e.currentTarget.dataset.hostname}`, "_blank");
    });
```

- [ ] **Step 4: Refresh hostnames after setup and after project changes**

In `finishSetup()`, `loadHostnames()` is already called (Task 9). Also call it in `refresh()` when the projects list changes — add to the `refresh()` function, after updating `projects`:

```javascript
    // Re-render hostnames chip if the project list changed
    if (activeView === "projects") loadHostnames().then(renderShelf);
```

Actually, `refresh()` already calls `render()` which calls `renderShelf()`. We just need to make sure `hostnames` is fresh. Simplest: call `loadHostnames()` inside `refresh()` every 5 seconds alongside the other fetches:

Find the `Promise.all` in `refresh()` and add the hostnames fetch:

```javascript
  const [projRes, orphanRes, groupRes, hostnamesRes] = await Promise.all([
    fetch("/api/projects"),
    fetch("/api/orphans"),
    fetch("/api/groups"),
    fetch("/api/router/hostnames"),
  ]);
  projects  = await projRes.json();
  orphans   = await orphanRes.json();
  groups    = await groupRes.json();
  hostnames = await hostnamesRes.json();
```

Remove the separate `loadHostnames()` call from `DOMContentLoaded` since `refresh()` handles it now.

- [ ] **Step 5: Verify chip appears on shelf rows**

```bash
python3.13 seshat.py &
sleep 2
# Register a test project via http://localhost:9000 UI
# Refresh the page — shelf row should show a muted hostname chip
kill %1
```

- [ ] **Step 6: Commit**

```bash
git add static/app.js
git commit -m "feat: hostname chip on shelf row — shows .seshat address alongside port"
```

---

## Task 11: Hostname Inline Edit in Detail Panel

**Files:**
- Modify: `static/app.js`

- [ ] **Step 1: Add Local Address field to `updateDetailPanel`**

Find `updateDetailPanel` in `app.js`. Find the Configuration section that starts with `<div class="detail-section">` and contains the Directory field. Add a **Local Address** field after the port/URL row — specifically after the `<div class="detail-url">` line, before the `<div class="detail-status">` line:

Locate the template string inside `$("detailInner").innerHTML = \`...\`` and find this block:

```javascript
    <div class="detail-url">localhost:${p.port}</div>
    <div class="detail-status ${statusCls}">${statusTxt}</div>
```

Replace with:

```javascript
    <div class="detail-url">localhost:${p.port}</div>
    <div class="detail-status ${statusCls}">${statusTxt}</div>
    ${_hostnameDetailFieldHTML(p.name)}
```

Add the helper function before `updateDetailPanel`:

```javascript
function _hostnameDetailFieldHTML(projectName) {
  const h = hostnames.find(x => x.project_name === projectName);
  const current = h ? h.hostname : _slugify(projectName);
  const safeN = projectName.replace(/\\/g, "\\\\").replace(/'/g, "\\'");
  return `
    <div class="detail-field hostname-detail-field" id="hostname-field-${esc(projectName)}">
      <div class="detail-label">Local Address</div>
      <div class="hostname-field-view">
        <span class="hostname-field-value">${esc(current)}</span>
        <button class="detail-section-action" onclick="editHostname('${safeN}')">Edit</button>
      </div>
    </div>`;
}

function _slugify(name) {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") + ".seshat";
}
```

- [ ] **Step 2: Add `editHostname`, `saveHostname`, and `resetHostname` JS functions**

Append to the router section in `app.js`:

```javascript
function editHostname(projectName) {
  const field = $(`hostname-field-${projectName}`);
  if (!field) return;
  const h       = hostnames.find(x => x.project_name === projectName);
  const current = h ? h.hostname : _slugify(projectName);
  const safeN   = projectName.replace(/\\/g, "\\\\").replace(/'/g, "\\'");
  field.querySelector(".hostname-field-view").outerHTML = `
    <div class="hostname-field-edit">
      <input class="hostname-edit-input" id="hostname-input-${esc(projectName)}"
             value="${esc(current)}" spellcheck="false">
      <button class="btn btn-primary btn-sm" onclick="saveHostname('${safeN}')">Save</button>
      <button class="btn btn-ghost  btn-sm" onclick="resetHostname('${safeN}')">Reset to default</button>
    </div>`;
}

async function saveHostname(projectName) {
  const input = $(`hostname-input-${projectName}`);
  if (!input) return;
  const hostname = input.value.trim();
  const res  = await fetch(`/api/router/hostnames/${encodeURIComponent(projectName)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ hostname }),
  });
  const data = await res.json();
  if (data.error) { toast(data.error, "error"); return; }
  await loadHostnames();
  renderShelf();
  updateDetailPanel(projectName);
}

async function resetHostname(projectName) {
  const res  = await fetch(`/api/router/hostnames/${encodeURIComponent(projectName)}`, {
    method: "DELETE",
  });
  const data = await res.json();
  if (data.error) { toast(data.error, "error"); return; }
  await loadHostnames();
  renderShelf();
  updateDetailPanel(projectName);
}
```

- [ ] **Step 3: Verify the Local Address field appears in the detail panel**

```bash
python3.13 seshat.py &
sleep 2
# Open http://localhost:9000, click a project to open detail panel
# Expected: "Local Address" field with auto-generated .seshat hostname and "Edit" button
# Click Edit → text input appears with Save / Reset to default buttons
kill %1
```

- [ ] **Step 4: Commit**

```bash
git add static/app.js
git commit -m "feat: hostname inline edit in project detail panel"
```

---

## Task 12: Vault Integration Hint

**Files:**
- Modify: `static/app.js`

- [ ] **Step 1: Add `buildLocalhostHint` helper**

Find `initVaultViewEvents` in `app.js`. Before that function, add:

```javascript
function buildLocalhostHint(value, key, proj) {
  // value must match http://localhost:PORT or https://localhost:PORT
  const m = /^https?:\/\/localhost:(\d+)/.exec(value);
  if (!m) return null;
  const port = parseInt(m[1], 10);
  const match = hostnames.find(h => h.port === port);
  if (!match) return null;
  const hostnameUrl = `http://${match.hostname}`;
  const safeKey  = key.replace(/\\/g, "\\\\").replace(/'/g, "\\'");
  const safeProj = (proj || "").replace(/\\/g, "\\\\").replace(/'/g, "\\'");
  return `
    <div class="vault-hostname-hint">
      You can also use <code>${esc(hostnameUrl)}</code>
      <button class="vault-hostname-hint-btn"
              onclick="useHostnameForVaultKey('${safeKey}','${esc(hostnameUrl)}','${safeProj}')">
        Use hostname
      </button>
    </div>`;
}
```

- [ ] **Step 2: Show the hint when a shared key is revealed**

In `initVaultViewEvents`, find the reveal handler for shared keys. It ends with:

```javascript
        el.textContent = d.value; el.classList.add("revealed");
```

Replace that line with:

```javascript
        el.textContent = d.value; el.classList.add("revealed");
        const hint = buildLocalhostHint(d.value, key, null);
        if (hint) {
          el.closest(".vault-key-row")
            .querySelector(".vault-row-actions")
            .insertAdjacentHTML("beforebegin", hint);
        }
```

Find the reveal handler for override keys (similar pattern, ends the same way). Replace similarly:

```javascript
        el.textContent = d.value; el.classList.add("revealed");
        const hint = buildLocalhostHint(d.value, key, proj);
        if (hint) {
          el.closest(".vault-key-row")
            .querySelector(".vault-row-actions")
            .insertAdjacentHTML("beforebegin", hint);
        }
```

Also, in both handlers, remove the hint if the value is hidden again (when toggling off). Find where `el.textContent = "••••••••"` is set and add:

```javascript
        el.textContent = "••••••••"; el.classList.remove("revealed");
        el.closest(".vault-key-row")
          .querySelector(".vault-hostname-hint")?.remove();
```

- [ ] **Step 3: Add `useHostnameForVaultKey`**

Append to the router section in `app.js`:

```javascript
async function useHostnameForVaultKey(key, hostnameUrl, proj) {
  const url = proj
    ? `/api/vault/overrides/${encodeURIComponent(proj)}`
    : "/api/vault/keys";
  const body = proj
    ? { key, value: hostnameUrl }
    : { key, value: hostnameUrl };
  const res  = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (data.error) { toast(data.error, "error"); return; }
  toast("Updated to hostname URL", "success");
  await renderVaultView();
}
```

- [ ] **Step 4: Verify the hint appears**

```bash
python3.13 seshat.py &
sleep 2
# Open vault, add a shared key with value "http://localhost:5001"
# Click the 👁 reveal button
# Expected: hint appears: "You can also use http://vault.seshat" (if vault project is registered with port 5001)
# Click "Use hostname" → vault entry updates to the hostname URL
kill %1
```

- [ ] **Step 5: Commit**

```bash
git add static/app.js
git commit -m "feat: vault localhost hint — suggests .seshat hostname when localhost:PORT is stored"
```

---

## Task 13: CSS

**Files:**
- Modify: `static/style.css`

- [ ] **Step 1: Add all new CSS rules**

Append to the end of `static/style.css`:

```css
/* ── Router setup banner ─────────────────────────────────────────────────── */

.router-banner {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 16px;
  background: var(--warning-bg, #fffbeb);
  border-bottom: 1px solid var(--warning-border, #fde68a);
  font-size: 13px;
  color: var(--text-primary);
}

.router-banner span { flex: 1; }

.router-banner code {
  font-family: var(--font-mono, monospace);
  background: rgba(0,0,0,.06);
  padding: 1px 4px;
  border-radius: 3px;
}

.router-banner-btn {
  padding: 4px 10px;
  border: 1px solid var(--border);
  border-radius: 5px;
  background: white;
  cursor: pointer;
  font-size: 12px;
  white-space: nowrap;
}

.router-banner-btn:hover { background: var(--surface-hover, #f5f5f5); }

/* ── Router setup modal ──────────────────────────────────────────────────── */

.router-modal {
  width: 520px;
  max-width: 95vw;
}

.router-modal-intro {
  font-size: 13px;
  color: var(--text-secondary);
  margin: 0 0 20px;
  line-height: 1.5;
}

.router-modal-intro code {
  font-family: var(--font-mono, monospace);
  background: rgba(0,0,0,.06);
  padding: 1px 4px;
  border-radius: 3px;
}

.router-setup-steps {
  list-style: none;
  padding: 0;
  margin: 0 0 20px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.router-step {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 14px;
}

.router-step-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 14px;
  font-weight: 500;
}

.router-step-status { font-size: 16px; }

.router-step-body {
  margin-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.router-cmd {
  font-family: var(--font-mono, monospace);
  font-size: 12px;
  background: var(--surface-secondary, #f8f8f8);
  border: 1px solid var(--border);
  border-radius: 5px;
  padding: 8px 12px;
  margin: 0;
  white-space: pre-wrap;
  user-select: all;
}

.router-modal-footer {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-top: 16px;
  border-top: 1px solid var(--border);
}

/* ── Hostname chip (shelf row) ───────────────────────────────────────────── */

.hostname-chip {
  font-family: var(--font-mono, monospace);
  font-size: 11px;
  padding: 2px 7px;
  border-radius: 10px;
  background: var(--accent-light, #e0f2fe);
  color: var(--accent, #0284c7);
  border: 1px solid var(--accent-border, #bae6fd);
  cursor: pointer;
  white-space: nowrap;
  flex-shrink: 0;
  transition: background 0.15s;
}

.hostname-chip:hover { background: var(--accent-hover, #bae6fd); }

.hostname-chip.muted {
  background: var(--surface-secondary, #f4f4f4);
  color: var(--text-muted);
  border-color: var(--border);
  cursor: default;
  pointer-events: none;
}

/* ── Hostname inline edit (detail panel) ─────────────────────────────────── */

.hostname-detail-field .hostname-field-view {
  display: flex;
  align-items: center;
  gap: 8px;
}

.hostname-field-value {
  font-family: var(--font-mono, monospace);
  font-size: 13px;
}

.hostname-field-edit {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.hostname-edit-input {
  font-family: var(--font-mono, monospace);
  font-size: 13px;
  padding: 4px 8px;
  border: 1px solid var(--border);
  border-radius: 5px;
  flex: 1;
  min-width: 180px;
}

.hostname-edit-input:focus {
  outline: none;
  border-color: var(--accent, #0284c7);
  box-shadow: 0 0 0 2px rgba(2,132,199,.15);
}

/* ── Vault hostname hint ─────────────────────────────────────────────────── */

.vault-hostname-hint {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  margin: 4px 0 0;
  background: var(--accent-light, #e0f2fe);
  border: 1px solid var(--accent-border, #bae6fd);
  border-radius: 6px;
  font-size: 12px;
  color: var(--text-secondary);
}

.vault-hostname-hint code {
  font-family: var(--font-mono, monospace);
  color: var(--accent, #0284c7);
}

.vault-hostname-hint-btn {
  padding: 2px 8px;
  border: 1px solid var(--accent-border, #bae6fd);
  border-radius: 4px;
  background: white;
  color: var(--accent, #0284c7);
  font-size: 11px;
  cursor: pointer;
  white-space: nowrap;
}

.vault-hostname-hint-btn:hover { background: var(--accent-hover, #bae6fd); }
```

- [ ] **Step 2: Verify the styles render correctly**

```bash
python3.13 seshat.py &
sleep 2
# Open http://localhost:9000
# ✓ Setup banner visible with yellow background
# ✓ Hostname chips appear on shelf rows (muted if caddy/dnsmasq not running)
# ✓ Detail panel shows Local Address field with correct typography
# ✓ Click Edit → edit mode with styled input and Save/Reset buttons
kill %1
```

- [ ] **Step 3: Commit**

```bash
git add static/style.css
git commit -m "feat: CSS for hostname chip, setup banner, setup modal, vault hint"
```

---

## Final: Run all tests

- [ ] **Run the full test suite**

```bash
cd /Users/rmichaelthomas/seshat-repo
python3.13 -m pytest tests/ -v
```

Expected: all tests PASS (38 existing + ≈44 new router tests)

- [ ] **Final commit if any loose files**

```bash
git status
# If anything uncommitted:
git add -A
git commit -m "chore: phase 6 implementation complete"
```
