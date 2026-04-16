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
    def _make_repo(name):
        return {"name": name, "full_name": f"u/{name}", "clone_url": f"https://github.com/u/{name}.git",
                "description": "", "language": "Python", "topics": [],
                "fork": False, "pushed_at": "2026-01-01T00:00:00Z", "default_branch": "main"}

    page1 = [_make_repo(f"repo-{i}") for i in range(100)]
    page2 = [_make_repo("repo-100")]
    responses = iter([_mock_response(page1), _mock_response(page2)])
    with patch("urllib.request.urlopen", side_effect=lambda *a, **k: next(responses)):
        repos = importer.fetch_repos()
    assert len(repos) == 101
    assert repos[0]["name"] == "repo-0"
    assert repos[100]["name"] == "repo-100"


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


def test_detect_local_path_ssh_clone(importer, tmp_path):
    # Repo was cloned via SSH but GitHub API returns HTTPS clone_url
    repo_dir = tmp_path / "my-repo"
    repo_dir.mkdir()
    git_dir = repo_dir / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text(
        "[remote \"origin\"]\n\turl = git@github.com:u/my-repo.git\n"
    )
    with patch("github._LOCAL_SEARCH_ROOTS", [tmp_path]):
        result = importer.detect_local_path("my-repo", "https://github.com/u/my-repo.git")
    assert result == str(repo_dir)


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
    import urllib.error
    with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(
        None, 404, "Not Found", {}, None
    )):
        result = importer.fetch_readme("owner/repo")
    assert result is None


def test_extract_port_contextual_keyword(importer):
    readme = "The server listens on listen: 8000"
    result = importer._extract_fields(readme)
    assert result["port"] == "8000"


def test_extract_port_no_false_positive(importer):
    # Generic colon-number should NOT match
    readme = "Set timeout: 30000 for the connection."
    result = importer._extract_fields(readme)
    assert result["port"] is None


def test_extract_start_command_make_run(importer):
    readme = "## Running\n```\nmake run\n```"
    result = importer._extract_fields(readme)
    assert result["start"] == "make run"


def test_extract_start_command_make_install_not_matched(importer):
    readme = "## Install\n```\nmake install\n```"
    result = importer._extract_fields(readme)
    assert result["start"] is None


def test_extract_notes_skips_badges(importer):
    readme = "# My App\n\n![CI](https://img.shields.io/badge/CI-passing)\n\nThis is the real description."
    result = importer._extract_fields(readme)
    assert result["notes"] == "This is the real description."


def test_extract_fields_empty_readme(importer):
    result = importer._extract_fields("")
    assert result["port"] is None
    assert result["start"] is None
    assert result["notes"] is None


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
    assert r["tags"] == ["obsidian", "python"]
    assert r["registered"] is False
    assert r["is_fork"] is False


def test_scan_registered_by_name(importer):
    repos = [{
        "name": "vault", "full_name": "u/vault",
        "clone_url": "https://github.com/u/vault.git",
        "description": "", "language": "Python",
        "topics": [], "fork": False,
        "pushed_at": "2026-01-01T00:00:00Z", "default_branch": "main",
    }]
    with patch.object(importer, "fetch_repos", return_value=repos), \
         patch.object(importer, "fetch_readme", return_value=None), \
         patch.object(importer, "detect_local_path", return_value=None):
        results = importer.scan(registered_names={"VAULT"})  # case-insensitive
    assert results[0]["registered"] is True


def test_scan_registered_by_local_path(importer):
    repos = [{
        "name": "vault", "full_name": "u/vault",
        "clone_url": "https://github.com/u/vault.git",
        "description": "", "language": "Python",
        "topics": [], "fork": False,
        "pushed_at": "2026-01-01T00:00:00Z", "default_branch": "main",
    }]
    with patch.object(importer, "fetch_repos", return_value=repos), \
         patch.object(importer, "fetch_readme", return_value=None), \
         patch.object(importer, "detect_local_path", return_value="/Users/u/vault"):
        results = importer.scan(registered_names={"/users/u/vault"})  # case-insensitive
    assert results[0]["registered"] is True


def test_extract_start_skips_npm_install(importer):
    readme = "## Setup\n```\nnpm install\n```\n## Run\n```\nnpm run dev\n```"
    result = importer._extract_fields(readme)
    assert result["start"] == "npm run dev"


def test_extract_start_skips_npm_ci(importer):
    readme = "## Setup\n```\nnpm ci\n```"
    result = importer._extract_fields(readme)
    assert result["start"] is None


def test_extract_start_skips_yarn_install(importer):
    readme = "## Setup\n```\nyarn install\n```\n## Run\n```\nyarn dev\n```"
    result = importer._extract_fields(readme)
    assert result["start"] == "yarn dev"


def test_extract_start_skips_yarn_add(importer):
    readme = "```\nyarn add express\n```"
    result = importer._extract_fields(readme)
    assert result["start"] is None
