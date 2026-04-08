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
