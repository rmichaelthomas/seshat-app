"""Tests for the Invariant post-action verification integration (SES-Q5).

Invariant runs POST-ACTION via a separate contract at ~/.seshat/invariant.limn,
orthogonal to the Agreement. If no contract exists, Invariant does not run and
the receipt carries no `invariant` block (backward-compatible omission, same
pattern as `revocation_state`). Escalation on failure is recorded on the
receipt; it never blocks the action.
"""

import json

import pytest

import agreements
import invariant_check
import receipts as receipts_mod

ENV_SNAPSHOT = {"listening_ports": [3000], "managed_projects": {}}
FACTS = json.dumps(ENV_SNAPSHOT, sort_keys=True)


class TestNoContractMeansNoBlock:
    def test_run_verification_returns_none_when_absent(self, monkeypatch):
        monkeypatch.setattr(agreements, "load_invariant", lambda: None)
        assert invariant_check.run_verification(ENV_SNAPSHOT) is None

    def test_receipt_has_no_invariant_key_when_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(receipts_mod, "RECEIPTS_DIR", tmp_path)
        monkeypatch.setattr(receipts_mod, "LOCK_PATH", tmp_path / ".chain.lock")

        receipts_mod.emit(
            action="start_project",
            target={"project": "test"},
            result={"status": "success"},
            env_before=ENV_SNAPSHOT,
            session_id="test_session",
            actor_type="test",
            agent_hint="test",
            env_after=ENV_SNAPSHOT,
            invariant=None,
        )

        files = list(tmp_path.glob("*.json"))
        assert len(files) == 1
        receipt = json.loads(files[0].read_text())
        assert "invariant" not in receipt


class TestPassingClaimConverges:
    def test_verified_and_converged(self, monkeypatch):
        contract = f'require env-facts is equal to "{FACTS}"'
        monkeypatch.setattr(agreements, "load_invariant", lambda: contract)

        block = invariant_check.run_verification(ENV_SNAPSHOT)

        assert block is not None
        assert block["converged"] is True
        assert len(block["claims"]) == 1
        assert block["claims"][0]["status"] == "verified"
        assert block["claims"][0]["escalation_reason"] is None

    def test_receipt_carries_the_block(self, tmp_path, monkeypatch):
        contract = f'require env-facts is equal to "{FACTS}"'
        monkeypatch.setattr(agreements, "load_invariant", lambda: contract)
        monkeypatch.setattr(receipts_mod, "RECEIPTS_DIR", tmp_path)
        monkeypatch.setattr(receipts_mod, "LOCK_PATH", tmp_path / ".chain.lock")

        block = invariant_check.run_verification(ENV_SNAPSHOT)
        receipts_mod.emit(
            action="start_project",
            target={"project": "test"},
            result={"status": "success"},
            env_before=ENV_SNAPSHOT,
            session_id="test_session",
            actor_type="test",
            agent_hint="test",
            env_after=ENV_SNAPSHOT,
            invariant=block,
        )

        files = list(tmp_path.glob("*.json"))
        receipt = json.loads(files[0].read_text())
        assert receipt["invariant"]["converged"] is True
        assert receipt["invariant"]["claims"][0]["status"] == "verified"


class TestFailingClaimEscalatesWithoutBlocking:
    def test_escalates(self, monkeypatch):
        contract = 'require env-facts is equal to "this-will-never-match"'
        monkeypatch.setattr(agreements, "load_invariant", lambda: contract)

        block = invariant_check.run_verification(ENV_SNAPSHOT)

        assert block is not None
        assert block["converged"] is False
        assert block["claims"][0]["status"] == "escalated"
        assert block["claims"][0]["escalation_reason"] in (
            "claim will not converge",
            "ground will not stabilize",
        )

    def test_action_result_and_receipt_are_unaffected(self, tmp_path, monkeypatch):
        """Escalation is recorded, not a reversal: the action's own result is
        unchanged and the receipt is still written."""
        contract = 'require env-facts is equal to "this-will-never-match"'
        monkeypatch.setattr(agreements, "load_invariant", lambda: contract)
        monkeypatch.setattr(receipts_mod, "RECEIPTS_DIR", tmp_path)
        monkeypatch.setattr(receipts_mod, "LOCK_PATH", tmp_path / ".chain.lock")

        action_result = {"status": "success"}
        block = invariant_check.run_verification(ENV_SNAPSHOT)
        receipts_mod.emit(
            action="start_project",
            target={"project": "test"},
            result=action_result,
            env_before=ENV_SNAPSHOT,
            session_id="test_session",
            actor_type="test",
            agent_hint="test",
            env_after=ENV_SNAPSHOT,
            invariant=block,
        )

        assert action_result == {"status": "success"}
        files = list(tmp_path.glob("*.json"))
        receipt = json.loads(files[0].read_text())
        assert receipt["result"] == {"status": "success"}
        assert receipt["invariant"]["claims"][0]["status"] == "escalated"


class TestHarnessUnavailable:
    def test_graceful_none_when_package_missing(self, monkeypatch):
        contract = f'require env-facts is equal to "{FACTS}"'
        monkeypatch.setattr(agreements, "load_invariant", lambda: contract)
        monkeypatch.setattr(invariant_check, "_INVARIANT_AVAILABLE", False)

        assert invariant_check.run_verification(ENV_SNAPSHOT) is None

    def test_harness_exception_is_caught_not_raised(self, monkeypatch):
        contract = f'require env-facts is equal to "{FACTS}"'
        monkeypatch.setattr(agreements, "load_invariant", lambda: contract)

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated harness failure")

        monkeypatch.setattr(invariant_check.invariant, "run", _boom)

        block = invariant_check.run_verification(ENV_SNAPSHOT)

        assert block is not None
        assert block["converged"] is False
        assert "simulated harness failure" in block["error"]


class TestBlockShapeMatchesPlatformValidator:
    def test_top_level_keys(self, monkeypatch):
        contract = f'require env-facts is equal to "{FACTS}"'
        monkeypatch.setattr(agreements, "load_invariant", lambda: contract)

        block = invariant_check.run_verification(ENV_SNAPSHOT)

        assert isinstance(block["claims"], list)
        assert isinstance(block["total_cycles"], int)
        assert isinstance(block["converged"], bool)
        assert isinstance(block["inherited_handlers"], list)
        assert isinstance(block["new_handlers"], list)
        assert "harness_version" in block
        assert "final" not in block

    def test_claim_shape(self, monkeypatch):
        contract = f'require env-facts is equal to "{FACTS}"'
        monkeypatch.setattr(agreements, "load_invariant", lambda: contract)

        block = invariant_check.run_verification(ENV_SNAPSHOT)
        claim = block["claims"][0]

        for key in ("name", "status", "escalation_reason", "cycles", "corrections", "handler"):
            assert key in claim
        assert claim["status"] in ("verified", "corrected", "escalated", "pending")

    def test_escalation_reason_set_iff_escalated(self, monkeypatch):
        passing = f'require env-facts is equal to "{FACTS}"'
        failing = 'require env-facts is equal to "this-will-never-match"'

        monkeypatch.setattr(agreements, "load_invariant", lambda: passing)
        pass_block = invariant_check.run_verification(ENV_SNAPSHOT)
        assert pass_block["claims"][0]["status"] == "verified"
        assert pass_block["claims"][0]["escalation_reason"] is None

        monkeypatch.setattr(agreements, "load_invariant", lambda: failing)
        fail_block = invariant_check.run_verification(ENV_SNAPSHOT)
        assert fail_block["claims"][0]["status"] == "escalated"
        assert fail_block["claims"][0]["escalation_reason"] is not None

    def test_converged_iff_every_claim_verified_or_corrected(self, monkeypatch):
        passing = f'require env-facts is equal to "{FACTS}"'
        failing = 'require env-facts is equal to "this-will-never-match"'

        monkeypatch.setattr(agreements, "load_invariant", lambda: passing)
        pass_block = invariant_check.run_verification(ENV_SNAPSHOT)
        assert pass_block["converged"] == all(
            c["status"] in ("verified", "corrected") for c in pass_block["claims"]
        )

        monkeypatch.setattr(agreements, "load_invariant", lambda: failing)
        fail_block = invariant_check.run_verification(ENV_SNAPSHOT)
        assert fail_block["converged"] == all(
            c["status"] in ("verified", "corrected") for c in fail_block["claims"]
        )
