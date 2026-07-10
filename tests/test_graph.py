# tests/test_graph.py
"""Unit tests for the governance-graph resolver (seshat_tui/graph.py).

Pure data tests — no Textual, no App, no filesystem. Hand-built receipt/
rule dicts stand in for what receipts.load() / data.summarize_agreement_rules
would produce.
"""
from seshat_tui.graph import (
    ClaimNode,
    Edge,
    GovernanceGraph,
    GovernanceNode,
    InvariantNode,
    ReceiptListNode,
    ReceiptNode,
    RevocationNode,
    RuleNode,
)


def _denied_receipt(rule_canonical="forbid action is stop_orphan", receipt_hash="a" * 40):
    return {
        "receipt_hash": receipt_hash,
        "previous_hash": None,
        "timestamp": "2026-07-10T12:00:00+00:00",
        "actor": {"session_id": "tui_abc123", "agent_hint": "tui"},
        "action": "stop_orphan",
        "target": {"port": 4321},
        "result": {"status": "denied", "mode": "forbidden", "rule": rule_canonical, "reason": "no."},
        "environment_after": {"listening_ports": [4321]},
    }


def _success_receipt(receipt_hash="b" * 40):
    return {
        "receipt_hash": receipt_hash,
        "previous_hash": "a" * 40,
        "timestamp": "2026-07-10T12:05:00+00:00",
        "actor": {"session_id": "tui_abc123", "agent_hint": "tui"},
        "action": "start_project",
        "target": {"project": "demo"},
        "result": {"status": "success", "pid": 999},
        "environment_after": {"listening_ports": [4321, 8000]},
    }


def test_node_type_is_open_string_not_enum():
    node = ReceiptNode(_denied_receipt())
    assert isinstance(node.node_type, str)
    assert node.node_type == "receipt"


def test_denied_receipt_yields_decided_by_edge_to_rule():
    rule_canonical = "forbid action is stop_orphan"
    receipt = _denied_receipt(rule_canonical=rule_canonical)
    graph = GovernanceGraph(
        receipts=[receipt],
        agreement_rules=[],
        revocation_rules=[{"canonical": rule_canonical, "verb": "forbid", "window": "active"}],
    )
    node = ReceiptNode(receipt)
    edges = node.edges(graph)
    labels = [e.label for e in edges]
    assert "decided by this rule" in labels
    decided = next(e for e in edges if e.label == "decided by this rule")
    assert isinstance(decided.target, RevocationNode)
    assert decided.target.canonical == rule_canonical


def test_success_receipt_has_no_decided_by_edge():
    graph = GovernanceGraph(receipts=[], agreement_rules=[], revocation_rules=[])
    node = ReceiptNode(_success_receipt())
    labels = [e.label for e in node.edges(graph)]
    assert not any("decided by" in label for label in labels)


def test_permit_rule_with_matching_revocation_yields_overridden_by_edge():
    permit = "permit actor is claude-code and action is stop_orphan"
    revoke = "forbid action is stop_orphan"
    graph = GovernanceGraph(
        receipts=[],
        agreement_rules=[{"canonical": permit, "verb": "permit", "window": "active"}],
        revocation_rules=[{"canonical": revoke, "verb": "forbid", "window": "active"}],
    )
    node = RuleNode(permit, "permit", "active")
    edges = node.edges(graph)
    overridden = [e for e in edges if "overridden by" in e.label]
    assert len(overridden) == 1
    assert isinstance(overridden[0].target, RevocationNode)
    assert overridden[0].target.canonical == revoke


def test_permit_rule_without_matching_revocation_has_no_overridden_by_edge():
    permit = "permit actor is claude-code and action is start_project"
    revoke = "forbid action is stop_orphan"
    graph = GovernanceGraph(
        receipts=[],
        agreement_rules=[{"canonical": permit, "verb": "permit", "window": "active"}],
        revocation_rules=[{"canonical": revoke, "verb": "forbid", "window": "active"}],
    )
    node = RuleNode(permit, "permit", "active")
    edges = node.edges(graph)
    assert not any("overridden by" in e.label for e in edges)


def test_revocation_node_is_terminal_never_overridden():
    revoke = "forbid action is stop_orphan"
    graph = GovernanceGraph(receipts=[], agreement_rules=[], revocation_rules=[{"canonical": revoke, "verb": "forbid", "window": "active"}])
    node = RevocationNode(revoke, "forbid", "active")
    edges = node.edges(graph)
    assert not any("overridden by" in e.label for e in edges)


def test_rule_denied_count_edge_reflects_join_on_result_rule_field():
    rule_canonical = "forbid action is stop_orphan"
    receipts = [_denied_receipt(rule_canonical=rule_canonical, receipt_hash="a" * 40),
                _denied_receipt(rule_canonical=rule_canonical, receipt_hash="c" * 40),
                _success_receipt(receipt_hash="b" * 40)]
    graph = GovernanceGraph(receipts=receipts, agreement_rules=[], revocation_rules=[])
    node = RevocationNode(rule_canonical, "forbid", "active")
    edges = node.edges(graph)
    denied_edge = next(e for e in edges if "denied" in e.label)
    assert "2" in denied_edge.label
    assert isinstance(denied_edge.target, ReceiptListNode)
    assert len(denied_edge.target.receipt_list) == 2


def test_rule_by_canonical_returns_stale_synthetic_node_never_none_for_dangling_ref():
    graph = GovernanceGraph(receipts=[], agreement_rules=[], revocation_rules=[])
    node = graph.rule_by_canonical("forbid action is an_action_no_longer_in_the_agreement")
    assert node is not None
    assert node.stale is True
    assert "no longer in current Agreement" in node.render_detail()


def test_rule_by_canonical_finds_exact_match_in_agreement_rules():
    canonical = "permit actor is claude-code and action is start_project"
    graph = GovernanceGraph(
        receipts=[], agreement_rules=[{"canonical": canonical, "verb": "permit", "window": "active"}], revocation_rules=[],
    )
    node = graph.rule_by_canonical(canonical)
    assert node is not None
    assert node.stale is False
    assert node.canonical == canonical


def test_invariant_node_yields_one_edge_per_claim_plus_on_receipt_edge():
    receipt = _success_receipt()
    block = {
        "claims": [
            {"name": "ports_stable", "status": "verified"},
            {"name": "no_orphans", "status": "escalated", "escalation_reason": "orphan on 4321"},
        ],
        "converged": True,
        "total_cycles": 1,
        "harness_version": "0.1.1",
    }
    receipt["invariant"] = block
    node = InvariantNode(block, receipt)
    graph = GovernanceGraph(receipts=[receipt], agreement_rules=[], revocation_rules=[])
    edges = node.edges(graph)
    claim_edges = [e for e in edges if e.label.startswith("claim:")]
    receipt_edges = [e for e in edges if e.label.startswith("on receipt")]
    assert len(claim_edges) == 2
    assert all(isinstance(e.target, ClaimNode) for e in claim_edges)
    assert len(receipt_edges) == 1
    assert isinstance(receipt_edges[0].target, ReceiptNode)


def test_receipt_with_invariant_block_yields_verified_by_edge():
    receipt = _success_receipt()
    receipt["invariant"] = {"claims": [{"name": "x", "status": "verified"}], "converged": True, "total_cycles": 1, "harness_version": "0.1"}
    graph = GovernanceGraph(receipts=[receipt], agreement_rules=[], revocation_rules=[])
    node = ReceiptNode(receipt)
    edges = node.edges(graph)
    verified = [e for e in edges if "verified by Invariant" in e.label]
    assert len(verified) == 1
    assert isinstance(verified[0].target, InvariantNode)


def test_claim_node_edges_back_to_source_receipt():
    receipt = _success_receipt()
    claim = {"name": "ports_stable", "status": "verified"}
    node = ClaimNode(claim, receipt)
    graph = GovernanceGraph(receipts=[receipt], agreement_rules=[], revocation_rules=[])
    edges = node.edges(graph)
    assert len(edges) == 1
    assert edges[0].label.startswith("on receipt")
    assert isinstance(edges[0].target, ReceiptNode)


def test_receipt_list_node_edges_one_per_receipt():
    receipts = [_denied_receipt(receipt_hash="a" * 40), _denied_receipt(receipt_hash="c" * 40)]
    node = ReceiptListNode("denied by rule X", receipts)
    graph = GovernanceGraph(receipts=receipts, agreement_rules=[], revocation_rules=[])
    edges = node.edges(graph)
    assert len(edges) == 2
    assert all(isinstance(e.target, ReceiptNode) for e in edges)


def test_graph_py_imports_no_textual():
    import seshat_tui.graph as graph_mod
    with open(graph_mod.__file__) as f:
        source = f.read()
    assert "import textual" not in source
    assert "from textual" not in source
