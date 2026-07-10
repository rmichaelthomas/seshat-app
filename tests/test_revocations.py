# tests/test_revocations.py
"""Tests for REVOKE local enforcement: temporal windows + revocations.limn."""
import hashlib
import json

import pytest

import agreements
import receipts as receipts_mod
from agreements import (
    _temporal_window,
    _validate_forbid_only,
    check_action,
    revocation_state,
)


# Cross-test isolation from any real ~/.seshat/revocations.limn on the host
# machine is provided by the autouse fixture in tests/conftest.py.


# ── Phase 1: temporal-window enforcement ────────────────────────────────────


class TestTemporalWindowUnit:
    """Direct unit tests for _temporal_window()."""

    def test_no_canonical_is_unbounded(self):
        assert _temporal_window(None) == "unbounded"

    def test_no_date_prefix_is_unbounded(self):
        assert _temporal_window('permit actor is claude-code and action is x') == "unbounded"

    def test_until_past_is_expired(self):
        assert _temporal_window('until "2020-01-01" permit actor is claude-code') == "expired"

    def test_until_future_is_active(self):
        assert _temporal_window('until "2099-01-01" permit actor is claude-code') == "active"

    def test_starting_future_is_future(self):
        assert _temporal_window('starting "2099-01-01" permit actor is claude-code') == "future"

    def test_starting_past_is_active(self):
        assert _temporal_window('starting "2020-01-01" permit actor is claude-code') == "active"

    def test_garbage_date_is_malformed(self):
        assert _temporal_window('until "not-a-date" permit actor is claude-code') == "malformed"

    def test_calendar_invalid_date_is_malformed(self):
        # Shape-valid (YYYY-MM-DD) but not a real calendar date — liminate's
        # own parser accepts the shape; only date.fromisoformat() catches this.
        assert _temporal_window('until "2020-13-45" permit actor is claude-code') == "malformed"


class TestTemporalWindowEnforcement:
    """Integration tests through check_action()."""

    def test_expired_permit_does_not_grant(self):
        agreement = 'until "2020-01-01" permit actor is "claude-code" and action is "start_project"'
        d = check_action("claude-code", "start_project", agreement_text=agreement)
        assert d.allowed is False
        assert d.mode == "default-deny"

    def test_future_permit_does_not_grant(self):
        agreement = 'starting "2099-01-01" permit actor is "claude-code" and action is "start_project"'
        d = check_action("claude-code", "start_project", agreement_text=agreement)
        assert d.allowed is False
        assert d.mode == "default-deny"

    def test_expired_forbid_lapses_and_matching_permit_decides(self):
        agreement = '''
until "2020-01-01" forbid action is "z"
permit actor is "claude-code" and action is "z"
'''
        d = check_action("claude-code", "z", agreement_text=agreement)
        assert d.allowed is True
        assert d.mode == "permitted"

    def test_future_forbid_lapses_and_default_denies_without_permit(self):
        agreement = 'starting "2099-01-01" forbid action is "z"'
        d = check_action("claude-code", "z", agreement_text=agreement)
        assert d.allowed is False
        assert d.mode == "default-deny"

    def test_malformed_date_denies_with_error_mode(self):
        agreement = 'until "not-a-date" permit actor is "claude-code" and action is "start_project"'
        d = check_action("claude-code", "start_project", agreement_text=agreement)
        assert d.allowed is False
        assert d.mode == "error"

    def test_calendar_invalid_date_denies_with_error_mode(self):
        agreement = 'until "2020-13-45" permit actor is "claude-code" and action is "start_project"'
        d = check_action("claude-code", "start_project", agreement_text=agreement)
        assert d.allowed is False
        assert d.mode == "error"

    def test_unbounded_rule_is_unaffected(self):
        agreement = 'permit actor is "claude-code" and action is "start_project"'
        d = check_action("claude-code", "start_project", agreement_text=agreement)
        assert d.allowed is True
        assert d.mode == "permitted"


# ── Phase 2: revocations loading, validation, composition ──────────────────


class TestValidateForbidOnly:
    def test_accepts_plain_forbid(self):
        assert _validate_forbid_only('forbid action is "stop_project"') is None

    def test_accepts_temporal_prefixed_forbid(self):
        assert _validate_forbid_only('until "2099-01-01" forbid action is "stop_project"') is None

    def test_accepts_comments_and_blank_lines(self):
        text = '''
-- this is a comment
forbid action is "stop_project"

-- another comment
forbid actor is "rogue-agent"
'''
        assert _validate_forbid_only(text) is None

    def test_rejects_permit_line(self):
        error = _validate_forbid_only('permit action is "stop_project"')
        assert error is not None
        assert "permit" in error.lower() or "'permit'" in error

    def test_rejects_permit_among_valid_forbids(self):
        text = '''
forbid action is "stop_project"
permit action is "start_project"
'''
        error = _validate_forbid_only(text)
        assert error is not None
        assert "line 3" in error


class TestRevocationComposition:
    AGREEMENT = '''
permit actor is "claude-code" and action is "stop_project"
permit actor is "claude-code" and action is "start_project"
'''

    def test_no_revocations_file_is_backward_compatible(self, monkeypatch):
        monkeypatch.setattr(agreements, "load_revocations", lambda: None)
        d = check_action("claude-code", "stop_project", agreement_text=self.AGREEMENT)
        assert d.allowed is True
        assert d.mode == "permitted"

    def test_revocation_forbid_wins_over_agreement_permit_surgically(self, monkeypatch):
        monkeypatch.setattr(
            agreements, "load_revocations", lambda: 'forbid action is "stop_project"'
        )
        denied = check_action("claude-code", "stop_project", agreement_text=self.AGREEMENT)
        assert denied.allowed is False
        assert denied.mode == "forbidden"

        still_allowed = check_action("claude-code", "start_project", agreement_text=self.AGREEMENT)
        assert still_allowed.allowed is True
        assert still_allowed.mode == "permitted"

    def test_blanket_actor_forbid_denies_every_action_for_that_actor_only(self, monkeypatch):
        monkeypatch.setattr(
            agreements, "load_revocations", lambda: 'forbid actor is "claude-code"'
        )
        denied = check_action("claude-code", "start_project", agreement_text=self.AGREEMENT)
        assert denied.allowed is False
        assert denied.mode == "forbidden"

        other_agreement = 'permit actor is "someone-else" and action is "start_project"'
        unaffected = check_action("someone-else", "start_project", agreement_text=other_agreement)
        assert unaffected.allowed is True
        assert unaffected.mode == "permitted"

    def test_permit_line_in_revocations_fails_closed(self, monkeypatch):
        monkeypatch.setattr(
            agreements, "load_revocations", lambda: 'permit action is "stop_project"'
        )
        d = check_action("claude-code", "start_project", agreement_text=self.AGREEMENT)
        assert d.allowed is False
        assert d.mode == "error"
        assert "revocations" in d.reason.lower()

        # Every action denied, not just the one referenced by the bad line.
        d2 = check_action("claude-code", "stop_project", agreement_text=self.AGREEMENT)
        assert d2.allowed is False
        assert d2.mode == "error"

    def test_expired_revocation_lapses(self, monkeypatch):
        monkeypatch.setattr(
            agreements,
            "load_revocations",
            lambda: 'until "2020-01-01" forbid action is "stop_project"',
        )
        d = check_action("claude-code", "stop_project", agreement_text=self.AGREEMENT)
        assert d.allowed is True
        assert d.mode == "permitted"

    def test_future_revocation_has_not_yet_taken_effect(self, monkeypatch):
        monkeypatch.setattr(
            agreements,
            "load_revocations",
            lambda: 'starting "2099-01-01" forbid action is "stop_project"',
        )
        d = check_action("claude-code", "stop_project", agreement_text=self.AGREEMENT)
        assert d.allowed is True
        assert d.mode == "permitted"


# ── Phase 3: revocation_state receipt field ─────────────────────────────────


class TestRevocationState:
    def test_none_when_no_revocations_file(self, monkeypatch):
        monkeypatch.setattr(agreements, "load_revocations", lambda: None)
        assert revocation_state() is None

    def test_head_hash_matches_sha256_of_content(self, monkeypatch, tmp_path):
        text = 'forbid action is "stop_project"\n'
        monkeypatch.setattr(agreements, "load_revocations", lambda: text)
        monkeypatch.setattr(
            agreements, "LAST_SYNCED_REVOCATIONS_PATH", tmp_path / "nonexistent" / ".marker"
        )
        state = revocation_state()
        assert state is not None
        assert state["head_hash"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert state["last_checked"] is None

    def test_last_checked_reads_marker(self, monkeypatch, tmp_path):
        text = 'forbid action is "stop_project"\n'
        marker = tmp_path / ".last_synced_revocations"
        marker.write_text("2026-07-07T00:00:00+00:00\n")
        monkeypatch.setattr(agreements, "load_revocations", lambda: text)
        monkeypatch.setattr(agreements, "LAST_SYNCED_REVOCATIONS_PATH", marker)
        state = revocation_state()
        assert state["last_checked"] == "2026-07-07T00:00:00+00:00"


# ── TI-Q4: agreement_hash() (v1.0i §50) — mirrors revocation_state() ────────


class TestAgreementHash:
    def test_none_when_no_agreement_file(self, monkeypatch):
        monkeypatch.setattr(agreements, "load_agreement", lambda: None)
        assert agreements.agreement_hash() is None

    def test_hash_matches_sha256_of_content(self, monkeypatch):
        text = 'permit actor is "claude-code" and action is "start_project"\n'
        monkeypatch.setattr(agreements, "load_agreement", lambda: text)
        assert agreements.agreement_hash() == hashlib.sha256(text.encode("utf-8")).hexdigest()

    def test_hash_changes_when_content_changes(self, monkeypatch):
        monkeypatch.setattr(agreements, "load_agreement", lambda: "permit actor is a and action is b")
        h1 = agreements.agreement_hash()
        monkeypatch.setattr(agreements, "load_agreement", lambda: "permit actor is a and action is c")
        h2 = agreements.agreement_hash()
        assert h1 != h2


class TestRevocationStateReceiptAdditivity:
    """revocation_state must be additive: omitted when None, present when
    given, and must never break hash-chain reproducibility either way."""

    @pytest.fixture
    def receipts_dir(self, tmp_path, monkeypatch):
        d = tmp_path / "receipts"
        d.mkdir()
        monkeypatch.setattr(receipts_mod, "RECEIPTS_DIR", d)
        monkeypatch.setattr(receipts_mod, "LOCK_PATH", d / ".chain.lock")
        monkeypatch.setattr(
            receipts_mod,
            "snapshot",
            lambda: {"listening_ports": [], "managed_projects": {}},
        )
        return d

    def _recompute_hash(self, receipt: dict) -> str:
        verify_copy = {k: v for k, v in receipt.items() if k != "receipt_hash"}
        canonical = json.dumps(verify_copy, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def test_field_omitted_when_none_and_chain_verifies(self, receipts_dir):
        receipts_mod.emit(
            action="start_project",
            target={"project": "p1"},
            result={"status": "success"},
            env_before={"listening_ports": [], "managed_projects": {}},
            session_id="s1",
            actor_type="test",
            agent_hint="test",
        )
        files = sorted(receipts_dir.glob("*.json"))
        receipt = json.loads(files[0].read_text())
        assert "revocation_state" not in receipt
        assert self._recompute_hash(receipt) == receipt["receipt_hash"]

    def test_field_present_when_given_and_chain_still_verifies(self, receipts_dir):
        state = {"head_hash": "deadbeef", "last_checked": None}
        receipts_mod.emit(
            action="start_project",
            target={"project": "p1"},
            result={"status": "success"},
            env_before={"listening_ports": [], "managed_projects": {}},
            session_id="s1",
            actor_type="test",
            agent_hint="test",
            revocation_state=state,
        )
        files = sorted(receipts_dir.glob("*.json"))
        receipt = json.loads(files[0].read_text())
        assert receipt["revocation_state"] == state
        assert self._recompute_hash(receipt) == receipt["receipt_hash"]

    def test_chain_mixes_with_and_without_field_without_breaking_links(self, receipts_dir):
        receipts_mod.emit(
            action="start_project",
            target={"project": "p1"},
            result={"status": "success"},
            env_before={"listening_ports": [], "managed_projects": {}},
            session_id="s1",
            actor_type="test",
            agent_hint="test",
        )
        receipts_mod.emit(
            action="stop_project",
            target={"project": "p1"},
            result={"status": "success"},
            env_before={"listening_ports": [], "managed_projects": {}},
            session_id="s1",
            actor_type="test",
            agent_hint="test",
            revocation_state={"head_hash": "abc", "last_checked": None},
        )
        files = sorted(receipts_dir.glob("*.json"))
        assert len(files) == 2
        r0 = json.loads(files[0].read_text())
        r1 = json.loads(files[1].read_text())

        assert r0["previous_hash"] is None
        assert r1["previous_hash"] == r0["receipt_hash"]
        assert self._recompute_hash(r0) == r0["receipt_hash"]
        assert self._recompute_hash(r1) == r1["receipt_hash"]
