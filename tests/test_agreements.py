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
