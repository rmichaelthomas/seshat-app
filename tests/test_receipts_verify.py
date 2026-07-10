"""CLI-level tests for `seshat receipts verify` (F-01): keyed-hash
verification, legacy-chain handling, and anchor-based truncation
detection. No existing test exercised this command directly before —
its logic lived inline in cli.py with zero coverage."""

import hashlib
import json

import pytest
from click.testing import CliRunner

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
        assert "hash mismatch" in result.output
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
        # A real v2 receipt chains from the legacy one (bootstraps the anchor).
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
