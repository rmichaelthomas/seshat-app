"""Tests for vault.py, focused on the Receipts API key case-sensitivity
regression: `seshat vault set` always uppercases the key it's given, so
every reader must ask for the same uppercase form — previously several
call sites asked for the lowercase form and never matched what `vault set`
(and the tool's own documented instructions) actually produce.
"""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

import vault as vault_mod
from vault import RECEIPTS_API_KEY_VAULT_KEY, Vault

# Fixed in-memory key so tests exercise the real encrypted _save/_load path
# (F-06: vault.py no longer supports an unencrypted fallback) without ever
# touching the real macOS Keychain.
_TEST_FERNET_KEY = Fernet.generate_key()


def _isolate_vault(monkeypatch, tmp_path):
    """Redirect vault storage to a temp dir and use a fixed in-memory
    Fernet key instead of the real Keychain-backed one, mirroring the
    pattern established for testing this module without touching macOS
    Keychain state."""
    seshat_dir = tmp_path / ".seshat"
    seshat_dir.mkdir()
    monkeypatch.setattr(vault_mod, "SESHAT_DIR", seshat_dir)
    monkeypatch.setattr(vault_mod, "VAULT_ENC", seshat_dir / "vault.enc")
    monkeypatch.setattr(vault_mod, "VAULT_PLAIN", seshat_dir / "vault.json")
    monkeypatch.setattr(Vault, "_fernet", lambda self: Fernet(_TEST_FERNET_KEY))


def test_receipts_api_key_constant_is_uppercase():
    assert RECEIPTS_API_KEY_VAULT_KEY == RECEIPTS_API_KEY_VAULT_KEY.upper()


class TestVaultEncryptionNonOptional:
    """F-06: storing a secret without crypto available must hard-fail,
    never silently write plaintext."""

    def test_set_raises_when_crypto_unavailable(self, monkeypatch, tmp_path):
        seshat_dir = tmp_path / ".seshat"
        seshat_dir.mkdir()
        monkeypatch.setattr(vault_mod, "SESHAT_DIR", seshat_dir)
        monkeypatch.setattr(vault_mod, "VAULT_ENC", seshat_dir / "vault.enc")
        monkeypatch.setattr(vault_mod, "VAULT_PLAIN", seshat_dir / "vault.json")
        monkeypatch.setattr(vault_mod, "_CRYPTO_OK", False)

        v = Vault()
        with pytest.raises(vault_mod.VaultEncryptionUnavailableError):
            v.set("SOME_KEY", "some-secret-value")

        assert not (seshat_dir / "vault.json").exists()
        assert not (seshat_dir / "vault.enc").exists()

    def test_set_encrypts_when_crypto_available(self, monkeypatch, tmp_path):
        _isolate_vault(monkeypatch, tmp_path)
        v = Vault()
        v.set("SOME_KEY", "some-secret-value")

        assert (tmp_path / ".seshat" / "vault.enc").exists()
        assert not (tmp_path / ".seshat" / "vault.json").exists()
        # The value is genuinely encrypted at rest, not just base64/obscured.
        raw = (tmp_path / ".seshat" / "vault.enc").read_bytes()
        assert b"some-secret-value" not in raw


def test_documented_vault_set_command_round_trips_through_the_constant(monkeypatch, tmp_path):
    """Regression: exactly what the CLI's own help text and every error
    message tell a user to run (`seshat vault set __RECEIPTS_API_KEY__
    <key>`), followed by every real .get() call site's lookup, must work."""
    _isolate_vault(monkeypatch, tmp_path)
    v = Vault()

    # This mirrors cli.py's vault_set command: `vault.set(key.strip().upper(), value)`.
    v.set(RECEIPTS_API_KEY_VAULT_KEY.strip().upper(), "receipts_testkey123")

    assert v.get(RECEIPTS_API_KEY_VAULT_KEY) == "receipts_testkey123"


def test_lowercase_get_no_longer_used_would_have_missed_it(monkeypatch, tmp_path):
    """Documents the bug this fix closes: a get() for the lowercase form
    (the old, wrong call-site literal) never matches what vault_set stores."""
    _isolate_vault(monkeypatch, tmp_path)
    v = Vault()
    v.set(RECEIPTS_API_KEY_VAULT_KEY.strip().upper(), "receipts_testkey123")

    assert v.get("__receipts_api_key__") is None
    assert v.get(RECEIPTS_API_KEY_VAULT_KEY) == "receipts_testkey123"


def test_cli_vault_set_command_then_receipts_sync_finds_the_key(monkeypatch, tmp_path):
    """End-to-end through the real CLI surface: `seshat vault set
    __RECEIPTS_API_KEY__ <key>` (exactly as documented) followed by a real
    receipts_sync invocation must see a configured key, not the
    'No Receipts API key configured' error."""
    from click.testing import CliRunner

    import cli as cli_mod
    import receipts as receipts_mod

    _isolate_vault(monkeypatch, tmp_path)
    # cli.py's module-level `vault = Vault()` was already constructed before
    # this test's monkeypatching, so redirect its `vault` binding directly.
    monkeypatch.setattr(cli_mod, "vault", Vault())

    # Isolate the receipts directory too — RECEIPTS_DIR is read dynamically
    # (patchable), but LAST_SYNCED_PATH was computed once at cli.py import
    # time and needs its own patch.
    receipts_dir = tmp_path / ".seshat" / "receipts"
    receipts_dir.mkdir(parents=True)
    monkeypatch.setattr(receipts_mod, "RECEIPTS_DIR", receipts_dir)
    monkeypatch.setattr(cli_mod, "LAST_SYNCED_PATH", receipts_dir / ".last_synced")

    runner = CliRunner()
    result = runner.invoke(cli_mod.cli, ["vault", "set", RECEIPTS_API_KEY_VAULT_KEY, "receipts_testkey123"])
    assert result.exit_code == 0, result.output

    # No unsent receipts -> receipts_sync short-circuits before the network
    # call, but only AFTER the "no key configured" guard — proving the key
    # was found.
    result = runner.invoke(cli_mod.cli, ["receipts", "sync"])
    assert "No Receipts API key configured" not in result.output
    assert "synced" in result.output.lower()
