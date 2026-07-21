"""CLI-level tests for `seshat receipts keys export` and `seshat receipts
keys register` (Prompt B / ID-Q6 harness side): hands this install's
receipt public key to a file for out-of-band handoff, or registers it with
the Prompt A platform endpoint. No existing test exercised either command
before this branch — `receipts keys` previously had only `show`.
"""

import json

import httpx
import pytest
from click.testing import CliRunner
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import ed25519

import cli
import receipts as receipts_module
import vault as vault_mod
from vault import RECEIPTS_API_KEY_VAULT_KEY, Vault

_TEST_FERNET_KEY = Fernet.generate_key()

# Mirrors the fixed keypair conftest.py's autouse _test_receipt_signing_key
# fixture installs, so --rotate's signature can be verified independently.
_FIXED_PRIVATE = ed25519.Ed25519PrivateKey.from_private_bytes(bytes(range(32, 64)))
_FIXED_PUBLIC = _FIXED_PRIVATE.public_key()


@pytest.fixture
def vault_setup(tmp_path, monkeypatch):
    """Isolates cli.vault from the real macOS Keychain/vault.enc, mirroring
    test_receipts_sync_anchor.py's synced_setup fixture. Does NOT pre-set an
    API key — tests opt in via configured_vault when they need one."""
    seshat_dir = tmp_path / ".seshat"
    seshat_dir.mkdir()
    monkeypatch.setattr(vault_mod, "SESHAT_DIR", seshat_dir)
    monkeypatch.setattr(vault_mod, "VAULT_ENC", seshat_dir / "vault.enc")
    monkeypatch.setattr(vault_mod, "VAULT_PLAIN", seshat_dir / "vault.json")
    monkeypatch.setattr(Vault, "_fernet", lambda self: Fernet(_TEST_FERNET_KEY))
    fresh_vault = Vault()
    monkeypatch.setattr(cli, "vault", fresh_vault)
    return fresh_vault


@pytest.fixture
def configured_vault(vault_setup):
    vault_setup.set(RECEIPTS_API_KEY_VAULT_KEY, "receipts_testkey123")
    return vault_setup


def _runner():
    return CliRunner()


class TestReceiptsKeysExport:
    """Mirrors `identity keys export` (cli.py's identity_keys_export)."""

    def test_writes_public_key_to_file(self, tmp_path, vault_setup):
        out = tmp_path / "receipt_pub.txt"
        result = _runner().invoke(cli.cli, ["receipts", "keys", "export", "--out", str(out)])
        assert result.exit_code == 0, result.output
        assert out.read_text() == receipts_module.receipt_public_key_hex() + "\n"

    def test_requires_out_option(self, vault_setup):
        result = _runner().invoke(cli.cli, ["receipts", "keys", "export"])
        assert result.exit_code != 0

    def test_confirms_to_console(self, tmp_path, vault_setup):
        out = tmp_path / "receipt_pub.txt"
        result = _runner().invoke(cli.cli, ["receipts", "keys", "export", "--out", str(out)])
        # Rich soft-wraps long paths in the CliRunner's narrow virtual
        # terminal, so compare with wrap-inserted newlines removed.
        assert str(out) in result.output.replace("\n", "")

    def test_never_writes_or_prints_the_private_key(self, tmp_path, vault_setup):
        out = tmp_path / "receipt_pub.txt"
        private_hex = receipts_module._private_key_hex(receipts_module._receipt_signing_key())
        result = _runner().invoke(cli.cli, ["receipts", "keys", "export", "--out", str(out)])
        assert result.exit_code == 0, result.output
        assert private_hex not in out.read_text()
        assert private_hex not in result.output


class TestReceiptsKeysRegister:
    def test_dry_run_prints_key_and_url_without_sending(self, monkeypatch, vault_setup):
        def fail_if_called(*a, **k):
            raise AssertionError("dry-run must never hit the network")

        monkeypatch.setattr(httpx, "post", fail_if_called)

        result = _runner().invoke(cli.cli, ["receipts", "keys", "register", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert receipts_module.receipt_public_key_hex() in result.output
        assert "/api/v1/receipts/keys" in result.output

    def test_dry_run_works_without_api_key_configured(self, monkeypatch, vault_setup):
        def fail_if_called(*a, **k):
            raise AssertionError("dry-run must never hit the network")

        monkeypatch.setattr(httpx, "post", fail_if_called)

        result = _runner().invoke(cli.cli, ["receipts", "keys", "register", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "No Receipts API key configured" not in result.output

    def test_requires_api_key_when_not_dry_run(self, vault_setup):
        result = _runner().invoke(cli.cli, ["receipts", "keys", "register"])
        assert result.exit_code != 0
        assert "No Receipts API key configured" in result.output
        assert f"seshat vault set {RECEIPTS_API_KEY_VAULT_KEY}" in result.output
        assert "https://liminate.dev/keys" in result.output

    def test_posts_public_key_on_first_registration(self, monkeypatch, configured_vault):
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return httpx.Response(200, json={"status": "registered"}, request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "post", fake_post)

        result = _runner().invoke(cli.cli, ["receipts", "keys", "register"])
        assert result.exit_code == 0, result.output
        assert captured["url"] == "https://liminate.dev/api/v1/receipts/keys"
        assert captured["json"] == {"public_key": receipts_module.receipt_public_key_hex()}
        assert captured["headers"]["Authorization"] == "Bearer receipts_testkey123"

    def test_uses_env_var_api_base_not_a_hardcoded_url(self, monkeypatch, configured_vault):
        monkeypatch.setenv("SESHAT_RECEIPTS_API", "https://custom.example.com")
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            return httpx.Response(200, json={"status": "registered"}, request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "post", fake_post)

        result = _runner().invoke(cli.cli, ["receipts", "keys", "register"])
        assert result.exit_code == 0, result.output
        assert captured["url"] == "https://custom.example.com/api/v1/receipts/keys"

    def test_403_without_rotate_explains_both_causes_and_does_not_retry(self, monkeypatch, configured_vault):
        calls = []

        def fake_post(url, json=None, headers=None, timeout=None):
            calls.append(json)
            return httpx.Response(403, json={"detail": "key mismatch"}, request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "post", fake_post)

        result = _runner().invoke(cli.cli, ["receipts", "keys", "register"])
        assert result.exit_code != 0
        assert len(calls) == 1, "must not auto-retry with a signature"
        assert all("signature" not in (c or {}) for c in calls)
        assert "different machine" in result.output
        assert "Keychain" in result.output

    def test_rotate_includes_a_valid_signature_over_the_public_key(self, monkeypatch, configured_vault):
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["json"] = json
            return httpx.Response(200, json={"status": "registered"}, request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "post", fake_post)

        result = _runner().invoke(cli.cli, ["receipts", "keys", "register", "--rotate"])
        assert result.exit_code == 0, result.output

        pub_hex = receipts_module.receipt_public_key_hex()
        assert captured["json"]["public_key"] == pub_hex
        signature = bytes.fromhex(captured["json"]["signature"])
        _FIXED_PUBLIC.verify(signature, bytes.fromhex(pub_hex))  # raises if invalid

    def test_without_rotate_no_signature_is_sent(self, monkeypatch, configured_vault):
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["json"] = json
            return httpx.Response(200, json={"status": "registered"}, request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "post", fake_post)

        result = _runner().invoke(cli.cli, ["receipts", "keys", "register"])
        assert result.exit_code == 0, result.output
        assert "signature" not in captured["json"]

    def test_network_failure_exits_nonzero_and_writes_nothing_locally(self, monkeypatch, configured_vault, tmp_path):
        def fake_post(*a, **k):
            raise httpx.RequestError("connection refused")

        monkeypatch.setattr(httpx, "post", fake_post)

        before = set(tmp_path.rglob("*"))
        result = _runner().invoke(cli.cli, ["receipts", "keys", "register"])
        after = set(tmp_path.rglob("*"))

        assert result.exit_code != 0
        assert after == before

    def test_non_403_http_error_reports_platform_detail_not_silent_success(self, monkeypatch, configured_vault):
        def fake_post(url, json=None, headers=None, timeout=None):
            return httpx.Response(500, json={"detail": "internal error"}, request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "post", fake_post)

        result = _runner().invoke(cli.cli, ["receipts", "keys", "register"])
        assert result.exit_code != 0
        assert "internal error" in result.output
        assert "Registered" not in result.output

    def test_private_key_never_appears_in_output_or_request_body(self, monkeypatch, configured_vault):
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["json"] = json
            return httpx.Response(200, json={"status": "registered"}, request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx, "post", fake_post)
        private_hex = receipts_module._private_key_hex(receipts_module._receipt_signing_key())

        result = _runner().invoke(cli.cli, ["receipts", "keys", "register", "--rotate"])
        assert result.exit_code == 0, result.output
        assert private_hex not in result.output
        assert private_hex not in json.dumps(captured["json"])
