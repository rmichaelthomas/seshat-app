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


# ── all_hostnames ──────────────────────────────────────────────────────────

def test_all_hostnames_empty_registry(rtr):
    assert rtr.all_hostnames() == []

def test_all_hostnames_auto_generates_slug(rtr):
    rtr.registry.add({"name": "My Vault", "directory": "/tmp/vault", "port": 5001})
    result = rtr.all_hostnames()
    assert len(result) == 1
    assert result[0]["project_name"] == "My Vault"
    assert result[0]["hostname"]     == "my-vault.seshat"
    assert result[0]["port"]         == 5001

def test_all_hostnames_uses_saved_override(rtr):
    rtr.registry.add({"name": "My Vault", "directory": "/tmp/vault", "port": 5001})
    rtr._write_hostnames({"My Vault": "vault.seshat"})
    result = rtr.all_hostnames()
    assert result[0]["hostname"] == "vault.seshat"

def test_all_hostnames_mixed_saved_and_auto(rtr):
    rtr.registry.add({"name": "Vault",  "directory": "/tmp/v", "port": 5001})
    rtr.registry.add({"name": "My API", "directory": "/tmp/a", "port": 3000})
    rtr._write_hostnames({"Vault": "vault.seshat"})
    result = rtr.all_hostnames()
    names = {r["project_name"]: r["hostname"] for r in result}
    assert names["Vault"]  == "vault.seshat"
    assert names["My API"] == "my-api.seshat"

def test_all_hostnames_project_without_port(rtr):
    rtr.registry.add({"name": "Docs", "directory": "/tmp/docs", "port": None})
    result = rtr.all_hostnames()
    assert result[0]["port"] is None
    assert result[0]["hostname"] == "docs.seshat"


# ── set_hostname ───────────────────────────────────────────────────────────

def test_set_hostname_persists_override(rtr, monkeypatch):
    rtr.registry.add({"name": "Vault", "directory": "/tmp/v", "port": 5001})
    monkeypatch.setattr(rtr, "_reload_caddy", lambda: {"ok": True})
    rtr.set_hostname("Vault", "vault.seshat")
    assert rtr._load_hostnames()["Vault"] == "vault.seshat"

def test_set_hostname_calls_reload(rtr, monkeypatch):
    rtr.registry.add({"name": "Vault", "directory": "/tmp/v", "port": 5001})
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
    rtr.registry.add({"name": "Vault", "directory": "/tmp/v", "port": 5001})
    rtr.registry.add({"name": "API",   "directory": "/tmp/a", "port": 3000})
    monkeypatch.setattr(rtr, "_reload_caddy", lambda: {"ok": True})
    rtr.set_hostname("Vault", "vault.seshat")
    with pytest.raises(ValueError, match="hostname_taken"):
        rtr.set_hostname("API", "vault.seshat")

def test_set_hostname_allows_overwriting_own(rtr, monkeypatch):
    rtr.registry.add({"name": "Vault", "directory": "/tmp/v", "port": 5001})
    monkeypatch.setattr(rtr, "_reload_caddy", lambda: {"ok": True})
    rtr.set_hostname("Vault", "vault.seshat")
    rtr.set_hostname("Vault", "vault.seshat")   # must not raise


# ── reset_hostname ─────────────────────────────────────────────────────────

def test_reset_hostname_removes_override(rtr, monkeypatch):
    rtr.registry.add({"name": "Vault", "directory": "/tmp/v", "port": 5001})
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
    rtr.registry.add({"name": "My Vault", "directory": "/tmp/v", "port": 5001})
    monkeypatch.setattr(rtr, "_reload_caddy", lambda: {"ok": True})
    rtr.set_hostname("My Vault", "vault.seshat")
    rtr.reset_hostname("My Vault")
    result = rtr.all_hostnames()
    assert result[0]["hostname"] == "my-vault.seshat"
