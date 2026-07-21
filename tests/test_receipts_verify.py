"""CLI-level tests for `seshat receipts verify` (F-01): keyed-hash
verification, legacy-chain handling, and anchor-based truncation
detection. No existing test exercised this command directly before —
its logic lived inline in cli.py with zero coverage."""

import hashlib
import hmac
import json

import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives.asymmetric import ed25519

import cli
import receipts as receipts_module


@pytest.fixture
def isolated_receipts(tmp_path, monkeypatch):
    receipts_dir = tmp_path / "receipts"
    receipts_dir.mkdir()
    monkeypatch.setattr(receipts_module, "RECEIPTS_DIR", receipts_dir)
    monkeypatch.setattr(receipts_module, "LOCK_PATH", receipts_dir / ".chain.lock")
    monkeypatch.setattr(receipts_module, "CHAIN_HEAD_PATH", receipts_dir / ".chain_head")
    monkeypatch.setattr(receipts_module, "snapshot", lambda: {
        "listening_ports": [], "managed_projects": {},
    })
    return receipts_dir


def _runner():
    return CliRunner()


def _emit(action="start_project"):
    return receipts_module.emit(
        action=action, target={"project": "p"}, result={"status": "success"},
        env_before={"listening_ports": [], "managed_projects": {}},
        session_id="s", actor_type="test", agent_hint="test",
    )


def _make_legacy_receipt(receipts_dir, filename, action, previous_hash=None):
    """Construct a pre-keying (legacy, unversioned, unkeyed) receipt file
    directly on disk — simulates a chain that predates F-01's keying."""
    receipt = {
        "type": "machine_action",
        "timestamp": "2020-01-01T00:00:00.000000+00:00",
        "actor": {"type": "test", "session_id": "legacy", "agent_hint": "legacy"},
        "action": action,
        "target": {"project": "p"},
        "result": {"status": "success"},
        "environment_before": {"listening_ports": [], "managed_projects": {}},
        "environment_after": {"listening_ports": [], "managed_projects": {}},
        "previous_hash": previous_hash,
    }
    canonical = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    receipt_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    receipt["receipt_hash"] = receipt_hash
    (receipts_dir / filename).write_text(json.dumps(receipt, indent=2))
    return receipt_hash


# The fixed HMAC key conftest.py's autouse _test_mac_key fixture installs —
# used here to hand-construct version-2 receipts directly on disk (ID-Q4
# Phase 2 migration tests), since receipts_module.emit() only ever produces
# version-3 receipts post-Phase-2.
_TEST_MAC_KEY = b"test-only-mac-key-not-for-real-use"


def _make_v2_receipt(receipts_dir, filename, action, previous_hash=None):
    """Construct a version-2 (HMAC-keyed) receipt file directly on disk —
    simulates a chain written before the Ed25519 upgrade (ID-Q4 Phase 2)."""
    receipt = {
        "type": "machine_action",
        "timestamp": "2021-01-01T00:00:00.000000+00:00",
        "actor": {"type": "test", "session_id": "v2", "agent_hint": "v2"},
        "action": action,
        "target": {"project": "p"},
        "result": {"status": "success"},
        "environment_before": {"listening_ports": [], "managed_projects": {}},
        "environment_after": {"listening_ports": [], "managed_projects": {}},
        "previous_hash": previous_hash,
        "receipt_version": 2,
    }
    canonical = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    receipt_hash = hmac.new(_TEST_MAC_KEY, canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    receipt["receipt_hash"] = receipt_hash
    (receipts_dir / filename).write_text(json.dumps(receipt, indent=2))
    return receipt_hash


class TestVerifyKeyedChain:
    def test_intact_keyed_chain_reports_intact(self, isolated_receipts):
        _emit("start_project")
        _emit("stop_project")
        result = _runner().invoke(cli.cli, ["receipts", "verify"])
        assert result.exit_code == 0
        assert "Chain intact" in result.output

    def test_tampered_field_is_detected(self, isolated_receipts):
        _emit("start_project")
        files = sorted(isolated_receipts.glob("*.json"))
        receipt = json.loads(files[0].read_text())
        receipt["action"] = "TAMPERED"
        files[0].write_text(json.dumps(receipt, indent=2))

        result = _runner().invoke(cli.cli, ["receipts", "verify"])
        # Rich wraps console output, so assert on a phrase that doesn't
        # straddle a likely wrap point rather than the full sentence.
        assert "signature verification" in result.output
        assert "receipt was modified" in result.output
        assert "Chain intact" not in result.output


class TestVerifyAnchorTruncation:
    def test_truncation_is_detected_distinctly_from_chain_intact(self, isolated_receipts):
        _emit("start_project")
        _emit("stop_project")
        _emit("start_group")

        files = sorted(isolated_receipts.glob("*.json"))
        assert len(files) == 3
        files[-1].unlink()  # delete the newest receipt — simulated truncation

        result = _runner().invoke(cli.cli, ["receipts", "verify"])
        assert "Chain intact" not in result.output
        assert "runcation" in result.output

    def test_no_truncation_when_chain_matches_anchor(self, isolated_receipts):
        _emit("start_project")
        _emit("stop_project")
        result = _runner().invoke(cli.cli, ["receipts", "verify"])
        assert "Chain intact" in result.output
        assert "runcation" not in result.output


class TestVerifyLegacyChain:
    def test_legacy_only_chain_verifies_with_a_warning(self, isolated_receipts):
        _make_legacy_receipt(
            isolated_receipts, "10000000T000000_legacy_00000000.json",
            "start_project", previous_hash=None,
        )
        result = _runner().invoke(cli.cli, ["receipts", "verify"])
        assert result.exit_code == 0
        assert "legacy" in result.output.lower()
        assert "hash mismatch" not in result.output

    def test_unversioned_receipt_after_keyed_graduation_is_rejected(self, isolated_receipts):
        h0 = _make_legacy_receipt(
            isolated_receipts, "10000000T000000_legacy_00000000.json",
            "start_project", previous_hash=None,
        )
        # A real keyed receipt (version 3, post-Phase-2) chains from the
        # legacy one (bootstraps the anchor).
        r1 = _emit("stop_project")
        assert r1["previous_hash"] == h0

        # Inject a rogue, unversioned ("legacy-looking") receipt AFTER the
        # chain graduated to keyed — filename sorts last, so it appears at
        # the tail. This can never be legitimate: the chain only ever
        # moves forward from unkeyed to keyed, never back.
        rogue = {
            "type": "machine_action",
            "timestamp": "2020-01-01T00:00:00.000000+00:00",
            "actor": {"type": "test", "session_id": "rogue", "agent_hint": "rogue"},
            "action": "forged_action",
            "target": {"project": "p"},
            "result": {"status": "success"},
            "environment_before": {}, "environment_after": {},
            "previous_hash": r1["receipt_hash"],
        }
        canonical = json.dumps(rogue, sort_keys=True, separators=(",", ":"))
        rogue["receipt_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        (isolated_receipts / "99999999T999999_rogue_ffffffff.json").write_text(
            json.dumps(rogue, indent=2)
        )

        result = _runner().invoke(cli.cli, ["receipts", "verify"])
        assert "Chain intact" not in result.output
        assert "forgery" in result.output.lower()


class TestVerifyMixedVersionChain:
    """ID-Q4 Phase 2 §A8 migration: a chain legitimately contains a
    version-2 (HMAC) prefix followed by a version-3 (Ed25519) suffix —
    both must verify in a single `receipts verify` run. This is the
    primary migration test; no re-signing of history, no flag day."""

    def test_v2_prefix_then_v3_suffix_verifies_in_one_run(self, isolated_receipts):
        h0 = _make_v2_receipt(
            isolated_receipts, "10000000T000000_v2_00000000.json",
            "start_project", previous_hash=None,
        )
        h1 = _make_v2_receipt(
            isolated_receipts, "10000001T000000_v2_00000001.json",
            "stop_project", previous_hash=h0,
        )

        # emit() only ever produces version-3 receipts post-Phase-2 — this
        # is the real upgrade path, chaining forward from the v2 tail.
        # The chain head anchor must be primed to match the hand-written
        # v2 tail so emit()'s own _recover_chain_state() links from it.
        receipts_module._write_chain_head(h1, 2)
        r2 = _emit("start_group")
        assert r2["previous_hash"] == h1
        assert r2["receipt_version"] == 3

        result = _runner().invoke(cli.cli, ["receipts", "verify"])
        assert result.exit_code == 0
        assert "Chain intact" in result.output
        assert "3 receipt(s) verified" in result.output


class TestVerifyDowngrade:
    """§A7/§A11 failure mode #4: the chain only ever moves forward — a
    version-2 receipt appearing after a version-3 one is a downgrade, not
    a fork, and must hard-fail exactly like unversioned-after-keyed does."""

    def test_v2_after_v3_is_rejected(self, isolated_receipts):
        r0 = _emit("start_project")
        assert r0["receipt_version"] == 3

        # Inject a rogue version-2 receipt AFTER the chain graduated to
        # version-3 — filename sorts last, so it appears at the tail.
        _make_v2_receipt(
            isolated_receipts, "99999999T999999_rogue_ffffffff.json",
            "forged_action", previous_hash=r0["receipt_hash"],
        )

        result = _runner().invoke(cli.cli, ["receipts", "verify"])
        assert "Chain intact" not in result.output
        assert "downgrade" in result.output.lower()


class TestVerifyPubkeyAuditor:
    """§A7: `seshat receipts verify --pubkey <hex>` is the auditor's
    command — it must work on a machine that has never held the private
    key (§10 benchmark 6/1 — the cross-org, public-key-only property)."""

    def test_pubkey_verifies_a_v3_chain_without_the_private_key(self, isolated_receipts, monkeypatch):
        _emit("start_project")
        _emit("stop_project")
        pubkey_hex = receipts_module.receipt_public_key_hex()

        # Prove --pubkey mode never reaches for the private key: break it
        # outright and confirm verification still succeeds.
        def _boom():
            raise RuntimeError("private key must never be read on the auditor's machine")
        monkeypatch.setattr(receipts_module, "_receipt_signing_key", _boom)

        result = _runner().invoke(cli.cli, ["receipts", "verify", "--pubkey", pubkey_hex])
        assert result.exit_code == 0
        assert "Chain intact" in result.output

    def test_pubkey_reports_v2_and_legacy_receipts_as_unverifiable_not_failures(
        self, isolated_receipts
    ):
        _make_legacy_receipt(
            isolated_receipts, "10000000T000000_legacy_00000000.json",
            "start_project", previous_hash=None,
        )
        h1 = _make_v2_receipt(
            isolated_receipts, "10000001T000000_v2_00000001.json", "stop_project",
            previous_hash=receipts_module._legacy_recover_chain_head(),
        )
        receipts_module._write_chain_head(h1, 2)
        r2 = _emit("start_group")
        assert r2["receipt_version"] == 3

        pubkey_hex = receipts_module.receipt_public_key_hex()
        result = _runner().invoke(cli.cli, ["receipts", "verify", "--pubkey", pubkey_hex])

        assert result.exit_code == 0
        assert "Chain intact" in result.output
        assert "unverifiable" in result.output.lower()
        assert "2 receipt(s)" in result.output  # legacy + v2, both unverifiable-by-method

    def test_pubkey_does_not_silently_fall_back_to_the_local_keychain_key(
        self, isolated_receipts, monkeypatch
    ):
        """§A11 failure mode #5: --pubkey must use the SUPPLIED key, never
        the local Keychain-backed one. Mock the local public-key accessor
        to a different (wrong) keypair entirely — if verification still
        passes, it proves the supplied --pubkey was actually used, not a
        silent fallback to local Keychain state."""
        _emit("start_project")
        correct_pubkey_hex = receipts_module.receipt_public_key_hex()

        wrong_key = ed25519.Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
        monkeypatch.setattr(receipts_module, "_receipt_public_key", lambda: wrong_key.public_key())

        result = _runner().invoke(cli.cli, ["receipts", "verify", "--pubkey", correct_pubkey_hex])
        assert result.exit_code == 0
        assert "Chain intact" in result.output

    def test_invalid_pubkey_hex_is_rejected_cleanly(self, isolated_receipts):
        _emit("start_project")
        result = _runner().invoke(cli.cli, ["receipts", "verify", "--pubkey", "not-hex"])
        assert result.exit_code != 0
        assert "Invalid --pubkey" in result.output


class TestReceiptsKeysShow:
    """Mirrors `identity keys show` — prints the receipt signing PUBLIC
    key only, never the private key (§A11 failure mode #7)."""

    def test_prints_only_the_public_key(self, isolated_receipts):
        expected_pub = receipts_module.receipt_public_key_hex()
        result = _runner().invoke(cli.cli, ["receipts", "keys", "show"])
        assert result.exit_code == 0
        assert expected_pub in result.output

    def test_never_prints_the_private_key(self, isolated_receipts):
        private_key = receipts_module._receipt_signing_key()
        private_hex = receipts_module._private_key_hex(private_key)
        result = _runner().invoke(cli.cli, ["receipts", "keys", "show"])
        assert private_hex not in result.output
