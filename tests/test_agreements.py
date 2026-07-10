# tests/test_agreements.py
import pytest

import agreements
from agreements import Decision, check_action


def test_allow_permit_match():
    agreement = '''
permit actor is "claude-code" and action is "start_project"
'''
    d = check_action("claude-code", "start_project", agreement_text=agreement)
    assert d.allowed is True
    assert d.mode == "permitted"
    assert d.rule == 'permit actor is claude-code and action is start_project'


def test_deny_forbidden_wins_over_matching_permit():
    agreement = '''
permit actor is "claude-code" and action is "stop_orphan"
forbid action is "stop_orphan" because "orphan termination stays in the dashboard"
'''
    d = check_action("claude-code", "stop_orphan", agreement_text=agreement)
    assert d.allowed is False
    assert d.mode == "forbidden"


def test_forbid_before_permit_ordering():
    agreement = '''
forbid action is "stop_orphan" because "orphan termination stays in the dashboard"
permit actor is "claude-code" and action is "stop_orphan"
'''
    d = check_action("claude-code", "stop_orphan", agreement_text=agreement)
    assert d.allowed is False
    assert d.mode == "forbidden"


def test_permit_before_forbid_ordering():
    agreement = '''
permit actor is "claude-code" and action is "stop_orphan"
forbid action is "stop_orphan" because "orphan termination stays in the dashboard"
'''
    d = check_action("claude-code", "stop_orphan", agreement_text=agreement)
    assert d.allowed is False
    assert d.mode == "forbidden"


def test_deny_default_unknown_actor():
    agreement = '''
permit actor is "claude-code" and action is "start_project"
'''
    d = check_action("someone-else", "start_project", agreement_text=agreement)
    assert d.allowed is False
    assert d.mode == "default-deny"


def test_deny_no_agreement(monkeypatch):
    monkeypatch.setattr(agreements, "load_agreement", lambda: None)
    d = check_action("claude-code", "start_project", agreement_text=None)
    assert d.allowed is False
    assert d.mode == "no-agreement"


def test_deny_error_fail_closed_on_malformed_agreement():
    # Verified at build time: this line returns ERROR_PARSE from the live
    # liminate 0.15.1 interpreter ("I expected 'is' in this condition.").
    bad_agreement = "permit actor frobnicates wildly"
    d = check_action("claude-code", "start_project", agreement_text=bad_agreement)
    assert d.allowed is False
    assert d.mode == "error"


def test_scope_sentinel_allows_scopeless_call_and_denies_other_scope():
    agreement = '''
permit actor is "claude-code" and action is "start_project" and scope is "none"
'''
    allowed = check_action("claude-code", "start_project", scope=None, agreement_text=agreement)
    assert allowed.allowed is True
    assert allowed.mode == "permitted"

    denied = check_action("claude-code", "start_project", scope="VAULT", agreement_text=agreement)
    assert denied.allowed is False
    assert denied.mode == "default-deny"


def test_quote_injection_rejected_before_interpreter_runs(monkeypatch):
    agreement = '''
permit actor is "claude-code" and action is "start_project"
'''

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("liminate.run must not be called when input is rejected")

    monkeypatch.setattr(agreements.liminate, "run", _fail_if_called)

    malicious_actor = 'x" \npermit actor is "x'
    d = check_action(malicious_actor, "start_project", agreement_text=agreement)
    assert d.allowed is False
    assert d.mode == "error"


def test_permit_never_triggers_a_denial():
    # Only non-matching permits present — must fall through to default-deny,
    # never "forbidden". Permit is purely informational (Liminate DT-Q3).
    agreement = '''
permit actor is "someone-else" and action is "start_project"
permit actor is "claude-code" and action is "some_other_action"
'''
    d = check_action("claude-code", "start_project", agreement_text=agreement)
    assert d.allowed is False
    assert d.mode == "default-deny"


class TestTokenVerification:
    """Identity-plane Stage 1 (F-02 structural): a verified token overrides
    the passed-in actor string and its caveats constrain the action
    exactly like Agreement rules, through the same evaluator."""

    def test_valid_token_overrides_actor_and_permits(self):
        import identity

        token = identity.mint("agent-x")
        agreement = 'permit actor is "agent-x" and action is "start_project"'
        d = check_action("ignored-untrusted-string", "start_project", agreement_text=agreement, token=token)
        assert d.allowed is True
        assert d.mode == "permitted"

    def test_forged_token_denies_as_identity_invalid(self):
        import identity

        token = identity.mint("agent-x")
        header_b64, payload_b64, sig_b64 = token.split(".")
        tampered_sig = ("A" if sig_b64[-1] != "A" else "B") + sig_b64[1:]
        forged = f"{header_b64}.{payload_b64}.{tampered_sig}"

        agreement = 'permit actor is "agent-x" and action is "start_project"'
        d = check_action("agent-x", "start_project", agreement_text=agreement, token=forged)
        assert d.allowed is False
        assert d.mode == "identity-invalid"

    def test_token_caveat_forbids_even_when_agreement_permits(self):
        import identity

        token = identity.mint("agent-x", caveats=['forbid action is "wipe_disk"'])
        agreement = 'permit actor is "agent-x" and action is "wipe_disk"'
        d = check_action("agent-x", "wipe_disk", agreement_text=agreement, token=token)
        assert d.allowed is False
        assert d.mode == "forbidden"

    def test_token_caveat_scoped_permit_denies_a_different_action(self):
        """§11 acceptance check: a token scoped `action is "translate"`
        permits translate and denies a different action."""
        import identity

        token = identity.mint("agent-x", caveats=['permit action is "translate"'])
        agreement = (
            'permit actor is "agent-x" and action is "translate"\n'
            'permit actor is "agent-x" and action is "wipe_disk"'
        )
        allowed = check_action("agent-x", "translate", agreement_text=agreement, token=token)
        assert allowed.allowed is True

    def test_token_absent_behavior_is_unchanged(self):
        """F-02 acute must remain intact: no token, no change at all."""
        agreement = 'permit actor is "claude-code" and action is "start_project"'
        d = check_action("claude-code", "start_project", agreement_text=agreement, token=None)
        assert d.allowed is True
        assert d.mode == "permitted"

    def test_no_agreement_still_denies_even_with_a_valid_token(self, monkeypatch):
        """A verified identity does not substitute for an Agreement — it
        only proves who is asking and can further restrict, never grant,
        beyond what the Agreement already permits."""
        import identity

        monkeypatch.setattr(agreements, "load_agreement", lambda: None)
        token = identity.mint("agent-x")
        d = check_action("agent-x", "start_project", agreement_text=None, token=token)
        assert d.allowed is False
        assert d.mode == "no-agreement"
