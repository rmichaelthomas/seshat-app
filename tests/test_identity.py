"""Tests for identity.py — capability-token (macaroon) issuance and
verification (identity-plane arc). ID-Q4 Phase 1 replaced the HMAC
macaroon chain with an Ed25519 next-key block chain as the alg mint()
produces; the legacy HS256-macaroon path is preserved unchanged (§7) and
exercised here via hand-built tokens (TestLegacyHmacInterop)."""
import base64
import json
import secrets

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

import agreements
import identity

# Captured at import time, before any per-test autouse fixture (see
# conftest.py's _test_identity_root_signing_key) monkeypatches these names
# to a fixed keypair — TestEd25519RootKeyManagement restores the real
# implementations temporarily to exercise the actual Keychain-backed
# generate-once-then-persist logic against an in-memory keyring stand-in.
_REAL_ROOT_SIGNING_KEY = identity._root_signing_key
_REAL_ROOT_PUBLIC_KEY = identity._root_public_key


def _decode_part(b64_part):
    padding = "=" * (-len(b64_part) % 4)
    return json.loads(base64.urlsafe_b64decode(b64_part + padding))


def _encode_part(obj):
    return base64.urlsafe_b64encode(
        json.dumps(obj, sort_keys=True).encode()
    ).rstrip(b"=").decode("ascii")


def _legacy_mint(identifier, caveats=None, *, nonce=None, location="test-host"):
    """Hand-construct a pre-ID-Q4-Phase-1 HS256-macaroon token, exactly as
    the old mint() body did — mint() itself only ever produces EdDSA-chain
    tokens now (§7: 'no flag to mint a legacy token'), so this is how a
    test proves the legacy path still verifies unchanged."""
    caveats = list(caveats or [])
    if nonce is None:
        nonce = secrets.token_hex(8)
    signature = identity._chain_signature(identifier, caveats, nonce=nonce)
    return identity._serialize(identifier, location, caveats, signature, nonce=nonce)


def test_mint_returns_a_three_part_jwt_shaped_string():
    token, holder_key = identity.mint("agent-x")
    parts = token.split(".")
    assert len(parts) == 3
    assert holder_key is not None


def test_verify_accepts_a_freshly_minted_token():
    token, _holder_key = identity.mint("agent-x", ttl_hours=None)
    verified = identity.verify(token)
    assert verified is not None
    assert verified.identifier == "agent-x"
    assert verified.caveats == []
    assert verified.delegation_path == []


def test_verify_rejects_a_forged_signature():
    token, _holder_key = identity.mint("agent-x")
    header_b64, payload_b64, sig_b64 = token.split(".")
    # Flip the first character of the signature part — still valid
    # base64url, but a different underlying byte string either way: it
    # fails to decode as the expected JSON signature list, or it decodes
    # to a list whose signature(s) no longer verify.
    tampered_sig = ("A" if sig_b64[0] != "A" else "B") + sig_b64[1:]
    forged = f"{header_b64}.{payload_b64}.{tampered_sig}"
    assert identity.verify(forged) is None


def test_verify_rejects_an_appended_caveat_without_re_signing():
    """The append-only macaroon property: adding a caveat after signing
    must invalidate the token, since the signature only covers the block
    content that existed when it was computed."""
    token, _holder_key = identity.mint("agent-x", caveats=['forbid action is "translate"'])
    header_b64, payload_b64, sig_b64 = token.split(".")
    payload = _decode_part(payload_b64)
    payload["blocks"][0]["caveats"].append('forbid action is "wipe_disk"')
    new_payload_b64 = _encode_part(payload)
    forged = f"{header_b64}.{new_payload_b64}.{sig_b64}"
    assert identity.verify(forged) is None


def test_verify_rejects_malformed_token_strings():
    assert identity.verify("not-a-token") is None
    assert identity.verify("only.two") is None
    assert identity.verify("") is None
    assert identity.verify("a.b.c") is None  # invalid base64/json in every part


def test_mint_is_not_deterministic_by_default_due_to_the_fresh_nonce():
    """Stage 3: mint() always mints a fresh nonce (identity-plane freshness/
    individual-token-revocability), so two calls with identical inputs now
    produce different tokens — the nonce is folded into the signed chain,
    so this is a deliberate change from Stage 1's determinism, not a bug.
    Passing the SAME explicit nonce is still deterministic (see below)."""
    t1, _ = identity.mint("agent-x", caveats=['forbid action is "wipe_disk"'], ttl_hours=None)
    t2, _ = identity.mint("agent-x", caveats=['forbid action is "wipe_disk"'], ttl_hours=None)
    assert t1 != t2


def test_mint_with_an_explicit_nonce_shares_the_nonce_but_not_the_full_token():
    """ID-Q4 Phase 1 change: unlike the legacy HMAC path, an EdDSA-chain
    mint() is NEVER byte-for-byte deterministic even with an explicit
    nonce — block 0 always embeds a freshly generated, random holder
    keypair (next_key) as part of the signed content, and a predictable
    holder key would be a forgeable one. The NONCE itself is still exactly
    what was passed, both times."""
    t1, _ = identity.mint("agent-x", caveats=['forbid action is "wipe_disk"'], ttl_hours=None, nonce="fixed-nonce")
    t2, _ = identity.mint("agent-x", caveats=['forbid action is "wipe_disk"'], ttl_hours=None, nonce="fixed-nonce")
    assert t1 != t2
    v1, v2 = identity.verify(t1), identity.verify(t2)
    assert v1.token_nonce == v2.token_nonce == "fixed-nonce"


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
    wins, never grants), so it is the only safe caveat verb.

    Alg-agnostic: is_legal_caveat() never touches mint/verify/attenuate,
    so it is identical for both the legacy and Ed25519 paths (unchanged
    by ID-Q4 Phase 1)."""

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
        token, _holder_key = identity.mint("agent-x", caveats=[
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
        _test_identity_root_key/_test_identity_root_signing_key fixtures
        share this same monkeypatch instance (pytest caches one per
        test), so undo() would also revert the Keychain-isolation patch.
        Restore is_legal_caveat explicitly instead."""
        original = identity.is_legal_caveat
        monkeypatch.setattr(identity, "is_legal_caveat", lambda line: True)
        token, _holder_key = identity.mint("agent-x", caveats=['remember a string called foo with "bar"'])
        monkeypatch.setattr(identity, "is_legal_caveat", original)
        assert identity.verify(token) is None


class TestAttenuation:
    """Identity-plane Stage 2, extended by ID-Q4 Phase 1: a token holder
    narrows its own token offline (no issuer round-trip) and can delegate
    it to a named sub-agent. The core invariant — a child token can only
    narrow authority, never broaden it — is enforced by the amendment_diff
    monotonicity classifier for both algorithms, AND, for EdDSA-chain
    tokens specifically, by the cryptographic fact that only a block
    signed by the current holder's own key (never the root's) can extend
    the chain at all (see TestEd25519HolderSideAttenuation)."""

    def test_attenuate_appends_a_caveat_and_still_verifies(self):
        parent, parent_key = identity.mint("agent-root", caveats=['forbid action is "wipe_disk"'], ttl_hours=None)
        child, _child_key = identity.attenuate(
            parent, ['forbid action is "delete_all"'], holder_private_key=parent_key,
        )
        verified = identity.verify(child)
        assert verified is not None
        assert verified.caveats == [
            'forbid action is "wipe_disk"',
            'forbid action is "delete_all"',
        ]

    def test_attenuate_without_delegate_to_keeps_identifier_and_empty_path(self):
        parent, parent_key = identity.mint("agent-root")
        child, _child_key = identity.attenuate(
            parent, ['forbid action is "wipe_disk"'], holder_private_key=parent_key,
        )
        verified = identity.verify(child)
        assert verified.identifier == "agent-root"
        assert verified.delegation_path == []

    def test_attenuate_with_delegate_to_records_the_hop(self):
        parent, parent_key = identity.mint("agent-root")
        child, _child_key = identity.attenuate(
            parent, ['forbid action is "wipe_disk"'], delegate_to="agent-child", holder_private_key=parent_key,
        )
        verified = identity.verify(child)
        assert verified is not None
        # The root stays the signed, Agreement-matching identity — see
        # the PR body for why this is a deliberate safety correction from
        # a literal "actor becomes the leaf" reading of the design.
        assert verified.identifier == "agent-root"
        assert verified.delegation_path == ["agent-root", "agent-child"]

    def test_two_hop_delegation_builds_the_full_path(self):
        root_token, root_key = identity.mint("agent-root")
        child_token, child_key = identity.attenuate(
            root_token, [], delegate_to="agent-child", holder_private_key=root_key,
        )
        grandchild_token, _grandchild_key = identity.attenuate(
            child_token, ['forbid action is "wipe_disk"'], delegate_to="agent-grandchild",
            holder_private_key=child_key,
        )
        verified = identity.verify(grandchild_token)
        assert verified is not None
        assert verified.delegation_path == ["agent-root", "agent-child", "agent-grandchild"]
        assert verified.identifier == "agent-root"

    def test_attenuate_rejects_an_illegal_added_caveat(self):
        parent, parent_key = identity.mint("agent-root")
        with pytest.raises(identity.IllegalCaveatError):
            identity.attenuate(
                parent, ['remember a string called foo with "bar"'], holder_private_key=parent_key,
            )

    def test_attenuate_rejects_an_unverifiable_parent_token(self):
        parent, parent_key = identity.mint("agent-root")
        header_b64, payload_b64, sig_b64 = parent.split(".")
        tampered_sig = ("A" if sig_b64[0] != "A" else "B") + sig_b64[1:]
        forged_parent = f"{header_b64}.{payload_b64}.{tampered_sig}"
        with pytest.raises(identity.IllegalCaveatError):
            identity.attenuate(
                forged_parent, ['forbid action is "wipe_disk"'], holder_private_key=parent_key,
            )

    def test_attenuate_refuses_a_non_monotonic_classification(self, monkeypatch):
        """Defense in depth (§9 failure mode 1): prove the monotonicity
        assertion is actually wired up and respected, not just assumed
        safe because attenuate() only ever appends forbid-only caveats.
        Forces classify_monotonicity_from_changes to report broadening
        and confirms attenuate() refuses regardless."""
        import amendment_diff

        monkeypatch.setattr(amendment_diff, "classify_monotonicity_from_changes", lambda changes: "de-escalating")
        parent, parent_key = identity.mint("agent-root")
        with pytest.raises(identity.IllegalCaveatError):
            identity.attenuate(
                parent, ['forbid action is "wipe_disk"'], holder_private_key=parent_key,
            )

    def test_tampering_a_delegated_token_denies(self):
        parent, parent_key = identity.mint("agent-root")
        child, _child_key = identity.attenuate(
            parent, ['forbid action is "wipe_disk"'], delegate_to="agent-child", holder_private_key=parent_key,
        )
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
        root_token, root_key = identity.mint("agent-root")
        # agent-root never had wipe_disk. "trusted-admin" does, in this
        # Agreement. Naively renaming to "trusted-admin" via delegate_to
        # must NOT grant wipe_disk to the holder of this token.
        escalated_token, _key = identity.attenuate(
            root_token, [], delegate_to="trusted-admin", holder_private_key=root_key,
        )

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
    needed to check_action's existing temporal-window logic. Unchanged by
    ID-Q4 Phase 1 — the TTL caveat is applied before block construction,
    identically for both algorithms."""

    def test_default_ttl_denies_once_expired(self):
        token, _key = identity.mint("agent-x", ttl_hours=0)
        d = agreements.check_action(
            "agent-x", "translate",
            agreement_text='permit actor is "agent-x" and action is "translate"',
            token=token,
        )
        assert d.allowed is False

    def test_within_default_ttl_window_is_allowed(self):
        token, _key = identity.mint("agent-x")  # default ttl (24h) — well within window
        d = agreements.check_action(
            "agent-x", "translate",
            agreement_text='permit actor is "agent-x" and action is "translate"',
            token=token,
        )
        assert d.allowed is True

    def test_ttl_none_mints_an_unbounded_token(self):
        token, _key = identity.mint("agent-x", ttl_hours=None)
        verified = identity.verify(token)
        assert verified is not None
        assert verified.caveats == []

    def test_explicit_temporal_caveat_suppresses_the_default_ttl(self):
        """If the caller already supplied their own temporal caveat, mint()
        must not also append its own default — the caller took control."""
        token, _key = identity.mint(
            "agent-x",
            caveats=['starting "2099-01-01" forbid action is "wipe_disk"'],
        )
        verified = identity.verify(token)
        assert verified.caveats == ['starting "2099-01-01" forbid action is "wipe_disk"']

    def test_a_past_ttl_does_not_affect_stage1_style_calls_with_ttl_none(self):
        """Backward compatible: an old-style mint (ttl_hours=None, as Stage
        1/2 always effectively were) never gets a forced expiry."""
        token, _key = identity.mint("agent-x", ttl_hours=None)
        d = agreements.check_action(
            "agent-x", "translate",
            agreement_text='permit actor is "agent-x" and action is "translate"',
            token=token,
        )
        assert d.allowed is True


class TestNonceAndRevocationIdentifiers:
    """Identity-plane Stage 3: a nonce is folded into the signed block
    (tamper-evident, like everything else), optional only on the legacy
    path (so pre-Stage-3 tokens without one still verify unchanged), and
    used to make a single delegated token individually revocable without
    touching its agent name or siblings."""

    def test_mint_includes_a_nonce_by_default(self):
        token, _key = identity.mint("agent-x")
        verified = identity.verify(token)
        assert verified.token_nonce is not None

    def test_nonce_is_tamper_evident(self):
        """Changing the nonce without the holder key must break the
        signature, exactly like changing a caveat or next_key would."""
        token, _key = identity.mint("agent-x", ttl_hours=None, nonce="original-nonce")
        header_b64, payload_b64, sig_b64 = token.split(".")
        payload = _decode_part(payload_b64)
        payload["blocks"][0]["nonce"] = "tampered-nonce"
        new_payload_b64 = _encode_part(payload)
        forged = f"{header_b64}.{new_payload_b64}.{sig_b64}"
        assert identity.verify(forged) is None

    def test_a_nonceless_token_still_verifies(self):
        """Backward compatibility, legacy path only: verify() must not
        require a nonce for an HS256-macaroon token — a pre-Stage-3 token
        (hand-constructed here without a nonce) still verifies. EdDSA-chain
        tokens always carry one (mint() never omits it), so this
        specifically exercises _chain_signature/_serialize directly."""
        signature = identity._chain_signature("agent-x", [])
        token = identity._serialize("agent-x", "test-host", [], signature)
        verified = identity.verify(token)
        assert verified is not None
        assert verified.token_nonce is None

    def test_revocation_identifiers_undelegated_is_just_the_root_and_nonce(self):
        token, _key = identity.mint("agent-x", ttl_hours=None, nonce="n1")
        verified = identity.verify(token)
        assert identity.revocation_identifiers(verified) == ["agent-x", "n1"]

    def test_revocation_identifiers_delegated_includes_the_full_path_and_nonce(self):
        root, root_key = identity.mint("agent-root", ttl_hours=None, nonce="root-nonce")
        child, _child_key = identity.attenuate(root, [], delegate_to="agent-child", holder_private_key=root_key)
        verified = identity.verify(child)
        ids = identity.revocation_identifiers(verified)
        assert ids[:2] == ["agent-root", "agent-child"]
        assert verified.token_nonce in ids
        assert verified.token_nonce != "root-nonce"  # attenuate gives the child a fresh nonce

    def test_attenuate_gives_the_child_a_fresh_nonce_not_the_parents(self):
        root, root_key = identity.mint("agent-root", ttl_hours=None, nonce="root-nonce")
        child, _child_key = identity.attenuate(root, [], delegate_to="agent-child", holder_private_key=root_key)
        verified_root = identity.verify(root)
        verified_child = identity.verify(child)
        assert verified_child.token_nonce != verified_root.token_nonce


class TestEd25519RootKeyManagement:
    """ID-Q4 Phase 1 §4: the root Ed25519 keypair is Keychain-backed,
    generated once and reused, mirroring _root_key()'s exact flow. These
    tests bypass the conftest-level fixed-keypair fixture (which exists so
    every OTHER test's mint/verify calls don't depend on Keychain at all)
    to exercise the real generate-once-then-persist logic against an
    in-memory keyring stand-in."""

    def _patch_keyring(self, monkeypatch):
        store = {}
        monkeypatch.setattr(
            identity.keyring, "get_password", lambda service, item: store.get((service, item))
        )
        monkeypatch.setattr(
            identity.keyring, "set_password",
            lambda service, item, value: store.__setitem__((service, item), value),
        )
        # Undo the conftest-level fixed-keypair monkeypatch for this test
        # only, so calls below hit the real Keychain-backed implementation
        # (against the in-memory store above) instead of the fixture's
        # bypass — see the module-level _REAL_ROOT_SIGNING_KEY comment.
        monkeypatch.setattr(identity, "_root_signing_key", _REAL_ROOT_SIGNING_KEY)
        monkeypatch.setattr(identity, "_root_public_key", _REAL_ROOT_PUBLIC_KEY)
        return store

    def test_generates_once_and_persists(self, monkeypatch):
        self._patch_keyring(monkeypatch)
        key1 = identity._root_signing_key()
        key2 = identity._root_signing_key()
        assert identity._private_key_hex(key1) == identity._private_key_hex(key2)

    def test_generation_also_backfills_the_public_item(self, monkeypatch):
        store = self._patch_keyring(monkeypatch)
        key = identity._root_signing_key()
        assert store[(identity.MAC_SERVICE_NAME, identity.ROOT_SIGNING_PUBLIC_KEY_ITEM)] == (
            identity._public_key_hex(key.public_key())
        )

    def test_root_public_key_never_needs_the_private_item(self, monkeypatch):
        store = self._patch_keyring(monkeypatch)
        identity._root_signing_key()  # populate both items once

        def _boom(service, item):
            if item == identity.ROOT_SIGNING_KEY_ITEM:
                raise RuntimeError("private key item unreadable")
            return store.get((service, item))
        monkeypatch.setattr(identity.keyring, "get_password", _boom)

        pub = identity._root_public_key()
        assert identity._public_key_hex(pub) == store[(identity.MAC_SERVICE_NAME, identity.ROOT_SIGNING_PUBLIC_KEY_ITEM)]

    def test_uses_a_distinct_keychain_item_from_the_legacy_hmac_key(self):
        """§11 failure mode #4: reusing ROOT_KEY_ITEM would stop every
        legacy HS256-macaroon token from verifying."""
        assert identity.ROOT_SIGNING_KEY_ITEM != identity.ROOT_KEY_ITEM
        assert identity.ROOT_SIGNING_PUBLIC_KEY_ITEM != identity.ROOT_KEY_ITEM

    def test_root_public_key_hex_is_64_hex_chars(self):
        pub_hex = identity.root_public_key_hex()
        assert len(pub_hex) == 64
        int(pub_hex, 16)  # raises ValueError if not valid hex

    def test_root_public_key_hex_fails_closed_when_unavailable(self, monkeypatch):
        def _boom():
            raise RuntimeError("keychain locked")
        monkeypatch.setattr(identity, "_root_public_key", _boom)
        with pytest.raises(identity.IdentityKeyUnavailableError):
            identity.root_public_key_hex()

    def test_mint_fails_closed_when_root_signing_key_unavailable(self, monkeypatch):
        def _boom():
            raise RuntimeError("keychain locked")
        monkeypatch.setattr(identity, "_root_signing_key", _boom)
        with pytest.raises(identity.IdentityKeyUnavailableError):
            identity.mint("agent-x")

    def test_verify_fails_closed_when_root_public_key_unavailable(self, monkeypatch):
        token, _key = identity.mint("agent-x")

        def _boom():
            raise RuntimeError("keychain locked")
        monkeypatch.setattr(identity, "_root_public_key", _boom)
        with pytest.raises(identity.IdentityKeyUnavailableError):
            identity.verify(token)


class TestEd25519HolderSideAttenuation:
    """ID-Q4 Phase 1's thesis (§10 benchmarks 1, 2, 3, 4, 6): a holder
    attenuates its own token using ONLY its own private key, and a
    verifier checks a chain using ONLY the root's PUBLIC key — the root
    private key is cryptographically unnecessary for either operation,
    not merely unused by convention (as the legacy HMAC path's docstring
    caveated the same property for that model)."""

    def test_attenuate_succeeds_with_the_root_private_key_unavailable(self, monkeypatch):
        """§10 benchmark 1."""
        token, holder_key = identity.mint("agent-root")

        def _boom():
            raise RuntimeError("keychain locked — simulating an unavailable root private key")
        monkeypatch.setattr(identity, "_root_signing_key", _boom)

        child, child_key = identity.attenuate(
            token, ['forbid action is "wipe_disk"'], delegate_to="agent-child",
            holder_private_key=holder_key,
        )
        assert child is not None
        assert child_key is not None

    def test_attenuate_never_reads_the_root_signing_key(self, monkeypatch):
        """Stronger than 'still works if unavailable': proves attenuate()
        never even ATTEMPTS to read the root private key."""
        token, holder_key = identity.mint("agent-root")
        calls = []
        monkeypatch.setattr(identity, "_root_signing_key", lambda: calls.append(True))
        identity.attenuate(token, ['forbid action is "wipe_disk"'], holder_private_key=holder_key)
        assert calls == []

    def test_verify_succeeds_with_the_root_private_key_unavailable(self, monkeypatch):
        """§10 benchmark 6 — the cross-org property: verification needs
        only the root PUBLIC key, never the private one."""
        token, holder_key = identity.mint("agent-root", ttl_hours=None)
        child, _child_key = identity.attenuate(
            token, ['forbid action is "wipe_disk"'], holder_private_key=holder_key,
        )

        def _boom():
            raise RuntimeError("keychain locked — simulating an unavailable root private key")
        monkeypatch.setattr(identity, "_root_signing_key", _boom)

        verified = identity.verify(child)
        assert verified is not None
        assert verified.caveats == ['forbid action is "wipe_disk"']

    def test_forged_block_signed_by_an_unrelated_key_is_rejected(self):
        """§10 benchmark 2."""
        token, _holder_key = identity.mint("agent-root")
        header_b64, payload_b64, _sig_b64 = token.split(".")
        payload = _decode_part(payload_b64)

        unrelated_key = ed25519.Ed25519PrivateKey.generate()
        forged_sig = unrelated_key.sign(identity._canonical_block(payload["blocks"][0]))
        forged_sig_part = _encode_part([identity._b64(forged_sig)])
        forged = f"{header_b64}.{payload_b64}.{forged_sig_part}"
        assert identity.verify(forged) is None

    def test_caveat_removal_from_a_signed_block_is_detected(self):
        """§10 benchmark 3."""
        token, _holder_key = identity.mint(
            "agent-root", caveats=['forbid action is "wipe_disk"'], ttl_hours=None,
        )
        header_b64, payload_b64, sig_b64 = token.split(".")
        payload = _decode_part(payload_b64)
        payload["blocks"][0]["caveats"] = []  # strip the caveat, keep the old signature
        new_payload_b64 = _encode_part(payload)
        forged = f"{header_b64}.{new_payload_b64}.{sig_b64}"
        assert identity.verify(forged) is None

    def test_root_anchoring_survives_two_delegation_hops(self):
        """§10 benchmark 4 / §9 invariant 2 (v1.0n catch #2: leaf-rename
        escalation) — identifier is ALWAYS block 0's, at any depth."""
        root_token, root_key = identity.mint("agent-root")
        child_token, child_key = identity.attenuate(
            root_token, [], delegate_to="agent-child", holder_private_key=root_key,
        )
        grandchild_token, _key = identity.attenuate(
            child_token, ['forbid action is "wipe_disk"'], delegate_to="agent-grandchild",
            holder_private_key=child_key,
        )
        verified = identity.verify(grandchild_token)
        assert verified is not None
        assert verified.identifier == "agent-root"
        assert verified.delegation_path == ["agent-root", "agent-child", "agent-grandchild"]

    def test_next_key_tamper_is_detected(self):
        """§11 failure mode #6: next_key must be covered by the signature
        — swapping it post-signing (to redirect who could extend the
        chain) must break verification."""
        token, _holder_key = identity.mint("agent-root")
        header_b64, payload_b64, sig_b64 = token.split(".")
        payload = _decode_part(payload_b64)
        attacker_key = ed25519.Ed25519PrivateKey.generate()
        payload["blocks"][0]["next_key"] = identity._public_key_hex(attacker_key.public_key())
        new_payload_b64 = _encode_part(payload)
        forged = f"{header_b64}.{new_payload_b64}.{sig_b64}"
        assert identity.verify(forged) is None

    def test_attenuate_rejects_a_holder_key_that_does_not_match_next_key(self):
        token, _holder_key = identity.mint("agent-root")
        wrong_key = ed25519.Ed25519PrivateKey.generate()
        with pytest.raises(identity.IllegalCaveatError):
            identity.attenuate(
                token, ['forbid action is "wipe_disk"'],
                holder_private_key=identity._private_key_hex(wrong_key),
            )

    def test_attenuate_requires_a_holder_private_key_for_an_eddsa_token(self):
        token, _holder_key = identity.mint("agent-root")
        with pytest.raises(identity.IllegalCaveatError):
            identity.attenuate(token, ['forbid action is "wipe_disk"'])

    def test_attenuate_rejects_a_malformed_holder_private_key(self):
        token, _holder_key = identity.mint("agent-root")
        with pytest.raises(identity.IllegalCaveatError):
            identity.attenuate(
                token, ['forbid action is "wipe_disk"'], holder_private_key="not-valid-hex",
            )


class TestHolderKeyEnvFallback:
    """ID-Q4 Phase 1 resolution for a gap discovered against the frozen
    mcp_server.py: attenuate_identity's signature has no parameter to
    carry a holder private key. attenuate() falls back to
    SESHAT_IDENTITY_HOLDER_KEY, mirroring exactly how SESHAT_IDENTITY_
    TOKEN already reaches that tool — a human provisions both env vars
    for an agent's MCP session, and the agent narrows its own session
    token. See test_mcp_enforcement_gate.py::TestDelegation for the
    MCP-level exercise of this same fallback."""

    def test_attenuate_falls_back_to_the_env_var_when_no_argument_given(self, monkeypatch):
        token, holder_key = identity.mint("agent-root", ttl_hours=None)
        monkeypatch.setenv(identity.HOLDER_KEY_ENV_VAR, holder_key)
        child, _child_key = identity.attenuate(token, ['forbid action is "wipe_disk"'])
        verified = identity.verify(child)
        assert verified is not None
        assert verified.caveats == ['forbid action is "wipe_disk"']

    def test_explicit_argument_takes_precedence_over_the_env_var(self, monkeypatch):
        token, holder_key = identity.mint("agent-root")
        monkeypatch.setenv(identity.HOLDER_KEY_ENV_VAR, "not-even-valid-hex")
        child, _child_key = identity.attenuate(
            token, ['forbid action is "wipe_disk"'], holder_private_key=holder_key,
        )
        assert identity.verify(child) is not None

    def test_still_fails_closed_without_the_env_var_or_argument(self, monkeypatch):
        token, _holder_key = identity.mint("agent-root")
        monkeypatch.delenv(identity.HOLDER_KEY_ENV_VAR, raising=False)
        with pytest.raises(identity.IllegalCaveatError):
            identity.attenuate(token, ['forbid action is "wipe_disk"'])

    def test_env_var_is_ignored_for_a_legacy_token(self, monkeypatch):
        """Legacy HS256-macaroon attenuation never uses a holder key at
        all — an env var set for an unrelated EdDSA session must not
        change legacy behavior."""
        parent = _legacy_mint("agent-root")
        monkeypatch.setenv(identity.HOLDER_KEY_ENV_VAR, "irrelevant-to-the-legacy-path")
        child, holder_key = identity.attenuate(parent, ['forbid action is "wipe_disk"'])
        assert holder_key is None
        assert identity.verify(child) is not None


class TestAlgConfusion:
    """§11 failure mode #8: an HMAC signature must never be accepted by
    the Ed25519 path, or vice versa — dispatch is strictly on header.alg,
    and an unrecognized alg must deny."""

    def test_header_claims_hmac_but_payload_is_eddsa_shaped_is_rejected(self):
        token, _key = identity.mint("agent-x")
        header_b64, payload_b64, sig_b64 = token.split(".")
        header = _decode_part(header_b64)
        header["alg"] = identity._ALG_HMAC
        forged_header_b64 = _encode_part(header)
        forged = f"{forged_header_b64}.{payload_b64}.{sig_b64}"
        assert identity.verify(forged) is None

    def test_header_claims_eddsa_but_payload_is_hmac_shaped_is_rejected(self):
        legacy_token = _legacy_mint("agent-x")
        header_b64, payload_b64, sig_b64 = legacy_token.split(".")
        header = _decode_part(header_b64)
        header["alg"] = identity._ALG_ED25519
        forged_header_b64 = _encode_part(header)
        forged = f"{forged_header_b64}.{payload_b64}.{sig_b64}"
        assert identity.verify(forged) is None

    def test_unrecognized_alg_is_rejected(self):
        token, _key = identity.mint("agent-x")
        header_b64, payload_b64, sig_b64 = token.split(".")
        header = _decode_part(header_b64)
        header["alg"] = "some-other-alg"
        forged_header_b64 = _encode_part(header)
        forged = f"{forged_header_b64}.{payload_b64}.{sig_b64}"
        assert identity.verify(forged) is None


class TestLegacyHmacInterop:
    """§7: tokens minted before ID-Q4 Phase 1 carry alg HS256-macaroon and
    must continue to verify unchanged — same field semantics, same
    delegation/attenuation behavior. Constructed via the legacy helpers
    directly (_chain_signature/_serialize), exactly matching what the
    pre-build mint() body produced."""

    def test_legacy_token_verifies_with_the_pre_build_field_semantics(self):
        token = _legacy_mint("agent-x", caveats=['forbid action is "wipe_disk"'], nonce="legacy-nonce")
        verified = identity.verify(token)
        assert verified is not None
        assert verified.identifier == "agent-x"
        assert verified.caveats == ['forbid action is "wipe_disk"']
        assert verified.delegation_path == []
        assert verified.token_nonce == "legacy-nonce"

    def test_legacy_token_attenuates_via_the_unchanged_root_key_path(self):
        parent = _legacy_mint("agent-root")
        child, holder_key = identity.attenuate(parent, ['forbid action is "wipe_disk"'])
        assert holder_key is None
        verified = identity.verify(child)
        assert verified is not None
        assert verified.caveats == ['forbid action is "wipe_disk"']

    def test_legacy_token_delegation_still_works(self):
        parent = _legacy_mint("agent-root")
        child, _key = identity.attenuate(parent, [], delegate_to="agent-child")
        verified = identity.verify(child)
        assert verified.identifier == "agent-root"
        assert verified.delegation_path == ["agent-root", "agent-child"]

    def test_holder_private_key_is_ignored_for_a_legacy_token(self):
        """§6: 'holder_private_key is ignored when the input token's alg
        is HS256-macaroon' — passing a bogus one must not raise."""
        parent = _legacy_mint("agent-root")
        child, holder_key = identity.attenuate(
            parent, ['forbid action is "wipe_disk"'], holder_private_key="not-even-valid-hex",
        )
        assert holder_key is None
        assert identity.verify(child) is not None

    def test_legacy_verify_fails_closed_when_root_key_unavailable(self, monkeypatch):
        token = _legacy_mint("agent-x")

        def _boom():
            raise RuntimeError("keychain locked")
        monkeypatch.setattr(identity, "_root_key", _boom)
        with pytest.raises(identity.IdentityKeyUnavailableError):
            identity.verify(token)

    def test_legacy_mint_fails_closed_when_root_key_unavailable(self, monkeypatch):
        def _boom():
            raise RuntimeError("keychain locked")
        monkeypatch.setattr(identity, "_root_key", _boom)
        with pytest.raises(identity.IdentityKeyUnavailableError):
            identity._chain_signature("agent-x", [])

    def test_legacy_forgery_still_rejected(self):
        token = _legacy_mint("agent-x")
        header_b64, payload_b64, sig_b64 = token.split(".")
        tampered_sig = ("A" if sig_b64[0] != "A" else "B") + sig_b64[1:]
        forged = f"{header_b64}.{payload_b64}.{tampered_sig}"
        assert identity.verify(forged) is None
