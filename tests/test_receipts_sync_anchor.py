"""`seshat receipts sync` pushes the local chain anchor (head hash + count)
alongside the existing receipts payload (F-01 B2) so the platform can, in
future, pin it per install. Additive to the existing /api/v1/ingest
payload — never a new required field the backend must already understand."""

import httpx
import pytest
from click.testing import CliRunner
from cryptography.fernet import Fernet

import cli
import receipts as receipts_module
import vault as vault_mod
from vault import RECEIPTS_API_KEY_VAULT_KEY, Vault

_TEST_FERNET_KEY = Fernet.generate_key()


@pytest.fixture
def synced_setup(tmp_path, monkeypatch):
    receipts_dir = tmp_path / "receipts"
    receipts_dir.mkdir()
    monkeypatch.setattr(receipts_module, "RECEIPTS_DIR", receipts_dir)
    monkeypatch.setattr(receipts_module, "LOCK_PATH", receipts_dir / ".chain.lock")
    monkeypatch.setattr(receipts_module, "CHAIN_HEAD_PATH", receipts_dir / ".chain_head")
    monkeypatch.setattr(receipts_module, "snapshot", lambda: {
        "listening_ports": [], "managed_projects": {},
    })
    monkeypatch.setattr(cli, "LAST_SYNCED_PATH", receipts_dir / ".last_synced")

    seshat_dir = tmp_path / ".seshat"
    seshat_dir.mkdir()
    monkeypatch.setattr(vault_mod, "SESHAT_DIR", seshat_dir)
    monkeypatch.setattr(vault_mod, "VAULT_ENC", seshat_dir / "vault.enc")
    monkeypatch.setattr(vault_mod, "VAULT_PLAIN", seshat_dir / "vault.json")
    monkeypatch.setattr(Vault, "_fernet", lambda self: Fernet(_TEST_FERNET_KEY))
    fresh_vault = Vault()
    fresh_vault.set(RECEIPTS_API_KEY_VAULT_KEY, "receipts_testkey123")
    monkeypatch.setattr(cli, "vault", fresh_vault)

    return receipts_dir


def _emit():
    return receipts_module.emit(
        action="start_project", target={"project": "p"}, result={"status": "success"},
        env_before={"listening_ports": [], "managed_projects": {}},
        session_id="s", actor_type="test", agent_hint="test",
    )


def test_sync_includes_chain_anchor_in_payload(synced_setup, monkeypatch):
    _emit()

    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["payload"] = json
        return httpx.Response(200, json={"ingested": 1}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)

    result = CliRunner().invoke(cli.cli, ["receipts", "sync"])
    assert result.exit_code == 0, result.output

    anchor = receipts_module._read_chain_head()
    assert anchor is not None
    assert captured["payload"]["chain_anchor"] == anchor


def test_sync_dry_run_never_calls_network(synced_setup, monkeypatch):
    _emit()

    def fail_if_called(*a, **k):
        raise AssertionError("dry-run must never hit the network")

    monkeypatch.setattr(httpx, "post", fail_if_called)

    result = CliRunner().invoke(cli.cli, ["receipts", "sync", "--dry-run"])
    assert result.exit_code == 0, result.output
