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


# ── _generate_caddyfile ────────────────────────────────────────────────────

def test_generate_caddyfile_empty_registry(rtr):
    assert rtr._generate_caddyfile() == ""

def test_generate_caddyfile_single_project(rtr):
    rtr.registry.add({"name": "Vault", "directory": "/tmp/v", "port": 5001})
    cf = rtr._generate_caddyfile()
    assert "vault.seshat" in cf
    assert "reverse_proxy localhost:5001" in cf

def test_generate_caddyfile_multi_project(rtr):
    rtr.registry.add({"name": "Vault", "directory": "/tmp/v", "port": 5001})
    rtr.registry.add({"name": "API",   "directory": "/tmp/a", "port": 3000})
    cf = rtr._generate_caddyfile()
    assert "vault.seshat" in cf
    assert "api.seshat" in cf
    assert "reverse_proxy localhost:5001" in cf
    assert "reverse_proxy localhost:3000" in cf

def test_generate_caddyfile_omits_project_without_port(rtr):
    rtr.registry.add({"name": "Docs", "directory": "/tmp/d", "port": None})
    assert rtr._generate_caddyfile() == ""

def test_generate_caddyfile_uses_saved_hostname(rtr, monkeypatch):
    rtr.registry.add({"name": "Vault", "directory": "/tmp/v", "port": 5001})
    monkeypatch.setattr(rtr, "_reload_caddy", lambda: {"ok": True})
    rtr.set_hostname("Vault", "myapp.seshat")
    cf = rtr._generate_caddyfile()
    assert "myapp.seshat" in cf
    assert "vault.seshat" not in cf


# ── _reload_caddy ──────────────────────────────────────────────────────────

def test_reload_caddy_returns_error_if_caddy_not_installed(rtr, monkeypatch, tmp_seshat):
    rtr.registry.add({"name": "Vault", "directory": "/tmp/v", "port": 5001})

    def fake_run(cmd, **_):
        m = MagicMock()
        if cmd[0] == "which":
            m.returncode = 1
        else:
            m.returncode = 0
            m.stdout = ""
            m.stderr = ""
        return m

    monkeypatch.setattr(router_module.subprocess, "run", fake_run)
    result = rtr._reload_caddy()
    assert result["ok"] is False
    assert "caddy not installed" in result["error"]

def test_reload_caddy_reloads_when_caddy_running(rtr, monkeypatch, tmp_seshat):
    rtr.registry.add({"name": "Vault", "directory": "/tmp/v", "port": 5001})
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
    rtr.registry.add({"name": "Vault", "directory": "/tmp/v", "port": 5001})
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
    rtr.registry.add({"name": "Vault", "directory": "/tmp/v", "port": 5001})

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


# ── configure_dnsmasq ──────────────────────────────────────────────────────

def test_configure_dnsmasq_appends_line(rtr, monkeypatch, tmp_path):
    conf = tmp_path / "etc" / "dnsmasq.conf"
    conf.parent.mkdir(parents=True, exist_ok=True)
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
    conf = tmp_path / "etc" / "dnsmasq.conf"
    conf.parent.mkdir(parents=True, exist_ok=True)
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
    conf = tmp_path / "etc" / "dnsmasq.conf"
    conf.parent.mkdir(parents=True, exist_ok=True)
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
