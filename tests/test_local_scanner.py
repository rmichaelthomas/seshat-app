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
