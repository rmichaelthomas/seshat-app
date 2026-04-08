# Phase 5 — Folder Organization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a dedicated Organize tab to Seshat with a folder map, tag-driven structure recommendations, a safe migration assistant (shutil.move + registry update + full Git verification + health check), and full rollback history.

**Architecture:** New `organizer.py` module handles all business logic. Six new routes added to `seshat.py` under `# ── Organize ──`. Frontend adds a third view (`"organize"`) to the existing `activeView` state machine in `app.js`, following the same pattern as the Vault view.

**Tech Stack:** Python 3.11+, Flask 3.x, PyYAML, shutil (stdlib), subprocess (stdlib), pytest

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `organizer.py` | All folder-org logic: map, recommendations, migrate, rollback, moves.log I/O |
| Create | `tests/__init__.py` | Empty — makes tests a package |
| Create | `tests/test_organizer.py` | Unit tests for organizer.py |
| Modify | `seshat.py` | 6 new API routes + `import organizer` |
| Modify | `requirements.txt` | Add `pytest>=8.0` |
| Modify | `templates/index.html` | Organize button in header + organizeView div |
| Modify | `static/app.js` | Organize view state + 5 new render/action functions |
| Modify | `static/style.css` | Styles for organize tab components |

---

## Task 1: Test infrastructure + moves.log data model

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/test_organizer.py`
- Create: `organizer.py` (stub + `_load_moves`, `_write_moves`, `_append_move`)
- Modify: `requirements.txt`

- [ ] **Step 1: Add pytest to requirements.txt**

Open `requirements.txt` and add one line at the end:

```
pytest>=8.0
```

- [ ] **Step 2: Install pytest**

```bash
pip install pytest>=8.0
```

Expected: `Successfully installed pytest-...`

- [ ] **Step 3: Create tests package**

```bash
mkdir -p tests && touch tests/__init__.py
```

- [ ] **Step 4: Write failing tests for moves.log I/O**

Create `tests/test_organizer.py`:

```python
import pytest
from pathlib import Path
from unittest.mock import patch

import registry as reg_module
import organizer as org_module
from registry import Registry
from organizer import Organizer


@pytest.fixture
def tmp_seshat(tmp_path, monkeypatch):
    """Patch ~/.seshat to a temp directory for all tests."""
    monkeypatch.setattr(reg_module, "SESHAT_DIR",    tmp_path)
    monkeypatch.setattr(reg_module, "REGISTRY_FILE", tmp_path / "registry.yaml")
    monkeypatch.setattr(reg_module, "STATE_FILE",    tmp_path / "state.json")
    monkeypatch.setattr(reg_module, "GROUPS_FILE",   tmp_path / "groups.yaml")
    monkeypatch.setattr(org_module, "SESHAT_DIR",    tmp_path)
    monkeypatch.setattr(org_module, "MOVES_FILE",    tmp_path / "moves.log")
    return tmp_path


@pytest.fixture
def org(tmp_seshat):
    """Fresh Organizer backed by a temp registry."""
    reg = Registry()
    return Organizer(reg)


# ── moves.log I/O ──────────────────────────────────────────────────────────

def test_load_history_empty(org):
    assert org.load_history() == []


def test_append_and_load_move(org):
    record = {
        "id":              "20260408-120000-test",
        "project":         "TestApp",
        "from":            "/old/path",
        "to":              "/new/path",
        "timestamp":       "2026-04-08T12:00:00+00:00",
        "git_verified":    True,
        "health_verified": True,
        "rolled_back":     False,
    }
    org._append_move(record)
    history = org.load_history()
    assert len(history) == 1
    assert history[0]["id"] == "20260408-120000-test"


def test_load_history_returns_newest_first(org):
    for i in range(3):
        org._append_move({
            "id": f"id-{i}", "project": "X", "from": "/a", "to": "/b",
            "timestamp": f"2026-04-0{i+1}T00:00:00+00:00",
            "git_verified": True, "health_verified": True, "rolled_back": False,
        })
    history = org.load_history()
    assert [h["id"] for h in history] == ["id-2", "id-1", "id-0"]


def test_load_history_filtered_by_project(org):
    org._append_move({
        "id": "a", "project": "Alpha", "from": "/a", "to": "/b",
        "timestamp": "2026-04-08T00:00:00+00:00",
        "git_verified": True, "health_verified": True, "rolled_back": False,
    })
    org._append_move({
        "id": "b", "project": "Beta", "from": "/c", "to": "/d",
        "timestamp": "2026-04-08T00:01:00+00:00",
        "git_verified": True, "health_verified": True, "rolled_back": False,
    })
    assert len(org.load_history("Alpha")) == 1
    assert org.load_history("Alpha")[0]["id"] == "a"
```

- [ ] **Step 5: Run tests to verify they fail**

```bash
pytest tests/test_organizer.py -v
```

Expected: `ImportError` or `ModuleNotFoundError` (organizer.py doesn't exist yet)

- [ ] **Step 6: Create organizer.py with moves.log I/O**

Create `organizer.py`:

```python
"""
organizer.py — folder map, recommended structure, safe migration, rollback.
"""

import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yaml

from registry import Registry, SESHAT_DIR

MOVES_FILE = SESHAT_DIR / "moves.log"

_YAML_OPTS = dict(default_flow_style=False, allow_unicode=True, sort_keys=False)

TAG_DIRS = {
    "infrastructure": "infrastructure",
    "games":          "games",
    "creative":       "creative",
    "civic":          "civic",
    "rag":            "infrastructure",
}


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


class Organizer:

    def __init__(self, registry: Registry):
        self.registry = registry

    # ── History ────────────────────────────────────────────────────────────

    def load_history(self, project_name: str = None) -> list[dict]:
        moves = self._load_moves()
        if project_name:
            moves = [m for m in moves if m["project"] == project_name]
        return list(reversed(moves))

    # ── Moves log I/O ──────────────────────────────────────────────────────

    def _load_moves(self) -> list[dict]:
        if not MOVES_FILE.exists():
            return []
        data = yaml.safe_load(MOVES_FILE.read_text()) or {}
        return data.get("moves", [])

    def _append_move(self, record: dict) -> None:
        moves = self._load_moves()
        moves.append(record)
        self._write_moves(moves)

    def _write_moves(self, moves: list[dict]) -> None:
        SESHAT_DIR.mkdir(exist_ok=True)
        MOVES_FILE.write_text(yaml.dump({"moves": moves}, **_YAML_OPTS))
```

- [ ] **Step 7: Run tests to verify they pass**

```bash
pytest tests/test_organizer.py -v
```

Expected: 4 tests PASS

- [ ] **Step 8: Commit**

```bash
git add requirements.txt tests/__init__.py tests/test_organizer.py organizer.py
git commit -m "feat: add organizer.py skeleton with moves.log I/O + tests"
```

---

## Task 2: folder_map()

**Files:**
- Modify: `organizer.py` — add `folder_map()`
- Modify: `tests/test_organizer.py` — add folder map tests

- [ ] **Step 1: Write failing tests**

Append to `tests/test_organizer.py`:

```python
# ── folder_map ─────────────────────────────────────────────────────────────

def test_folder_map_empty(org):
    assert org.folder_map() == []


def test_folder_map_groups_by_parent(org, tmp_seshat):
    # Create actual directories so expanduser/resolve works
    proj_a = tmp_seshat / "projects" / "app-a"
    proj_b = tmp_seshat / "projects" / "app-b"
    proj_c = tmp_seshat / "other" / "app-c"
    proj_a.mkdir(parents=True)
    proj_b.mkdir(parents=True)
    proj_c.mkdir(parents=True)

    org.registry.add({"name": "AppA", "port": 3000, "directory": str(proj_a),
                       "start": "npm start", "tags": ["games"], "url": "", "stop": "",
                       "notes": "", "dependencies": [], "env": []})
    org.registry.add({"name": "AppB", "port": 3001, "directory": str(proj_b),
                       "start": "npm start", "tags": [], "url": "", "stop": "",
                       "notes": "", "dependencies": [], "env": []})
    org.registry.add({"name": "AppC", "port": 4000, "directory": str(proj_c),
                       "start": "flask run", "tags": ["infrastructure"], "url": "", "stop": "",
                       "notes": "", "dependencies": [], "env": []})

    result = org.folder_map()
    assert len(result) == 2
    parents = [g["parent"] for g in result]
    assert str(proj_a.parent) in parents
    assert str(proj_c.parent) in parents

    group_projects = next(g for g in result if g["parent"] == str(proj_a.parent))
    names = [p["name"] for p in group_projects["projects"]]
    assert "AppA" in names
    assert "AppB" in names


def test_folder_map_single_project(org, tmp_seshat):
    proj = tmp_seshat / "work" / "myapp"
    proj.mkdir(parents=True)
    org.registry.add({"name": "MyApp", "port": 5000, "directory": str(proj),
                       "start": "python app.py", "tags": [], "url": "", "stop": "",
                       "notes": "", "dependencies": [], "env": []})
    result = org.folder_map()
    assert len(result) == 1
    assert result[0]["parent"] == str(proj.parent)
    assert result[0]["projects"][0]["name"] == "MyApp"
```

- [ ] **Step 2: Run tests to see them fail**

```bash
pytest tests/test_organizer.py -v -k "folder_map"
```

Expected: `AttributeError: 'Organizer' object has no attribute 'folder_map'`

- [ ] **Step 3: Add folder_map() to organizer.py**

Add this method inside the `Organizer` class, after `__init__`:

```python
# ── Folder map ─────────────────────────────────────────────────────────────

def folder_map(self) -> list[dict]:
    projects = self.registry.list()
    groups: dict[str, list] = {}
    for p in projects:
        expanded = str(Path(p["directory"]).expanduser().resolve())
        parent   = str(Path(expanded).parent)
        groups.setdefault(parent, []).append({
            "name":      p["name"],
            "port":      p["port"],
            "tags":      p.get("tags", []),
            "directory": expanded,
        })
    return [{"parent": k, "projects": v} for k, v in sorted(groups.items())]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_organizer.py -v
```

Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add organizer.py tests/test_organizer.py
git commit -m "feat: organizer folder_map()"
```

---

## Task 3: recommend_structure()

**Files:**
- Modify: `organizer.py` — add `recommend_structure()`
- Modify: `tests/test_organizer.py` — add recommendation tests

- [ ] **Step 1: Write failing tests**

Append to `tests/test_organizer.py`:

```python
# ── recommend_structure ────────────────────────────────────────────────────

def _add_project(org, name, port, directory, tags):
    Path(directory).mkdir(parents=True, exist_ok=True)
    org.registry.add({"name": name, "port": port, "directory": directory,
                       "start": "npm start", "tags": tags, "url": "", "stop": "",
                       "notes": "", "dependencies": [], "env": []})


def test_recommend_maps_tag_to_subdir(org, tmp_seshat):
    _add_project(org, "VAULT", 5001, str(tmp_seshat / "old" / "vault"), ["infrastructure", "rag"])
    recs = org.recommend_structure(root=str(tmp_seshat / "Projects"))
    assert len(recs) == 1
    rec = recs[0]
    assert rec["project_name"] == "VAULT"
    assert "infrastructure" in rec["suggested"]
    assert "vault" in rec["suggested"]


def test_recommend_unknown_tag_goes_to_misc(org, tmp_seshat):
    _add_project(org, "RandomApp", 9001, str(tmp_seshat / "old" / "random"), ["unknown-tag"])
    recs = org.recommend_structure(root=str(tmp_seshat / "Projects"))
    assert "misc" in recs[0]["suggested"]


def test_recommend_slug_lowercases_and_hyphenates(org, tmp_seshat):
    _add_project(org, "SLAPS Prototype", 3000, str(tmp_seshat / "old" / "slaps"), ["games"])
    recs = org.recommend_structure(root=str(tmp_seshat / "Projects"))
    assert recs[0]["slug"] == "slaps-prototype"
    assert "slaps-prototype" in recs[0]["suggested"]


def test_recommend_current_path_is_resolved(org, tmp_seshat):
    _add_project(org, "App", 8000, str(tmp_seshat / "old" / "app"), [])
    recs = org.recommend_structure()
    assert Path(recs[0]["current"]).is_absolute()


def test_recommend_rag_tag_maps_to_infrastructure(org, tmp_seshat):
    _add_project(org, "RAGApp", 5002, str(tmp_seshat / "old" / "rag"), ["rag"])
    recs = org.recommend_structure(root=str(tmp_seshat / "Projects"))
    assert "infrastructure" in recs[0]["suggested"]
```

- [ ] **Step 2: Run tests to see them fail**

```bash
pytest tests/test_organizer.py -v -k "recommend"
```

Expected: `AttributeError: 'Organizer' object has no attribute 'recommend_structure'`

- [ ] **Step 3: Add recommend_structure() to organizer.py**

Add this method inside `Organizer`, after `folder_map`:

```python
# ── Recommendations ────────────────────────────────────────────────────────

def recommend_structure(self, root: str = "~/Projects") -> list[dict]:
    root_path = Path(root).expanduser()
    result = []
    for p in self.registry.list():
        current  = str(Path(p["directory"]).expanduser().resolve())
        slug     = _slugify(p["name"])
        subdir   = next(
            (TAG_DIRS[t] for t in (p.get("tags") or []) if t in TAG_DIRS),
            "misc",
        )
        suggested = str(root_path / subdir / slug)
        result.append({
            "project_name": p["name"],
            "current":      current,
            "suggested":    suggested,
            "slug":         slug,
        })
    return result
```

- [ ] **Step 4: Run all tests**

```bash
pytest tests/test_organizer.py -v
```

Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add organizer.py tests/test_organizer.py
git commit -m "feat: organizer recommend_structure()"
```

---

## Task 4: migrate() — core move

**Files:**
- Modify: `organizer.py` — add `migrate()` with move + registry update + log write (git/health stubbed as ok)
- Modify: `tests/test_organizer.py` — add migration tests

- [ ] **Step 1: Write failing tests**

Append to `tests/test_organizer.py`:

```python
# ── migrate ────────────────────────────────────────────────────────────────

def test_migrate_moves_directory(org, tmp_seshat):
    src = tmp_seshat / "old" / "myapp"
    src.mkdir(parents=True)
    dst = tmp_seshat / "new" / "myapp"
    dst.parent.mkdir(parents=True)

    org.registry.add({"name": "MyApp", "port": 5000, "directory": str(src),
                       "start": "python app.py", "tags": [], "url": "", "stop": "",
                       "notes": "", "dependencies": [], "env": []})

    with patch("organizer.Organizer._git_verify", return_value={"ok": True}), \
         patch("organizer.Organizer._health_check", return_value={"ok": True, "check_type": "unknown"}):
        result = org.migrate("MyApp", str(dst))

    assert result["ok"] is True
    assert dst.exists()
    assert not src.exists()


def test_migrate_updates_registry(org, tmp_seshat):
    src = tmp_seshat / "old" / "myapp"
    src.mkdir(parents=True)
    dst = tmp_seshat / "new" / "myapp"
    dst.parent.mkdir(parents=True)

    org.registry.add({"name": "MyApp", "port": 5000, "directory": str(src),
                       "start": "python app.py", "tags": [], "url": "", "stop": "",
                       "notes": "", "dependencies": [], "env": []})

    with patch("organizer.Organizer._git_verify", return_value={"ok": True}), \
         patch("organizer.Organizer._health_check", return_value={"ok": True, "check_type": "unknown"}):
        org.migrate("MyApp", str(dst))

    updated = org.registry.get("MyApp")
    assert updated["directory"] == str(dst)


def test_migrate_appends_move_record(org, tmp_seshat):
    src = tmp_seshat / "old" / "myapp"
    src.mkdir(parents=True)
    dst = tmp_seshat / "new" / "myapp"
    dst.parent.mkdir(parents=True)

    org.registry.add({"name": "MyApp", "port": 5000, "directory": str(src),
                       "start": "python app.py", "tags": [], "url": "", "stop": "",
                       "notes": "", "dependencies": [], "env": []})

    with patch("organizer.Organizer._git_verify", return_value={"ok": True}), \
         patch("organizer.Organizer._health_check", return_value={"ok": True, "check_type": "unknown"}):
        result = org.migrate("MyApp", str(dst))

    history = org.load_history()
    assert len(history) == 1
    assert history[0]["id"] == result["move_id"]
    assert history[0]["rolled_back"] is False


def test_migrate_warns_if_project_running(org, tmp_seshat):
    src = tmp_seshat / "old" / "myapp"
    src.mkdir(parents=True)
    dst = tmp_seshat / "new" / "myapp"
    dst.parent.mkdir(parents=True)

    org.registry.add({"name": "MyApp", "port": 5000, "directory": str(src),
                       "start": "python app.py", "tags": [], "url": "", "stop": "",
                       "notes": "", "dependencies": [], "env": []})
    org.registry.set_pid("MyApp", 12345)

    result = org.migrate("MyApp", str(dst), force=False)
    assert result == {"warning": "project_running"}
    assert not dst.exists()  # no move happened


def test_migrate_force_proceeds_if_running(org, tmp_seshat):
    src = tmp_seshat / "old" / "myapp"
    src.mkdir(parents=True)
    dst = tmp_seshat / "new" / "myapp"
    dst.parent.mkdir(parents=True)

    org.registry.add({"name": "MyApp", "port": 5000, "directory": str(src),
                       "start": "python app.py", "tags": [], "url": "", "stop": "",
                       "notes": "", "dependencies": [], "env": []})
    org.registry.set_pid("MyApp", 12345)

    with patch("organizer.Organizer._git_verify", return_value={"ok": True}), \
         patch("organizer.Organizer._health_check", return_value={"ok": True, "check_type": "unknown"}):
        result = org.migrate("MyApp", str(dst), force=True)

    assert result["ok"] is True
    assert dst.exists()


def test_migrate_rejects_existing_destination(org, tmp_seshat):
    src = tmp_seshat / "old" / "myapp"
    src.mkdir(parents=True)
    dst = tmp_seshat / "new" / "myapp"
    dst.mkdir(parents=True)  # already exists

    org.registry.add({"name": "MyApp", "port": 5000, "directory": str(src),
                       "start": "python app.py", "tags": [], "url": "", "stop": "",
                       "notes": "", "dependencies": [], "env": []})

    with pytest.raises(ValueError, match="already exists"):
        org.migrate("MyApp", str(dst))


def test_migrate_rejects_missing_parent(org, tmp_seshat):
    src = tmp_seshat / "old" / "myapp"
    src.mkdir(parents=True)
    dst = tmp_seshat / "nonexistent_parent" / "myapp"

    org.registry.add({"name": "MyApp", "port": 5000, "directory": str(src),
                       "start": "python app.py", "tags": [], "url": "", "stop": "",
                       "notes": "", "dependencies": [], "env": []})

    with pytest.raises(ValueError, match="Parent directory does not exist"):
        org.migrate("MyApp", str(dst))


def test_migrate_unknown_project_raises(org):
    with pytest.raises(ValueError, match="not found"):
        org.migrate("GhostApp", "/some/path")
```

- [ ] **Step 2: Run tests to see them fail**

```bash
pytest tests/test_organizer.py -v -k "migrate"
```

Expected: `AttributeError: 'Organizer' object has no attribute 'migrate'`

- [ ] **Step 3: Add migrate() to organizer.py**

Add this method inside `Organizer`, after `recommend_structure`:

```python
# ── Migration ──────────────────────────────────────────────────────────────

def migrate(self, project_name: str, destination: str, force: bool = False) -> dict:
    project = self.registry.get(project_name)
    if not project:
        raise ValueError(f"Project '{project_name}' not found.")

    state = self.registry.get_state()
    if project_name in state and not force:
        return {"warning": "project_running"}

    current = Path(project["directory"]).expanduser().resolve()
    dest    = Path(destination).expanduser().resolve()

    if dest.exists():
        raise ValueError(f"Destination already exists: {dest}")
    if not dest.parent.exists():
        raise ValueError(f"Parent directory does not exist: {dest.parent}")

    shutil.move(str(current), str(dest))

    self.registry.update(project_name, {"directory": str(dest)})

    git_result    = self._git_verify(str(dest))
    health_result = self._health_check({**project, "directory": str(dest)})

    move_id = (
        f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        f"-{_slugify(project_name)}"
    )
    self._append_move({
        "id":              move_id,
        "project":         project_name,
        "from":            str(current),
        "to":              str(dest),
        "timestamp":       datetime.now(timezone.utc).isoformat(),
        "git_verified":    git_result["ok"],
        "health_verified": health_result["ok"],
        "rolled_back":     False,
    })

    return {
        "ok":           True,
        "move_id":      move_id,
        "git_result":   git_result,
        "health_result": health_result,
    }
```

Also add stub implementations of `_git_verify` and `_health_check` so the class is complete:

```python
# ── Git verification (stub — implemented in Task 5) ────────────────────────

def _git_verify(self, directory: str) -> dict:
    return {"ok": True}

# ── Health check (stub — implemented in Task 6) ────────────────────────────

def _health_check(self, project: dict) -> dict:
    return {"ok": True, "check_type": "unknown"}
```

- [ ] **Step 4: Run all tests**

```bash
pytest tests/test_organizer.py -v
```

Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add organizer.py tests/test_organizer.py
git commit -m "feat: organizer migrate() with move, registry update, and log"
```

---

## Task 5: _git_verify()

**Files:**
- Modify: `organizer.py` — implement `_git_verify()`
- Modify: `tests/test_organizer.py` — add git verify tests

- [ ] **Step 1: Write failing tests**

Append to `tests/test_organizer.py`:

```python
# ── _git_verify ────────────────────────────────────────────────────────────

def test_git_verify_no_git_dir(org, tmp_seshat):
    path = tmp_seshat / "notarepo"
    path.mkdir()
    result = org._git_verify(str(path))
    assert result["ok"] is False
    assert "Not a git repository" in result["error"]


def test_git_verify_passes_when_all_checks_ok(org, tmp_seshat, monkeypatch):
    path = tmp_seshat / "myrepo"
    path.mkdir()
    (path / ".git").mkdir()

    import subprocess as sp
    def fake_run(cmd, **kwargs):
        m = MagicMock()
        m.returncode = 0
        if "remote" in cmd:
            m.stdout = "origin\thttps://github.com/x/y.git (fetch)\n"
        else:
            m.stdout = ""
        m.stderr = ""
        return m

    monkeypatch.setattr(sp, "run", fake_run)
    result = org._git_verify(str(path))
    assert result["ok"] is True


def test_git_verify_fails_if_no_remote(org, tmp_seshat, monkeypatch):
    path = tmp_seshat / "myrepo"
    path.mkdir()
    (path / ".git").mkdir()

    import subprocess as sp
    call_count = [0]
    def fake_run(cmd, **kwargs):
        m = MagicMock()
        call_count[0] += 1
        if "remote" in cmd:
            m.returncode = 0
            m.stdout = ""   # empty = no remotes
            m.stderr = ""
        else:
            m.returncode = 0
            m.stdout = ""
            m.stderr = ""
        return m

    monkeypatch.setattr(sp, "run", fake_run)
    result = org._git_verify(str(path))
    assert result["ok"] is False
    assert "remote" in result["error"].lower()


def test_git_verify_fails_if_status_nonzero(org, tmp_seshat, monkeypatch):
    path = tmp_seshat / "myrepo"
    path.mkdir()
    (path / ".git").mkdir()

    import subprocess as sp
    def fake_run(cmd, **kwargs):
        m = MagicMock()
        m.returncode = 128
        m.stdout = ""
        m.stderr = "fatal: not a git repository"
        return m

    monkeypatch.setattr(sp, "run", fake_run)
    result = org._git_verify(str(path))
    assert result["ok"] is False


def test_git_verify_fails_if_fetch_dry_run_fails(org, tmp_seshat, monkeypatch):
    path = tmp_seshat / "myrepo"
    path.mkdir()
    (path / ".git").mkdir()

    import subprocess as sp
    def fake_run(cmd, **kwargs):
        m = MagicMock()
        if "fetch" in cmd:
            m.returncode = 1
            m.stdout = ""
            m.stderr = "fatal: unable to access"
        else:
            m.returncode = 0
            m.stdout = "origin\thttps://github.com/x/y (fetch)\n" if "remote" in cmd else ""
            m.stderr = ""
        return m

    monkeypatch.setattr(sp, "run", fake_run)
    result = org._git_verify(str(path))
    assert result["ok"] is False
    assert "reachable" in result["error"].lower()
```

- [ ] **Step 2: Run tests to see them fail**

```bash
pytest tests/test_organizer.py -v -k "git_verify"
```

Expected: the `test_git_verify_no_git_dir` test PASSES (stub returns `{"ok": True}` regardless), others FAIL or PASS incorrectly — confirming the stub needs replacing.

- [ ] **Step 3: Replace the _git_verify stub in organizer.py**

Replace the stub `_git_verify` method with:

```python
def _git_verify(self, directory: str) -> dict:
    path = Path(directory)

    if not (path / ".git").exists():
        return {"ok": False, "error": "Not a git repository"}

    def run(cmd: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd, cwd=directory,
            capture_output=True, text=True, timeout=15,
        )

    r = run(["git", "status"])
    if r.returncode != 0:
        return {"ok": False, "error": f"git status failed: {r.stderr.strip()}"}

    r = run(["git", "remote", "-v"])
    if r.returncode != 0 or not r.stdout.strip():
        return {"ok": False, "error": "No git remote configured"}

    r = run(["git", "fetch", "--dry-run"])
    if r.returncode != 0:
        return {"ok": False, "error": f"Remote not reachable: {r.stderr.strip()}"}

    return {"ok": True}
```

- [ ] **Step 4: Run all tests**

```bash
pytest tests/test_organizer.py -v
```

Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add organizer.py tests/test_organizer.py
git commit -m "feat: organizer _git_verify() with full git checks"
```

---

## Task 6: _health_check()

**Files:**
- Modify: `organizer.py` — implement `_health_check()`
- Modify: `tests/test_organizer.py` — add health check tests

- [ ] **Step 1: Write failing tests**

Append to `tests/test_organizer.py`:

```python
# ── _health_check ──────────────────────────────────────────────────────────

def test_health_check_npm_with_package_json(org, tmp_seshat):
    path = tmp_seshat / "myapp"
    path.mkdir()
    (path / "package.json").write_text("{}")
    project = {"start": "npm start", "directory": str(path)}
    result = org._health_check(project)
    assert result["ok"] is True
    assert result["check_type"] == "package.json"


def test_health_check_npm_without_package_json(org, tmp_seshat):
    path = tmp_seshat / "myapp"
    path.mkdir()
    project = {"start": "npm start", "directory": str(path)}
    result = org._health_check(project)
    assert result["ok"] is False
    assert "package.json" in result["error"]


def test_health_check_python_with_requirements_txt(org, tmp_seshat):
    path = tmp_seshat / "myapp"
    path.mkdir()
    (path / "requirements.txt").write_text("flask\n")
    project = {"start": "flask run --port 5001", "directory": str(path)}
    result = org._health_check(project)
    assert result["ok"] is True
    assert result["check_type"] == "requirements.txt"


def test_health_check_python_with_pyproject_toml(org, tmp_seshat):
    path = tmp_seshat / "myapp"
    path.mkdir()
    (path / "pyproject.toml").write_text("[tool.poetry]\n")
    project = {"start": "python -m uvicorn app:app", "directory": str(path)}
    result = org._health_check(project)
    assert result["ok"] is True
    assert result["check_type"] == "pyproject.toml"


def test_health_check_python_missing_both(org, tmp_seshat):
    path = tmp_seshat / "myapp"
    path.mkdir()
    project = {"start": "flask run", "directory": str(path)}
    result = org._health_check(project)
    assert result["ok"] is False


def test_health_check_cargo(org, tmp_seshat):
    path = tmp_seshat / "myapp"
    path.mkdir()
    (path / "Cargo.toml").write_text("[package]\n")
    project = {"start": "cargo run", "directory": str(path)}
    result = org._health_check(project)
    assert result["ok"] is True
    assert result["check_type"] == "Cargo.toml"


def test_health_check_unknown_command_passes(org, tmp_seshat):
    path = tmp_seshat / "myapp"
    path.mkdir()
    project = {"start": "ruby server.rb", "directory": str(path)}
    result = org._health_check(project)
    assert result["ok"] is True
    assert result["check_type"] == "unknown"
```

- [ ] **Step 2: Run tests to see them fail**

```bash
pytest tests/test_organizer.py -v -k "health_check"
```

Expected: most fail because the stub always returns `{"ok": True, "check_type": "unknown"}`

- [ ] **Step 3: Replace the _health_check stub in organizer.py**

Replace the stub `_health_check` method with:

```python
def _health_check(self, project: dict) -> dict:
    start = project.get("start", "").lower()
    dirp  = Path(project["directory"])

    if any(kw in start for kw in ("npm", "yarn", "pnpm", "bun")):
        if (dirp / "package.json").exists():
            return {"ok": True, "check_type": "package.json"}
        return {"ok": False, "error": f"package.json not found in {dirp}"}

    if any(kw in start for kw in ("python", "flask", "uvicorn", "gunicorn")):
        if (dirp / "requirements.txt").exists():
            return {"ok": True, "check_type": "requirements.txt"}
        if (dirp / "pyproject.toml").exists():
            return {"ok": True, "check_type": "pyproject.toml"}
        return {
            "ok":    False,
            "error": f"Neither requirements.txt nor pyproject.toml found in {dirp}",
        }

    if "cargo" in start:
        if (dirp / "Cargo.toml").exists():
            return {"ok": True, "check_type": "Cargo.toml"}
        return {"ok": False, "error": f"Cargo.toml not found in {dirp}"}

    return {"ok": True, "check_type": "unknown"}
```

- [ ] **Step 4: Run all tests**

```bash
pytest tests/test_organizer.py -v
```

Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add organizer.py tests/test_organizer.py
git commit -m "feat: organizer _health_check()"
```

---

## Task 7: rollback()

**Files:**
- Modify: `organizer.py` — add `rollback()`
- Modify: `tests/test_organizer.py` — add rollback tests

- [ ] **Step 1: Write failing tests**

Append to `tests/test_organizer.py`:

```python
# ── rollback ───────────────────────────────────────────────────────────────

def test_rollback_moves_folder_back(org, tmp_seshat):
    src = tmp_seshat / "old" / "myapp"
    src.mkdir(parents=True)
    dst = tmp_seshat / "new" / "myapp"
    dst.parent.mkdir(parents=True)

    org.registry.add({"name": "MyApp", "port": 5000, "directory": str(src),
                       "start": "python app.py", "tags": [], "url": "", "stop": "",
                       "notes": "", "dependencies": [], "env": []})

    with patch("organizer.Organizer._git_verify", return_value={"ok": True}), \
         patch("organizer.Organizer._health_check", return_value={"ok": True, "check_type": "unknown"}):
        result = org.migrate("MyApp", str(dst))
    move_id = result["move_id"]

    with patch("organizer.Organizer._git_verify", return_value={"ok": True}):
        rb = org.rollback(move_id)

    assert rb["ok"] is True
    assert src.exists()
    assert not dst.exists()


def test_rollback_updates_registry(org, tmp_seshat):
    src = tmp_seshat / "old" / "myapp"
    src.mkdir(parents=True)
    dst = tmp_seshat / "new" / "myapp"
    dst.parent.mkdir(parents=True)

    org.registry.add({"name": "MyApp", "port": 5000, "directory": str(src),
                       "start": "python app.py", "tags": [], "url": "", "stop": "",
                       "notes": "", "dependencies": [], "env": []})

    with patch("organizer.Organizer._git_verify", return_value={"ok": True}), \
         patch("organizer.Organizer._health_check", return_value={"ok": True, "check_type": "unknown"}):
        result = org.migrate("MyApp", str(dst))

    with patch("organizer.Organizer._git_verify", return_value={"ok": True}):
        org.rollback(result["move_id"])

    assert org.registry.get("MyApp")["directory"] == str(src)


def test_rollback_marks_record(org, tmp_seshat):
    src = tmp_seshat / "old" / "myapp"
    src.mkdir(parents=True)
    dst = tmp_seshat / "new" / "myapp"
    dst.parent.mkdir(parents=True)

    org.registry.add({"name": "MyApp", "port": 5000, "directory": str(src),
                       "start": "python app.py", "tags": [], "url": "", "stop": "",
                       "notes": "", "dependencies": [], "env": []})

    with patch("organizer.Organizer._git_verify", return_value={"ok": True}), \
         patch("organizer.Organizer._health_check", return_value={"ok": True, "check_type": "unknown"}):
        result = org.migrate("MyApp", str(dst))

    with patch("organizer.Organizer._git_verify", return_value={"ok": True}):
        org.rollback(result["move_id"])

    history = org.load_history()
    assert history[0]["rolled_back"] is True


def test_rollback_rejects_already_rolled_back(org, tmp_seshat):
    src = tmp_seshat / "old" / "myapp"
    src.mkdir(parents=True)
    dst = tmp_seshat / "new" / "myapp"
    dst.parent.mkdir(parents=True)

    org.registry.add({"name": "MyApp", "port": 5000, "directory": str(src),
                       "start": "python app.py", "tags": [], "url": "", "stop": "",
                       "notes": "", "dependencies": [], "env": []})

    with patch("organizer.Organizer._git_verify", return_value={"ok": True}), \
         patch("organizer.Organizer._health_check", return_value={"ok": True, "check_type": "unknown"}):
        result = org.migrate("MyApp", str(dst))

    with patch("organizer.Organizer._git_verify", return_value={"ok": True}):
        org.rollback(result["move_id"])

    with pytest.raises(ValueError, match="already been rolled back"):
        org.rollback(result["move_id"])


def test_rollback_unknown_id_raises(org):
    with pytest.raises(ValueError, match="not found"):
        org.rollback("nonexistent-id")
```

- [ ] **Step 2: Run tests to see them fail**

```bash
pytest tests/test_organizer.py -v -k "rollback"
```

Expected: `AttributeError: 'Organizer' object has no attribute 'rollback'`

- [ ] **Step 3: Add rollback() to organizer.py**

Add this method inside `Organizer`, after `migrate`:

```python
# ── Rollback ───────────────────────────────────────────────────────────────

def rollback(self, move_id: str) -> dict:
    moves = self._load_moves()
    record = next((m for m in moves if m["id"] == move_id), None)
    if not record:
        raise ValueError(f"Move record '{move_id}' not found.")
    if record.get("rolled_back"):
        raise ValueError(f"Move '{move_id}' has already been rolled back.")

    dest   = Path(record["to"]).expanduser().resolve()
    origin = Path(record["from"]).expanduser().resolve()

    if not dest.exists():
        raise ValueError(f"Cannot roll back: '{dest}' no longer exists.")
    if origin.exists():
        raise ValueError(
            f"Cannot roll back: original location '{origin}' is already occupied."
        )

    shutil.move(str(dest), str(origin))
    self.registry.update(record["project"], {"directory": str(origin)})
    git_result = self._git_verify(str(origin))

    record["rolled_back"] = True
    self._write_moves(moves)

    return {"ok": True, "git_result": git_result}
```

- [ ] **Step 4: Run all tests**

```bash
pytest tests/test_organizer.py -v
```

Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add organizer.py tests/test_organizer.py
git commit -m "feat: organizer rollback()"
```

---

## Task 8: seshat.py — 6 Organize API routes

**Files:**
- Modify: `seshat.py` — import Organizer + 6 new routes

- [ ] **Step 1: Add import and instance to seshat.py**

In `seshat.py`, find the imports block at the top. After:

```python
from registry import Registry
from scanner  import Scanner
from runner   import Runner
from vault    import Vault
import deps as deps_module
```

Add:

```python
from organizer import Organizer
```

Then, after the line `vault = Vault()`, add:

```python
organizer = Organizer(registry)
```

- [ ] **Step 2: Add the 6 Organize routes to seshat.py**

Find the comment block `# ── Open in Finder / Terminal / Browser ───` in `seshat.py`. Insert the following block **before** it:

```python
# ── Organize ───────────────────────────────────────────────────────────────


@app.route("/api/organize/map", methods=["GET"])
def get_folder_map():
    return jsonify(organizer.folder_map())


@app.route("/api/organize/recommendations", methods=["GET"])
def get_recommendations():
    root = request.args.get("root", "~/Projects")
    return jsonify(organizer.recommend_structure(root))


@app.route("/api/organize/migrate", methods=["POST"])
def migrate_project():
    data        = request.json or {}
    project     = (data.get("project") or "").strip()
    destination = (data.get("destination") or "").strip()
    force       = bool(data.get("force", False))

    if not project or not destination:
        return jsonify({"error": "Missing required fields: project, destination"}), 400

    try:
        result = organizer.migrate(project, destination, force=force)
        if "warning" in result:
            return jsonify(result), 200
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/organize/history", methods=["GET"])
def get_move_history():
    return jsonify(organizer.load_history())


@app.route("/api/organize/history/<project_name>", methods=["GET"])
def get_project_move_history(project_name):
    return jsonify(organizer.load_history(project_name))


@app.route("/api/organize/rollback", methods=["POST"])
def rollback_move():
    data    = request.json or {}
    move_id = (data.get("move_id") or "").strip()
    if not move_id:
        return jsonify({"error": "Missing required field: move_id"}), 400
    try:
        return jsonify(organizer.rollback(move_id))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
```

- [ ] **Step 3: Smoke test — start the server and verify routes exist**

```bash
python seshat.py &
sleep 2
curl -s http://localhost:9000/api/organize/map
```

Expected: `[]` (or a list if projects are registered)

```bash
curl -s http://localhost:9000/api/organize/recommendations
```

Expected: `[]`

```bash
curl -s http://localhost:9000/api/organize/history
```

Expected: `[]`

```bash
kill %1
```

- [ ] **Step 4: Run all Python tests**

```bash
pytest tests/ -v
```

Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add seshat.py
git commit -m "feat: seshat.py organize API routes"
```

---

## Task 9: index.html — Organize tab HTML

**Files:**
- Modify: `templates/index.html`

- [ ] **Step 1: Add Organize button to the header**

In `templates/index.html`, find:

```html
      <button class="btn btn-ghost btn-vault" id="vaultBtn" title="Secrets Vault">⚿ Vault</button>
```

Add the Organize button immediately before it:

```html
      <button class="btn btn-ghost btn-organize" id="organizeBtn" title="Folder Organization">⌂ Organize</button>
```

- [ ] **Step 2: Add organizeView div to the main area**

Find:

```html
      <!-- ── Vault view ─────────────────────────────────────────────────── -->
      <div id="vaultView" style="display:none">
```

Add the following block **before** it:

```html
      <!-- ── Organize view ──────────────────────────────────────────────── -->
      <div id="organizeView" style="display:none">
        <div id="organizeContent">

          <!-- Section 1: Folder Map -->
          <div class="organize-section">
            <div class="organize-section-header">
              <div class="organize-section-title">Folder Map</div>
              <button class="btn btn-ghost btn-sm" onclick="loadFolderMap()">↺ Refresh</button>
            </div>
            <div id="folderMapContent">
              <div class="empty-state"><div class="empty-state-title">Loading…</div></div>
            </div>
          </div>

          <!-- Section 2: Recommended Structure -->
          <div class="organize-section">
            <div class="organize-section-header">
              <div class="organize-section-title">Recommended Structure</div>
              <div class="organize-root-input">
                <label>Root:</label>
                <input type="text" id="structureRoot" value="~/Projects" style="width:200px">
                <button class="btn btn-ghost btn-sm" onclick="loadRecommendations()">↺ Refresh</button>
                <button class="btn btn-primary btn-sm" id="moveAllBtn" onclick="moveAll()">Move All</button>
              </div>
            </div>
            <div id="recommendationsContent">
              <div class="empty-state"><div class="empty-state-title">Loading…</div></div>
            </div>
          </div>

          <!-- Section 3: Move History -->
          <div class="organize-section">
            <div class="organize-section-header">
              <div class="organize-section-title">Move History</div>
              <button class="btn btn-ghost btn-sm" onclick="loadMoveHistory()">↺ Refresh</button>
            </div>
            <div id="moveHistoryContent">
              <div class="empty-state"><div class="empty-state-sub">No moves recorded yet.</div></div>
            </div>
          </div>

        </div>
      </div>
```

- [ ] **Step 3: Verify the page loads without JS errors**

```bash
python seshat.py &
sleep 2
open http://localhost:9000
# Check browser console — should show no errors
kill %1
```

- [ ] **Step 4: Commit**

```bash
git add templates/index.html
git commit -m "feat: index.html organize tab structure"
```

---

## Task 10: app.js — view switching + folder map

**Files:**
- Modify: `static/app.js`

- [ ] **Step 1: Add organize view to the activeView state machine**

In `static/app.js`, find:

```js
let activeView   = "projects";   // "projects" | "vault"
```

Replace with:

```js
let activeView   = "projects";   // "projects" | "vault" | "organize"
```

- [ ] **Step 2: Wire up the Organize button in DOMContentLoaded**

Find:

```js
  $("vaultBtn").addEventListener("click", toggleVaultView);
```

Add immediately after:

```js
  $("organizeBtn").addEventListener("click", toggleOrganizeView);
```

- [ ] **Step 3: Add view toggle function**

Find the `toggleVaultView` function:

```js
function toggleVaultView() {
  if (activeView === "vault") {
    showProjectView();
  } else {
    showVaultView();
  }
}
```

Add immediately after:

```js
function toggleOrganizeView() {
  if (activeView === "organize") {
    showProjectView();
  } else {
    showOrganizeView();
  }
}
```

- [ ] **Step 4: Add showOrganizeView() and update showProjectView()**

Find `showProjectView()`:

```js
function showProjectView() {
  activeView = "projects";
  $("projectView").style.display = "block";
  $("vaultView").style.display   = "none";
  $("vaultBtn").classList.remove("active");
  $("addProjectBtn").style.display = "";
  closeDetail();
  render();
}
```

Replace with:

```js
function showProjectView() {
  activeView = "projects";
  $("projectView").style.display  = "block";
  $("vaultView").style.display    = "none";
  $("organizeView").style.display = "none";
  $("vaultBtn").classList.remove("active");
  $("organizeBtn").classList.remove("active");
  $("addProjectBtn").style.display = "";
  closeDetail();
  render();
}
```

Find `showVaultView()` and add the hide-organize line:

```js
async function showVaultView() {
  activeView = "vault";
  $("projectView").style.display  = "none";
  $("vaultView").style.display    = "block";
  $("organizeView").style.display = "none";
  $("vaultBtn").classList.add("active");
  $("organizeBtn").classList.remove("active");
  $("addProjectBtn").style.display = "none";
  closeDetail();
  await renderVaultView();
}
```

Add `showOrganizeView()` after `showVaultView()`:

```js
async function showOrganizeView() {
  activeView = "organize";
  $("projectView").style.display  = "none";
  $("vaultView").style.display    = "none";
  $("organizeView").style.display = "block";
  $("organizeBtn").classList.add("active");
  $("vaultBtn").classList.remove("active");
  $("addProjectBtn").style.display = "none";
  closeDetail();
  await Promise.all([loadFolderMap(), loadRecommendations(), loadMoveHistory()]);
}
```

- [ ] **Step 5: Add loadFolderMap() and renderFolderMap()**

Add these functions before the `// ── Utilities ──` comment block at the bottom of `app.js`:

```js
// ── Organize view ──────────────────────────────────────────────────────────

async function loadFolderMap() {
  const el = $("folderMapContent");
  if (!el) return;
  try {
    const res  = await fetch("/api/organize/map");
    const data = await res.json();
    el.innerHTML = renderFolderMap(data);
  } catch (e) {
    el.innerHTML = `<div class="empty-state"><div class="empty-state-sub">Could not load folder map.</div></div>`;
  }
}

function renderFolderMap(groups) {
  if (!groups || groups.length === 0) {
    return `<div class="empty-state"><div class="empty-state-sub">No projects registered yet.</div></div>`;
  }
  return groups.map(g => `
    <div class="folder-group">
      <div class="folder-group-header">${esc(shortPath(g.parent))}</div>
      ${g.projects.map(p => `
        <div class="folder-group-row">
          <span class="folder-project-name">${esc(p.name)}</span>
          <span class="folder-project-port">:${p.port}</span>
          <span class="folder-project-dir">${esc(shortPath(p.directory))}</span>
          ${(p.tags||[]).slice(0,3).map(t=>`<span class="tag">${esc(t)}</span>`).join("")}
        </div>`).join("")}
    </div>`).join("");
}
```

- [ ] **Step 6: Verify Organize tab opens and shows folder map**

```bash
python seshat.py &
sleep 2
open http://localhost:9000
# Click "Organize" button — should show Folder Map section
kill %1
```

- [ ] **Step 7: Commit**

```bash
git add static/app.js
git commit -m "feat: app.js organize view switching and folder map"
```

---

## Task 11: app.js — Recommendations + single Move

**Files:**
- Modify: `static/app.js`

- [ ] **Step 1: Add loadRecommendations() and renderRecommendations()**

Add inside the `// ── Organize view ──` block in `app.js`, after `renderFolderMap`:

```js
async function loadRecommendations() {
  const el   = $("recommendationsContent");
  const root = ($("structureRoot") || {}).value || "~/Projects";
  if (!el) return;
  try {
    const res  = await fetch(`/api/organize/recommendations?root=${encodeURIComponent(root)}`);
    const data = await res.json();
    el.innerHTML = renderRecommendations(data);
  } catch (e) {
    el.innerHTML = `<div class="empty-state"><div class="empty-state-sub">Could not load recommendations.</div></div>`;
  }
}

function renderRecommendations(recs) {
  if (!recs || recs.length === 0) {
    return `<div class="empty-state"><div class="empty-state-sub">No projects to organize.</div></div>`;
  }
  return `
    <table class="organize-table">
      <thead>
        <tr>
          <th>Project</th>
          <th>Current Location</th>
          <th>Suggested Location</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        ${recs.map(r => {
          const already = r.current === r.suggested;
          return `
            <tr class="rec-row ${already ? 'rec-row--done' : ''}" data-project="${esc(r.project_name)}">
              <td class="rec-name">${esc(r.project_name)}</td>
              <td class="rec-current mono">${esc(shortPath(r.current))}</td>
              <td class="rec-dest">
                <input class="rec-dest-input mono" type="text"
                  value="${esc(r.suggested)}"
                  ${already ? 'disabled' : ''}
                  data-original="${esc(r.suggested)}">
              </td>
              <td class="rec-action">
                ${already
                  ? `<span class="rec-done-badge">✓ moved</span>`
                  : `<button class="btn btn-ghost btn-sm move-btn"
                       onclick="moveSingle('${esc(r.project_name.replace(/'/g, "\\'"))}', this)">
                       Move
                     </button>`}
              </td>
            </tr>`;
        }).join("")}
      </tbody>
    </table>`;
}
```

- [ ] **Step 2: Add moveSingle()**

Add inside the `// ── Organize view ──` block:

```js
async function moveSingle(projectName, btn) {
  const row  = btn.closest(".rec-row");
  const dest = row.querySelector(".rec-dest-input").value.trim();
  if (!dest) { toast("Destination cannot be empty", "error"); return; }

  // Check if project is running
  const p = projects.find(x => x.name === projectName);
  if (p && p.status === "running") {
    if (!confirm(
      `"${projectName}" is currently running. Moving it won't affect the running process, ` +
      `but the next start will use the new location.\n\nContinue?`
    )) return;
  }

  btn.disabled = true;
  btn.textContent = "Moving…";

  const result = await _doMigrate(projectName, dest, true);
  if (!result) {
    btn.disabled = false;
    btn.textContent = "Move";
    return;
  }

  toast(`${projectName} moved`, "success");
  await Promise.all([loadFolderMap(), loadRecommendations()]);
}

async function _doMigrate(projectName, destination, force) {
  try {
    const res  = await fetch("/api/organize/migrate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project: projectName, destination, force }),
    });
    const data = await res.json();
    if (!res.ok) { toast(data.error || "Migration failed", "error"); return null; }
    return data;
  } catch (e) {
    toast(`Migration error: ${e.message}`, "error");
    return null;
  }
}
```

- [ ] **Step 3: Verify single Move works end-to-end**

```bash
python seshat.py &
sleep 2
open http://localhost:9000
# Register a test project, click Organize, edit destination, click Move
kill %1
```

- [ ] **Step 4: Commit**

```bash
git add static/app.js
git commit -m "feat: app.js recommendations table and single move"
```

---

## Task 12: app.js — Move All

**Files:**
- Modify: `static/app.js`

- [ ] **Step 1: Add moveAll()**

Add inside the `// ── Organize view ──` block:

```js
async function moveAll() {
  const rows = document.querySelectorAll(".rec-row:not(.rec-row--done)");
  if (rows.length === 0) { toast("Nothing to move", "info"); return; }

  // Collect destinations from the editable inputs
  const moves = [...rows].map(row => ({
    project:     row.dataset.project,
    destination: row.querySelector(".rec-dest-input").value.trim(),
  })).filter(m => m.destination);

  // Identify running projects
  const runningNames = moves
    .filter(m => projects.find(p => p.name === m.project && p.status === "running"))
    .map(m => m.project);

  if (runningNames.length > 0) {
    if (!confirm(
      `${runningNames.length} project${runningNames.length > 1 ? "s are" : " is"} currently running: ` +
      `${runningNames.join(", ")}.\n\n` +
      `Moving them won't affect running processes, but the next start will use the new locations.\n\nContinue?`
    )) return;
  }

  const btn = $("moveAllBtn");
  btn.disabled = true;
  btn.textContent = "Moving…";

  let succeeded = 0;
  for (const { project, destination } of moves) {
    const result = await _doMigrate(project, destination, true);
    if (!result) {
      // _doMigrate already toasted the error; stop on hard failure
      break;
    }
    succeeded++;
  }

  btn.disabled = false;
  btn.textContent = "Move All";

  if (succeeded > 0) {
    toast(`${succeeded} project${succeeded > 1 ? "s" : ""} moved`, "success");
    await Promise.all([loadFolderMap(), loadRecommendations(), loadMoveHistory()]);
  }
}
```

- [ ] **Step 2: Commit**

```bash
git add static/app.js
git commit -m "feat: app.js Move All with running-project warning"
```

---

## Task 13: app.js — Move History + Rollback

**Files:**
- Modify: `static/app.js`

- [ ] **Step 1: Add loadMoveHistory() and renderMoveHistory()**

Add inside the `// ── Organize view ──` block:

```js
async function loadMoveHistory() {
  const el = $("moveHistoryContent");
  if (!el) return;
  try {
    const res  = await fetch("/api/organize/history");
    const data = await res.json();
    el.innerHTML = renderMoveHistory(data);
  } catch (e) {
    el.innerHTML = `<div class="empty-state"><div class="empty-state-sub">Could not load history.</div></div>`;
  }
}

function renderMoveHistory(moves) {
  if (!moves || moves.length === 0) {
    return `<div class="empty-state"><div class="empty-state-sub">No moves recorded yet.</div></div>`;
  }
  return `
    <table class="organize-table">
      <thead>
        <tr>
          <th>Project</th>
          <th>From</th>
          <th>To</th>
          <th>Date</th>
          <th>Git</th>
          <th>Health</th>
          <th>Status</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        ${moves.map(m => {
          const rolledBack = m.rolled_back;
          const date = new Date(m.timestamp).toLocaleDateString("en-US", {
            month: "short", day: "numeric", year: "numeric",
          });
          const safeId = esc(m.id.replace(/'/g, "\\'"));
          return `
            <tr class="history-row ${rolledBack ? 'history-row--rolled-back' : ''}">
              <td>${esc(m.project)}</td>
              <td class="mono history-path">${esc(shortPath(m.from))}</td>
              <td class="mono history-path">${esc(shortPath(m.to))}</td>
              <td class="history-date">${date}</td>
              <td class="history-check">${m.git_verified ? "✓" : "✗"}</td>
              <td class="history-check">${m.health_verified ? "✓" : "✗"}</td>
              <td>${rolledBack
                ? `<span class="history-status rolled-back">rolled back</span>`
                : `<span class="history-status moved">moved</span>`}</td>
              <td>
                ${rolledBack
                  ? ""
                  : `<button class="btn btn-ghost btn-sm rollback-btn"
                       onclick="doRollback('${safeId}')">Roll Back</button>`}
              </td>
            </tr>`;
        }).join("")}
      </tbody>
    </table>`;
}
```

- [ ] **Step 2: Add doRollback()**

Add inside the `// ── Organize view ──` block:

```js
async function doRollback(moveId) {
  if (!confirm("Roll back this move? The folder will be moved to its original location and the registry will be updated.")) return;
  try {
    const res  = await fetch("/api/organize/rollback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ move_id: moveId }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    toast("Rolled back successfully", "success");
    await Promise.all([loadFolderMap(), loadRecommendations(), loadMoveHistory()]);
  } catch (e) {
    toast(`Rollback failed: ${e.message}`, "error");
  }
}
```

- [ ] **Step 3: Commit**

```bash
git add static/app.js
git commit -m "feat: app.js move history and rollback"
```

---

## Task 14: style.css — Organize tab styles

**Files:**
- Modify: `static/style.css`

- [ ] **Step 1: Add organize styles**

Append to the end of `static/style.css`:

```css
/* ── Organize tab ──────────────────────────────────────────────────────── */

.btn-organize {
  margin-right: 4px;
}

.organize-section {
  border: 1px solid var(--border);
  border-radius: 6px;
  margin-bottom: 20px;
  overflow: hidden;
}

.organize-section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  background: var(--bg-secondary, var(--bg));
  border-bottom: 1px solid var(--border);
  flex-wrap: wrap;
  gap: 8px;
}

.organize-section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
}

.organize-root-input {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--text-muted);
}

.organize-root-input input {
  font-family: var(--mono);
  font-size: 12px;
  padding: 3px 6px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--bg);
  color: var(--text);
}

/* Folder map */

.folder-group {
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
}

.folder-group:last-child {
  border-bottom: none;
}

.folder-group-header {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--text-muted);
  margin-bottom: 6px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.folder-group-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
  font-size: 13px;
}

.folder-project-name {
  font-weight: 500;
  min-width: 140px;
}

.folder-project-port {
  color: var(--text-muted);
  font-family: var(--mono);
  font-size: 11px;
  min-width: 50px;
}

.folder-project-dir {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--text-muted);
  flex: 1;
}

/* Recommendations table */

.organize-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.organize-table th {
  text-align: left;
  padding: 8px 12px;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  border-bottom: 1px solid var(--border);
  background: var(--bg-secondary, var(--bg));
}

.organize-table td {
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  vertical-align: middle;
}

.organize-table tr:last-child td {
  border-bottom: none;
}

.rec-row--done {
  opacity: 0.5;
}

.rec-name {
  font-weight: 500;
  white-space: nowrap;
}

.rec-dest-input {
  width: 100%;
  min-width: 220px;
  font-family: var(--mono);
  font-size: 11px;
  padding: 3px 6px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--bg);
  color: var(--text);
}

.rec-dest-input:disabled {
  opacity: 0.5;
  cursor: default;
}

.rec-done-badge {
  font-size: 11px;
  color: var(--green);
}

/* Move history */

.history-row--rolled-back td {
  opacity: 0.5;
}

.history-path {
  font-size: 11px;
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.history-date {
  white-space: nowrap;
  color: var(--text-muted);
  font-size: 11px;
}

.history-check {
  text-align: center;
}

.history-status {
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 10px;
}

.history-status.moved {
  background: color-mix(in srgb, var(--green) 15%, transparent);
  color: var(--green);
}

.history-status.rolled-back {
  background: color-mix(in srgb, var(--text-muted) 15%, transparent);
  color: var(--text-muted);
}
```

- [ ] **Step 2: Verify the Organize tab looks clean**

```bash
python seshat.py &
sleep 2
open http://localhost:9000
# Navigate to Organize tab — check all three sections render correctly
kill %1
```

- [ ] **Step 3: Run all tests**

```bash
pytest tests/ -v
```

Expected: all tests PASS

- [ ] **Step 4: Commit**

```bash
git add static/style.css
git commit -m "feat: style.css organize tab styles"
```

---

## Self-Review Checklist

- [x] **Spec coverage:** folder map ✓ (Tasks 2, 10) | recommendations ✓ (Tasks 3, 11) | migration with Git + health ✓ (Tasks 4–6, 8, 11) | rollback ✓ (Tasks 7, 8, 13) | full history ✓ (Tasks 1, 13) | running-project warning ✓ (Tasks 4, 11–12) | Move All with consolidated warning ✓ (Task 12) | dedicated tab ✓ (Tasks 9–14)
- [x] **Placeholder scan:** no TBDs — all code is complete
- [x] **Type consistency:** `_git_verify` returns `{ok, error?}` — used consistently in `migrate` and `rollback`; `_health_check` returns `{ok, check_type?, error?}` — consistent; `load_history` returns `list[dict]` — consistent across routes and JS
