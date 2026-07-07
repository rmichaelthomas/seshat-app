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
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from registry import Registry
from scanner import Scanner

registry = Registry()
scanner = Scanner()

# ── Receipt storage ─────────────────────────────────────────────────────────

RECEIPTS_DIR = Path.home() / ".seshat" / "receipts"
RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)

LOCK_PATH = RECEIPTS_DIR / ".chain.lock"


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


def recover_chain_head() -> str | None:
    """Read the most recent receipt file and return its receipt_hash, or None."""
    try:
        files = sorted(RECEIPTS_DIR.glob("*.json"))
        if not files:
            return None
        last = json.loads(files[-1].read_text())
        return last.get("receipt_hash")
    except (json.JSONDecodeError, OSError, KeyError):
        return None


def emit(
    action: str,
    target: dict,
    result: dict,
    env_before: dict,
    session_id: str,
    actor_type: str,
    agent_hint: str,
    env_after: dict | None = None,
) -> None:
    """Write a hash-chained receipt to disk with file locking.

    This is the ONLY function that writes receipt files. The chain head is
    re-read from disk under an exclusive lock immediately before building
    the receipt, so concurrent writers never fork the chain.
    """
    if env_after is None:
        env_after = snapshot()

    lock_fd = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        previous_hash = recover_chain_head()

        receipt = {
            "type": "machine_action",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "actor": {
                "type": actor_type,
                "session_id": session_id,
                "agent_hint": agent_hint,
            },
            "action": action,
            "target": target,
            "result": result,
            "environment_before": env_before,
            "environment_after": env_after,
            "previous_hash": previous_hash,
        }

        canonical = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
        receipt_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
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
    finally:
        lock_fd.close()


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
