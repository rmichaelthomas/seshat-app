"""graph.py — the governance graph: pure data resolution over receipts,
Agreement rules, and revocations. No Textual imports (unit-testable
without a running app) — DrillScreen (screens.py) is the only consumer
that renders this.

Trust boundary: nothing here writes to ~/.seshat/. edges() never calls
agreements.check_action() to decide anything; the permit<->revocation
"overridden by" edge is a display heuristic (see revocation_overriding),
never an enforcement result.

Forward-compat (TI-Q4): node_type is a free string, not a closed enum.
A future Sentinel node slots in as a new GovernanceNode subclass without
touching DrillScreen, which renders any node via render_detail() + edges()
only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import agreements

from .colors import COLORS, DOMAIN_ACCENTS, DOMAIN_GLYPHS

# Mirrors seshat_tui/domains/invariant.py's _STATUS_STYLE — duplicated
# rather than imported to avoid a graph.py <-> domains/ import cycle
# (domains/invariant.py imports node classes from this module).
_CLAIM_STATUS_STYLE = {"verified": COLORS["green"], "corrected": COLORS["cyan"], "escalated": COLORS["red"]}


@dataclass
class Edge:
    label: str
    target: "GovernanceNode"


class GovernanceNode:
    """Base for every drillable authority-graph object. Concrete subclasses
    set node_type/glyph/accent/title in their own __init__ (a plain base
    class, not typing.Protocol, so they can share this constructor) and
    override render_detail()/edges()."""

    def __init__(self, node_type: str, glyph: str, accent: str | None, title: str) -> None:
        self.node_type = node_type
        self.glyph = glyph
        self.accent = accent
        self.title = title

    def render_detail(self) -> str:
        raise NotImplementedError

    def edges(self, graph: "GovernanceGraph") -> list[Edge]:
        raise NotImplementedError


def _short_hash(value: str | None, n: int = 12) -> str:
    return f"{value[:n]}…" if value else "—"


class ReceiptNode(GovernanceNode):
    def __init__(self, receipt: dict) -> None:
        self.receipt = receipt
        receipt_hash = receipt.get("receipt_hash", "")
        super().__init__(
            node_type="receipt",
            glyph=DOMAIN_GLYPHS["receipts"],
            accent=DOMAIN_ACCENTS["receipts"],
            title=f"receipt {receipt_hash[:8]}…" if receipt_hash else "receipt",
        )

    def render_detail(self) -> str:
        r = self.receipt
        result = r.get("result", {})
        status = result.get("status", "?")
        status_color = COLORS["green"] if status == "success" else COLORS["red"]
        actor = r.get("actor", {})
        target = r.get("target", {})
        lines = [
            f"[b]Receipt {_short_hash(r.get('receipt_hash'))}[/b]",
            f"[#9A8B6E]{r.get('timestamp', '—')}[/#9A8B6E]",
            "",
            "[#9A8B6E b]ACTOR[/#9A8B6E b]",
            f"session   {actor.get('session_id') or '—'}",
            f"agent     {actor.get('agent_hint') or '—'}",
            "",
            "[#9A8B6E b]ACTION[/#9A8B6E b]",
            r.get("action", "—"),
            "",
            "[#9A8B6E b]TARGET[/#9A8B6E b]",
            json.dumps(target, separators=(",", ":")),
            "",
            "[#9A8B6E b]RESULT[/#9A8B6E b]",
            f"[{status_color}]{status}[/{status_color}]",
        ]
        if status != "success":
            reason = result.get("reason") or result.get("error") or "—"
            lines.append(f"reason    [#DD6E5A]{reason}[/#DD6E5A]")
        lines += [
            "",
            "[#9A8B6E b]CHAIN[/#9A8B6E b]",
            f"prev      {_short_hash(r.get('previous_hash'))}",
            f"hash      {_short_hash(r.get('receipt_hash'))}",
        ]
        return "\n".join(lines)

    def edges(self, graph: "GovernanceGraph") -> list[Edge]:
        edges: list[Edge] = []
        result = self.receipt.get("result", {})
        rule_canonical = result.get("rule")
        if rule_canonical:
            rule_node = graph.rule_by_canonical(rule_canonical)
            if rule_node is not None:
                edges.append(Edge("decided by this rule", rule_node))
        block = self.receipt.get("invariant")
        if block:
            n = len(block.get("claims", []))
            edges.append(Edge(f"verified by Invariant ({n} claims)", InvariantNode(block, self.receipt)))
        # TI-Q4 (v1.0i §49-50) — exact agreement_hash join to a platform
        # Sentinel verdict. Offline-additive: no verdict for this hash (no
        # platform key, no sentinel registered, or the fetch failed this
        # cycle) means simply no edge here, same as at f32b601.
        verdict = graph.sentinel_verdict_for(self.receipt.get("agreement_hash"))
        if verdict is not None:
            edges.append(Edge(f"watched by Sentinel ({verdict.get('verdict')})", SentinelVerdictNode(verdict)))
        return edges


class RuleNode(GovernanceNode):
    def __init__(
        self,
        canonical: str,
        verb: str | None,
        window: str,
        *,
        is_revocation: bool = False,
        stale: bool = False,
    ) -> None:
        self.canonical = canonical
        self.verb = verb
        self.window = window
        self.is_revocation = is_revocation
        self.stale = stale
        glyph = DOMAIN_GLYPHS["revocations"] if is_revocation else DOMAIN_GLYPHS["agreements"]
        accent = DOMAIN_ACCENTS["revocations"] if is_revocation else DOMAIN_ACCENTS["agreements"]
        short = canonical if len(canonical) <= 28 else canonical[:27] + "…"
        super().__init__(
            node_type="revocation" if is_revocation else "rule",
            glyph=glyph,
            accent=accent,
            title=f"rule {short}",
        )

    def render_detail(self) -> str:
        verb = self.verb or "?"
        verb_color = COLORS["red"] if verb == "forbid" else COLORS["green"]
        source = "revocation (platform)" if self.is_revocation else "agreement"
        lines = [
            f"[b]{'Revocation' if self.is_revocation else 'Rule'}[/b]",
            f"[{verb_color} b]{verb}[/{verb_color} b]  [#9A8B6E]· {self.window} · {source}[/#9A8B6E]",
        ]
        if self.stale:
            lines.append("[#DD6E5A]⚠ rule no longer in current Agreement[/#DD6E5A]")
        lines += ["", "[#9A8B6E b]CANONICAL[/#9A8B6E b]", self.canonical]
        return "\n".join(lines)

    def edges(self, graph: "GovernanceGraph") -> list[Edge]:
        edges: list[Edge] = []
        denied = graph.receipts_denied_by(self.canonical)
        if denied:
            edges.append(Edge(f"denied {len(denied)} receipt(s)", ReceiptListNode(f"denied by {self.title}", denied)))
        if not self.is_revocation and self.verb == "permit":
            revocation_node = graph.revocation_overriding(self.canonical)
            if revocation_node is not None:
                edges.append(Edge("overridden by revocation", revocation_node))
        return edges


class RevocationNode(RuleNode):
    """A RuleNode specialization: always is_revocation=True, glyph/accent
    forced to revocations' ⊘/red. Terminal upward — RuleNode.edges()
    already skips the 'overridden by' check when is_revocation is True, so
    a revocation is never shown as itself overridden."""

    def __init__(self, canonical: str, verb: str | None, window: str, *, stale: bool = False) -> None:
        super().__init__(canonical, verb, window, is_revocation=True, stale=stale)


class InvariantNode(GovernanceNode):
    def __init__(self, block: dict, source_receipt: dict) -> None:
        self.block = block
        self.source_receipt = source_receipt
        super().__init__(
            node_type="invariant",
            glyph=DOMAIN_GLYPHS["invariant"],
            accent=COLORS["cyan"],
            title="invariant block",
        )

    def render_detail(self) -> str:
        block = self.block
        converged = "converged" if block.get("converged") else "did not converge"
        cycles = block.get("total_cycles", 0)
        version = block.get("harness_version") or "?"
        lines = [
            "[b]Invariant verification[/b]",
            f"[#9A8B6E]{converged} · {cycles} cycle(s) · harness v{version}[/#9A8B6E]",
            "",
            "[#9A8B6E b]CLAIMS[/#9A8B6E b]",
        ]
        for claim in block.get("claims", []):
            status = claim.get("status", "?")
            style = _CLAIM_STATUS_STYLE.get(status, COLORS["text_3"])
            lines.append(f"[{style}]{claim.get('name', '?')} · {status}[/{style}]")
        return "\n".join(lines)

    def edges(self, graph: "GovernanceGraph") -> list[Edge]:
        edges = [Edge(f"claim: {c.get('name', '?')}", ClaimNode(c, self.source_receipt)) for c in self.block.get("claims", [])]
        receipt_hash = self.source_receipt.get("receipt_hash", "")
        if receipt_hash:
            edges.append(Edge(f"on receipt {receipt_hash[:8]}…", ReceiptNode(self.source_receipt)))
        return edges


class ClaimNode(GovernanceNode):
    def __init__(self, claim: dict, source_receipt: dict) -> None:
        self.claim = claim
        self.source_receipt = source_receipt
        status = claim.get("status", "?")
        accent = _CLAIM_STATUS_STYLE.get(status, COLORS["text_3"])
        super().__init__(
            node_type="claim",
            glyph=DOMAIN_GLYPHS["invariant"],
            accent=accent,
            title=f"claim {claim.get('name', '?')}",
        )

    def render_detail(self) -> str:
        claim = self.claim
        status = claim.get("status", "?")
        style = _CLAIM_STATUS_STYLE.get(status, COLORS["text_3"])
        lines = [f"[b]{claim.get('name', '?')}[/b]", f"[{style}]{status}[/{style}]"]
        if claim.get("escalation_reason"):
            lines += ["", "[#9A8B6E b]ESCALATION[/#9A8B6E b]", claim["escalation_reason"]]
        if claim.get("cycles") is not None:
            lines += ["", f"cycles    {claim['cycles']}"]
        env = self.source_receipt.get("environment_after", {})
        lines += [
            "",
            "[#9A8B6E b]ENVIRONMENT (FROM RECEIPT)[/#9A8B6E b]",
            f"listening_ports: {env.get('listening_ports', [])}",
        ]
        return "\n".join(lines)

    def edges(self, graph: "GovernanceGraph") -> list[Edge]:
        receipt_hash = self.source_receipt.get("receipt_hash", "")
        if not receipt_hash:
            return []
        return [Edge(f"on receipt {receipt_hash[:8]}…", ReceiptNode(self.source_receipt))]


class ReceiptListNode(GovernanceNode):
    """Aggregate node for 'denied N receipts' edges. Not selectable
    row-by-row — its own edges() returns one edge per receipt, keeping the
    drill uniform (every deeper step is an edge selection)."""

    def __init__(self, label: str, receipts: list[dict]) -> None:
        self.label = label
        self.receipt_list = receipts
        super().__init__(
            node_type="receipt_list",
            glyph=DOMAIN_GLYPHS["receipts"],
            accent=DOMAIN_ACCENTS["receipts"],
            title=label,
        )

    def render_detail(self) -> str:
        if not self.receipt_list:
            return f"[b]{self.label}[/b]\n\n[#9A8B6E](no receipts)[/#9A8B6E]"
        lines = [f"[b]{self.label}[/b]", ""]
        for idx, r in enumerate(self.receipt_list, start=1):
            ts = (r.get("timestamp") or "")[:19].replace("T", " ")
            action = r.get("action", "—")
            target = r.get("target", {})
            target_str = target.get("project") or target.get("group") or target.get("key") or target.get("port") or "—"
            status = r.get("result", {}).get("status", "?")
            status_color = COLORS["green"] if status == "success" else COLORS["red"]
            lines.append(f"{idx}. [#9A8B6E]{ts}[/#9A8B6E] {action} · {target_str} [{status_color}]{status}[/{status_color}]")
        return "\n".join(lines)

    def edges(self, graph: "GovernanceGraph") -> list[Edge]:
        edges = []
        for r in self.receipt_list:
            receipt_hash = r.get("receipt_hash", "")
            label = f"→ {receipt_hash[:8]}…" if receipt_hash else "→ receipt"
            edges.append(Edge(label, ReceiptNode(r)))
        return edges


class SentinelVerdictNode(GovernanceNode):
    """A platform Sentinel verdict about the Agreement a receipt was decided
    under (TI-Q4, v1.0i §49-50). Advisory display only — never written to
    ~/.seshat/*.limn, never influences enforcement. Reached from a
    ReceiptNode via the agreement_hash join. Terminal: a verdict is a leaf
    in the local graph."""

    def __init__(self, verdict: dict) -> None:
        self.verdict = verdict
        v = verdict.get("verdict") or "unknown"
        accent = {
            "holding": COLORS["green"],
            "drifted": COLORS["red"],
            "expired": COLORS["orange"],
        }.get(v, COLORS["text_3"])
        super().__init__(
            node_type="sentinel_verdict",
            glyph=DOMAIN_GLYPHS["invariant"],
            accent=accent,
            title=f"sentinel · {v}",
        )

    def render_detail(self) -> str:
        v = self.verdict
        status = v.get("verdict") or "unknown"
        style = {
            "holding": COLORS["green"],
            "drifted": COLORS["red"],
            "expired": COLORS["orange"],
        }.get(status, COLORS["text_3"])
        lines = [
            "[b]Sentinel verdict[/b]",
            f"[{style}]{status}[/{style}]",
        ]
        if v.get("reason"):
            lines += ["", "[#9A8B6E b]REASON[/#9A8B6E b]", v["reason"]]
        lines += [
            "",
            "[#9A8B6E b]SENTINEL[/#9A8B6E b]",
            f"id          {v.get('sentinel_id') or '—'}",
            f"last run    {v.get('last_run_at') or '—'}",
            f"agreement   {_short_hash(v.get('agreement_hash'))}",
        ]
        return "\n".join(lines)

    def edges(self, graph: "GovernanceGraph") -> list[Edge]:
        return []


def _condition_tokens(canonical: str) -> set[str]:
    """Word-level token set of a canonical rule's condition clause (the
    part after the verb). Used only by revocation_overriding's display
    heuristic below — reuses agreements._verb_of to find the verb (never
    re-derives verb semantics) and then simply slices past it."""
    verb = agreements._verb_of(canonical)
    words = canonical.split()
    if verb and verb in words:
        words = words[words.index(verb) + 1 :]
    return {w.strip('"') for w in words if w not in ("and", "is")}


class GovernanceGraph:
    """Holds a snapshot of receipts + Agreement/revocation rules and
    resolves cross-references between them. Rebuilt on each app refresh
    cycle from the same data the domains already load — never re-reads
    ~/.seshat/ on its own."""

    def __init__(
        self,
        receipts: list[dict],
        agreement_rules: list[dict],
        revocation_rules: list[dict],
        sentinel_verdicts: dict[str, dict] | None = None,
    ) -> None:
        self.receipts = receipts or []
        self.agreement_rules = agreement_rules or []
        self.revocation_rules = revocation_rules or []
        # TI-Q4 (v1.0i §50) — agreement_hash -> verdict dict. Empty/None when
        # there's no platform key, no registered sentinel, or the best-effort
        # fetch failed this cycle; the graph works offline exactly as before.
        self.sentinel_verdicts = sentinel_verdicts or {}

    def rule_by_canonical(self, canonical: str | None) -> RuleNode | None:
        if not canonical:
            return None
        for r in self.agreement_rules:
            if "error" not in r and r.get("canonical") == canonical:
                return RuleNode(r["canonical"], r.get("verb"), r.get("window", "unbounded"))
        for r in self.revocation_rules:
            if "error" not in r and r.get("canonical") == canonical:
                return RevocationNode(r["canonical"], r.get("verb"), r.get("window", "unbounded"))
        # Dangling reference (e.g. the Agreement changed since this receipt
        # was written): render a synthetic, clearly-marked node rather than
        # returning None into an edge that promised a rule.
        return RuleNode(canonical, agreements._verb_of(canonical), agreements._temporal_window(canonical), stale=True)

    def receipts_denied_by(self, canonical: str) -> list[dict]:
        """Best-effort join: rule.canonical == receipt.result.rule on
        denied receipts. Mirrors data.denial_count_for_rule's join but
        returns the receipts themselves. Field confirmed at
        mcp_server.py's _enforce() (result['rule'] = decision.rule, only
        ever set on a denial)."""
        return [
            r
            for r in self.receipts
            if r.get("result", {}).get("status") != "success" and r.get("result", {}).get("rule") == canonical
        ]

    def sentinel_verdict_for(self, agreement_hash: str | None) -> dict | None:
        """Exact agreement_hash match only — this is a hard join key (TI-Q4
        D2, v1.0i §50), never a heuristic like revocation_overriding below."""
        if not agreement_hash:
            return None
        return self.sentinel_verdicts.get(agreement_hash)

    def revocation_overriding(self, permit_canonical: str) -> RevocationNode | None:
        """Display-only heuristic — NOT enforcement. A revocation is shown
        as 'overriding' a permit when the revocation's condition tokens
        are a subset of the permit's (no more specific than the permit on
        every dimension it names). This can over-match (a candidate shown
        that isn't a true structural overlap) but is deliberately biased
        against under-matching: a missed override would hide the answer to
        'why was this denied?', which is worse than an extra candidate for
        a human to inspect. Mirrors the same judgment call already made
        for the dashboard's Agreements view (simple substring/field
        comparison, not real semantic overlap detection)."""
        permit_tokens = _condition_tokens(permit_canonical)
        for r in self.revocation_rules:
            if "error" in r:
                continue
            revocation_tokens = _condition_tokens(r["canonical"])
            if revocation_tokens and revocation_tokens.issubset(permit_tokens):
                return RevocationNode(r["canonical"], r.get("verb"), r.get("window", "unbounded"))
        return None
