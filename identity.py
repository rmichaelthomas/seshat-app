#!/usr/bin/env python3
"""
identity.py — HMAC capability-token (macaroon) issuance and verification.

Stage 1 of the identity-plane arc (F-02 structural half): an agent presents
an unforgeable token instead of a self-declared MCP_AGENT_HINT string, and
the harness verifies it locally against a Keychain-held root key — no
network round-trip, no external identity provider.

Structure mirrors a macaroon: identifier + location + an ordered list of
caveats, bound by an HMAC chain (sig_0 = HMAC(root_key, identifier),
sig_i = HMAC(sig_{i-1}, caveat_i)). Appending a caveat requires the prior
signature; removing or reordering one breaks the chain — the same
append-only property Stage 2 (delegation) needs to attenuate a token
without a redesign.

Serialization is a JWT-shaped compact form (base64url header.payload.
signature) per the JOSE-representability requirement — the signature
itself is the macaroon chain (not a single HMAC over header+payload, so it
will not verify against a standard JWT library), but a later asymmetric
upgrade only changes the signature algorithm, not this three-part shape.

Same Keychain service receipts.py/vault.py use, a distinct key item — one
key-storage mechanism in this codebase, not two.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import socket
from dataclasses import dataclass, field
from pathlib import Path

import keyring
import liminate

import amendment_diff

MAC_SERVICE_NAME = "seshat"
ROOT_KEY_ITEM = "identity_root_key"

IDENTITY_DIR = Path.home() / ".seshat" / "identity"

_ALG = "HS256-macaroon"
_TYP = "SIT"  # Seshat Identity Token

# Identity-plane Stage 2 (delegation): a delegation hop is recorded as an
# ordinary, legal forbid caveat over the actor fact — no special-casing in
# is_legal_caveat is needed, and it rides the same HMAC chain (tamper-
# evident) as every other caveat. The marker never matches a real actor
# value at enforcement time (real agent names never carry this prefix),
# so it is inert with respect to enforcement and exists purely as an
# embedded, verifiable audit record.
_DELEGATION_MARKER_PREFIX = "seshat-delegate:"
_DELEGATION_MARKER_RE = re.compile(r'^forbid actor is "seshat-delegate:(.+)"$')


class IdentityKeyUnavailableError(RuntimeError):
    """The identity root key could not be obtained. mint()/verify() refuse
    to operate without it — fail closed, mirroring receipts.py's
    ReceiptKeyUnavailableError (F-01 pattern). Never falls back to an
    unkeyed or default signature."""


class IllegalCaveatError(ValueError):
    """A caveat line falls outside the locked decidable subset (§5).
    Raised by mint() before any signing happens — an illegal caveat must
    never be baked into a token in the first place."""


@dataclass
class VerifiedIdentity:
    identifier: str
    caveats: list[str] = field(default_factory=list)
    # [] when the token has never been delegated; else [root, ..., leaf].
    # `identifier` above is ALWAYS the root (the signed payload identifier,
    # unchanged across every attenuation hop) — see attenuate()'s docstring
    # for why: check_action's Agreement-matching actor must never key off
    # a self-chosen delegate_to string, or a holder could rename itself to
    # an unrelated, more-privileged Agreement actor and inherit permissions
    # the root never had. The leaf (delegation_path[-1]) is for audit/
    # receipt display only (see mcp_server.py's _agreement_actor).
    delegation_path: list[str] = field(default_factory=list)


def _root_key() -> bytes:
    """Per-install HMAC root key, Keychain-backed via `keyring` — generated
    once and stored the same way receipts.py stores its MAC key. Any
    failure here propagates to the caller; mint()/verify() convert that
    into the fail-closed IdentityKeyUnavailableError."""
    raw = keyring.get_password(MAC_SERVICE_NAME, ROOT_KEY_ITEM)
    if not raw:
        raw = secrets.token_hex(32)
        keyring.set_password(MAC_SERVICE_NAME, ROOT_KEY_ITEM, raw)
    return bytes.fromhex(raw)


def _chain_signature(identifier: str, caveats: list[str]) -> bytes:
    try:
        key = _root_key()
    except Exception as exc:
        raise IdentityKeyUnavailableError(
            "Cannot mint or verify an identity token: the Keychain-backed "
            "root key is unavailable. Refusing to operate unkeyed."
        ) from exc
    sig = hmac.new(key, identifier.encode("utf-8"), hashlib.sha256).digest()
    for caveat in caveats:
        sig = hmac.new(sig, caveat.encode("utf-8"), hashlib.sha256).digest()
    return sig


def _location() -> str:
    return socket.gethostname()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + padding)


# ── Caveat legality (§5 locked subset) ──────────────────────────────────────

def _strip_temporal_prefix(line: str) -> str:
    """Return LINE with a leading 'starting "<date>"' / 'until "<date>"'
    prefix removed (both may be present, in either order — the grammar
    allows stacking them). This mirrors the same token-skip loop already
    duplicated between agreements._temporal_window and agreements._verb_of
    — it is NOT a second date parser (no date validity check happens
    here); date validity is checked separately via agreements._temporal_window
    on the ORIGINAL, unstripped line before this function is ever called.
    """
    words = line.split()
    i = 0
    while i < len(words) - 1 and words[i] in ("starting", "until"):
        i += 2
    return " ".join(words[i:])


def is_legal_caveat(line: str) -> bool:
    """True only for the locked §5 subset: an optional temporal prefix,
    followed by exactly one **forbid** statement whose predicate resolves
    fully against just the (actor, action, scope) facts — proven by
    actually running it through the real liminate interpreter with those
    three facts remembered as dummy probe values, the same shape
    agreements.check_action() composes at real enforcement time. This is
    deliberately NOT a hand-rolled predicate grammar: reusing the real
    evaluator is what proves offline decidability, rather than asserting
    it.

    `permit` is deliberately NOT a legal caveat verb, despite the design
    naming "forbid / permit" as the allow-deny shape: Liminate composes a
    caveat into the SAME flat evaluation pool as the Agreement (§6), and
    its `permit` semantics are purely additive/non-blocking (confirmed:
    `check_action`'s permit-scan grants on ANY matching permit result,
    regardless of source) — so a `permit` caveat can GRANT authority the
    Agreement never gave, inverting the one property a macaroon caveat
    must have: it can only narrow authority, never widen it. A `forbid`
    caveat has no such escalation path (forbid always wins, never grants),
    so it is the only verb that can safely appear in a caveat. See the PR
    body for the concrete escalation this closes.

    Fails closed (False) for: a malformed date, more or fewer than one
    parsed statement, a verb other than forbid (including 'permit' and
    'other'), or a predicate referencing anything the interpreter can't
    resolve from just actor/action/scope (an unbounded/external
    predicate).
    """
    # Deferred import: agreements.py imports this module at load time to
    # call identity.verify(); importing agreements at identity.py's module
    # level would cycle back before agreements.py finishes initializing.
    # By the time this function is actually CALLED, both modules have
    # fully loaded, so the import here is safe and cheap (cached).
    import agreements

    stripped = line.strip()
    if not stripped or stripped.startswith("--"):
        return False

    # Cardinality check against the ORIGINAL text, before any temporal-
    # prefix stripping: _strip_temporal_prefix joins on whitespace, which
    # would silently collapse an embedded newline into a space and hide a
    # smuggled second statement from the count below.
    if len(amendment_diff.parse_statements(stripped)) != 1:
        return False

    if agreements._temporal_window(stripped) == "malformed":
        return False

    remainder = _strip_temporal_prefix(stripped)
    statements = amendment_diff.parse_statements(remainder)
    if len(statements) != 1:
        return False
    if statements[0]["verb"] != "forbid":
        return False

    probe = (
        'remember a string called actor with "__seshat_probe_actor__"\n'
        'remember a string called action with "__seshat_probe_action__"\n'
        'remember a string called scope with "__seshat_probe_scope__"\n'
        f"{stripped}\n"
    )
    try:
        result = liminate.run(probe, enter_phase2=False, auto_confirm_amber=True)
    except Exception:
        return False
    return not any(r.status.name in agreements._ERROR_STATUS_NAMES for r in result.results)


# ── Mint / verify ────────────────────────────────────────────────────────────

def _serialize(identifier: str, location: str, caveats: list[str], signature: bytes) -> str:
    header = {"alg": _ALG, "typ": _TYP}
    payload = {"identifier": identifier, "location": location, "caveats": caveats}
    return ".".join((
        _b64(json.dumps(header, sort_keys=True).encode("utf-8")),
        _b64(json.dumps(payload, sort_keys=True).encode("utf-8")),
        _b64(signature),
    ))


def mint(identifier: str, caveats: list[str] | None = None, *, location: str | None = None) -> str:
    """Build and sign a new capability token for IDENTIFIER. Human-initiated
    only (never called from an MCP-reachable path — see cli.py's `identity
    mint`, which is the only caller outside tests).

    Every caveat must pass is_legal_caveat() — the whole mint call raises
    IllegalCaveatError if any one doesn't, rather than silently dropping
    the offending line.
    """
    caveats = list(caveats or [])
    for line in caveats:
        if not is_legal_caveat(line):
            raise IllegalCaveatError(
                f"Caveat is outside the locked decidable subset (§5): {line!r}"
            )

    loc = location or _location()
    signature = _chain_signature(identifier, caveats)
    return _serialize(identifier, loc, caveats, signature)


def _verify_raw(token: str) -> tuple[str, str, list[str]] | None:
    """Internal: verify TOKEN's structure and signature, returning the raw
    (identifier, location, caveats) straight from the payload — before any
    delegation-path derivation. attenuate() uses this to recover the true
    signing identifier (the root, which never changes across delegation
    hops), separately from VerifiedIdentity.identifier (also the root, but
    exposed via the public verify()). Same fail-closed rules as verify():
    structural problems return None; IdentityKeyUnavailableError
    propagates uncaught.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    header_b64, payload_b64, sig_b64 = parts

    try:
        header = json.loads(_b64d(header_b64))
        payload = json.loads(_b64d(payload_b64))
        signature = _b64d(sig_b64)
    except Exception:
        return None

    if header.get("alg") != _ALG or header.get("typ") != _TYP:
        return None

    identifier = payload.get("identifier")
    location = payload.get("location")
    caveats = payload.get("caveats")
    if not isinstance(identifier, str) or not isinstance(location, str) or not isinstance(caveats, list):
        return None
    if not all(isinstance(c, str) for c in caveats):
        return None

    for line in caveats:
        if not is_legal_caveat(line):
            return None

    expected = _chain_signature(identifier, caveats)
    if not hmac.compare_digest(expected, signature):
        return None

    return identifier, location, caveats


def _delegation_path(root_identifier: str, caveats: list[str]) -> list[str]:
    """[] if CAVEATS carries no delegation markers (never delegated);
    else [root_identifier, hop_1, ..., hop_n] in the order the markers
    were appended (attenuate() only ever appends, so this is also
    chronological delegation order)."""
    hops = [m.group(1) for c in caveats if (m := _DELEGATION_MARKER_RE.match(c.strip()))]
    if not hops:
        return []
    return [root_identifier, *hops]


def verify(token: str) -> VerifiedIdentity | None:
    """Recompute the HMAC chain from the root key and compare, timing-safe,
    against the token's signature. Any structural problem (wrong part
    count, bad base64, bad JSON, missing field, unrecognized alg, an
    illegal caveat, or a signature mismatch) returns None — deny, don't
    raise — EXCEPT IdentityKeyUnavailableError, which propagates uncaught
    exactly like receipts.ReceiptKeyUnavailableError does through emit():
    a missing key is an infrastructure fail-closed condition, not a
    per-token verdict.
    """
    raw = _verify_raw(token)
    if raw is None:
        return None
    identifier, _location, caveats = raw
    return VerifiedIdentity(
        identifier=identifier,
        caveats=caveats,
        delegation_path=_delegation_path(identifier, caveats),
    )


def attenuate(token: str, added_caveats: list[str] | None = None, *, delegate_to: str | None = None) -> str:
    """Narrow TOKEN by appending ADDED_CAVEATS (and, if DELEGATE_TO is
    given, a delegation-marker caveat naming the sub-agent this narrower
    token is being handed to), then recompute the signature chain over the
    full, extended caveat list.

    Agent-reachable (unlike mint): a delegating agent narrows its own
    token at runtime to hand to a sub-agent. This is safe because
    attenuation can ONLY narrow, never broaden — enforced three ways:
    added caveats must pass is_legal_caveat (forbid-only, same gate as
    mint), they are APPENDED, never inserted/removed (the parent's caveat
    list is an immutable prefix of the child's), and the whole change is
    asserted `monotonic` via amendment_diff.classify_monotonicity_from_changes
    before a token is produced — enforced-and-tested, not merely assumed
    from the append-only structure.

    Design note on the key: in this single-trust-domain model the harness
    holds the root key and both mints and verifies, so this recomputes the
    ENTIRE HMAC chain against the root key rather than doing a holder-
    side-only append the way a distributed macaroon/Biscuit would need to
    (where the holder lacks the root key). The append-only property here
    is an enforced invariant of THIS function (the monotonicity assertion
    below), not a cryptographic guarantee that a holder without the root
    key couldn't forge — a holder-side-only, root-key-less attenuation is
    the asymmetric upgrade explicitly deferred to Stage 3.C / future work.

    Design note on delegate_to: it renames only the audit-visible LEAF
    (VerifiedIdentity.delegation_path[-1]) — the token's signed, Agreement-
    matching identifier (VerifiedIdentity.identifier) always stays the
    ROOT. A free-form actor rename that Agreement-matching itself trusted
    would let any holder of a valid token rename itself to an unrelated,
    more-privileged Agreement actor and inherit permissions the root never
    had — the caveat-monotonicity check only examines caveats, not a bare
    identity swap. See is_legal_caveat's and VerifiedIdentity's docstrings,
    and the PR body, for the full reasoning.
    """
    raw = _verify_raw(token)
    if raw is None:
        raise IllegalCaveatError(
            "Cannot attenuate: the input token does not verify (invalid, "
            "forged, or already carrying an illegal caveat)."
        )
    root_identifier, location, parent_caveats = raw

    new_caveats = list(added_caveats or [])
    if delegate_to is not None:
        new_caveats.append(f'forbid actor is "{_DELEGATION_MARKER_PREFIX}{delegate_to}"')

    for line in new_caveats:
        if not is_legal_caveat(line):
            raise IllegalCaveatError(
                f"Caveat is outside the locked decidable subset (§5): {line!r}"
            )

    child_caveats = parent_caveats + new_caveats

    changes = amendment_diff.diff_statements("\n".join(parent_caveats), "\n".join(child_caveats))
    classification = amendment_diff.classify_monotonicity_from_changes(changes)
    if classification != "monotonic":
        raise IllegalCaveatError(
            "Refusing to attenuate: the requested change is not "
            f"authority-narrowing (classified {classification!r})."
        )

    signature = _chain_signature(root_identifier, child_caveats)
    return _serialize(root_identifier, location, child_caveats, signature)
