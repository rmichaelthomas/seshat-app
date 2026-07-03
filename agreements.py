"""
agreements.py — Agreement loading, fact composition, and enforcement decisions.

An Agreement is a Liminate contract at ~/.seshat/agreement.limn expressing
which (actor, action, scope) tuples an agent may act on. Enforcement is
deny-by-default: no Agreement, no matching permit, or any evaluation error
all deny. A matching forbid always wins over a matching permit.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import liminate

AGREEMENT_PATH = Path.home() / ".seshat" / "agreement.limn"

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
) -> Decision:
    """Evaluate whether `actor` may perform `action` (optionally scoped).

    `scope` is always remembered as a fact — sentinel "none" when the call
    has no project/group target — so an Agreement conditioning on scope
    never hits an unknown-name error on scope-less actions.
    """
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

    composed = (
        f'remember a string called actor with "{actor}"\n'
        f'remember a string called action with "{action}"\n'
        f'remember a string called scope with "{scope_value}"\n'
        "\n"
        f"{agreement_text}"
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

    for r in result.results:
        if r.status.name == "PROHIBITION_VIOLATED":
            return Decision(
                allowed=False,
                mode="forbidden",
                rule=r.canonical,
                reason=r.message or "Forbidden by Agreement.",
            )

    for r in result.results:
        if _verb_of(r.canonical) == "permit" and r.output:
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
