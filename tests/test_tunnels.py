# tests/test_tunnels.py
import json
from unittest.mock import MagicMock, patch

import pytest
import yaml

import deps as deps_module
import registry as reg_module
import tunnels as tunnels_module
from registry import Registry
from tunnels import TunnelManager, _endpoint_name


@pytest.fixture
def tmp_seshat(tmp_path, monkeypatch):
    """Redirect all ~/.seshat paths to a temp directory."""
    monkeypatch.setattr(reg_module,     "SESHAT_DIR",    tmp_path)
    monkeypatch.setattr(reg_module,     "REGISTRY_FILE", tmp_path / "registry.yaml")
    monkeypatch.setattr(reg_module,     "STATE_FILE",    tmp_path / "state.json")
    monkeypatch.setattr(reg_module,     "GROUPS_FILE",   tmp_path / "groups.yaml")
    monkeypatch.setattr(tunnels_module, "SESHAT_DIR",    tmp_path)
    monkeypatch.setattr(tunnels_module, "NGROK_CONFIG",  tmp_path / "ngrok.yml")
    monkeypatch.setattr(tunnels_module, "PID_FILE",      tmp_path / "ngrok.pid")
    monkeypatch.setattr(tunnels_module, "LOG_DIR",       tmp_path / "logs")
    return tmp_path


@pytest.fixture
def mgr(tmp_seshat):
    """Fresh TunnelManager backed by a temp registry."""
    return TunnelManager(Registry())


def _add(reg, name, port, tunnel=None):
    project = {"name": name, "port": port, "directory": "/tmp", "start": "true"}
    if tunnel is not None:
        project["tunnel"] = tunnel
    return reg.add(project)


# ── _endpoint_name ──────────────────────────────────────────────────────────

def test_endpoint_name_lowercases():
    assert _endpoint_name("VaultMCP") == "vaultmcp"

def test_endpoint_name_replaces_separators():
    assert _endpoint_name("vault_mcp server") == "vault-mcp-server"

def test_endpoint_name_strips_edges():
    assert _endpoint_name("  vault-mcp  ") == "vault-mcp"


# ── tunneled_projects ───────────────────────────────────────────────────────

def test_no_tunneled_projects_when_none_declared(mgr):
    _add(mgr.registry, "plain", 3000)
    assert mgr.tunneled_projects() == []

def test_tunneled_project_is_discovered(mgr):
    _add(mgr.registry, "vault-mcp", 6150,
         tunnel={"provider": "ngrok", "domain": "abc.ngrok-free.dev"})
    result = mgr.tunneled_projects()
    assert len(result) == 1
    assert result[0]["name"]     == "vault-mcp"
    assert result[0]["port"]     == 6150
    assert result[0]["domain"]   == "abc.ngrok-free.dev"
    assert result[0]["provider"] == "ngrok"

def test_tunnel_provider_defaults_to_ngrok(mgr):
    _add(mgr.registry, "vault-mcp", 6150, tunnel={"domain": "abc.ngrok-free.dev"})
    assert mgr.tunneled_projects()[0]["provider"] == "ngrok"

def test_tunnel_without_domain_is_allowed(mgr):
    _add(mgr.registry, "vault-mcp", 6150, tunnel={"provider": "ngrok"})
    assert mgr.tunneled_projects()[0]["domain"] is None

def test_project_without_port_is_skipped(mgr):
    mgr.registry.add({"name": "portless", "port": None, "directory": "/tmp",
                      "start": "true", "tunnel": {"provider": "ngrok"}})
    assert mgr.tunneled_projects() == []

def test_non_ngrok_provider_is_excluded(mgr):
    _add(mgr.registry, "cf", 8080, tunnel={"provider": "cloudflare"})
    assert mgr.tunneled_projects() == []


# ── config generation ───────────────────────────────────────────────────────

def test_generated_config_is_version_3(mgr):
    _add(mgr.registry, "vault-mcp", 6150, tunnel={"provider": "ngrok"})
    parsed = yaml.safe_load(mgr._generate_config())
    assert parsed["version"] == "3"

def test_generated_config_maps_endpoint_to_upstream_port(mgr):
    _add(mgr.registry, "vault-mcp", 6150, tunnel={"provider": "ngrok"})
    parsed = yaml.safe_load(mgr._generate_config())
    endpoint = parsed["endpoints"][0]
    assert endpoint["name"] == "vault-mcp"
    assert endpoint["upstream"]["url"] == 6150

def test_generated_config_includes_declared_domain_as_https_url(mgr):
    _add(mgr.registry, "vault-mcp", 6150,
         tunnel={"provider": "ngrok", "domain": "abc.ngrok-free.dev"})
    endpoint = yaml.safe_load(mgr._generate_config())["endpoints"][0]
    assert endpoint["url"] == "https://abc.ngrok-free.dev"

def test_declared_domain_keeps_existing_scheme(mgr):
    _add(mgr.registry, "vault-mcp", 6150,
         tunnel={"provider": "ngrok", "domain": "https://abc.ngrok-free.dev"})
    endpoint = yaml.safe_load(mgr._generate_config())["endpoints"][0]
    assert endpoint["url"] == "https://abc.ngrok-free.dev"

def test_endpoint_omits_url_when_no_domain(mgr):
    _add(mgr.registry, "vault-mcp", 6150, tunnel={"provider": "ngrok"})
    endpoint = yaml.safe_load(mgr._generate_config())["endpoints"][0]
    assert "url" not in endpoint

def test_multiple_projects_share_one_config(mgr):
    """The whole point: free-tier ngrok allows one agent, many endpoints."""
    _add(mgr.registry, "vault-mcp", 6150, tunnel={"provider": "ngrok"})
    _add(mgr.registry, "other",     7000, tunnel={"provider": "ngrok"})
    endpoints = yaml.safe_load(mgr._generate_config())["endpoints"]
    assert [e["name"] for e in endpoints] == ["vault-mcp", "other"]

def test_generate_config_empty_when_no_tunnels(mgr):
    _add(mgr.registry, "plain", 3000)
    assert mgr._generate_config() == ""

def test_write_config_creates_file(mgr, tmp_seshat):
    _add(mgr.registry, "vault-mcp", 6150, tunnel={"provider": "ngrok"})
    mgr._write_config()
    assert (tmp_seshat / "ngrok.yml").exists()

def test_write_config_does_not_contain_authtoken(mgr, tmp_seshat):
    """Seshat owns only the endpoints file; credentials stay in ngrok's own config."""
    _add(mgr.registry, "vault-mcp", 6150, tunnel={"provider": "ngrok"})
    mgr._write_config()
    assert "authtoken" not in (tmp_seshat / "ngrok.yml").read_text()


# ── agent lifecycle ─────────────────────────────────────────────────────────

def test_start_is_noop_when_no_tunnels_declared(mgr):
    _add(mgr.registry, "plain", 3000)
    with patch.object(tunnels_module.subprocess, "Popen") as popen:
        result = mgr.start()
    assert result["ok"] is True
    assert result["endpoints"] == 0
    popen.assert_not_called()

def test_start_fails_cleanly_when_ngrok_not_installed(mgr):
    _add(mgr.registry, "vault-mcp", 6150, tunnel={"provider": "ngrok"})
    with patch.object(tunnels_module.shutil, "which", return_value=None), \
         patch.object(TunnelManager, "is_running", return_value=False):
        result = mgr.start()
    assert result["ok"] is False
    assert "not installed" in result["error"]


def test_start_refuses_to_adopt_a_hand_started_agent(mgr):
    """Free tier allows one agent session; a foreign one must be reported, not
    silently treated as ours."""
    _add(mgr.registry, "vault-mcp", 6150, tunnel={"provider": "ngrok"})
    with patch.object(TunnelManager, "is_running", return_value=True), \
         patch.object(tunnels_module.subprocess, "Popen") as popen:
        result = mgr.start()          # no pidfile written -> not Seshat's agent
    popen.assert_not_called()
    assert result["ok"] is False
    assert result["status"] == "foreign_agent"


def test_start_reports_already_running_for_its_own_agent(mgr, tmp_seshat):
    _add(mgr.registry, "vault-mcp", 6150, tunnel={"provider": "ngrok"})
    (tmp_seshat / "ngrok.pid").write_text("4242")
    with patch.object(TunnelManager, "is_running", return_value=True), \
         patch.object(tunnels_module.subprocess, "Popen") as popen:
        result = mgr.start()
    popen.assert_not_called()
    assert result["status"] == "already_running"

def test_start_invokes_ngrok_start_all_with_seshat_config(mgr, tmp_seshat):
    _add(mgr.registry, "vault-mcp", 6150, tunnel={"provider": "ngrok"})
    with patch.object(tunnels_module.shutil, "which", return_value="/usr/bin/ngrok"), \
         patch.object(tunnels_module.subprocess, "Popen") as popen, \
         patch.object(TunnelManager, "is_running", return_value=False):
        popen.return_value = MagicMock(pid=4242)
        result = mgr.start()

    argv = popen.call_args[0][0]
    assert argv[:3] == ["ngrok", "start", "--all"]
    assert str(tmp_seshat / "ngrok.yml") in argv
    assert result["ok"] is True
    assert result["pid"] == 4242

def test_start_writes_pidfile(mgr, tmp_seshat):
    _add(mgr.registry, "vault-mcp", 6150, tunnel={"provider": "ngrok"})
    with patch.object(tunnels_module.shutil, "which", return_value="/usr/bin/ngrok"), \
         patch.object(tunnels_module.subprocess, "Popen") as popen, \
         patch.object(TunnelManager, "is_running", return_value=False):
        popen.return_value = MagicMock(pid=4242)
        mgr.start()
    assert (tmp_seshat / "ngrok.pid").read_text().strip() == "4242"

def test_reload_restarts_a_running_agent(mgr):
    """ngrok has no hot config reload, so a config change means a restart."""
    _add(mgr.registry, "vault-mcp", 6150, tunnel={"provider": "ngrok"})
    with patch.object(TunnelManager, "is_running", side_effect=[True, False]), \
         patch.object(TunnelManager, "stop") as stop, \
         patch.object(TunnelManager, "start", return_value={"ok": True}) as start:
        mgr.reload()
    stop.assert_called_once()
    start.assert_called_once()

def test_reload_starts_agent_when_not_running(mgr):
    _add(mgr.registry, "vault-mcp", 6150, tunnel={"provider": "ngrok"})
    with patch.object(TunnelManager, "is_running", return_value=False), \
         patch.object(TunnelManager, "stop") as stop, \
         patch.object(TunnelManager, "start", return_value={"ok": True}) as start:
        mgr.reload()
    stop.assert_not_called()
    start.assert_called_once()

def test_reload_stops_agent_when_last_tunnel_removed(mgr):
    """No endpoints left to serve — don't leave a stray agent holding the session."""
    _add(mgr.registry, "plain", 3000)
    with patch.object(TunnelManager, "is_running", return_value=True), \
         patch.object(TunnelManager, "stop", return_value={"ok": True}) as stop, \
         patch.object(TunnelManager, "start") as start:
        result = mgr.reload()
    stop.assert_called_once()
    start.assert_not_called()
    assert result["ok"] is True

def test_ensure_leaves_a_correct_running_agent_alone(mgr, tmp_seshat):
    """Starting one project must not drop another project's live tunnel."""
    _add(mgr.registry, "vault-mcp", 6150, tunnel={"provider": "ngrok"})
    mgr._write_config()
    (tmp_seshat / "ngrok.pid").write_text("4242")
    with patch.object(TunnelManager, "is_running", return_value=True), \
         patch.object(TunnelManager, "reload") as reload_:
        result = mgr.ensure()
    reload_.assert_not_called()
    assert result["status"] == "already_running"


def test_ensure_reloads_when_registry_gained_a_tunnel(mgr, tmp_seshat):
    _add(mgr.registry, "vault-mcp", 6150, tunnel={"provider": "ngrok"})
    mgr._write_config()
    (tmp_seshat / "ngrok.pid").write_text("4242")
    _add(mgr.registry, "new-one", 7000, tunnel={"provider": "ngrok"})
    with patch.object(TunnelManager, "is_running", return_value=True), \
         patch.object(TunnelManager, "reload", return_value={"ok": True}) as reload_:
        mgr.ensure()
    reload_.assert_called_once()


def test_ensure_starts_agent_when_down(mgr, tmp_seshat):
    _add(mgr.registry, "vault-mcp", 6150, tunnel={"provider": "ngrok"})
    mgr._write_config()
    with patch.object(TunnelManager, "is_running", return_value=False), \
         patch.object(TunnelManager, "reload", return_value={"ok": True}) as reload_:
        mgr.ensure()
    reload_.assert_called_once()


def test_ensure_is_noop_without_tunnels(mgr):
    _add(mgr.registry, "plain", 3000)
    with patch.object(TunnelManager, "reload") as reload_:
        result = mgr.ensure()
    reload_.assert_not_called()
    assert result["status"] == "no_tunnels"


def test_stop_removes_pidfile(mgr, tmp_seshat):
    (tmp_seshat / "ngrok.pid").write_text("4242")
    with patch.object(tunnels_module.os, "kill") as kill:
        mgr.stop()
    kill.assert_called_once()
    assert not (tmp_seshat / "ngrok.pid").exists()

def test_stop_is_safe_when_no_pidfile(mgr):
    result = mgr.stop()
    assert result["ok"] is True

def test_stop_clears_pidfile_for_dead_process(mgr, tmp_seshat):
    (tmp_seshat / "ngrok.pid").write_text("4242")
    with patch.object(tunnels_module.os, "kill", side_effect=ProcessLookupError):
        result = mgr.stop()
    assert result["ok"] is True
    assert not (tmp_seshat / "ngrok.pid").exists()


# ── status ──────────────────────────────────────────────────────────────────

_API_PAYLOAD = {
    "tunnels": [
        {"name": "vault-mcp", "proto": "https",
         "public_url": "https://abc.ngrok-free.dev",
         "config": {"addr": "http://localhost:6150"}},
        {"name": "other", "proto": "https",
         "public_url": "https://def.ngrok-free.dev",
         "config": {"addr": "http://localhost:7000"}},
    ]
}


def _fake_api(payload):
    """Patch the ngrok local API to return payload."""
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__  = lambda s, *a: False
    return resp


def test_status_maps_public_url_to_each_project(mgr):
    _add(mgr.registry, "vault-mcp", 6150, tunnel={"provider": "ngrok"})
    _add(mgr.registry, "other",     7000, tunnel={"provider": "ngrok"})
    with patch.object(tunnels_module, "urlopen", return_value=_fake_api(_API_PAYLOAD)):
        result = mgr.status()
    by_name = {e["name"]: e for e in result["endpoints"]}
    assert by_name["vault-mcp"]["public_url"] == "https://abc.ngrok-free.dev"
    assert by_name["other"]["public_url"]     == "https://def.ngrok-free.dev"
    assert result["running"] is True

def test_status_reports_declared_but_unserved_endpoint(mgr):
    """Declared in the registry, absent from the live agent — the honest-status case."""
    _add(mgr.registry, "vault-mcp", 6150, tunnel={"provider": "ngrok"})
    _add(mgr.registry, "missing",   9999, tunnel={"provider": "ngrok"})
    with patch.object(tunnels_module, "urlopen", return_value=_fake_api(_API_PAYLOAD)):
        result = mgr.status()
    by_name = {e["name"]: e for e in result["endpoints"]}
    assert by_name["missing"]["status"] == "disconnected"
    assert by_name["vault-mcp"]["status"] == "connected"

def test_status_when_agent_not_running(mgr):
    _add(mgr.registry, "vault-mcp", 6150, tunnel={"provider": "ngrok"})
    with patch.object(tunnels_module, "urlopen", side_effect=ConnectionRefusedError):
        result = mgr.status()
    assert result["running"] is False
    assert result["endpoints"][0]["status"] == "disconnected"


# ── deps.py: per-project tunnel matching ────────────────────────────────────

def test_ngrok_dep_check_matches_the_projects_own_port():
    with patch.object(deps_module, "urlopen", return_value=_fake_api(_API_PAYLOAD)):
        result = deps_module._check_one(
            {"type": "tunnel", "provider": "ngrok", "port": 7000}
        )
    assert result["status"] == "connected"
    assert result["public_url"] == "https://def.ngrok-free.dev"

def test_ngrok_dep_check_reports_disconnected_for_unserved_port():
    """Previously any live tunnel made every project look connected."""
    with patch.object(deps_module, "urlopen", return_value=_fake_api(_API_PAYLOAD)):
        result = deps_module._check_one(
            {"type": "tunnel", "provider": "ngrok", "port": 9999}
        )
    assert result["status"] == "disconnected"

def test_ngrok_dep_check_without_port_falls_back_to_first_tunnel():
    with patch.object(deps_module, "urlopen", return_value=_fake_api(_API_PAYLOAD)):
        result = deps_module._check_one({"type": "tunnel", "provider": "ngrok"})
    assert result["status"] == "connected"

def test_ngrok_dep_check_when_agent_down():
    with patch.object(deps_module, "urlopen", side_effect=ConnectionRefusedError):
        result = deps_module._check_one(
            {"type": "tunnel", "provider": "ngrok", "port": 6150}
        )
    assert result["status"] == "disconnected"
    assert "not running" in result["detail"]
