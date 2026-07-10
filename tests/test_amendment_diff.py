# tests/test_amendment_diff.py
#
# TI-Q7 (v1.0k §55-57) — this corpus is byte-identical (case-for-case) to
# liminate-dev/tests/test_differ.py's monotonicity corpus. Both must agree
# on every case: this file's classify_amendment() is a verbatim port of
# app/differ.py's, proven by the cross-repo classifier-parity gate (§9).
import pytest

from amendment_diff import (
    apply_delta,
    classify_amendment,
    classify_monotonicity,
    classify_monotonicity_from_changes,
    diff_statements,
    entrenchment_violations,
)

_PERMIT_START = 'permit actor is "claude-code" and action is "start_project"\n'
_FORBID_STOP_ORPHAN = 'forbid action is "stop_orphan" because "orphan termination stays in the dashboard"\n'

MONOTONICITY_CASES = [
    pytest.param(
        _PERMIT_START,
        _PERMIT_START + 'forbid action is "delete_prod"\n',
        "monotonic",
        id="added-forbid",
    ),
    pytest.param(
        _PERMIT_START,
        _PERMIT_START + 'require approval is equal to "yes"\n',
        "monotonic",
        id="added-require",
    ),
    pytest.param(
        _PERMIT_START + 'permit actor is "claude-code" and action is "stop_project"\n',
        _PERMIT_START,
        "monotonic",
        id="removed-permit",
    ),
    pytest.param(
        "require quantity at least 10\n", "require quantity at least 20\n",
        "monotonic", id="require-tightened-floor",
    ),
    pytest.param(
        "require latency at most 200\n", "require latency at most 100\n",
        "monotonic", id="require-tightened-ceiling",
    ),
    pytest.param(
        _PERMIT_START + _FORBID_STOP_ORPHAN, _PERMIT_START + _FORBID_STOP_ORPHAN,
        "monotonic", id="noop-all-unchanged",
    ),
    pytest.param(
        _PERMIT_START + _FORBID_STOP_ORPHAN, _PERMIT_START,
        "de-escalating", id="removed-forbid",
    ),
    pytest.param(
        _PERMIT_START, _PERMIT_START + 'permit actor is "claude-code" and action is "wipe_disk"\n',
        "de-escalating", id="added-permit",
    ),
    pytest.param(
        'require approval is equal to "yes"\n' + _PERMIT_START, _PERMIT_START,
        "de-escalating", id="removed-require",
    ),
    pytest.param(
        "require quantity at least 10\n", "require quantity at least 5\n",
        "de-escalating", id="require-loosened-floor",
    ),
    pytest.param(
        "require latency at most 100\n", "require latency at most 200\n",
        "de-escalating", id="require-loosened-ceiling",
    ),
    pytest.param(
        "require score changes by 10\n", "require score changes by 20\n",
        "de-escalating", id="require-ambiguous-threshold",
    ),
    pytest.param(
        "forbid deleting_prod always\n", "forbid deleting_prod always unless override_approved\n",
        "de-escalating", id="forbid-gained-unless",
    ),
    pytest.param(
        _PERMIT_START, 'permit actor is "claude-code" and scope is "any"\n',
        "de-escalating", id="modified-permit-not-otherwise-classified",
    ),
    pytest.param(
        _PERMIT_START, _PERMIT_START + 'forbid action is "delete_prod"\n' + 'require approval is equal to "yes"\n',
        "monotonic", id="mixed-all-monotonic",
    ),
    pytest.param(
        _PERMIT_START,
        _PERMIT_START + 'forbid action is "delete_prod"\n' + 'permit actor is "claude-code" and action is "wipe_disk"\n',
        "de-escalating", id="mixed-one-de-escalating-taints-whole",
    ),
]


@pytest.mark.parametrize("before,after,expected", MONOTONICITY_CASES)
def test_classify_monotonicity(before, after, expected):
    assert classify_monotonicity(before, after) == expected


def test_classify_monotonicity_from_changes_matches_source_based():
    before, after, expected = MONOTONICITY_CASES[0].values
    changes = diff_statements(before, after)
    assert classify_monotonicity_from_changes(changes) == expected == classify_monotonicity(before, after)


def test_entrenchment_violations_flags_removal_of_protected_quoted_literal():
    before = _PERMIT_START + _FORBID_STOP_ORPHAN
    after = _PERMIT_START
    violations = entrenchment_violations(before, after, {("forbid", "stop_orphan")})
    assert violations == [("forbid", "stop_orphan")]


def test_entrenchment_violations_empty_when_key_untouched():
    before = _PERMIT_START + _FORBID_STOP_ORPHAN
    after = before + 'forbid action is "delete_prod"\n'
    assert entrenchment_violations(before, after, {("forbid", "stop_orphan")}) == []


def test_classify_amendment_entrenchment_takes_precedence_over_monotonic():
    before = _PERMIT_START + 'require approval is equal to "yes"\n'
    after = before + 'require secondary_approval is equal to "yes"\n'
    result = classify_amendment(before, after, {("require", "secondary_approval")})
    assert result["class"] == "entrenched-violation"
    assert result["violations"] == [("require", "secondary_approval")]


def test_classify_amendment_no_entrenchment_falls_through_to_monotonicity():
    before = _PERMIT_START + _FORBID_STOP_ORPHAN
    after = _PERMIT_START
    result = classify_amendment(before, after, set())
    assert result == {"class": "de-escalating"}


# ── apply_delta() — local-only proposed-source construction ───────────────

def test_apply_delta_adds_and_removes_exact_lines():
    before = _PERMIT_START + _FORBID_STOP_ORPHAN
    after = apply_delta(before, additions=['forbid action is "delete_prod"'], removals=[_FORBID_STOP_ORPHAN.strip()])
    assert "delete_prod" in after
    assert "stop_orphan" not in after
    assert _PERMIT_START.strip() in after


def test_apply_delta_no_match_removal_is_a_noop():
    before = _PERMIT_START
    after = apply_delta(before, additions=[], removals=["forbid action is \"nonexistent\""])
    assert after.strip() == before.strip()


def test_apply_delta_empty_additions_are_skipped():
    before = _PERMIT_START
    after = apply_delta(before, additions=["", "   "], removals=[])
    assert after.strip() == before.strip()
