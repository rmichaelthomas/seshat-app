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
