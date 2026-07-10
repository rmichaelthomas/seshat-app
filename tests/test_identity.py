"""Tests for identity.py — HMAC capability-token (macaroon) issuance and
verification (identity-plane arc, Stage 1 / F-02 structural)."""
import base64
import json

import pytest

import identity


def test_mint_returns_a_three_part_jwt_shaped_string():
    token = identity.mint("agent-x")
    parts = token.split(".")
    assert len(parts) == 3


def test_verify_accepts_a_freshly_minted_token():
    token = identity.mint("agent-x")
    verified = identity.verify(token)
    assert verified is not None
    assert verified.identifier == "agent-x"
    assert verified.caveats == []


def test_verify_rejects_a_forged_signature():
    token = identity.mint("agent-x")
    header_b64, payload_b64, sig_b64 = token.split(".")
    # Flip the last character of the signature — still valid base64url,
    # but a different byte string, so the recomputed HMAC chain can't match.
    tampered_sig = ("A" if sig_b64[-1] != "A" else "B") + sig_b64[1:]
    forged = f"{header_b64}.{payload_b64}.{tampered_sig}"
    assert identity.verify(forged) is None


def test_verify_rejects_an_appended_caveat_without_re_signing():
    """The append-only macaroon property: adding a caveat after signing
    must invalidate the token, since the signature only covers the
    caveats that existed when it was computed."""
    token = identity.mint("agent-x", caveats=['permit action is "translate"'])
    header_b64, payload_b64, sig_b64 = token.split(".")
    padding = "=" * (-len(payload_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
    payload["caveats"].append('permit action is "wipe_disk"')
    new_payload_b64 = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True).encode()
    ).rstrip(b"=").decode("ascii")
    forged = f"{header_b64}.{new_payload_b64}.{sig_b64}"
    assert identity.verify(forged) is None


def test_verify_rejects_malformed_token_strings():
    assert identity.verify("not-a-token") is None
    assert identity.verify("only.two") is None
    assert identity.verify("") is None
    assert identity.verify("a.b.c") is None  # invalid base64/json in every part


def test_mint_fails_closed_when_root_key_unavailable(monkeypatch):
    def _boom():
        raise RuntimeError("keychain locked")
    monkeypatch.setattr(identity, "_root_key", _boom)
    with pytest.raises(identity.IdentityKeyUnavailableError):
        identity.mint("agent-x")


def test_verify_fails_closed_when_root_key_unavailable(monkeypatch):
    token = identity.mint("agent-x")

    def _boom():
        raise RuntimeError("keychain locked")
    monkeypatch.setattr(identity, "_root_key", _boom)
    with pytest.raises(identity.IdentityKeyUnavailableError):
        identity.verify(token)


def test_mint_is_deterministic_for_same_identifier_and_caveats():
    """No nonce in the chain formula (§4 is explicit: sig_0 = HMAC(root_key,
    identifier)) — same inputs, same key, same token. This is intentional
    for Stage 1 (no expiry/lifecycle yet); Stage 3 owns freshness."""
    t1 = identity.mint("agent-x", caveats=['permit action is "translate"'])
    t2 = identity.mint("agent-x", caveats=['permit action is "translate"'])
    assert t1 == t2


class TestCaveatLegality:
    """§5: the locked, decidable caveat subset. 'is one of {...}' set-
    membership is expressed as an OR-chain of equality clauses — the
    installed liminate==0.16.0 interpreter has no literal 'is one of {set}'
    token (confirmed: it raises ERROR_PARSE for that syntax), so membership
    is legal via repeated 'X is "a" or X is "b"' equality clauses, which
    the interpreter does support and which check_action already evaluates
    via auto_confirm_amber=True."""

    def test_identity_equality_is_legal(self):
        assert identity.is_legal_caveat('permit actor is "agent-x"') is True

    def test_action_equality_is_legal(self):
        assert identity.is_legal_caveat('permit action is "translate"') is True

    def test_action_set_membership_via_or_is_legal(self):
        assert identity.is_legal_caveat(
            'permit action is "translate" or action is "summarize"'
        ) is True

    def test_scope_equality_is_legal(self):
        assert identity.is_legal_caveat('permit scope is "none"') is True

    def test_temporal_window_is_legal(self):
        assert identity.is_legal_caveat(
            'until "2099-01-01" forbid action is "wipe_disk"'
        ) is True
        assert identity.is_legal_caveat(
            'starting "2020-01-01" permit action is "translate"'
        ) is True

    def test_forbid_permit_over_actor_action_scope_is_legal(self):
        assert identity.is_legal_caveat(
            'permit actor is "agent-x" and action is "translate" and scope is "none"'
        ) is True

    def test_malformed_date_is_illegal(self):
        assert identity.is_legal_caveat(
            'until "not-a-date" forbid action is "wipe_disk"'
        ) is False

    def test_verb_other_is_illegal(self):
        assert identity.is_legal_caveat('remember a string called foo with "bar"') is False
        assert identity.is_legal_caveat('define something: nonsense') is False

    def test_unresolvable_external_predicate_is_illegal(self):
        assert identity.is_legal_caveat(
            'permit action is "translate" and reviewed_by is "someone"'
        ) is False

    def test_multi_statement_line_is_illegal(self):
        assert identity.is_legal_caveat(
            'permit action is "translate"\nforbid action is "wipe_disk"'
        ) is False

    def test_blank_and_comment_lines_are_illegal(self):
        assert identity.is_legal_caveat("") is False
        assert identity.is_legal_caveat("   ") is False
        assert identity.is_legal_caveat('-- a comment') is False

    def test_each_legal_shape_round_trips_through_parse_statements(self):
        """§5: 'A caveat line MUST parse cleanly through
        amendment_diff.parse_statements' — proven directly here, not just
        indirectly through is_legal_caveat's internal check."""
        import amendment_diff

        shapes = [
            'permit actor is "agent-x"',
            'permit action is "translate" or action is "summarize"',
            'permit scope is "none"',
            'forbid action is "wipe_disk"',
        ]
        for line in shapes:
            statements = amendment_diff.parse_statements(line)
            assert len(statements) == 1
            assert statements[0]["verb"] in ("forbid", "permit")


class TestMintRejectsIllegalCaveats:
    def test_mint_raises_on_illegal_caveat(self):
        with pytest.raises(identity.IllegalCaveatError):
            identity.mint("agent-x", caveats=['remember a string called foo with "bar"'])

    def test_mint_succeeds_with_all_legal_caveats(self):
        token = identity.mint("agent-x", caveats=[
            'permit action is "translate"',
            'until "2099-01-01" forbid action is "wipe_disk"',
        ])
        verified = identity.verify(token)
        assert verified is not None
        assert verified.caveats == [
            'permit action is "translate"',
            'until "2099-01-01" forbid action is "wipe_disk"',
        ]

    def test_verify_rejects_a_token_smuggling_an_illegal_caveat_past_a_future_mint_bug(self, monkeypatch):
        """Defense in depth (§5: 'Enforce at BOTH mint and verify'): even
        if mint() somehow signed an illegal caveat, verify() must still
        catch it and refuse the whole token — not trust that mint()
        already checked.

        Note: can't use monkeypatch.undo() here — the autouse
        _test_identity_root_key fixture shares this same monkeypatch
        instance (pytest caches one per test), so undo() would also
        revert the Keychain-isolation patch. Restore is_legal_caveat
        explicitly instead."""
        original = identity.is_legal_caveat
        monkeypatch.setattr(identity, "is_legal_caveat", lambda line: True)
        token = identity.mint("agent-x", caveats=['remember a string called foo with "bar"'])
        monkeypatch.setattr(identity, "is_legal_caveat", original)
        assert identity.verify(token) is None
