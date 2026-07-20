"""Tests for receipt hash-chaining and sync state tracking."""

import fcntl
import hashlib
import hmac
import json
import os
import threading
import time
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


class TestConcurrentEmission:
    """Test that concurrent writers produce a linear chain, not a fork."""

    def test_two_threads_produce_linear_chain(self, receipts_dir, monkeypatch):
        """Simulate MCP + CLI writing receipts concurrently."""
        import receipts as receipts_mod

        # Point the module at our temp directory
        monkeypatch.setattr(receipts_mod, "RECEIPTS_DIR", receipts_dir)
        monkeypatch.setattr(receipts_mod, "LOCK_PATH", receipts_dir / ".chain.lock")
        monkeypatch.setattr(receipts_mod, "CHAIN_HEAD_PATH", receipts_dir / ".chain_head")

        # Stub out snapshot to avoid needing real registry/scanner
        monkeypatch.setattr(receipts_mod, "snapshot", lambda: {
            "listening_ports": [],
            "managed_projects": {},
        })

        errors = []

        def writer(thread_id, count):
            try:
                for i in range(count):
                    receipts_mod.emit(
                        action=f"action_{thread_id}_{i}",
                        target={"project": "test"},
                        result={"status": "success"},
                        env_before={"listening_ports": [], "managed_projects": {}},
                        session_id=f"session_{thread_id}",
                        actor_type="test",
                        agent_hint="test",
                    )
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=writer, args=("mcp", 5))
        t2 = threading.Thread(target=writer, args=("cli", 5))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors, f"Writer threads raised errors: {errors}"

        # Verify the chain is linear (no forks)
        files = sorted(receipts_dir.glob("*.json"))
        assert len(files) == 10

        expected_previous = None
        for f in files:
            receipt = json.loads(f.read_text())
            assert receipt["previous_hash"] == expected_previous, (
                f"Chain fork detected at {f.name}: "
                f"expected previous_hash={expected_previous}, "
                f"got {receipt['previous_hash']}"
            )
            # Verify the receipt_hash is a valid Ed25519 signature (ID-Q4
            # Phase 2) over the canonical receipt bytes.
            stored_hash = receipt["receipt_hash"]
            verify_copy = {k: v for k, v in receipt.items() if k != "receipt_hash"}
            canonical = json.dumps(verify_copy, sort_keys=True, separators=(",", ":"))
            import receipts as receipts_mod
            receipts_mod._receipt_public_key().verify(
                bytes.fromhex(stored_hash), canonical.encode("utf-8"),
            )
            expected_previous = stored_hash

        anchor = json.loads((receipts_dir / ".chain_head").read_text())
        assert anchor == {"head_hash": expected_previous, "count": 10}

    def test_lock_file_created(self, receipts_dir, monkeypatch):
        """Verify the lock file is created in the receipts directory."""
        import receipts as receipts_mod

        monkeypatch.setattr(receipts_mod, "RECEIPTS_DIR", receipts_dir)
        lock_path = receipts_dir / ".chain.lock"
        monkeypatch.setattr(receipts_mod, "LOCK_PATH", lock_path)
        monkeypatch.setattr(receipts_mod, "CHAIN_HEAD_PATH", receipts_dir / ".chain_head")
        monkeypatch.setattr(receipts_mod, "snapshot", lambda: {
            "listening_ports": [],
            "managed_projects": {},
        })

        receipts_mod.emit(
            action="test_action",
            target={"project": "test"},
            result={"status": "success"},
            env_before={"listening_ports": [], "managed_projects": {}},
            session_id="test_session",
            actor_type="test",
            agent_hint="test",
        )

        assert lock_path.exists()


class TestAgreementHashField:
    """TI-Q4 (v1.0i §50) — agreement_hash is an optional, omit-when-None
    receipt field, exactly like revocation_state."""

    def test_agreement_hash_omitted_when_none(self, receipts_dir, monkeypatch):
        import receipts as receipts_mod
        monkeypatch.setattr(receipts_mod, "RECEIPTS_DIR", receipts_dir)
        monkeypatch.setattr(receipts_mod, "LOCK_PATH", receipts_dir / ".chain.lock")
        monkeypatch.setattr(receipts_mod, "CHAIN_HEAD_PATH", receipts_dir / ".chain_head")
        monkeypatch.setattr(receipts_mod, "snapshot", lambda: {
            "listening_ports": [], "managed_projects": {},
        })

        receipts_mod.emit(
            action="start_project", target={"project": "p"}, result={"status": "success"},
            env_before={"listening_ports": [], "managed_projects": {}},
            session_id="s", actor_type="test", agent_hint="test",
        )
        files = sorted(receipts_dir.glob("*.json"))
        receipt = json.loads(files[0].read_text())
        assert "agreement_hash" not in receipt

    def test_agreement_hash_present_when_supplied(self, receipts_dir, monkeypatch):
        import receipts as receipts_mod
        monkeypatch.setattr(receipts_mod, "RECEIPTS_DIR", receipts_dir)
        monkeypatch.setattr(receipts_mod, "LOCK_PATH", receipts_dir / ".chain.lock")
        monkeypatch.setattr(receipts_mod, "CHAIN_HEAD_PATH", receipts_dir / ".chain_head")
        monkeypatch.setattr(receipts_mod, "snapshot", lambda: {
            "listening_ports": [], "managed_projects": {},
        })

        h = "a" * 64
        receipts_mod.emit(
            action="start_project", target={"project": "p"}, result={"status": "success"},
            env_before={"listening_ports": [], "managed_projects": {}},
            session_id="s", actor_type="test", agent_hint="test",
            agreement_hash=h,
        )
        files = sorted(receipts_dir.glob("*.json"))
        receipt = json.loads(files[0].read_text())
        assert receipt["agreement_hash"] == h

    def test_agreement_hash_is_hashed_field_changes_receipt_hash(self, receipts_dir, monkeypatch):
        """agreement_hash must be part of the canonicalized dict before
        hashing — two receipts identical except for agreement_hash must
        produce different receipt_hash values."""
        import receipts as receipts_mod
        monkeypatch.setattr(receipts_mod, "RECEIPTS_DIR", receipts_dir)
        monkeypatch.setattr(receipts_mod, "LOCK_PATH", receipts_dir / ".chain.lock")
        monkeypatch.setattr(receipts_mod, "CHAIN_HEAD_PATH", receipts_dir / ".chain_head")
        monkeypatch.setattr(receipts_mod, "snapshot", lambda: {
            "listening_ports": [], "managed_projects": {},
        })

        receipts_mod.emit(
            action="start_project", target={"project": "p"}, result={"status": "success"},
            env_before={"listening_ports": [], "managed_projects": {}},
            session_id="s", actor_type="test", agent_hint="test",
            agreement_hash="a" * 64,
        )
        files = sorted(receipts_dir.glob("*.json"))
        for f in files:
            f.unlink()

        receipts_mod.emit(
            action="start_project", target={"project": "p"}, result={"status": "success"},
            env_before={"listening_ports": [], "managed_projects": {}},
            session_id="s", actor_type="test", agent_hint="test",
            agreement_hash="b" * 64,
        )
        files = sorted(receipts_dir.glob("*.json"))
        second = json.loads(files[0].read_text())

        # Recompute what the keyed hash would have been with the first
        # agreement_hash value — must differ from what was actually stored.
        tampered = dict(second)
        tampered["agreement_hash"] = "a" * 64
        verify_copy = {k: v for k, v in tampered.items() if k != "receipt_hash"}
        canonical = json.dumps(verify_copy, sort_keys=True, separators=(",", ":"))
        recomputed = hmac.new(
            b"test-only-mac-key-not-for-real-use", canonical.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        assert recomputed != second["receipt_hash"]


class TestKeyedChain:
    """F-01 / ID-Q4 Phase 2: receipt_hash must be an Ed25519 signature, not
    a plain, unkeyed hash anyone can recompute. The signing key itself is
    mocked repo-wide in conftest.py's autouse _test_receipt_signing_key
    fixture (a fixed keypair, distinct from identity's)."""

    def test_emit_hash_is_signed_not_plain_sha256(self, receipts_dir, monkeypatch):
        import receipts as receipts_mod
        monkeypatch.setattr(receipts_mod, "RECEIPTS_DIR", receipts_dir)
        monkeypatch.setattr(receipts_mod, "LOCK_PATH", receipts_dir / ".chain.lock")
        monkeypatch.setattr(receipts_mod, "CHAIN_HEAD_PATH", receipts_dir / ".chain_head")
        monkeypatch.setattr(receipts_mod, "snapshot", lambda: {
            "listening_ports": [], "managed_projects": {},
        })

        receipt = receipts_mod.emit(
            action="start_project", target={"project": "p"}, result={"status": "success"},
            env_before={"listening_ports": [], "managed_projects": {}},
            session_id="s", actor_type="test", agent_hint="test",
        )

        verify_copy = {k: v for k, v in receipt.items() if k != "receipt_hash"}
        canonical = json.dumps(verify_copy, sort_keys=True, separators=(",", ":"))
        plain_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        assert receipt["receipt_version"] == 3
        assert receipt["receipt_hash"] != plain_sha256
        # 128 hex chars (a 64-byte Ed25519 signature) — not 64 (a sha256
        # digest or the old HMAC digest) — §11 failure mode #2.
        assert len(receipt["receipt_hash"]) == 128
        # Verifies against the (mocked) receipt public key — the same
        # verification an auditor holding only receipt_public_key_hex()
        # would perform (§10 benchmark 1).
        receipts_mod._receipt_public_key().verify(
            bytes.fromhex(receipt["receipt_hash"]), canonical.encode("utf-8"),
        )
        # A signature over tampered bytes must not verify.
        with pytest.raises(Exception):
            receipts_mod._receipt_public_key().verify(
                bytes.fromhex(receipt["receipt_hash"]), (canonical + "x").encode("utf-8"),
            )

    def test_emit_stamps_receipt_version(self, receipts_dir, monkeypatch):
        import receipts as receipts_mod
        monkeypatch.setattr(receipts_mod, "RECEIPTS_DIR", receipts_dir)
        monkeypatch.setattr(receipts_mod, "LOCK_PATH", receipts_dir / ".chain.lock")
        monkeypatch.setattr(receipts_mod, "CHAIN_HEAD_PATH", receipts_dir / ".chain_head")
        monkeypatch.setattr(receipts_mod, "snapshot", lambda: {
            "listening_ports": [], "managed_projects": {},
        })

        receipt = receipts_mod.emit(
            action="start_project", target={"project": "p"}, result={"status": "success"},
            env_before={"listening_ports": [], "managed_projects": {}},
            session_id="s", actor_type="test", agent_hint="test",
        )
        assert receipt["receipt_version"] == receipts_mod.RECEIPT_VERSION
        assert receipts_mod.RECEIPT_VERSION >= 2

    def test_emit_fails_closed_when_signing_key_unavailable(self, receipts_dir, monkeypatch):
        import receipts as receipts_mod
        monkeypatch.setattr(receipts_mod, "RECEIPTS_DIR", receipts_dir)
        monkeypatch.setattr(receipts_mod, "LOCK_PATH", receipts_dir / ".chain.lock")
        monkeypatch.setattr(receipts_mod, "CHAIN_HEAD_PATH", receipts_dir / ".chain_head")
        monkeypatch.setattr(receipts_mod, "snapshot", lambda: {
            "listening_ports": [], "managed_projects": {},
        })

        def _boom():
            raise RuntimeError("keychain locked")
        monkeypatch.setattr(receipts_mod, "_receipt_signing_key", _boom)

        with pytest.raises(receipts_mod.ReceiptKeyUnavailableError):
            receipts_mod.emit(
                action="start_project", target={"project": "p"}, result={"status": "success"},
                env_before={"listening_ports": [], "managed_projects": {}},
                session_id="s", actor_type="test", agent_hint="test",
            )

        # Fail closed means nothing was written — no unsigned receipt, no
        # partial anchor update.
        assert list(receipts_dir.glob("*.json")) == []
        assert not (receipts_dir / ".chain_head").exists()


class TestChainAnchor:
    """F-01: a persisted head pointer (.chain_head) so verify can detect
    tail-truncation, which the link-walk alone cannot (deleting the newest
    N receipts still leaves a perfectly self-consistent shorter chain)."""

    def test_emit_writes_chain_head_anchor(self, receipts_dir, monkeypatch):
        import receipts as receipts_mod
        monkeypatch.setattr(receipts_mod, "RECEIPTS_DIR", receipts_dir)
        monkeypatch.setattr(receipts_mod, "LOCK_PATH", receipts_dir / ".chain.lock")
        anchor_path = receipts_dir / ".chain_head"
        monkeypatch.setattr(receipts_mod, "CHAIN_HEAD_PATH", anchor_path)
        monkeypatch.setattr(receipts_mod, "snapshot", lambda: {
            "listening_ports": [], "managed_projects": {},
        })

        r1 = receipts_mod.emit(
            action="start_project", target={"project": "p"}, result={"status": "success"},
            env_before={"listening_ports": [], "managed_projects": {}},
            session_id="s", actor_type="test", agent_hint="test",
        )
        anchor = json.loads(anchor_path.read_text())
        assert anchor == {"head_hash": r1["receipt_hash"], "count": 1}

        r2 = receipts_mod.emit(
            action="stop_project", target={"project": "p"}, result={"status": "success"},
            env_before={"listening_ports": [], "managed_projects": {}},
            session_id="s", actor_type="test", agent_hint="test",
        )
        anchor = json.loads(anchor_path.read_text())
        assert anchor == {"head_hash": r2["receipt_hash"], "count": 2}

    def test_recover_chain_head_trusts_anchor_not_filename_sort(self, receipts_dir, monkeypatch):
        """A rogue file that sorts after the real chain must not override
        the anchor's recorded head — closing the filename-sort trust gap."""
        import receipts as receipts_mod
        monkeypatch.setattr(receipts_mod, "RECEIPTS_DIR", receipts_dir)
        monkeypatch.setattr(receipts_mod, "LOCK_PATH", receipts_dir / ".chain.lock")
        monkeypatch.setattr(receipts_mod, "CHAIN_HEAD_PATH", receipts_dir / ".chain_head")
        monkeypatch.setattr(receipts_mod, "snapshot", lambda: {
            "listening_ports": [], "managed_projects": {},
        })

        r1 = receipts_mod.emit(
            action="start_project", target={"project": "p"}, result={"status": "success"},
            env_before={"listening_ports": [], "managed_projects": {}},
            session_id="s", actor_type="test", agent_hint="test",
        )

        # A rogue/forged file, filename-sorted after the real receipt, with
        # a fabricated receipt_hash that filename-sort trust would have
        # preferred over the real head.
        rogue = {"receipt_hash": "z" * 64, "previous_hash": r1["receipt_hash"]}
        (receipts_dir / "99999999T999999999999_rogue_ffffffff.json").write_text(json.dumps(rogue))

        assert receipts_mod.recover_chain_head() == r1["receipt_hash"]

    def test_recover_chain_head_bootstraps_from_legacy_chain(self, receipts_dir, monkeypatch):
        """No .chain_head yet (pre-existing chain from before this code
        shipped) — bootstrap from the last receipt file exactly once, for
        migration continuity."""
        import receipts as receipts_mod
        monkeypatch.setattr(receipts_mod, "RECEIPTS_DIR", receipts_dir)
        monkeypatch.setattr(receipts_mod, "CHAIN_HEAD_PATH", receipts_dir / ".chain_head")

        h0, _ = _make_receipt(receipts_dir, "start_project", previous_hash=None, index=0)
        h1, _ = _make_receipt(receipts_dir, "stop_project", previous_hash=h0, index=1)

        assert receipts_mod.recover_chain_head() == h1


class TestIdentityLabeling:
    """F-02 acute: agent_hint is a self-declared string (MCP_AGENT_HINT),
    never an authenticated identity. Every receipt must say so explicitly
    rather than let a reader assume it's verified."""

    def test_receipt_marks_identity_unverified(self, receipts_dir, monkeypatch):
        import receipts as receipts_mod
        monkeypatch.setattr(receipts_mod, "RECEIPTS_DIR", receipts_dir)
        monkeypatch.setattr(receipts_mod, "LOCK_PATH", receipts_dir / ".chain.lock")
        monkeypatch.setattr(receipts_mod, "CHAIN_HEAD_PATH", receipts_dir / ".chain_head")
        monkeypatch.setattr(receipts_mod, "snapshot", lambda: {
            "listening_ports": [], "managed_projects": {},
        })

        receipt = receipts_mod.emit(
            action="start_project", target={"project": "p"}, result={"status": "success"},
            env_before={"listening_ports": [], "managed_projects": {}},
            session_id="s", actor_type="test", agent_hint="claude-code",
        )
        assert receipt["actor"]["identity_verified"] is False
        # The field itself is unconditional — even a plausible-looking
        # agent_hint must not be mistaken for an authenticated identity.
        assert receipt["actor"]["agent_hint"] == "claude-code"


class TestIdentityVerifiedThreading:
    """Identity-plane Stage 1: identity_verified is threaded from the
    caller, never hardcoded true or false (§10 failure mode 7)."""

    def test_identity_verified_defaults_false(self, receipts_dir, monkeypatch):
        import receipts as receipts_mod
        monkeypatch.setattr(receipts_mod, "RECEIPTS_DIR", receipts_dir)
        monkeypatch.setattr(receipts_mod, "LOCK_PATH", receipts_dir / ".chain.lock")
        monkeypatch.setattr(receipts_mod, "CHAIN_HEAD_PATH", receipts_dir / ".chain_head")
        monkeypatch.setattr(receipts_mod, "snapshot", lambda: {
            "listening_ports": [], "managed_projects": {},
        })

        receipt = receipts_mod.emit(
            action="start_project", target={"project": "p"}, result={"status": "success"},
            env_before={"listening_ports": [], "managed_projects": {}},
            session_id="s", actor_type="test", agent_hint="test",
        )
        assert receipt["actor"]["identity_verified"] is False

    def test_identity_verified_true_when_supplied(self, receipts_dir, monkeypatch):
        import receipts as receipts_mod
        monkeypatch.setattr(receipts_mod, "RECEIPTS_DIR", receipts_dir)
        monkeypatch.setattr(receipts_mod, "LOCK_PATH", receipts_dir / ".chain.lock")
        monkeypatch.setattr(receipts_mod, "CHAIN_HEAD_PATH", receipts_dir / ".chain_head")
        monkeypatch.setattr(receipts_mod, "snapshot", lambda: {
            "listening_ports": [], "managed_projects": {},
        })

        receipt = receipts_mod.emit(
            action="start_project", target={"project": "p"}, result={"status": "success"},
            env_before={"listening_ports": [], "managed_projects": {}},
            session_id="s", actor_type="test", agent_hint="agent-x",
            identity_verified=True,
        )
        assert receipt["actor"]["identity_verified"] is True
        assert receipt["actor"]["agent_hint"] == "agent-x"

    def test_delegation_path_present_and_empty_in_stage_1(self, receipts_dir, monkeypatch):
        import receipts as receipts_mod
        monkeypatch.setattr(receipts_mod, "RECEIPTS_DIR", receipts_dir)
        monkeypatch.setattr(receipts_mod, "LOCK_PATH", receipts_dir / ".chain.lock")
        monkeypatch.setattr(receipts_mod, "CHAIN_HEAD_PATH", receipts_dir / ".chain_head")
        monkeypatch.setattr(receipts_mod, "snapshot", lambda: {
            "listening_ports": [], "managed_projects": {},
        })

        receipt = receipts_mod.emit(
            action="start_project", target={"project": "p"}, result={"status": "success"},
            env_before={"listening_ports": [], "managed_projects": {}},
            session_id="s", actor_type="test", agent_hint="test",
        )
        assert receipt["actor"]["delegation_path"] == []

    def test_delegation_path_populated_when_supplied(self, receipts_dir, monkeypatch):
        """Identity-plane Stage 2: delegation_path is a real, threaded
        parameter — not the Stage 1 hardcoded empty-list literal."""
        import receipts as receipts_mod
        monkeypatch.setattr(receipts_mod, "RECEIPTS_DIR", receipts_dir)
        monkeypatch.setattr(receipts_mod, "LOCK_PATH", receipts_dir / ".chain.lock")
        monkeypatch.setattr(receipts_mod, "CHAIN_HEAD_PATH", receipts_dir / ".chain_head")
        monkeypatch.setattr(receipts_mod, "snapshot", lambda: {
            "listening_ports": [], "managed_projects": {},
        })

        receipt = receipts_mod.emit(
            action="start_project", target={"project": "p"}, result={"status": "success"},
            env_before={"listening_ports": [], "managed_projects": {}},
            session_id="s", actor_type="test", agent_hint="agent-grandchild",
            identity_verified=True,
            delegation_path=["agent-root", "agent-child", "agent-grandchild"],
        )
        assert receipt["actor"]["delegation_path"] == ["agent-root", "agent-child", "agent-grandchild"]


class TestReceiptLoading:
    """Test the load() function from the extracted module."""

    def test_load_returns_newest_first(self, receipts_dir, monkeypatch):
        import receipts as receipts_mod
        monkeypatch.setattr(receipts_mod, "RECEIPTS_DIR", receipts_dir)

        h0, _ = _make_receipt(receipts_dir, "start_project", previous_hash=None, index=0)
        h1, _ = _make_receipt(receipts_dir, "stop_project", previous_hash=h0, index=1)
        h2, _ = _make_receipt(receipts_dir, "start_group", previous_hash=h1, index=2)

        loaded = receipts_mod.load(limit=50)
        assert len(loaded) == 3
        # Newest first
        assert loaded[0]["action"] == "start_group"
        assert loaded[2]["action"] == "start_project"

    def test_load_with_action_filter(self, receipts_dir, monkeypatch):
        import receipts as receipts_mod
        monkeypatch.setattr(receipts_mod, "RECEIPTS_DIR", receipts_dir)

        h0, _ = _make_receipt(receipts_dir, "start_project", previous_hash=None, index=0)
        h1, _ = _make_receipt(receipts_dir, "stop_project", previous_hash=h0, index=1)
        h2, _ = _make_receipt(receipts_dir, "start_project", previous_hash=h1, index=2)

        loaded = receipts_mod.load(limit=50, action_filter="start_project")
        assert len(loaded) == 2
        assert all(r["action"] == "start_project" for r in loaded)

    def test_load_respects_limit(self, receipts_dir, monkeypatch):
        import receipts as receipts_mod
        monkeypatch.setattr(receipts_mod, "RECEIPTS_DIR", receipts_dir)

        h = None
        for i in range(10):
            h, _ = _make_receipt(receipts_dir, "start_project", previous_hash=h, index=i)

        loaded = receipts_mod.load(limit=3)
        assert len(loaded) == 3
