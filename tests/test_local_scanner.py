# tests/test_local_scanner.py
import pytest
from pathlib import Path
from local_scanner import LocalScanner

SIGNAL_FILES = [
    "package.json", "pyproject.toml", "setup.py", "setup.cfg",
    "Cargo.toml", "go.mod", "Gemfile", "requirements.txt", "Makefile",
]


@pytest.fixture
def scanner():
    return LocalScanner()


def test_finds_candidate_with_package_json(scanner, tmp_path):
    proj = tmp_path / "my-app"
    proj.mkdir()
    (proj / "package.json").write_text("{}")
    results = scanner.scan(str(tmp_path), registered_names=set())
    assert len(results) == 1
    assert results[0]["name"] == "my-app"
    assert results[0]["directory"] == str(proj)


def test_skips_directory_without_signal_files(scanner, tmp_path):
    empty = tmp_path / "not-a-project"
    empty.mkdir()
    results = scanner.scan(str(tmp_path), registered_names=set())
    assert results == []


def test_git_dir_counts_as_signal(scanner, tmp_path):
    proj = tmp_path / "bare-git"
    proj.mkdir()
    (proj / ".git").mkdir()
    results = scanner.scan(str(tmp_path), registered_names=set())
    assert len(results) == 1


def test_skips_files_at_root_level(scanner, tmp_path):
    # A file at the root level (not a subdirectory) should be ignored
    (tmp_path / "README.md").write_text("hello")
    results = scanner.scan(str(tmp_path), registered_names=set())
    assert results == []


def test_registered_flag_set_when_name_matches(scanner, tmp_path):
    proj = tmp_path / "vault"
    proj.mkdir()
    (proj / "requirements.txt").write_text("")
    results = scanner.scan(str(tmp_path), registered_names={"vault"})
    assert results[0]["registered"] is True


def test_registered_flag_false_when_no_match(scanner, tmp_path):
    proj = tmp_path / "my-app"
    proj.mkdir()
    (proj / "requirements.txt").write_text("")
    results = scanner.scan(str(tmp_path), registered_names={"other"})
    assert results[0]["registered"] is False


def test_registered_flag_case_insensitive(scanner, tmp_path):
    proj = tmp_path / "MyApp"
    proj.mkdir()
    (proj / "go.mod").write_text("module myapp")
    results = scanner.scan(str(tmp_path), registered_names={"myapp"})
    assert results[0]["registered"] is True


def test_directory_not_found_raises(scanner, tmp_path):
    with pytest.raises(ValueError, match="not found"):
        scanner.scan(str(tmp_path / "does-not-exist"), registered_names=set())


def test_tilde_expansion(scanner, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    proj = tmp_path / "my-app"
    proj.mkdir()
    (proj / "package.json").write_text("{}")
    results = scanner.scan("~", registered_names=set())
    assert any(r["name"] == "my-app" for r in results)


import json


def _make_project(tmp_path, name, files: dict) -> Path:
    """Helper: create a project dir with given file contents."""
    proj = tmp_path / name
    proj.mkdir()
    for fname, content in files.items():
        (proj / fname).write_text(content)
    return proj


def test_extract_port_from_dotenv(scanner, tmp_path):
    _make_project(tmp_path, "app", {".env": "PORT=4321\nDEBUG=true\n"})
    results = scanner.scan(str(tmp_path), registered_names=set())
    assert results[0]["port"] == "4321"


def test_extract_port_from_dotenv_local(scanner, tmp_path):
    _make_project(tmp_path, "app", {".env.local": "PORT=5555\n"})
    results = scanner.scan(str(tmp_path), registered_names=set())
    assert results[0]["port"] == "5555"


def test_extract_port_from_package_json_scripts(scanner, tmp_path):
    pkg = json.dumps({"scripts": {"dev": "next dev -p 3100", "start": "node index.js"}})
    _make_project(tmp_path, "app", {"package.json": pkg})
    results = scanner.scan(str(tmp_path), registered_names=set())
    assert results[0]["port"] == "3100"


def test_extract_start_from_package_json_dev(scanner, tmp_path):
    pkg = json.dumps({"scripts": {"dev": "npm run dev", "start": "node index.js"}})
    _make_project(tmp_path, "app", {"package.json": pkg})
    results = scanner.scan(str(tmp_path), registered_names=set())
    assert results[0]["start"] == "npm run dev"


def test_extract_start_from_package_json_start_fallback(scanner, tmp_path):
    pkg = json.dumps({"scripts": {"start": "node server.js"}})
    _make_project(tmp_path, "app", {"package.json": pkg})
    results = scanner.scan(str(tmp_path), registered_names=set())
    assert results[0]["start"] == "node server.js"


def test_extract_port_from_readme(scanner, tmp_path):
    readme = "# My App\n\nRuns on PORT=7777 by default.\n"
    _make_project(tmp_path, "app", {"requirements.txt": "", "README.md": readme})
    results = scanner.scan(str(tmp_path), registered_names=set())
    assert results[0]["port"] == "7777"


def test_extract_start_from_readme_code_block(scanner, tmp_path):
    readme = "# My App\n\n```\npython3 app.py\n```\n"
    _make_project(tmp_path, "app", {"requirements.txt": "", "README.md": readme})
    results = scanner.scan(str(tmp_path), registered_names=set())
    assert results[0]["start"] == "python3 app.py"


def test_no_extraction_when_no_signals(scanner, tmp_path):
    _make_project(tmp_path, "app", {"go.mod": "module app\n"})
    results = scanner.scan(str(tmp_path), registered_names=set())
    assert results[0]["port"] is None
    assert results[0]["start"] is None


def test_dotenv_takes_priority_over_readme(scanner, tmp_path):
    _make_project(tmp_path, "app", {
        ".env": "PORT=1111\n",
        "README.md": "PORT=9999",
        "requirements.txt": "",
    })
    results = scanner.scan(str(tmp_path), registered_names=set())
    assert results[0]["port"] == "1111"


def test_unreadable_file_skipped(scanner, tmp_path):
    import os
    proj = _make_project(tmp_path, "app", {
        "requirements.txt": "",
        ".env": "PORT=9000\n",
    })
    os.chmod(proj / ".env", 0o000)
    try:
        results = scanner.scan(str(tmp_path), registered_names=set())
        # Should still return the candidate, just with no port
        assert len(results) == 1
        assert results[0]["port"] is None
    finally:
        os.chmod(proj / ".env", 0o644)


def test_extract_start_skips_npm_install_in_readme(scanner, tmp_path):
    readme = "## Setup\n```\nnpm install\n```\n## Run\n```\nnpm run dev\n```"
    _make_project(tmp_path, "app", {"package.json": "{}", "README.md": readme})
    results = scanner.scan(str(tmp_path), registered_names=set())
    # package.json has no scripts, so falls through to README
    assert results[0]["start"] == "npm run dev"


def test_extract_start_all_from_readme(scanner, tmp_path):
    readme = "## Usage\n```\nnpm run server\nnpm run dev\n```"
    _make_project(tmp_path, "app", {"requirements.txt": "", "README.md": readme})
    results = scanner.scan(str(tmp_path), registered_names=set())
    assert results[0]["start_all"] == ["npm run server", "npm run dev"]


def test_extract_start_all_from_package_json(scanner, tmp_path):
    # package.json scripts.dev and scripts.start are alternatives, not companions
    # only the preferred one (dev) should be picked
    pkg = json.dumps({"scripts": {"dev": "next dev", "start": "node index.js"}})
    _make_project(tmp_path, "app", {"package.json": pkg})
    results = scanner.scan(str(tmp_path), registered_names=set())
    assert results[0]["start"] == "next dev"
    assert results[0]["start_all"] == ["next dev"]


def test_extract_start_all_empty_when_none(scanner, tmp_path):
    _make_project(tmp_path, "app", {"go.mod": "module app\n"})
    results = scanner.scan(str(tmp_path), registered_names=set())
    assert results[0]["start_all"] == []


def test_scan_combines_multiple_start_commands(scanner, tmp_path):
    readme = "## Usage\n```\nnpm run server\nnpm run dev\n```"
    _make_project(tmp_path, "app", {"requirements.txt": "", "README.md": readme})
    results = scanner.scan(str(tmp_path), registered_names=set())
    assert results[0]["start"] == "npm run server & npm run dev"
    assert results[0]["start_all"] == ["npm run server", "npm run dev"]
