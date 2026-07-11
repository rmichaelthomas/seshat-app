#!/usr/bin/env python3
"""
receipts.py — Shared receipt emission and chain-integrity module.

Single source of truth for machine-action receipts. Both cli.py and
mcp_server.py call into this module instead of maintaining their own
in-memory chain-head state. Every write re-reads the chain head from disk
under an exclusive file lock, so concurrent writers (MCP session + CLI +
dashboard) produce one linear chain instead of forking.
"""

import fcntl
import hashlib
import hmac
import json
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path

import keyring

from registry import Registry
from scanner import Scanner

registry = Registry()
scanner = Scanner()

# ── Receipt storage ─────────────────────────────────────────────────────────

RECEIPTS_DIR = Path.home() / ".seshat" / "receipts"
RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)

LOCK_PATH = RECEIPTS_DIR / ".chain.lock"
CHAIN_HEAD_PATH = RECEIPTS_DIR / ".chain_head"

# receipt_version 2 = keyed (HMAC-SHA256) + anchored (.chain_head). A
# receipt with no receipt_version (or < 2) predates this and was hashed
# with a plain, unkeyed sha256 — verify still reads it, but only as a
# legacy, link-verified-only prefix (F-01 §7 failure mode #2).
RECEIPT_VERSION = 2

# Same Keychain service vault.py uses for its Fernet key — one
# key-storage mechanism in this codebase, not two.
MAC_SERVICE_NAME = "seshat"
MAC_KEY_ITEM = "receipt_mac_key"


class ReceiptKeyUnavailableError(RuntimeError):
    """The receipt MAC key could not be obtained. emit() refuses to write
    an unkeyed receipt rather than silently falling back to a plain hash
    anyone could recompute (F-01) — fail closed, not fail open."""


def _mac_key() -> bytes:
    """Per-install HMAC key for receipt hashing, Keychain-backed via
    `keyring` — generated once and stored the same way vault.py stores its
    Fernet key. Any failure here (missing keyring, locked Keychain, etc.)
    propagates to the caller; _keyed_hash is the single seam that converts
    that into the fail-closed ReceiptKeyUnavailableError."""
    raw = keyring.get_password(MAC_SERVICE_NAME, MAC_KEY_ITEM)
    if not raw:
        raw = secrets.token_hex(32)
        keyring.set_password(MAC_SERVICE_NAME, MAC_KEY_ITEM, raw)
    return bytes.fromhex(raw)


def _keyed_hash(canonical: str) -> str:
    try:
        key = _mac_key()
    except Exception as exc:
        raise ReceiptKeyUnavailableError(
            "Cannot key the receipt chain: the Keychain-backed MAC key is "
            "unavailable. Refusing to emit an unkeyed receipt."
        ) from exc
    return hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def snapshot() -> dict:
    """Capture current environment state (listening ports + managed projects)."""
    scan = scanner.scan()
    state = registry.get_state()
    return {
        "listening_ports": sorted(scan.keys()),
        "managed_projects": {
            name: {"pid": info.get("pid"), "started_by": info.get("started_by")}
            for name, info in state.items()
        },
    }


def _legacy_recover_chain_head() -> str | None:
    """Pre-anchor method: trust the highest-sorted filename. Filenames are
    attacker-controllable (F-01), so this is used only to bootstrap
    .chain_head the first time this code runs against a chain that
    predates it — never trusted once an anchor exists."""
    try:
        files = sorted(RECEIPTS_DIR.glob("*.json"))
        if not files:
            return None
        last = json.loads(files[-1].read_text())
        return last.get("receipt_hash")
    except (json.JSONDecodeError, OSError, KeyError):
        return None


def _read_chain_head() -> dict | None:
    try:
        return json.loads(CHAIN_HEAD_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _write_chain_head(head_hash: str, count: int) -> None:
    CHAIN_HEAD_PATH.write_text(json.dumps({"head_hash": head_hash, "count": count}))


def _recover_chain_state() -> tuple[str | None, int]:
    """Return (head_hash, count) from the persisted anchor — NOT from
    sorting receipt filenames (F-01: filenames are attacker-controllable,
    and file presence alone can't reveal a deleted tail). Bootstraps the
    anchor once from the legacy glob method the first time this runs
    against a chain that predates .chain_head."""
    anchor = _read_chain_head()
    if anchor is not None:
        return anchor.get("head_hash"), anchor.get("count", 0)
    legacy_head = _legacy_recover_chain_head()
    legacy_count = len(list(RECEIPTS_DIR.glob("*.json")))
    return legacy_head, legacy_count


def recover_chain_head() -> str | None:
    """Return the current chain head hash from the persisted anchor."""
    head_hash, _count = _recover_chain_state()
    return head_hash


def emit(
    action: str,
    target: dict,
    result: dict,
    env_before: dict,
    session_id: str,
    actor_type: str,
    agent_hint: str,
    env_after: dict | None = None,
    *,
    revocation_state: dict | None = None,
    agreement_hash: str | None = None,
    invariant: dict | None = None,
    identity_verified: bool = False,
    delegation_path: list[str] | None = None,
) -> dict:
    """Write a hash-chained receipt to disk with file locking. Returns the
    written receipt dict (including its receipt_hash) so callers that need
    to reference the receipt they just wrote — e.g. amend_agreement handing
    a receipt id back to the caller — don't have to re-derive it.

    This is the ONLY function that writes receipt files. The chain head is
    re-read from disk under an exclusive lock immediately before building
    the receipt, so concurrent writers never fork the chain.
    """
    if env_after is None:
        env_after = snapshot()

    lock_fd = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        previous_hash, previous_count = _recover_chain_state()

        receipt = {
            "type": "machine_action",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "actor": {
                "type": actor_type,
                "session_id": session_id,
                "agent_hint": agent_hint,
                # F-02 (acute → structural): true only when a verified HMAC
                # capability token backed this action (threaded from the
                # caller via check_action's Decision, never hardcoded here
                # — see identity.py). False remains the honest default for
                # every token-absent call, exactly as before the identity
                # plane existed.
                "identity_verified": identity_verified,
                # Stage 2 (delegation): the full [root, ..., leaf] chain
                # when the verified token was delegated; [] otherwise
                # (undelegated token, or no token at all).
                "delegation_path": delegation_path if delegation_path is not None else [],
            },
            "action": action,
            "target": target,
            "result": result,
            "environment_before": env_before,
            "environment_after": env_after,
        }
        if revocation_state is not None:
            receipt["revocation_state"] = revocation_state
        if agreement_hash is not None:
            receipt["agreement_hash"] = agreement_hash
        if invariant is not None:
            receipt["invariant"] = invariant
        receipt["previous_hash"] = previous_hash
        receipt["receipt_version"] = RECEIPT_VERSION

        canonical = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
        # Fail closed: _keyed_hash raises ReceiptKeyUnavailableError rather
        # than falling back to an unkeyed hash. Nothing is written below if
        # this raises (F-01).
        receipt_hash = _keyed_hash(canonical)
        receipt["receipt_hash"] = receipt_hash

        # Microsecond resolution (not just seconds): under the lock, concurrent
        # writers can land in the same wall-clock second, and a second-resolution
        # timestamp would make filenames sort out of true write order — silently
        # reintroducing the chain-fork risk this module exists to close.
        filename = (
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}"
            f"_{action}_{uuid.uuid4().hex[:8]}.json"
        )
        (RECEIPTS_DIR / filename).write_text(json.dumps(receipt, indent=2))

        # Anchor write, same lock as the receipt write (§7 failure mode #3
        # — writing it outside the lock would let a race leave the anchor
        # and the receipt files disagreeing about the true head).
        _write_chain_head(receipt_hash, previous_count + 1)
    finally:
        lock_fd.close()

    return receipt


def load(limit: int = 50, action_filter: str | None = None) -> list[dict]:
    """Load recent receipts from disk, newest first."""
    if not RECEIPTS_DIR.exists():
        return []
    files = sorted(RECEIPTS_DIR.glob("*.json"), reverse=True)
    results = []
    for f in files:
        if len(results) >= limit:
            break
        try:
            r = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if action_filter and r.get("action") != action_filter:
            continue
        results.append(r)
    return results
