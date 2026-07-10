# tests/test_dashboard_endpoints.py
"""Tests for the five read-only dashboard endpoints added to seshat.py:

    GET  /api/agreement
    GET  /api/revocations
    GET  /api/invariant
    GET  /api/invariant/last-run
    POST /api/agreement/check

These previously had only manual curl verification. Of particular note is
`_summarize_agreement_rules()`, which has a two-round bug history: an
initial fix over-corrected and flagged every well-formed rule as an error
(because enumeration never remembers actor/action/scope, so even a
perfectly well-formed rule comes back `status.name == "ERROR_SEMANTIC"`); a
second fix corrected this by gating error-detection on `r.canonical is
None` instead. `TestSummarizeAgreementRules.test_normal_multirule_agreement_renders_without_errors`
locks in that round-2 behavior.

Isolation: tests/conftest.py's autouse fixtures already neutralize
`agreements.load_revocations` / `agreements.load_invariant` for every test
in this suite. Tests below additionally monkeypatch `agreements.load_agreement`
and `seshat.RECEIPTS_DIR` wherever the handler under test touches them, so
nothing here ever reads or writes the real ~/.seshat/.
"""
import hashlib
import json

import pytest

import agreements
import seshat
from seshat import _summarize_agreement_rules


@pytest.fixture
def client():
    seshat.app.config["TESTING"] = True
    return seshat.app.test_client()


# ── _summarize_agreement_rules() — the round-2 regression guard ────────────


class TestSummarizeAgreementRules:
    def test_normal_multirule_agreement_renders_without_errors(self):
        """Both a well-formed permit and a well-formed forbid line must render
        their normal canonical/verb/window shape with NO "error" key — even
        though, absent any remembered actor/action facts, the interpreter
        reports ERROR_SEMANTIC status for each individually. This is exactly
        the round-1 regression: gating error-detection on r.status.name
        instead of `r.canonical is None` would flag both of these
        well-formed rules as errors.
        """
        text = '''
permit actor is "x" and action is "y"
forbid action is "z"
'''
        rules = _summarize_agreement_rules(text)
        assert len(rules) == 2

        permit_rule = rules[0]
        assert "error" not in permit_rule
        assert permit_rule["canonical"] == 'permit actor is x and action is y'
        assert permit_rule["verb"] == "permit"
        assert permit_rule["window"] == "unbounded"
        assert permit_rule["line"] == 2

        forbid_rule = rules[1]
        assert "error" not in forbid_rule
        assert forbid_rule["canonical"] == 'forbid action is z'
        assert forbid_rule["verb"] == "forbid"
        assert forbid_rule["window"] == "unbounded"
        assert forbid_rule["line"] == 3

    def test_malformed_text_returns_error_entry_not_exception(self):
        """Genuinely malformed input must come back as an {"error": ...}
        entry — never raise, never 500."""
        text = "permit actor frobnicates wildly"
        rules = _summarize_agreement_rules(text)
        assert len(rules) == 1
        assert "error" in rules[0]
        assert isinstance(rules[0]["error"], str)
        assert rules[0]["error"]  # non-empty


# ── GET /api/agreement ───────────────────────────────────────────────────


class TestGetAgreement:
    def test_exists_true_returns_text_and_parsed_rules(self, client, monkeypatch):
        text = 'permit actor is "claude-code" and action is "start_project"\n'
        monkeypatch.setattr(agreements, "load_agreement", lambda: text)

        resp = client.get("/api/agreement")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["exists"] is True
        assert data["text"] == text
        assert len(data["rules"]) == 1
        assert "error" not in data["rules"][0]
        assert data["rules"][0]["verb"] == "permit"

    def test_exists_false_when_no_agreement_file(self, client, monkeypatch):
        monkeypatch.setattr(agreements, "load_agreement", lambda: None)

        resp = client.get("/api/agreement")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data == {"exists": False, "text": None, "rules": []}


# ── GET /api/revocations ─────────────────────────────────────────────────


class TestGetRevocations:
    def test_exists_true_returns_text_rules_and_sync_state(self, client, monkeypatch, tmp_path):
        text = 'forbid action is "stop_orphan"\n'
        monkeypatch.setattr(agreements, "load_revocations", lambda: text)
        monkeypatch.setattr(
            agreements, "LAST_SYNCED_REVOCATIONS_PATH", tmp_path / "nonexistent" / ".marker"
        )

        resp = client.get("/api/revocations")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["exists"] is True
        assert data["text"] == text
        assert len(data["rules"]) == 1
        assert "error" not in data["rules"][0]
        assert data["sync"]["head_hash"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert data["sync"]["last_checked"] is None

    def test_exists_false_when_no_revocations_file(self, client, monkeypatch):
        monkeypatch.setattr(agreements, "load_revocations", lambda: None)

        resp = client.get("/api/revocations")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data == {"exists": False, "text": None, "rules": [], "sync": None}


# ── GET /api/invariant ───────────────────────────────────────────────────


class TestGetInvariant:
    def test_exists_true_returns_text(self, client, monkeypatch):
        text = "verify actor is claude-code\n"
        monkeypatch.setattr(agreements, "load_invariant", lambda: text)

        resp = client.get("/api/invariant")
        assert resp.status_code == 200
        assert resp.get_json() == {"exists": True, "text": text}

    def test_exists_false_when_no_invariant_file(self, client, monkeypatch):
        monkeypatch.setattr(agreements, "load_invariant", lambda: None)

        resp = client.get("/api/invariant")
        assert resp.status_code == 200
        assert resp.get_json() == {"exists": False, "text": None}


# ── GET /api/invariant/last-run ──────────────────────────────────────────


def _write_receipt(receipts_dir, filename, *, invariant=None, index=0):
    """Write a minimal but well-shaped receipt JSON file directly to disk —
    mirrors tests/test_receipts.py's _make_receipt helper, extended with an
    optional `invariant` block. Bypasses receipts.emit() entirely (that
    would pull in the real Registry/Scanner for its environment snapshot),
    so this stays a pure filesystem fixture, isolated by tmp_path."""
    receipt = {
        "type": "machine_action",
        "timestamp": f"2026-07-0{index + 1}T10:00:00.000000+00:00",
        "actor": {"type": "test", "session_id": "test_session", "agent_hint": "test"},
        "action": "start_project",
        "target": {"project": "test-project"},
        "result": {"status": "success"},
        "environment_before": {"listening_ports": [], "managed_projects": {}},
        "environment_after": {"listening_ports": [], "managed_projects": {}},
        "previous_hash": None,
    }
    if invariant is not None:
        receipt["invariant"] = invariant
    canonical = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    receipt["receipt_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    (receipts_dir / filename).write_text(json.dumps(receipt, indent=2))
    return receipt


class TestGetInvariantLastRun:
    def test_no_receipts_dir_at_all(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr(seshat, "RECEIPTS_DIR", tmp_path / "does-not-exist")

        resp = client.get("/api/invariant/last-run")
        assert resp.status_code == 200
        assert resp.get_json() == {"exists": False}

    def test_receipts_exist_but_none_carry_invariant_key(self, client, monkeypatch, tmp_path):
        receipts_dir = tmp_path / "receipts"
        receipts_dir.mkdir()
        _write_receipt(receipts_dir, "20260701T100000_start_project_00000001.json", index=0)
        _write_receipt(receipts_dir, "20260702T100000_start_project_00000002.json", index=1)
        monkeypatch.setattr(seshat, "RECEIPTS_DIR", receipts_dir)

        resp = client.get("/api/invariant/last-run")
        assert resp.status_code == 200
        assert resp.get_json() == {"exists": False}

    def test_receipt_with_invariant_block_is_returned(self, client, monkeypatch, tmp_path):
        receipts_dir = tmp_path / "receipts"
        receipts_dir.mkdir()
        invariant_block = {
            "claims": [],
            "total_cycles": 3,
            "converged": True,
            "inherited_handlers": [],
            "new_handlers": [],
            "harness_version": "0.1.0",
        }
        written = _write_receipt(
            receipts_dir,
            "20260703T100000_start_project_00000003.json",
            invariant=invariant_block,
            index=2,
        )
        monkeypatch.setattr(seshat, "RECEIPTS_DIR", receipts_dir)

        resp = client.get("/api/invariant/last-run")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["exists"] is True
        assert data["invariant"] == invariant_block
        assert data["receipt_timestamp"] == written["timestamp"]
        assert data["receipt_hash"] == written["receipt_hash"]


# ── POST /api/agreement/check ────────────────────────────────────────────


class TestAgreementCheck:
    AGREEMENT = '''
permit actor is "claude-code" and action is "start_project"
forbid action is "stop_orphan" because "orphan termination stays in the dashboard"
'''

    def test_missing_actor_returns_400(self, client):
        resp = client.post("/api/agreement/check", json={"action": "start_project"})
        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_missing_action_returns_400(self, client):
        resp = client.post("/api/agreement/check", json={"actor": "claude-code"})
        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_missing_body_returns_400(self, client):
        resp = client.post("/api/agreement/check", json={})
        assert resp.status_code == 400
        assert "error" in resp.get_json()

    def test_permit_match_allows(self, client, monkeypatch):
        monkeypatch.setattr(agreements, "load_agreement", lambda: self.AGREEMENT)

        resp = client.post(
            "/api/agreement/check",
            json={"actor": "claude-code", "action": "start_project"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["allowed"] is True
        assert data["mode"] == "permitted"
        assert data["rule"] == 'permit actor is claude-code and action is start_project'
        assert data["reason"] == "Permitted by Agreement."

    def test_forbid_match_denies(self, client, monkeypatch):
        monkeypatch.setattr(agreements, "load_agreement", lambda: self.AGREEMENT)

        resp = client.post(
            "/api/agreement/check",
            json={"actor": "claude-code", "action": "stop_orphan"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["allowed"] is False
        assert data["mode"] == "forbidden"
        assert data["rule"] is not None
        assert data["reason"]

    def test_no_matching_rule_default_denies(self, client, monkeypatch):
        monkeypatch.setattr(agreements, "load_agreement", lambda: self.AGREEMENT)

        resp = client.post(
            "/api/agreement/check",
            json={"actor": "claude-code", "action": "some_other_action"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["allowed"] is False
        assert data["mode"] == "default-deny"
