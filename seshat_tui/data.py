"""data.py — read-only data-shaping helpers for the TUI domains.

Nothing in this module writes to ~/.seshat/. Verb/window/decision semantics
always defer to agreements.* (never re-derived here) per the build's
"no reimplemented enforcement logic" invariant.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import agreements
import liminate

SPARK_BLOCKS = "▁▂▃▄▅▆▇█"


def shorten_path(path: str) -> str:
    home = str(Path.home())
    return ("~" + path[len(home):]) if path.startswith(home) else path


def summarize_agreement_rules(text: str) -> list[dict]:
    """Parse `.limn` Agreement/revocations text and enumerate each rule.

    Mirrors seshat.py's _summarize_agreement_rules (see tests/
    test_dashboard_endpoints.py for the two-round bug history this
    gating avoids): gate on `r.canonical is None`, never on
    `r.status.name`, because enumeration never remembers actor/action/
    scope, so a perfectly well-formed rule routinely comes back
    ERROR_SEMANTIC anyway. Not imported from seshat.py to avoid pulling
    a Flask app into the TUI process; verb/window extraction still
    defers entirely to agreements._verb_of / agreements._temporal_window.
    """
    try:
        result = liminate.run(text, enter_phase2=False, auto_confirm_amber=True)
    except Exception as e:
        return [{"error": str(e)}]

    rules = []
    for r in result.results:
        if r.canonical is None:
            rules.append({"error": r.message or r.status.name})
            continue
        rule = {
            "canonical": r.canonical,
            "verb":      agreements._verb_of(r.canonical),
            "window":    agreements._temporal_window(r.canonical),
        }
        if r.line is not None:
            rule["line"] = r.line
        rules.append(rule)
    return rules


def sync_freshness(last_checked: str | None) -> str:
    """'fresh' (<=1h) / 'stale' (>1h) / 'never' (no timestamp).

    Threshold matches the judgment call already made for the dashboard's
    equivalent Revocations view, for cross-surface consistency.
    """
    if not last_checked:
        return "never"
    try:
        checked = datetime.fromisoformat(last_checked)
    except ValueError:
        return "never"
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - checked
    return "fresh" if age.total_seconds() <= 3600 else "stale"


def denial_count_for_rule(canonical: str, receipts: list[dict]) -> int:
    """Best-effort join: rule.canonical == receipt.result.rule on denied
    receipts. Field confirmed at mcp_server.py's _enforce() (result["rule"]
    = decision.rule on every denial)."""
    count = 0
    for r in receipts:
        result = r.get("result", {})
        if result.get("status") != "success" and result.get("rule") == canonical:
            count += 1
    return count


def build_sparkline(receipts: list[dict], buckets: int = 18) -> str:
    """Bucket receipt timestamps into `buckets` equal windows spanning the
    oldest-to-newest receipt and render as a block-height histogram."""
    if not receipts:
        return SPARK_BLOCKS[0] * buckets

    timestamps = []
    for r in receipts:
        ts = r.get("timestamp", "")
        try:
            timestamps.append(datetime.fromisoformat(ts))
        except ValueError:
            continue
    if not timestamps:
        return SPARK_BLOCKS[0] * buckets

    lo, hi = min(timestamps), max(timestamps)
    span = (hi - lo).total_seconds() or 1.0
    counts = [0] * buckets
    for ts in timestamps:
        idx = int((ts - lo).total_seconds() / span * (buckets - 1))
        counts[min(max(idx, 0), buckets - 1)] += 1

    peak = max(counts) or 1
    return "".join(
        SPARK_BLOCKS[min(int(c / peak * (len(SPARK_BLOCKS) - 1)), len(SPARK_BLOCKS) - 1)]
        for c in counts
    )


def last_invariant_block(receipts: list[dict]) -> tuple[dict | None, dict | None]:
    """Return (invariant_block, source_receipt) for the most recent receipt
    carrying an 'invariant' key, or (None, None). Never calls
    invariant_check.run_verification() — reads only what already happened."""
    for r in receipts:
        block = r.get("invariant")
        if block:
            return block, r
    return None, None
