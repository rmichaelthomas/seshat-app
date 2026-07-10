"""
agreements.py — Agreement loading, fact composition, and enforcement decisions.

An Agreement is a Liminate contract at ~/.seshat/agreement.limn expressing
which (actor, action, scope) tuples an agent may act on. Enforcement is
deny-by-default: no Agreement, no matching permit, or any evaluation error
all deny. A matching forbid always wins over a matching permit.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import liminate

import amendment_diff
import identity

AGREEMENT_PATH = Path.home() / ".seshat" / "agreement.limn"
REVOCATIONS_PATH = Path.home() / ".seshat" / "revocations.limn"
LAST_SYNCED_REVOCATIONS_PATH = Path.home() / ".seshat" / "revocations" / ".last_synced_revocations"
INVARIANT_PATH = Path.home() / ".seshat" / "invariant.limn"
ENTRENCHED_PATH = Path.home() / ".seshat" / "entrenched.limn"

# F-07: how old the last successful `seshat revocations sync` may be
# before check_action treats an *existing* revocations.limn as stale and
# denies by default rather than enforcing against possibly-outdated
# revocations. Configurable via SESHAT_REVOCATION_STALENESS_HOURS. Deny-
# by-default is the posture implemented here; a missing revocations.limn
# (the feature was never used) is unaffected by this gate.
DEFAULT_REVOCATION_STALENESS_HOURS = 24

_ERROR_STATUS_NAMES = {
    "ERROR_PARSE",
    "ERROR_SEMANTIC",
    "ERROR_RUNTIME",
    "AMBER_PRECEDENCE",
    "AMBER_AMBIGUITY",
}


@dataclass
class Decision:
    allowed: bool
    mode: str          # "permitted" | "forbidden" | "default-deny" | "no-agreement" | "error"
    rule: str | None   # canonical form of the rule that decided, when one did
    reason: str        # human-readable, goes into the denial receipt and MCP error


def load_agreement() -> str | None:
    """Return the Agreement file's text, or None if the file doesn't exist."""
    try:
        return AGREEMENT_PATH.read_text()
    except FileNotFoundError:
        return None


def load_revocations() -> str | None:
    """Return the revocations file text, or None if it doesn't exist."""
    try:
        return REVOCATIONS_PATH.read_text()
    except FileNotFoundError:
        return None


def load_invariant() -> str | None:
    """Return the Invariant verification contract text, or None if absent."""
    try:
        return INVARIANT_PATH.read_text()
    except FileNotFoundError:
        return None


def load_entrenched() -> str | None:
    """Return the entrenched-rules file text, or None if it doesn't exist."""
    try:
        return ENTRENCHED_PATH.read_text()
    except FileNotFoundError:
        return None


def entrenched_keys() -> set[tuple[str, str]]:
    """Parse entrenched.limn to a set of (verb, subject) protected keys.

    Empty set when the file is absent — nothing entrenched by default.
    entrenched.limn uses the same statement syntax as an Agreement; each
    line names a (verb, subject) to protect. Parsing reuses the ported
    amendment_diff.parse_statements (TI-Q7, v1.0k §57) — never a second,
    ad hoc extractor. entrenched.limn is off the amendment surface (§57):
    it is read here but never written by any agent-reachable code path,
    only by `seshat entrench` (a human terminal command).
    """
    text = load_entrenched()
    if text is None:
        return set()
    return {(s["verb"], s["subject"]) for s in amendment_diff.parse_statements(text)}


def agreement_hash() -> str | None:
    """SHA-256 of ~/.seshat/agreement.limn content, or None when no Agreement
    exists (backward-compatible omission, mirroring revocation_state()'s
    None-when-absent contract). TI-Q4 (v1.0i §50) — the local<->platform
    join key stamped on every Seshat receipt."""
    text = load_agreement()
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def revocation_state() -> dict | None:
    """Return {'head_hash': ..., 'last_checked': ...} describing the current
    revocations file, or None when no revocations.limn exists (backward-
    compatible omission). head_hash is the SHA-256 of the file content;
    last_checked is read from the .last_synced_revocations marker written by
    `seshat revocations sync` (None if never synced)."""
    revocations_text = load_revocations()
    if revocations_text is None:
        return None
    head_hash = hashlib.sha256(revocations_text.encode("utf-8")).hexdigest()
    try:
        last_checked = LAST_SYNCED_REVOCATIONS_PATH.read_text().strip() or None
    except FileNotFoundError:
        last_checked = None
    return {"head_hash": head_hash, "last_checked": last_checked}


def _revocation_staleness_window() -> timedelta:
    hours = float(os.environ.get(
        "SESHAT_REVOCATION_STALENESS_HOURS", DEFAULT_REVOCATION_STALENESS_HOURS
    ))
    return timedelta(hours=hours)


def _revocations_are_stale(last_checked: str | None) -> bool:
    """True if revocations.limn exists but hasn't been refreshed within
    the policy window — including never (last_checked is None) or an
    unparseable marker, both treated as stale (deny-by-default, F-07)."""
    if last_checked is None:
        return True
    try:
        checked_at = datetime.fromisoformat(last_checked)
    except ValueError:
        return True
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - checked_at > _revocation_staleness_window()


def _temporal_window(canonical: str | None) -> str:
    """Return 'active' | 'expired' | 'future' | 'unbounded' | 'malformed' for a
    rule's starting/until prefix. Ported from liminate-dev serializers.py
    (_extract_temporal_window) so the harness enforces the same window
    semantics the platform records. Interpreter untouched — this reads the
    canonical string only.

    A malformed date is NOT silently ignored here the way the platform's
    display-only serializer ignores it: this function is an ENFORCEMENT
    path, so an unparseable date must surface to the caller as a deny, per
    §8.A (malformed dates deny). Return 'malformed' for that case; the
    caller maps it to an error decision.
    """
    if not canonical:
        return "unbounded"
    starting_date = None
    until_date = None
    words = canonical.split()
    i = 0
    while i < len(words):
        w = words[i]
        if w == "starting" and i + 1 < len(words):
            raw = words[i + 1].strip('"')
            try:
                starting_date = date.fromisoformat(raw)
            except ValueError:
                return "malformed"
            i += 2
            continue
        if w == "until" and i + 1 < len(words):
            raw = words[i + 1].strip('"')
            try:
                until_date = date.fromisoformat(raw)
            except ValueError:
                return "malformed"
            i += 2
            continue
        break
    if starting_date is None and until_date is None:
        return "unbounded"
    today = datetime.now(timezone.utc).date()
    if starting_date is not None and today < starting_date:
        return "future"
    if until_date is not None and today > until_date:
        return "expired"
    return "active"


def _validate_forbid_only(revocations_text: str) -> str | None:
    """Return None if every non-blank, non-comment line is a forbid statement
    (optionally with a starting/until prefix). Return an error string naming
    the first offending line otherwise.

    A revocation subtracts authority; it must never grant. A permit (or any
    non-forbid verb) in revocations.limn is a validation failure, not a
    silently-ignored line — a malformed kill order must be loud. Verb
    extraction reuses _verb_of() (the existing skip-prefix helper), so a
    'starting "..." forbid ...' line validates correctly.
    """
    for lineno, line in enumerate(revocations_text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        verb = _verb_of(stripped)
        if verb != "forbid":
            return f"line {lineno}: expected a forbid statement, got {verb!r}: {stripped!r}"
    return None


def _verb_of(canonical: str | None) -> str | None:
    """Extract the verb word from a canonical rendering, skipping any
    statement-initial modifiers (starting, until, inherited).

    Mirrors liminate-dev's _extract_verb_from_canonical (app/serializers.py)
    so verb identification survives interpreter message-text changes.
    """
    if not canonical:
        return None
    words = canonical.split()
    i = 0
    while i < len(words):
        w = words[i]
        if w == "starting" or w == "until":
            i += 2
            continue
        if w == "inherited":
            i += 1
            continue
        return w
    return None


def _has_injection_chars(value: str) -> bool:
    return '"' in value or "\n" in value or "\r" in value


def check_action(
    actor: str,
    action: str,
    scope: str | None = None,
    agreement_text: str | None = None,
    token: str | None = None,
) -> Decision:
    """Evaluate whether `actor` may perform `action` (optionally scoped).

    `scope` is always remembered as a fact — sentinel "none" when the call
    has no project/group target — so an Agreement conditioning on scope
    never hits an unknown-name error on scope-less actions.

    Identity-plane Stage 1: when `token` is supplied, it is verified here
    (identity.verify) BEFORE anything else — an invalid token (forged,
    tampered, or carrying an illegal caveat) denies immediately with
    mode="identity-invalid", regardless of whether an Agreement even
    exists. On success, `actor` is OVERRIDDEN by the token's verified
    identifier (the passed-in string is now untrusted and ignored), and
    the token's caveats are spliced into the same Liminate program the
    Agreement evaluates against — deny-by-default and forbid-wins,
    identical to Agreement semantics, because it is the same evaluator.

    F-02 (acute): when `token` is None, behavior is byte-for-byte
    unchanged from before the identity plane existed — `actor` is a
    self-declared string (MCP_AGENT_HINT), not an authenticated identity.
    Actor-scoped permit/forbid rules remain advisory, not a security
    boundary. Every receipt still records this explicitly via
    actor.identity_verified.
    """
    caveat_text: str | None = None
    if token is not None:
        verified = identity.verify(token)
        if verified is None:
            return Decision(
                allowed=False,
                mode="identity-invalid",
                rule=None,
                reason=(
                    "Identity token failed verification — forged, tampered, "
                    "or carrying an illegal caveat. Denying by default."
                ),
            )
        actor = verified.identifier
        caveat_text = "\n".join(verified.caveats) if verified.caveats else None

    if agreement_text is None:
        agreement_text = load_agreement()
    if agreement_text is None:
        return Decision(
            allowed=False,
            mode="no-agreement",
            rule=None,
            reason="No Agreement exists at ~/.seshat/agreement.limn. Run: seshat agreement init",
        )

    scope_value = scope if scope is not None else "none"

    for field_name, value in (("actor", actor), ("action", action), ("scope", scope_value)):
        if _has_injection_chars(value):
            return Decision(
                allowed=False,
                mode="error",
                rule=None,
                reason=f"Invalid characters in {field_name}: quotes and newlines are not permitted.",
            )

    revocations_text = load_revocations()
    if revocations_text is not None:
        state = revocation_state()
        if state is not None and _revocations_are_stale(state.get("last_checked")):
            window = _revocation_staleness_window()
            return Decision(
                allowed=False,
                mode="stale-revocations",
                rule=None,
                reason=(
                    "revocations.limn exists but hasn't been refreshed within "
                    f"the last {window} — denying by default rather than "
                    "enforcing against possibly-outdated revocations. Run: "
                    "seshat revocations sync"
                ),
            )
        validation_error = _validate_forbid_only(revocations_text)
        if validation_error is not None:
            return Decision(
                allowed=False,
                mode="error",
                rule=None,
                reason=f"Invalid revocations.limn: {validation_error}",
            )

    composed = (
        f'remember a string called actor with "{actor}"\n'
        f'remember a string called action with "{action}"\n'
        f'remember a string called scope with "{scope_value}"\n'
        "\n"
        + (f"{caveat_text}\n" if caveat_text else "")
        + (f"{revocations_text}\n" if revocations_text else "")
        + f"{agreement_text}"
    )

    try:
        result = liminate.run(composed, enter_phase2=False, auto_confirm_amber=True)
    except Exception as e:
        return Decision(
            allowed=False,
            mode="error",
            rule=None,
            reason=f"Agreement evaluation raised an exception: {e}",
        )

    for r in result.results:
        if r.status.name in _ERROR_STATUS_NAMES:
            return Decision(
                allowed=False,
                mode="error",
                rule=r.canonical,
                reason=f"Agreement evaluation error ({r.status.name}): {r.message or 'unknown error'}",
            )

    # Temporal gate: a line whose starting/until window has not yet opened or
    # has already closed is treated as absent — it cannot fire a forbid and
    # cannot satisfy a permit. Computed once per line so both scans below
    # (and any future ones) see a consistent verdict for a given result.
    windows: list[str] = []
    for r in result.results:
        window = _temporal_window(r.canonical)
        if window == "malformed":
            return Decision(
                allowed=False,
                mode="error",
                rule=r.canonical,
                reason=f"Malformed date in temporal window: {r.canonical}",
            )
        windows.append(window)

    for r, window in zip(result.results, windows):
        if r.status.name == "PROHIBITION_VIOLATED":
            if window in ("expired", "future"):
                continue
            return Decision(
                allowed=False,
                mode="forbidden",
                rule=r.canonical,
                reason=r.message or "Forbidden by Agreement.",
            )

    for r, window in zip(result.results, windows):
        if _verb_of(r.canonical) == "permit" and r.output:
            if window in ("expired", "future"):
                continue
            return Decision(
                allowed=True,
                mode="permitted",
                rule=r.canonical,
                reason="Permitted by Agreement.",
            )

    return Decision(
        allowed=False,
        mode="default-deny",
        rule=None,
        reason="No Agreement rule permits this action (deny-by-default).",
    )
