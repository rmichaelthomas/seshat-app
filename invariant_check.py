"""
invariant_check.py — Post-action semantic verification via Invariant.

After a permitted action executes, if ~/.seshat/invariant.limn exists,
run the Invariant harness against it using the current environment
snapshot as claim ground truth. Returns the `invariant` block dict for
the receipt (the shape liminate-dev validates on ingest), or None when
no verification contract is present.

The verification "agent" is deterministic: it maps every claim source
to a string rendering of current environment facts. No LLM, no Narratia.
Escalation is recorded on the receipt; it never blocks the action.
"""

from __future__ import annotations

import dataclasses
import json
from importlib.metadata import version as _pkg_version

import agreements

try:
    import liminate_invariant as invariant
    from liminate_invariant import AgentResponse, AgentTask
    _INVARIANT_AVAILABLE = True
except ImportError:
    _INVARIANT_AVAILABLE = False


def _harness_version() -> str | None:
    try:
        return _pkg_version("liminate-invariant")
    except Exception:
        return None


def _source_names(contract: str) -> set[str]:
    """Return every bare source-name identifier a require/forbid condition
    references (the name each `remember a source called <name>` injection
    must supply). Claim text from identify_claims() is the full statement
    line, not usable as a source name directly (compose_source rejects any
    name containing whitespace) -- this walks the token stream instead to
    pull out just the identifier immediately left of each comparison."""
    from liminate import tokenize, reorder

    names: set[str] = set()
    for line in contract.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        tokens = reorder(tokenize(line))
        if not isinstance(tokens, list):
            continue
        verb_token = next((t for t in tokens if t.type.name == "VERB"), None)
        if verb_token is None or verb_token.value not in ("require", "forbid"):
            continue
        for i, tok in enumerate(tokens[:-1]):
            if tok.type.name == "UNKNOWN" and tokens[i + 1].type.name == "OPERATOR":
                names.add(tok.value)
    return names


def _env_agent(env_snapshot: dict):
    """Build an AgentTask->AgentResponse callable that answers every claim
    with a deterministic rendering of the environment snapshot. The
    verification contract's claims (require/forbid) are checked by the
    interpreter against these injected facts."""
    facts = json.dumps(env_snapshot, sort_keys=True)

    def _agent(task: "AgentTask") -> "AgentResponse":
        # Every claim source resolves to the same environment fact string.
        # Contracts written for Seshat verification reference environment
        # facts; the interpreter evaluates require/forbid against them.
        return AgentResponse(claims={name: facts for name in _source_names(task.contract)})

    return _agent


def run_verification(env_snapshot: dict) -> dict | None:
    """Run the Invariant verification contract against the environment.

    Returns the invariant block dict for the receipt, or None when no
    contract exists or the harness is unavailable. Never raises into the
    caller — a verification failure is recorded, not propagated as an
    exception that would break receipt emission.
    """
    if not _INVARIANT_AVAILABLE:
        return None

    contract = agreements.load_invariant()
    if contract is None:
        return None

    try:
        result = invariant.run(contract, _env_agent(env_snapshot), max_cycles=1)
    except Exception as e:
        # A harness error must not break the action or its receipt. Record
        # it as a degenerate block so the developer sees something went wrong.
        return {
            "claims": [],
            "total_cycles": 0,
            "converged": False,
            "inherited_handlers": [],
            "new_handlers": [],
            "harness_version": _harness_version(),
            "error": f"invariant harness error: {e}",
        }

    block = dataclasses.asdict(result)
    block.pop("final", None)  # ContractResult is not JSON-serializable
    block["harness_version"] = _harness_version()
    return block
