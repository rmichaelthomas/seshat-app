import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
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

    # Verify sorted order
    assert result == sorted(result, key=lambda g: g["parent"])


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

    proj_entry = result[0]["projects"][0]
    assert "port" in proj_entry
    assert "tags" in proj_entry
    assert "directory" in proj_entry


def test_folder_map_expands_home_directory(org, tmp_seshat, monkeypatch):
    # Register a project using ~ in the directory path
    proj = tmp_seshat / "work" / "myapp"
    proj.mkdir(parents=True)
    # Monkeypatch Path.home() and HOME env var so "~" resolves to tmp_seshat
    monkeypatch.setattr(Path, "home", lambda: tmp_seshat)
    monkeypatch.setenv("HOME", str(tmp_seshat))
    org.registry.add({"name": "HomeApp", "port": 6000, "directory": "~/work/myapp",
                       "start": "python app.py", "tags": [], "url": "", "stop": "",
                       "notes": "", "dependencies": [], "env": []})
    result = org.folder_map()
    assert len(result) == 1
    # directory in result should be fully expanded (no ~)
    assert "~" not in result[0]["projects"][0]["directory"]
    assert result[0]["projects"][0]["directory"].startswith(str(tmp_seshat))


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
    def fake_run(cmd, **kwargs):
        m = MagicMock()
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
    assert "git status failed" in result["error"]


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
