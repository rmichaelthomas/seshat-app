import pytest
from pathlib import Path
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
