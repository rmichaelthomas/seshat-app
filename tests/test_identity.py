"""Tests for identity.py — HMAC capability-token (macaroon) issuance and
verification (identity-plane arc, Stage 1 / F-02 structural)."""
import base64
import json

import pytest

import agreements
import identity


def test_mint_returns_a_three_part_jwt_shaped_string():
    token = identity.mint("agent-x")
    parts = token.split(".")
    assert len(parts) == 3


def test_verify_accepts_a_freshly_minted_token():
    token = identity.mint("agent-x", ttl_hours=None)
    verified = identity.verify(token)
    assert verified is not None
    assert verified.identifier == "agent-x"
    assert verified.caveats == []
    assert verified.delegation_path == []


def test_verify_rejects_a_forged_signature():
    token = identity.mint("agent-x")
    header_b64, payload_b64, sig_b64 = token.split(".")
    # Flip the last character of the signature — still valid base64url,
    # but a different byte string, so the recomputed HMAC chain can't match.
    tampered_sig = ("A" if sig_b64[0] != "A" else "B") + sig_b64[1:]
    forged = f"{header_b64}.{payload_b64}.{tampered_sig}"
    assert identity.verify(forged) is None


def test_verify_rejects_an_appended_caveat_without_re_signing():
    """The append-only macaroon property: adding a caveat after signing
    must invalidate the token, since the signature only covers the
    caveats that existed when it was computed."""
    token = identity.mint("agent-x", caveats=['forbid action is "translate"'])
    header_b64, payload_b64, sig_b64 = token.split(".")
    padding = "=" * (-len(payload_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
    payload["caveats"].append('forbid action is "wipe_disk"')
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


def test_mint_is_not_deterministic_by_default_due_to_the_fresh_nonce():
    """Stage 3: mint() always mints a fresh nonce (identity-plane freshness/
    individual-token-revocability), so two calls with identical inputs now
    produce different tokens — the nonce is folded into the signed chain,
    so this is a deliberate change from Stage 1's determinism, not a bug.
    Passing the SAME explicit nonce is still deterministic (see below)."""
    t1 = identity.mint("agent-x", caveats=['forbid action is "wipe_disk"'], ttl_hours=None)
    t2 = identity.mint("agent-x", caveats=['forbid action is "wipe_disk"'], ttl_hours=None)
    assert t1 != t2


def test_mint_with_an_explicit_nonce_is_deterministic():
    t1 = identity.mint("agent-x", caveats=['forbid action is "wipe_disk"'], ttl_hours=None, nonce="fixed-nonce")
    t2 = identity.mint("agent-x", caveats=['forbid action is "wipe_disk"'], ttl_hours=None, nonce="fixed-nonce")
    assert t1 == t2


class TestCaveatLegality:
    """§5: the locked, decidable caveat subset. 'is one of {...}' set-
    membership is expressed as an OR-chain of equality clauses — the
    installed liminate==0.16.0 interpreter has no literal 'is one of {set}'
    token (confirmed: it raises ERROR_PARSE for that syntax), so membership
    is legal via repeated 'X is "a" or X is "b"' equality clauses, which
    the interpreter does support and which check_action already evaluates
    via auto_confirm_amber=True.

    Caveats are forbid-only — NOT 'forbid / permit' as the design's own
    §5 wording literally reads. A caveat is spliced into the exact same
    evaluation pool as the Agreement (§6), and Liminate's permit semantics
    are purely additive/non-blocking: a 'permit' caveat can therefore
    GRANT authority the Agreement never gave (confirmed empirically before
    writing this fix — see the PR body), inverting the one property a
    macaroon caveat must have. 'forbid' has no such path (forbid always
    wins, never grants), so it is the only safe caveat verb."""

    def test_identity_equality_is_legal(self):
        assert identity.is_legal_caveat('forbid actor is "agent-x"') is True

    def test_action_equality_is_legal(self):
        assert identity.is_legal_caveat('forbid action is "wipe_disk"') is True

    def test_action_set_membership_via_or_is_legal(self):
        assert identity.is_legal_caveat(
            'forbid action is "wipe_disk" or action is "delete_all"'
        ) is True

    def test_scope_equality_is_legal(self):
        assert identity.is_legal_caveat('forbid scope is "production"') is True

    def test_temporal_window_is_legal(self):
        assert identity.is_legal_caveat(
            'until "2099-01-01" forbid action is "wipe_disk"'
        ) is True
        assert identity.is_legal_caveat(
            'starting "2020-01-01" forbid action is "wipe_disk"'
        ) is True

    def test_forbid_over_actor_action_scope_is_legal(self):
        assert identity.is_legal_caveat(
            'forbid actor is "agent-x" and action is "wipe_disk" and scope is "production"'
        ) is True

    def test_permit_verb_is_illegal(self):
        """The critical, security-relevant case: a permit caveat must be
        rejected outright, since it can grant instead of restrict."""
        assert identity.is_legal_caveat('permit action is "wipe_disk"') is False
        assert identity.is_legal_caveat('permit actor is "agent-x"') is False

    def test_malformed_date_is_illegal(self):
        assert identity.is_legal_caveat(
            'until "not-a-date" forbid action is "wipe_disk"'
        ) is False

    def test_verb_other_is_illegal(self):
        assert identity.is_legal_caveat('remember a string called foo with "bar"') is False
        assert identity.is_legal_caveat('define something: nonsense') is False

    def test_unresolvable_external_predicate_is_illegal(self):
        assert identity.is_legal_caveat(
            'forbid action is "wipe_disk" and reviewed_by is "someone"'
        ) is False

    def test_multi_statement_line_is_illegal(self):
        assert identity.is_legal_caveat(
            'forbid action is "translate"\nforbid action is "wipe_disk"'
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
            'forbid actor is "agent-x"',
            'forbid action is "wipe_disk" or action is "delete_all"',
            'forbid scope is "production"',
            'forbid action is "wipe_disk"',
        ]
        for line in shapes:
            statements = amendment_diff.parse_statements(line)
            assert len(statements) == 1
            assert statements[0]["verb"] == "forbid"


class TestMintRejectsIllegalCaveats:
    def test_mint_raises_on_illegal_caveat(self):
        with pytest.raises(identity.IllegalCaveatError):
            identity.mint("agent-x", caveats=['remember a string called foo with "bar"'])

    def test_mint_succeeds_with_all_legal_caveats(self):
        token = identity.mint("agent-x", caveats=[
            'forbid action is "delete_all"',
            'until "2099-01-01" forbid action is "wipe_disk"',
        ])
        verified = identity.verify(token)
        assert verified is not None
        assert verified.caveats == [
            'forbid action is "delete_all"',
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


class TestAttenuation:
    """Identity-plane Stage 2: a token holder narrows its own token offline
    (no issuer round-trip) and can delegate it to a named sub-agent. The
    core invariant — a child token can only narrow authority, never
    broaden it — is enforced by the amendment_diff monotonicity classifier,
    not merely assumed from append-only structure."""

    def test_attenuate_appends_a_caveat_and_still_verifies(self):
        parent = identity.mint("agent-root", caveats=['forbid action is "wipe_disk"'], ttl_hours=None)
        child = identity.attenuate(parent, ['forbid action is "delete_all"'])
        verified = identity.verify(child)
        assert verified is not None
        assert verified.caveats == [
            'forbid action is "wipe_disk"',
            'forbid action is "delete_all"',
        ]

    def test_attenuate_without_delegate_to_keeps_identifier_and_empty_path(self):
        parent = identity.mint("agent-root")
        child = identity.attenuate(parent, ['forbid action is "wipe_disk"'])
        verified = identity.verify(child)
        assert verified.identifier == "agent-root"
        assert verified.delegation_path == []

    def test_attenuate_with_delegate_to_records_the_hop(self):
        parent = identity.mint("agent-root")
        child = identity.attenuate(parent, ['forbid action is "wipe_disk"'], delegate_to="agent-child")
        verified = identity.verify(child)
        assert verified is not None
        # The root stays the signed, Agreement-matching identity — see
        # the PR body for why this is a deliberate safety correction from
        # a literal "actor becomes the leaf" reading of the design.
        assert verified.identifier == "agent-root"
        assert verified.delegation_path == ["agent-root", "agent-child"]

    def test_two_hop_delegation_builds_the_full_path(self):
        root_token = identity.mint("agent-root")
        child_token = identity.attenuate(root_token, [], delegate_to="agent-child")
        grandchild_token = identity.attenuate(
            child_token, ['forbid action is "wipe_disk"'], delegate_to="agent-grandchild"
        )
        verified = identity.verify(grandchild_token)
        assert verified is not None
        assert verified.delegation_path == ["agent-root", "agent-child", "agent-grandchild"]
        assert verified.identifier == "agent-root"

    def test_attenuate_rejects_an_illegal_added_caveat(self):
        parent = identity.mint("agent-root")
        with pytest.raises(identity.IllegalCaveatError):
            identity.attenuate(parent, ['remember a string called foo with "bar"'])

    def test_attenuate_rejects_an_unverifiable_parent_token(self):
        parent = identity.mint("agent-root")
        header_b64, payload_b64, sig_b64 = parent.split(".")
        tampered_sig = ("A" if sig_b64[0] != "A" else "B") + sig_b64[1:]
        forged_parent = f"{header_b64}.{payload_b64}.{tampered_sig}"
        with pytest.raises(identity.IllegalCaveatError):
            identity.attenuate(forged_parent, ['forbid action is "wipe_disk"'])

    def test_attenuate_refuses_a_non_monotonic_classification(self, monkeypatch):
        """Defense in depth (§9 failure mode 1): prove the monotonicity
        assertion is actually wired up and respected, not just assumed
        safe because attenuate() only ever appends forbid-only caveats.
        Forces classify_monotonicity_from_changes to report broadening
        and confirms attenuate() refuses regardless."""
        import amendment_diff

        monkeypatch.setattr(amendment_diff, "classify_monotonicity_from_changes", lambda changes: "de-escalating")
        parent = identity.mint("agent-root")
        with pytest.raises(identity.IllegalCaveatError):
            identity.attenuate(parent, ['forbid action is "wipe_disk"'])

    def test_tampering_a_delegated_token_denies(self):
        parent = identity.mint("agent-root")
        child = identity.attenuate(parent, ['forbid action is "wipe_disk"'], delegate_to="agent-child")
        header_b64, payload_b64, sig_b64 = child.split(".")
        tampered_sig = ("A" if sig_b64[0] != "A" else "B") + sig_b64[1:]
        forged = f"{header_b64}.{payload_b64}.{tampered_sig}"
        assert identity.verify(forged) is None

    def test_delegate_to_cannot_escalate_via_an_unrelated_agreement_actor_rule(self):
        """The critical security regression test for this stage: a bare
        identity rename via delegate_to must NEVER unlock a DIFFERENT
        actor's Agreement-granted permissions. This is exactly why
        VerifiedIdentity.identifier stays the ROOT (see module docstring
        and the PR body) — check_action's actor-matching must never key
        off a self-chosen delegate_to string."""
        import agreements

        root_token = identity.mint("agent-root")
        # agent-root never had wipe_disk. "trusted-admin" does, in this
        # Agreement. Naively renaming to "trusted-admin" via delegate_to
        # must NOT grant wipe_disk to the holder of this token.
        escalated_token = identity.attenuate(root_token, [], delegate_to="trusted-admin")

        agreement = (
            'permit actor is "agent-root" and action is "translate"\n'
            'permit actor is "trusted-admin" and action is "wipe_disk"'
        )
        d = agreements.check_action(
            "ignored-untrusted-string", "wipe_disk", agreement_text=agreement, token=escalated_token
        )
        assert d.allowed is False
        assert d.mode == "default-deny"

        # The legitimately-scoped action still works, proving the token
        # is still usable for what agent-root actually holds.
        d2 = agreements.check_action(
            "ignored-untrusted-string", "translate", agreement_text=agreement, token=escalated_token
        )
        assert d2.allowed is True


class TestDefaultTTL:
    """Identity-plane Stage 3: short-lived-by-default issuance. The TTL
    mechanism is a 'starting <expiry-date> forbid actor is "<name>"'
    caveat — deliberately `starting`, not `until`: `until` would make a
    forbid go INERT after the date (backwards for token expiry). Confirmed
    against the real interpreter before implementation: a starting-dated
    forbid on just the actor fact (no action/scope in the predicate) is a
    blanket denial once its window opens, and agreements._temporal_window
    already reports 'active'/'future' correctly for it — zero changes
    needed to check_action's existing temporal-window logic."""

    def test_default_ttl_denies_once_expired(self):
        token = identity.mint("agent-x", ttl_hours=0)
        d = agreements.check_action(
            "agent-x", "translate",
            agreement_text='permit actor is "agent-x" and action is "translate"',
            token=token,
        )
        assert d.allowed is False

    def test_within_default_ttl_window_is_allowed(self):
        token = identity.mint("agent-x")  # default ttl (24h) — well within window
        d = agreements.check_action(
            "agent-x", "translate",
            agreement_text='permit actor is "agent-x" and action is "translate"',
            token=token,
        )
        assert d.allowed is True

    def test_ttl_none_mints_an_unbounded_token(self):
        token = identity.mint("agent-x", ttl_hours=None)
        verified = identity.verify(token)
        assert verified is not None
        assert verified.caveats == []

    def test_explicit_temporal_caveat_suppresses_the_default_ttl(self):
        """If the caller already supplied their own temporal caveat, mint()
        must not also append its own default — the caller took control."""
        token = identity.mint(
            "agent-x",
            caveats=['starting "2099-01-01" forbid action is "wipe_disk"'],
        )
        verified = identity.verify(token)
        assert verified.caveats == ['starting "2099-01-01" forbid action is "wipe_disk"']

    def test_a_past_ttl_does_not_affect_stage1_style_calls_with_ttl_none(self):
        """Backward compatible: an old-style mint (ttl_hours=None, as Stage
        1/2 always effectively were) never gets a forced expiry."""
        token = identity.mint("agent-x", ttl_hours=None)
        d = agreements.check_action(
            "agent-x", "translate",
            agreement_text='permit actor is "agent-x" and action is "translate"',
            token=token,
        )
        assert d.allowed is True


class TestNonceAndRevocationIdentifiers:
    """Identity-plane Stage 3: a nonce is folded into the signed HMAC
    chain (tamper-evident, like everything else), optional so pre-Stage-3
    tokens without one still verify unchanged, and used to make a single
    delegated token individually revocable without touching its agent
    name or siblings."""

    def test_mint_includes_a_nonce_by_default(self):
        token = identity.mint("agent-x")
        verified = identity.verify(token)
        assert verified.token_nonce is not None

    def test_nonce_is_tamper_evident(self):
        """Changing the nonce without the root key must break the
        signature, exactly like changing a caveat or the identifier
        would."""
        token = identity.mint("agent-x", ttl_hours=None, nonce="original-nonce")
        header_b64, payload_b64, sig_b64 = token.split(".")
        padding = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
        payload["nonce"] = "tampered-nonce"
        new_payload_b64 = base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True).encode()
        ).rstrip(b"=").decode("ascii")
        forged = f"{header_b64}.{new_payload_b64}.{sig_b64}"
        assert identity.verify(forged) is None

    def test_a_nonceless_token_still_verifies(self):
        """Backward compatibility: mint() always adds a nonce going
        forward, but verify() must not require one — a pre-Stage-3 token
        (hand-constructed here without a nonce) still verifies."""
        signature = identity._chain_signature("agent-x", [])
        token = identity._serialize("agent-x", "test-host", [], signature)
        verified = identity.verify(token)
        assert verified is not None
        assert verified.token_nonce is None

    def test_revocation_identifiers_undelegated_is_just_the_root_and_nonce(self):
        token = identity.mint("agent-x", ttl_hours=None, nonce="n1")
        verified = identity.verify(token)
        assert identity.revocation_identifiers(verified) == ["agent-x", "n1"]

    def test_revocation_identifiers_delegated_includes_the_full_path_and_nonce(self):
        root = identity.mint("agent-root", ttl_hours=None, nonce="root-nonce")
        child = identity.attenuate(root, [], delegate_to="agent-child")
        verified = identity.verify(child)
        ids = identity.revocation_identifiers(verified)
        assert ids[:2] == ["agent-root", "agent-child"]
        assert verified.token_nonce in ids
        assert verified.token_nonce != "root-nonce"  # attenuate gives the child a fresh nonce

    def test_attenuate_gives_the_child_a_fresh_nonce_not_the_parents(self):
        root = identity.mint("agent-root", ttl_hours=None, nonce="root-nonce")
        child = identity.attenuate(root, [], delegate_to="agent-child")
        verified_root = identity.verify(root)
        verified_child = identity.verify(child)
        assert verified_child.token_nonce != verified_root.token_nonce
