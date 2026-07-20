#!/usr/bin/env python3
"""
identity.py — capability-token (macaroon) issuance and verification.

Identity-plane arc, ID-Q4 Phase 1: an agent presents an unforgeable token
instead of a self-declared MCP_AGENT_HINT string, and the harness verifies
it locally — no network round-trip, no external identity provider.

Two algorithms coexist (§7 dual-algorithm migration, mirroring
receipts.py's RECEIPT_VERSION precedent):

- **EdDSA-chain** (current, `mint()` always produces this): an Ed25519
  next-key block chain. Block 0 is signed by the ROOT private key; every
  later block is signed by the key the PRIOR block named as its `next_key`
  — the holder's own key, never the root's. A token verifies against the
  root PUBLIC key alone, and a holder attenuates its own token without
  ever touching the root private key — a cryptographic guarantee, not an
  enforced invariant of attenuate(). This is what makes cross-org
  verification possible: an auditor holding only root_public_key_hex() can
  verify a delegation chain without being handed a forging key.
- **HS256-macaroon** (legacy, preserved for tokens minted before this
  build): identifier + location + an ordered list of caveats, bound by an
  HMAC chain (sig_0 = HMAC(root_key, identifier), sig_i = HMAC(sig_{i-1},
  caveat_i)). In this single-trust-domain model the harness holds the root
  key and both mints and verifies — attenuation recomputes the whole HMAC
  chain against the root key rather than a holder-side-only append.

Serialization is a JWT-shaped compact form (base64url header.payload.
signature) per the JOSE-representability requirement for both algorithms —
the signature part itself is alg-specific (a single HMAC digest for
HS256-macaroon; a list of per-block Ed25519 signatures for EdDSA-chain), so
neither will verify against a standard JWT library, but the three-part
shape stays exactly what the pre-Ed25519 module docstring promised: "a
later asymmetric upgrade only changes the signature algorithm, not this
three-part shape."

Same Keychain service receipts.py/vault.py use, distinct key items — one
key-storage mechanism in this codebase, not two.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import keyring
import liminate
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

import amendment_diff

MAC_SERVICE_NAME = "seshat"
ROOT_KEY_ITEM = "identity_root_key"

# ID-Q4 Phase 1: the Ed25519 root signing key, a *distinct* Keychain item
# from ROOT_KEY_ITEM — never reused. ROOT_KEY_ITEM's stored value is an
# HMAC secret and must stay readable, unchanged, for legacy HS256-macaroon
# verification (§7 / failure mode #4: reusing it would stop every legacy
# token from verifying).
ROOT_SIGNING_KEY_ITEM = "identity_root_signing_key"
# The root's PUBLIC key, cached under its own item at generation time so
# verify() can recover it WITHOUT ever touching the private-key item —
# this is what makes "verification works from the public key alone even
# when the private key is unavailable" true for this install, not just for
# a hypothetical future external auditor (§10 benchmark 6).
ROOT_SIGNING_PUBLIC_KEY_ITEM = "identity_root_signing_public_key"

IDENTITY_DIR = Path.home() / ".seshat" / "identity"

# ID-Q4 Phase 1: mcp_server.py's attenuate_identity tool is byte-identical
# to main — its signature has no parameter to carry a holder private key.
# attenuate() falls back to this env var when the caller doesn't pass one
# explicitly, mirroring exactly how SESHAT_IDENTITY_TOKEN already reaches
# mcp_server.py: a human sets both env vars for an agent's MCP session,
# and the agent narrows its own session token. See attenuate()'s
# docstring.
HOLDER_KEY_ENV_VAR = "SESHAT_IDENTITY_HOLDER_KEY"

_ALG_HMAC = "HS256-macaroon"
_ALG_ED25519 = "EdDSA-chain"
_TYP = "SIT"  # Seshat Identity Token

# Identity-plane Stage 3: short-lived-by-default issuance. 24h is a sane
# bounded default for an agent token; a human overrides it explicitly at
# mint time (CLI --ttl/--until) when they mean to issue something longer-
# or un-lived.
DEFAULT_TTL_HOURS = 24

# Identity-plane Stage 2 (delegation), legacy HMAC path only: a delegation
# hop is recorded as an ordinary, legal forbid caveat over the actor fact
# — no special-casing in is_legal_caveat is needed, and it rides the same
# HMAC chain (tamper-evident) as every other caveat. The EdDSA-chain path
# does not need this trick (§5): delegate_to is a structural field on each
# signed block, so delegation_path is read directly off the chain instead
# of pattern-matching an embedded marker caveat.
_DELEGATION_MARKER_PREFIX = "seshat-delegate:"
_DELEGATION_MARKER_RE = re.compile(r'^forbid actor is "seshat-delegate:(.+)"$')


class IdentityKeyUnavailableError(RuntimeError):
    """The identity root key (HMAC or Ed25519, whichever the caller needed)
    could not be obtained. mint()/verify()/attenuate() refuse to operate
    without it — fail closed, mirroring receipts.py's
    ReceiptKeyUnavailableError (F-01 pattern). Never falls back to an
    unkeyed or default signature."""


class IllegalCaveatError(ValueError):
    """A caveat line falls outside the locked decidable subset (§5), or an
    Ed25519 attenuation was attempted without a usable holder private key.
    Raised by mint() before any signing happens — an illegal caveat must
    never be baked into a token in the first place."""


@dataclass
class VerifiedIdentity:
    identifier: str
    caveats: list[str] = field(default_factory=list)
    # [] when the token has never been delegated; else [root, ..., leaf].
    # `identifier` above is ALWAYS the root (the signed, Agreement-matching
    # identity, unchanged across every attenuation hop — for EdDSA-chain
    # tokens this is block 0's identifier; for HS256-macaroon tokens it is
    # the signed payload identifier) — see attenuate()'s docstring for why:
    # check_action's Agreement-matching actor must never key off a
    # self-chosen delegate_to string, or a holder could rename itself to
    # an unrelated, more-privileged Agreement actor and inherit permissions
    # the root never had. The leaf (delegation_path[-1]) is for audit/
    # receipt display only (see mcp_server.py's _agreement_actor).
    delegation_path: list[str] = field(default_factory=list)
    # None for a token minted before Stage 3, or one hand-built without a
    # nonce; otherwise the tamper-evident per-token identifier that lets
    # `identity revoke --token` kill this one token without revoking the
    # whole agent name (see revocation_identifiers()). For an EdDSA-chain
    # token this is the LEAF block's own nonce — the currently-held
    # token's own identifier.
    token_nonce: str | None = None


def _root_key() -> bytes:
    """Per-install HMAC root key, Keychain-backed via `keyring` — generated
    once and stored the same way receipts.py stores its MAC key. Any
    failure here propagates to the caller; _chain_signature converts that
    into the fail-closed IdentityKeyUnavailableError. Legacy HS256-macaroon
    path only (§7) — unchanged by ID-Q4 Phase 1."""
    raw = keyring.get_password(MAC_SERVICE_NAME, ROOT_KEY_ITEM)
    if not raw:
        raw = secrets.token_hex(32)
        keyring.set_password(MAC_SERVICE_NAME, ROOT_KEY_ITEM, raw)
    return bytes.fromhex(raw)


def _chain_signature(identifier: str, caveats: list[str], nonce: str | None = None) -> bytes:
    """The macaroon HMAC chain: sig_0 = HMAC(root_key, identifier), then
    one extra fold for the nonce (if present, right after the identifier
    and before any caveats), then one fold per caveat in order. When
    nonce is None this is byte-identical to the pre-Stage-3 formula, so
    every token minted before the nonce existed still verifies unchanged.
    Legacy HS256-macaroon path only (§7) — unchanged by ID-Q4 Phase 1.
    """
    try:
        key = _root_key()
    except Exception as exc:
        raise IdentityKeyUnavailableError(
            "Cannot mint or verify an identity token: the Keychain-backed "
            "root key is unavailable. Refusing to operate unkeyed."
        ) from exc
    sig = hmac.new(key, identifier.encode("utf-8"), hashlib.sha256).digest()
    if nonce is not None:
        sig = hmac.new(sig, f"nonce:{nonce}".encode("utf-8"), hashlib.sha256).digest()
    for caveat in caveats:
        sig = hmac.new(sig, caveat.encode("utf-8"), hashlib.sha256).digest()
    return sig


# ── Ed25519 root key management (ID-Q4 Phase 1) ─────────────────────────────

def _private_key_hex(key: ed25519.Ed25519PrivateKey) -> str:
    return key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    ).hex()


def _public_key_hex(key: ed25519.Ed25519PublicKey) -> str:
    return key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()


def _root_signing_key() -> ed25519.Ed25519PrivateKey:
    """Per-install Ed25519 root signing key, Keychain-backed via `keyring`
    — generated once and stored as raw 32-byte hex, the same shape/flow
    _root_key() uses for its HMAC secret. Generation also backfills
    ROOT_SIGNING_PUBLIC_KEY_ITEM so _root_public_key() never needs this
    function (or Keychain access to the private half) again. Any failure
    here propagates to the caller; mint()'s and attenuate()'s call sites
    convert that into the fail-closed IdentityKeyUnavailableError via
    _require_root_signing_key()."""
    raw = keyring.get_password(MAC_SERVICE_NAME, ROOT_SIGNING_KEY_ITEM)
    if raw:
        return ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(raw))
    private_key = ed25519.Ed25519PrivateKey.generate()
    keyring.set_password(MAC_SERVICE_NAME, ROOT_SIGNING_KEY_ITEM, _private_key_hex(private_key))
    keyring.set_password(
        MAC_SERVICE_NAME, ROOT_SIGNING_PUBLIC_KEY_ITEM, _public_key_hex(private_key.public_key())
    )
    return private_key


def _root_public_key() -> ed25519.Ed25519PublicKey:
    """The root's PUBLIC key only — reads ROOT_SIGNING_PUBLIC_KEY_ITEM
    directly, a distinct Keychain item from the private key, so this never
    needs the private key to be available (§10 benchmark 6: the cross-org,
    public-key-only verification property). Falls back to
    _root_signing_key() only to bootstrap a fresh install that has never
    minted anything yet (mirrors _root_key()'s lazy-generate-on-first-use
    behavior) — once that runs once, this function never touches the
    private-key item again."""
    raw = keyring.get_password(MAC_SERVICE_NAME, ROOT_SIGNING_PUBLIC_KEY_ITEM)
    if raw:
        return ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(raw))
    return _root_signing_key().public_key()


def _require_root_signing_key() -> ed25519.Ed25519PrivateKey:
    try:
        return _root_signing_key()
    except Exception as exc:
        raise IdentityKeyUnavailableError(
            "Cannot mint an identity token: the Keychain-backed Ed25519 "
            "root signing key is unavailable. Refusing to operate unkeyed."
        ) from exc


def _require_root_public_key() -> ed25519.Ed25519PublicKey:
    try:
        return _root_public_key()
    except Exception as exc:
        raise IdentityKeyUnavailableError(
            "Cannot verify an identity token: the Keychain-backed Ed25519 "
            "root public key is unavailable. Refusing to operate unkeyed."
        ) from exc


def root_public_key_hex() -> str:
    """The root's public key, 64 hex chars — the value to export/hand to a
    third party (an auditor, an enterprise SIEM) for independent, cross-
    org verification of every EdDSA-chain token this install mints,
    without ever handing over a forging key. This is what ID-Q4 Phase 1
    exists to produce (§4)."""
    return _public_key_hex(_require_root_public_key())


def _location() -> str:
    return socket.gethostname()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + padding)


def _canonical_block(block: dict) -> bytes:
    """Canonical signing bytes for one Ed25519 chain block — the same
    canonical-form discipline receipts.py's emit()/_keyed_hash already use
    (sort_keys, comma/colon separators, no whitespace). Sign and verify
    both call this on the identical block dict, so any drift between the
    two would fail every token (§11 failure mode #1)."""
    return json.dumps(block, sort_keys=True, separators=(",", ":")).encode("utf-8")


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
    fully against the enforcement facts — proven by actually running it
    through the real liminate interpreter with those facts bound to dummy
    probe values, the same shape agreements.check_action() composes at
    real enforcement time. This is deliberately NOT a hand-rolled
    predicate grammar: reusing the real evaluator is what proves offline
    decidability, rather than asserting it.

    The decidable subset resolves against SEVEN facts: actor, action and
    scope (text-composed into the probe program, mirroring check_action's
    unchanged F-02 composition) plus the four
    agreements.NEW_ENFORCEMENT_FACTS — actor-teams, delegation-path,
    delegation-depth, token-nonce — bound via liminate.run(inject=...) as
    inert data. Both this probe and check_action source those four from
    agreements.new_fact_probe_values() / the same constant, so a fact
    added to enforcement but not to the probe (or vice versa) breaks the
    parity test rather than silently drifting: a caveat that passes
    legality here must not error at enforcement time.

    The forbid-only rule below is unchanged and load-bearing for the new
    facts specifically: membership and provenance are expressible in a
    caveat only as PROHIBITION. A caveat can forbid on team membership
    or delegation depth (narrowing), but can never assert membership to
    gain authority (widening).

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
        result = liminate.run(
            probe, enter_phase2=False, auto_confirm_amber=True,
            inject=agreements.new_fact_probe_values(),
        )
    except Exception:
        return False
    return not any(r.status.name in agreements._ERROR_STATUS_NAMES for r in result.results)


# ── Serialization ────────────────────────────────────────────────────────────

def _serialize(
    identifier: str, location: str, caveats: list[str], signature: bytes, *, nonce: str | None = None,
) -> str:
    """Legacy HS256-macaroon compact-form serializer — unchanged by ID-Q4
    Phase 1 (§7)."""
    header = {"alg": _ALG_HMAC, "typ": _TYP}
    payload = {"identifier": identifier, "location": location, "caveats": caveats}
    if nonce is not None:
        payload["nonce"] = nonce
    return ".".join((
        _b64(json.dumps(header, sort_keys=True).encode("utf-8")),
        _b64(json.dumps(payload, sort_keys=True).encode("utf-8")),
        _b64(signature),
    ))


def _serialize_eddsa(identifier: str, location: str, blocks: list[dict], signatures: list[bytes]) -> str:
    """EdDSA-chain compact-form serializer. The signature part is a
    base64url-encoded JSON list of per-block base64url signatures — one
    entry per block, in chain order — keeping the outer three-part
    header.payload.signature shape identical to the legacy form while
    carrying a chain of signatures instead of one."""
    header = {"alg": _ALG_ED25519, "typ": _TYP}
    payload = {"identifier": identifier, "location": location, "blocks": blocks}
    sig_list = [_b64(s) for s in signatures]
    return ".".join((
        _b64(json.dumps(header, sort_keys=True).encode("utf-8")),
        _b64(json.dumps(payload, sort_keys=True).encode("utf-8")),
        _b64(json.dumps(sig_list).encode("utf-8")),
    ))


def _peek_header(token: str) -> dict | None:
    """Parse and return TOKEN's header dict without verifying anything —
    used only to dispatch on `alg` before running the algorithm-specific
    verifier. Returns None on any structural problem (wrong part count,
    bad base64/JSON, non-dict header)."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        header = json.loads(_b64d(parts[0]))
    except Exception:
        return None
    return header if isinstance(header, dict) else None


def _has_explicit_temporal_caveat(caveats: list[str]) -> bool:
    """True if any of CAVEATS already opens with 'starting'/'until' — the
    caller has taken control of temporal scoping themselves, so mint()'s
    own default-ttl caveat should not also be appended."""
    for line in caveats:
        words = line.strip().split()
        if words and words[0] in ("starting", "until"):
            return True
    return False


# ── Mint ─────────────────────────────────────────────────────────────────────

def mint(
    identifier: str,
    caveats: list[str] | None = None,
    *,
    ttl_hours: float | None = DEFAULT_TTL_HOURS,
    nonce: str | None = None,
    location: str | None = None,
) -> tuple[str, str]:
    """Build and sign a new EdDSA-chain capability token for IDENTIFIER.
    Human-initiated only (never called from an MCP-reachable path — see
    cli.py's `identity mint`, which is the only caller outside tests).

    Every caveat must pass is_legal_caveat() — the whole mint call raises
    IllegalCaveatError if any one doesn't, rather than silently dropping
    the offending line.

    Short-lived by default (identity-plane Stage 3): unless the caller's
    own CAVEATS already carry an explicit starting/until line, or
    TTL_HOURS is None (an explicitly unbounded token — a human opt-in via
    the CLI, not the default), a
    `starting "<today + ttl_hours>" forbid actor is "<identifier>"` caveat
    is appended. This is a blanket denial of every action for this actor
    once its window opens — deliberately `starting`, not `until` (`until`
    would make the forbid go INERT after the date, the wrong direction for
    "the token has expired"). Confirmed against the live interpreter: a
    forbid whose predicate names only the actor fact (no action/scope)
    fires for any action once `agreements._temporal_window` reports its
    window `active` — no changes needed to check_action's existing
    temporal-window handling for this to work.

    NONCE (identity-plane Stage 3): a tamper-evident, per-token identifier
    folded into the signed block (block 0), always present unless the
    caller passes one explicitly (mostly useful for deterministic tests) —
    auto-generated via `secrets.token_hex(8)` otherwise. Lets a specific
    token be revoked (`identity revoke --token`) without revoking the
    whole agent name.

    ID-Q4 PHASE 1 — the Ed25519 upgrade: block 0, `{identifier, location,
    nonce, caveats, next_key}`, is signed with the Keychain-backed ROOT
    private key. `next_key` names a FRESH, one-off holder keypair
    generated right here — never the root's own key, never persisted to
    Keychain (the root key is the only long-lived secret this module
    owns). Returns (token, holder_private_key_hex): the caller must hold
    onto the private half to attenuate this exact token later — see
    attenuate()'s `holder_private_key` parameter and §6.1. This mirrors
    how the token itself is already handled by callers (a live bearer
    credential, handed back and never assumed to be re-derivable).
    """
    caveats = list(caveats or [])
    if ttl_hours is not None and not _has_explicit_temporal_caveat(caveats):
        expiry = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).date().isoformat()
        caveats.append(f'starting "{expiry}" forbid actor is "{identifier}"')

    for line in caveats:
        if not is_legal_caveat(line):
            raise IllegalCaveatError(
                f"Caveat is outside the locked decidable subset (§5): {line!r}"
            )

    loc = location or _location()
    if nonce is None:
        nonce = secrets.token_hex(8)

    holder_key = ed25519.Ed25519PrivateKey.generate()
    block0 = {
        "identifier": identifier,
        "location": loc,
        "nonce": nonce,
        "caveats": caveats,
        "next_key": _public_key_hex(holder_key.public_key()),
    }
    root_key = _require_root_signing_key()
    signature = root_key.sign(_canonical_block(block0))

    token = _serialize_eddsa(identifier, loc, [block0], [signature])
    return token, _private_key_hex(holder_key)


# ── Verify ───────────────────────────────────────────────────────────────────

def _verify_raw(token: str) -> tuple[str, str, list[str], str | None] | None:
    """Internal: verify a legacy HS256-macaroon TOKEN's structure and
    signature, returning the raw (identifier, location, caveats, nonce)
    straight from the payload — before any delegation-path derivation.
    Legacy path only (§7) — unchanged by ID-Q4 Phase 1, aside from
    referencing the renamed _ALG_HMAC constant. Same fail-closed rules as
    verify(): structural problems return None; IdentityKeyUnavailableError
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

    if header.get("alg") != _ALG_HMAC or header.get("typ") != _TYP:
        return None

    identifier = payload.get("identifier")
    location = payload.get("location")
    caveats = payload.get("caveats")
    nonce = payload.get("nonce")
    if not isinstance(identifier, str) or not isinstance(location, str) or not isinstance(caveats, list):
        return None
    if nonce is not None and not isinstance(nonce, str):
        return None
    if not all(isinstance(c, str) for c in caveats):
        return None

    for line in caveats:
        if not is_legal_caveat(line):
            return None

    expected = _chain_signature(identifier, caveats, nonce=nonce)
    if not hmac.compare_digest(expected, signature):
        return None

    return identifier, location, caveats, nonce


def _verify_raw_eddsa(token: str) -> tuple[str, str, list[dict], list[bytes]] | None:
    """Internal: verify an EdDSA-chain TOKEN — walk the block chain from
    the root PUBLIC key (block 0), then from each block's own `next_key`
    (every later block), re-checking every per-block caveat's legality
    along the way. Returns (identifier, location, blocks, signatures) —
    the raw, still-underived block list plus their signatures (attenuate()
    reuses both to extend the chain without re-signing history). Same
    fail-closed contract as _verify_raw: any structural or signature
    problem returns None; IdentityKeyUnavailableError propagates uncaught
    (the root PUBLIC key is required — a missing Keychain entry is an
    infrastructure condition, not a per-token verdict).
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    header_b64, payload_b64, sig_b64 = parts

    try:
        header = json.loads(_b64d(header_b64))
        payload = json.loads(_b64d(payload_b64))
        sig_list_b64 = json.loads(_b64d(sig_b64))
    except Exception:
        return None

    if not isinstance(header, dict) or header.get("alg") != _ALG_ED25519 or header.get("typ") != _TYP:
        return None
    if not isinstance(payload, dict):
        return None

    identifier = payload.get("identifier")
    location = payload.get("location")
    blocks = payload.get("blocks")
    if not isinstance(identifier, str) or not isinstance(location, str):
        return None
    if not isinstance(blocks, list) or not blocks or not all(isinstance(b, dict) for b in blocks):
        return None
    if not isinstance(sig_list_b64, list) or len(sig_list_b64) != len(blocks):
        return None

    try:
        signatures = [_b64d(s) for s in sig_list_b64]
    except Exception:
        return None

    block0 = blocks[0]
    if block0.get("identifier") != identifier or block0.get("location") != location:
        return None

    # Required before any signature check: an unkeyed/missing root public
    # key is a fail-closed infrastructure condition (invariant 1), so this
    # must raise IdentityKeyUnavailableError rather than be caught below
    # and folded into an ordinary deny.
    verifying_key: ed25519.Ed25519PublicKey = _require_root_public_key()

    for i, block in enumerate(blocks):
        caveats = block.get("caveats")
        nonce = block.get("nonce")
        next_key_hex = block.get("next_key")
        if not isinstance(caveats, list) or not all(isinstance(c, str) for c in caveats):
            return None
        if not isinstance(nonce, str) or not isinstance(next_key_hex, str):
            return None
        for c in caveats:
            if not is_legal_caveat(c):
                return None

        try:
            verifying_key.verify(signatures[i], _canonical_block(block))
        except InvalidSignature:
            return None
        except Exception:
            return None

        try:
            verifying_key = ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(next_key_hex))
        except Exception:
            return None

    return identifier, location, blocks, signatures


def _delegation_path(root_identifier: str, caveats: list[str]) -> list[str]:
    """[] if CAVEATS carries no delegation markers (never delegated);
    else [root_identifier, hop_1, ..., hop_n] in the order the markers
    were appended (attenuate() only ever appends, so this is also
    chronological delegation order). Legacy HS256-macaroon path only."""
    hops = [m.group(1) for c in caveats if (m := _DELEGATION_MARKER_RE.match(c.strip()))]
    if not hops:
        return []
    return [root_identifier, *hops]


def _delegation_path_eddsa(root_identifier: str, blocks: list[dict]) -> list[str]:
    """[] if no block after block 0 carries a `delegate_to`; else
    [root_identifier, hop_1, ..., hop_n] in block order. Structural —
    unlike the legacy path, delegate_to is a signed block field, not a
    caveat-embedded marker, so no pattern-matching is needed."""
    hops = [b["delegate_to"] for b in blocks[1:] if b.get("delegate_to") is not None]
    if not hops:
        return []
    return [root_identifier, *hops]


def verify(token: str) -> VerifiedIdentity | None:
    """Dispatch on TOKEN's header `alg` and verify against the matching
    algorithm: `EdDSA-chain` walks the block chain from the root PUBLIC
    key; `HS256-macaroon` recomputes the legacy HMAC chain from the root
    key. Any structural problem (wrong part count, bad base64, bad JSON,
    missing field, unrecognized alg, an illegal caveat, or a signature
    mismatch) returns None — deny, don't raise — EXCEPT
    IdentityKeyUnavailableError, which propagates uncaught exactly like
    receipts.ReceiptKeyUnavailableError does through emit(): a missing key
    is an infrastructure fail-closed condition, not a per-token verdict.
    """
    header = _peek_header(token)
    if header is None:
        return None
    alg = header.get("alg")

    if alg == _ALG_ED25519:
        raw = _verify_raw_eddsa(token)
        if raw is None:
            return None
        identifier, _location, blocks, _signatures = raw
        all_caveats = [c for block in blocks for c in block["caveats"]]
        return VerifiedIdentity(
            identifier=identifier,
            caveats=all_caveats,
            delegation_path=_delegation_path_eddsa(identifier, blocks),
            token_nonce=blocks[-1]["nonce"],
        )

    if alg == _ALG_HMAC:
        raw = _verify_raw(token)
        if raw is None:
            return None
        identifier, _location, caveats, nonce = raw
        return VerifiedIdentity(
            identifier=identifier,
            caveats=caveats,
            delegation_path=_delegation_path(identifier, caveats),
            token_nonce=nonce,
        )

    return None


def revocation_identifiers(verified: VerifiedIdentity) -> list[str]:
    """Every name a revocation could match to kill this verified token:
    the root identifier (or the full [root, ..., leaf] delegation path,
    when delegated), plus the token's own nonce if it has one. Any one of
    these being the subject of a matching `forbid actor is "..."` in
    revocations.limn should deny — see agreements.check_action's
    identity-plane Stage 3 revocation block. Alg-agnostic: operates only
    on VerifiedIdentity's fields, which mean the same thing for both
    algorithms."""
    ids = list(verified.delegation_path) if verified.delegation_path else [verified.identifier]
    if verified.token_nonce:
        ids.append(verified.token_nonce)
    return ids


# ── Attenuate ────────────────────────────────────────────────────────────────

def attenuate(
    token: str,
    added_caveats: list[str] | None = None,
    *,
    delegate_to: str | None = None,
    holder_private_key: str | None = None,
) -> tuple[str, str | None]:
    """Narrow TOKEN by appending ADDED_CAVEATS (and, if DELEGATE_TO is
    given, recording a delegation hop to the sub-agent this narrower token
    is being handed to), producing a new, still-valid token.

    Dispatches on TOKEN's own header `alg`:

    - **EdDSA-chain**: holder-side-only, root-key-less attenuation (ID-Q4
      Phase 1 — the deferred Stage 3.C work). HOLDER_PRIVATE_KEY (the hex
      private key `mint()` or a prior `attenuate()` call returned for this
      exact token) signs the new block; the root private key is never
      read. This is now a cryptographic guarantee, not merely an enforced
      invariant of this function: a holder without HOLDER_PRIVATE_KEY
      cannot produce a block that verifies, full stop. Returns the new
      holder's freshly generated private key (hex) alongside the token —
      required to attenuate the result again later.
    - **HS256-macaroon** (legacy, §7): unchanged single-trust-domain
      behavior — the whole chain is recomputed against the root key, since
      the harness holds it and both mints and verifies in this model.
      HOLDER_PRIVATE_KEY is ignored. The second return value is always
      None (no holder keypair exists in this model).

    HOLDER_PRIVATE_KEY env-var fallback: when omitted (None) for an
    EdDSA-chain token, this reads the `SESHAT_IDENTITY_HOLDER_KEY`
    environment variable instead of failing immediately. This exists
    because `mcp_server.attenuate_identity` — deliberately unmodified by
    ID-Q4 Phase 1 — has no parameter through which an agent could pass a
    holder key; it mirrors exactly how `SESHAT_IDENTITY_TOKEN` already
    reaches that tool (a human provisions both env vars for an agent's
    MCP session, and the agent narrows its own session token). An
    explicit HOLDER_PRIVATE_KEY argument always takes precedence over the
    env var. Still fails closed (IllegalCaveatError) if neither is set.

    Agent-reachable (unlike mint): a delegating agent narrows its own
    token at runtime to hand to a sub-agent. This is safe because
    attenuation can ONLY narrow, never broaden — enforced multiple ways
    for both algorithms: added caveats must pass is_legal_caveat
    (forbid-only, same gate as mint), they are APPENDED, never inserted/
    removed (the parent's caveat list is an immutable prefix of the
    child's), and the whole change is asserted `monotonic` via
    amendment_diff.classify_monotonicity_from_changes before a token is
    produced — enforced-and-tested, not merely assumed from the
    append-only structure. For EdDSA-chain tokens specifically, forging a
    broader token is ALSO cryptographically impossible without the
    root key, independent of this function's own checks.

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
    if holder_private_key is None:
        holder_private_key = os.environ.get(HOLDER_KEY_ENV_VAR)

    header = _peek_header(token)
    if header is not None and header.get("alg") == _ALG_ED25519:
        return _attenuate_eddsa(
            token, added_caveats, delegate_to=delegate_to, holder_private_key=holder_private_key
        )
    return _attenuate_hmac(token, added_caveats, delegate_to=delegate_to)


def _attenuate_hmac(
    token: str, added_caveats: list[str] | None = None, *, delegate_to: str | None = None,
) -> tuple[str, None]:
    """Legacy HS256-macaroon attenuation (§7) — same recompute-the-whole-
    chain-against-the-root-key behavior as before ID-Q4 Phase 1, just
    wrapped to return (token, None) instead of a bare token string."""
    raw = _verify_raw(token)
    if raw is None:
        raise IllegalCaveatError(
            "Cannot attenuate: the input token does not verify (invalid, "
            "forged, or already carrying an illegal caveat)."
        )
    root_identifier, location, parent_caveats, _parent_nonce = raw

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

    # A fresh nonce, not the parent's: the child is a functionally
    # distinct bearer credential (identity-plane Stage 3) — revoking it
    # by nonce must not touch the parent or a sibling delegation.
    child_nonce = secrets.token_hex(8)
    signature = _chain_signature(root_identifier, child_caveats, nonce=child_nonce)
    token_str = _serialize(root_identifier, location, child_caveats, signature, nonce=child_nonce)
    return token_str, None


def _attenuate_eddsa(
    token: str,
    added_caveats: list[str] | None = None,
    *,
    delegate_to: str | None = None,
    holder_private_key: str | None = None,
) -> tuple[str, str]:
    """ID-Q4 Phase 1: holder-side-only Ed25519 attenuation. Appends one
    new block, signed with HOLDER_PRIVATE_KEY — the root private key is
    never read anywhere in this function."""
    raw = _verify_raw_eddsa(token)
    if raw is None:
        raise IllegalCaveatError(
            "Cannot attenuate: the input token does not verify (invalid, "
            "forged, or already carrying an illegal caveat)."
        )
    identifier, location, blocks, signatures = raw

    if not holder_private_key:
        raise IllegalCaveatError(
            "Cannot attenuate an EdDSA-chain identity token without the "
            "current holder's private key — pass holder_private_key or "
            f"set {HOLDER_KEY_ENV_VAR}. Holder-side attenuation never uses "
            "the root private key."
        )
    try:
        holder_key = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(holder_private_key))
    except Exception as exc:
        raise IllegalCaveatError(
            f"Cannot attenuate: holder_private_key is not a valid Ed25519 private key: {exc}"
        ) from exc

    # The supplied key must actually be the one this token's chain names
    # as its current holder (block[-1]'s next_key) — otherwise an
    # unrelated key would silently sign a block that can never verify
    # (§11 failure mode #6: next_key IS covered by the signature, so a
    # mismatch here would always be caught at verify() time too, but
    # failing loudly here gives a much clearer error to the caller).
    expected_next_key_hex = blocks[-1]["next_key"]
    if _public_key_hex(holder_key.public_key()) != expected_next_key_hex:
        raise IllegalCaveatError(
            "Cannot attenuate: holder_private_key does not match the "
            "public key this token's chain names as its current holder."
        )

    if delegate_to is not None and ('"' in delegate_to or "\n" in delegate_to or "\r" in delegate_to):
        # Defense in depth ahead of the inject boundary (mirrors
        # agreements._has_injection_chars): delegation-path is always
        # bound as inert data via liminate.run(inject=...), so a hostile
        # delegate_to could never actually break out into program text —
        # but refusing it here, the same way the legacy caveat-marker
        # path incidentally did by riding is_legal_caveat, keeps the
        # hostile hop from ever being minted in the first place.
        raise IllegalCaveatError(
            f"delegate_to contains characters that cannot appear in a delegation hop: {delegate_to!r}"
        )

    new_caveats = list(added_caveats or [])
    for line in new_caveats:
        if not is_legal_caveat(line):
            raise IllegalCaveatError(
                f"Caveat is outside the locked decidable subset (§5): {line!r}"
            )

    parent_caveats = [c for block in blocks for c in block["caveats"]]
    child_caveats_all = parent_caveats + new_caveats
    changes = amendment_diff.diff_statements(
        "\n".join(parent_caveats), "\n".join(child_caveats_all)
    )
    classification = amendment_diff.classify_monotonicity_from_changes(changes)
    if classification != "monotonic":
        raise IllegalCaveatError(
            "Refusing to attenuate: the requested change is not "
            f"authority-narrowing (classified {classification!r})."
        )

    delegatee_key = ed25519.Ed25519PrivateKey.generate()
    new_block = {
        "nonce": secrets.token_hex(8),
        "caveats": new_caveats,
        "next_key": _public_key_hex(delegatee_key.public_key()),
    }
    if delegate_to is not None:
        new_block["delegate_to"] = delegate_to

    # Signed with the HOLDER's key — never the root's (the whole point).
    new_signature = holder_key.sign(_canonical_block(new_block))

    new_token = _serialize_eddsa(
        identifier, location, blocks + [new_block], signatures + [new_signature]
    )
    return new_token, _private_key_hex(delegatee_key)
