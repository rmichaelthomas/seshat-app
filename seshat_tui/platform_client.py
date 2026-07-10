"""platform_client.py — read-only platform query client (TI-Q4, v1.0i §49-50).

Fetches Sentinel verdicts joined by agreement_hash for the drill graph's
SentinelVerdictNode (graph.py). Never writes anything back to the platform
or to ~/.seshat/*.limn — advisory display data only. Mirrors the
httpx + Bearer-auth pattern already established by
seshat_tui/domains/receipts.py's _do_receipts_sync.
"""

from __future__ import annotations

import httpx

RECEIPTS_API_DEFAULT = "https://liminate.dev"


def fetch_sentinel_verdicts(
    api_base: str, api_key: str, agreement_hashes: list[str]
) -> dict[str, dict]:
    """POST the hashes to the platform's verdict-lookup endpoint (Phase B);
    return {agreement_hash: verdict_dict}. Read-only. Raises httpx.HTTPError
    on a network/HTTP failure — the caller (app.py's best-effort graph
    rebuild) already guards every call site and degrades to an empty map."""
    if not agreement_hashes:
        return {}
    resp = httpx.post(
        f"{api_base}/api/v1/sentinels/verdicts-by-agreement",
        json={"agreement_hashes": agreement_hashes},
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json().get("verdicts", {})
