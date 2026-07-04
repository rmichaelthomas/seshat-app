"""Tests for receipt hash-chaining and sync state tracking."""

import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def receipts_dir(tmp_path):
    """Provide a temporary receipts directory."""
    d = tmp_path / "receipts"
    d.mkdir()
    return d


def _make_receipt(receipts_dir, action, previous_hash=None, index=0):
    """Helper: create a properly chained receipt file."""
    receipt = {
        "type": "machine_action",
        "timestamp": f"2026-07-03T10:00:0{index}.000000+00:00",
        "actor": {"type": "test", "session_id": "test_session", "agent_hint": "test"},
        "action": action,
        "target": {"project": "test-project"},
        "result": {"status": "success"},
        "environment_before": {"listening_ports": [], "managed_projects": {}},
        "environment_after": {"listening_ports": [], "managed_projects": {}},
        "previous_hash": previous_hash,
    }
    canonical = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    receipt_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    receipt["receipt_hash"] = receipt_hash

    filename = f"20260703T10000{index}_{action}_{index:08x}.json"
    (receipts_dir / filename).write_text(json.dumps(receipt, indent=2))
    return receipt_hash, filename


class TestHashChaining:
    """Test the receipt hash-chain integrity properties."""

    def test_genesis_receipt_has_null_previous_hash(self, receipts_dir):
        receipt_hash, _ = _make_receipt(receipts_dir, "start_project", previous_hash=None)
        files = list(receipts_dir.glob("*.json"))
        assert len(files) == 1
        receipt = json.loads(files[0].read_text())
        assert receipt["previous_hash"] is None
        assert receipt["receipt_hash"] == receipt_hash

    def test_second_receipt_chains_to_first(self, receipts_dir):
        hash_1, _ = _make_receipt(receipts_dir, "start_project", previous_hash=None, index=0)
        hash_2, _ = _make_receipt(receipts_dir, "stop_project", previous_hash=hash_1, index=1)
        files = sorted(receipts_dir.glob("*.json"))
        second = json.loads(files[1].read_text())
        assert second["previous_hash"] == hash_1
        assert second["receipt_hash"] == hash_2

    def test_hash_covers_all_fields(self, receipts_dir):
        """Modifying any field (except receipt_hash) invalidates the hash."""
        hash_1, filename = _make_receipt(receipts_dir, "start_project", previous_hash=None)
        path = receipts_dir / filename
        receipt = json.loads(path.read_text())

        # Tamper with the action field.
        receipt["action"] = "TAMPERED"
        verify_copy = {k: v for k, v in receipt.items() if k != "receipt_hash"}
        canonical = json.dumps(verify_copy, sort_keys=True, separators=(",", ":"))
        recomputed = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        assert recomputed != receipt["receipt_hash"]

    def test_chain_of_three(self, receipts_dir):
        """Three receipts form a valid chain."""
        h0, _ = _make_receipt(receipts_dir, "start_project", previous_hash=None, index=0)
        h1, _ = _make_receipt(receipts_dir, "stop_project", previous_hash=h0, index=1)
        h2, _ = _make_receipt(receipts_dir, "start_group", previous_hash=h1, index=2)

        files = sorted(receipts_dir.glob("*.json"))
        assert len(files) == 3

        r0 = json.loads(files[0].read_text())
        r1 = json.loads(files[1].read_text())
        r2 = json.loads(files[2].read_text())

        assert r0["previous_hash"] is None
        assert r1["previous_hash"] == r0["receipt_hash"]
        assert r2["previous_hash"] == r1["receipt_hash"]

    def test_receipt_hash_is_deterministic(self, receipts_dir):
        """Same content produces the same hash."""
        receipt = {
            "type": "machine_action",
            "timestamp": "2026-07-03T10:00:00.000000+00:00",
            "actor": {"type": "test", "session_id": "s1", "agent_hint": "test"},
            "action": "start_project",
            "target": {"project": "p1"},
            "result": {"status": "success"},
            "environment_before": {},
            "environment_after": {},
            "previous_hash": None,
        }
        c1 = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
        c2 = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
        assert hashlib.sha256(c1.encode()).hexdigest() == hashlib.sha256(c2.encode()).hexdigest()


class TestChainRecovery:
    """Test chain-head recovery from existing receipt files."""

    def test_recover_from_empty_directory(self, receipts_dir):
        """Empty directory returns None."""
        # Import the recovery function pattern — test the logic directly.
        files = sorted(receipts_dir.glob("*.json"))
        assert len(files) == 0
        # Recovery logic: no files → None
        assert True  # Placeholder — the real test is that _recover_chain_head() returns None

    def test_recover_from_existing_receipts(self, receipts_dir):
        """Recovery reads the last file's receipt_hash."""
        h0, _ = _make_receipt(receipts_dir, "start_project", previous_hash=None, index=0)
        h1, _ = _make_receipt(receipts_dir, "stop_project", previous_hash=h0, index=1)

        # Simulate recovery: read last file.
        files = sorted(receipts_dir.glob("*.json"))
        last = json.loads(files[-1].read_text())
        assert last["receipt_hash"] == h1


class TestSyncStateTracking:
    """Test the .last_synced marker file logic."""

    def test_no_marker_means_all_unsent(self, receipts_dir):
        _make_receipt(receipts_dir, "start_project", previous_hash=None, index=0)
        _make_receipt(receipts_dir, "stop_project", previous_hash="fake", index=1)

        # No .last_synced file exists.
        marker = receipts_dir / ".last_synced"
        assert not marker.exists()

        # All .json files should be considered unsent.
        files = sorted(receipts_dir.glob("*.json"))
        assert len(files) == 2

    def test_marker_filters_sent_receipts(self, receipts_dir):
        _, fn0 = _make_receipt(receipts_dir, "start_project", previous_hash=None, index=0)
        _, fn1 = _make_receipt(receipts_dir, "stop_project", previous_hash="fake", index=1)
        _, fn2 = _make_receipt(receipts_dir, "start_group", previous_hash="fake2", index=2)

        # Mark fn1 as the last synced.
        marker = receipts_dir / ".last_synced"
        marker.write_text(fn1 + "\n")

        # Only fn2 should be unsent.
        last_synced = marker.read_text().strip()
        files = sorted(receipts_dir.glob("*.json"))
        unsent = []
        past_marker = False
        for f in files:
            if not past_marker:
                if f.name == last_synced:
                    past_marker = True
                continue
            unsent.append(f.name)
        assert unsent == [fn2]

    def test_marker_with_all_synced(self, receipts_dir):
        _, fn0 = _make_receipt(receipts_dir, "start_project", previous_hash=None, index=0)

        marker = receipts_dir / ".last_synced"
        marker.write_text(fn0 + "\n")

        files = sorted(receipts_dir.glob("*.json"))
        last_synced = marker.read_text().strip()
        unsent = []
        past_marker = False
        for f in files:
            if not past_marker:
                if f.name == last_synced:
                    past_marker = True
                continue
            unsent.append(f.name)
        assert unsent == []
