"""F-11: the MCP `_enforce()` gate was convention-only — each tool called
it by hand as its first statement, so a newly-added tool that forgot to
could register and serve unenforced with nothing catching it. The gate is
now structural: tools are registered through `_enforced_tool(...)`, which
always calls `_enforce()` before the tool body runs regardless of what
the body does, and `_assert_all_tools_enforced()` audits every registered
tool at import time and raises if any lack the marker `_enforced_tool`
stamps — so a bare `@mcp.tool()` addition fails the whole module import,
not just a code review.
"""
import pytest

import mcp_server


def test_module_import_passes_its_own_structural_audit():
    """mcp_server.py calls _assert_all_tools_enforced() at module load —
    the fact that it imported at all (collected above) already proves
    this, but assert it explicitly and by name."""
    mcp_server._assert_all_tools_enforced()


def test_all_ten_tools_are_registered():
    names = {tool.name for tool in mcp_server.mcp._tool_manager.list_tools()}
    assert names == {
        "start_project", "stop_project", "start_group", "stop_group",
        "register_project", "stop_orphan", "set_secret",
        "set_project_override", "amend_agreement", "attenuate_identity",
    }


def test_every_registered_tool_carries_the_enforced_marker():
    for tool in mcp_server.mcp._tool_manager.list_tools():
        assert getattr(tool.fn, mcp_server._ENFORCED_MARKER, False), (
            f"tool {tool.name!r} is registered without the structural "
            "enforcement gate"
        )


def test_tool_registered_without_the_gate_fails_the_structural_audit():
    """A hypothetical new tool that uses bare @mcp.tool() (forgetting
    @_enforced_tool) must be caught — not silently served unenforced."""
    @mcp_server.mcp.tool()
    def _unenforced_test_tool() -> str:
        return "should never have registered unenforced"

    try:
        with pytest.raises(RuntimeError, match="_unenforced_test_tool"):
            mcp_server._assert_all_tools_enforced()
    finally:
        mcp_server.mcp._tool_manager.remove_tool("_unenforced_test_tool")


def test_enforced_tool_denies_before_the_body_runs(monkeypatch):
    """The gate fires even if the tool body would otherwise blow up —
    proving enforcement happens structurally before dispatch, not as a
    convention the body chooses to honor."""
    def _boom(*a, **k):
        raise AssertionError("tool body must not run when denied")
    monkeypatch.setattr(mcp_server, "registry", type("R", (), {"get": _boom})())
    monkeypatch.setattr(mcp_server.agreements, "load_agreement", lambda: None)

    result = mcp_server.start_project(name="whatever")
    assert "No Agreement exists" in result


def test_enforced_tool_still_calls_the_body_when_permitted(monkeypatch):
    monkeypatch.setattr(
        mcp_server.agreements,
        "load_agreement",
        lambda: 'permit actor is "unknown-agent" and action is "stop_orphan"',
    )
    monkeypatch.setattr(mcp_server.scanner, "scan", lambda: {})
    monkeypatch.setattr(mcp_server.receipts, "snapshot", lambda: {
        "listening_ports": [], "managed_projects": {},
    })

    import json
    result = json.loads(mcp_server.stop_orphan(port=4242))
    assert result["status"] == "failure"
    assert "No process found" in result["error"]


class TestIdentityTokenWiring:
    """Identity-plane Stage 1: SESHAT_IDENTITY_TOKEN, when present and
    valid, becomes the actor (overriding MCP_AGENT_HINT) and every emitted
    receipt for that call carries identity_verified: true."""

    def test_agreement_actor_falls_back_without_a_token(self, monkeypatch):
        monkeypatch.delenv("SESHAT_IDENTITY_TOKEN", raising=False)
        monkeypatch.setenv("MCP_AGENT_HINT", "claude-code")
        assert mcp_server._agreement_actor() == "claude-code"

    def test_agreement_actor_uses_verified_identifier_with_a_valid_token(self, monkeypatch):
        import identity
        token = identity.mint("agent-x")
        monkeypatch.setenv("SESHAT_IDENTITY_TOKEN", token)
        monkeypatch.setenv("MCP_AGENT_HINT", "claude-code")
        assert mcp_server._agreement_actor() == "agent-x"

    def test_agreement_actor_falls_back_on_an_invalid_token(self, monkeypatch):
        monkeypatch.setenv("SESHAT_IDENTITY_TOKEN", "not-a-real-token")
        monkeypatch.setenv("MCP_AGENT_HINT", "claude-code")
        assert mcp_server._agreement_actor() == "claude-code"

    def test_enforced_tool_permits_with_a_valid_token_and_matching_agreement(self, monkeypatch):
        import identity
        token = identity.mint("agent-x")
        monkeypatch.setenv("SESHAT_IDENTITY_TOKEN", token)
        monkeypatch.setattr(
            mcp_server.agreements, "load_agreement",
            lambda: 'permit actor is "agent-x" and action is "stop_orphan"',
        )
        monkeypatch.setattr(mcp_server.scanner, "scan", lambda: {})
        monkeypatch.setattr(mcp_server.receipts, "snapshot", lambda: {
            "listening_ports": [], "managed_projects": {},
        })

        import json
        result = json.loads(mcp_server.stop_orphan(port=4242))
        assert result["status"] == "failure"
        assert "No process found" in result["error"]

    def test_enforced_tool_denies_identity_invalid_with_a_forged_token(self, monkeypatch):
        import identity
        token = identity.mint("agent-x")
        header_b64, payload_b64, sig_b64 = token.split(".")
        forged = f"{header_b64}.{payload_b64}." + (("A" if sig_b64[0] != "A" else "B") + sig_b64[1:])
        monkeypatch.setenv("SESHAT_IDENTITY_TOKEN", forged)
        monkeypatch.setattr(
            mcp_server.agreements, "load_agreement",
            lambda: 'permit actor is "agent-x" and action is "stop_orphan"',
        )
        monkeypatch.setattr(mcp_server.receipts, "snapshot", lambda: {
            "listening_ports": [], "managed_projects": {},
        })

        result = mcp_server.stop_orphan(port=4242)
        assert "DENIED" in result
        assert "Identity token failed verification" in result

    def test_emit_marks_identity_verified_true_when_token_present(self, monkeypatch, tmp_path):
        import identity
        token = identity.mint("agent-x")
        monkeypatch.setenv("SESHAT_IDENTITY_TOKEN", token)
        monkeypatch.setattr(mcp_server.receipts, "RECEIPTS_DIR", tmp_path)
        monkeypatch.setattr(mcp_server.receipts, "LOCK_PATH", tmp_path / ".chain.lock")
        monkeypatch.setattr(mcp_server.receipts, "CHAIN_HEAD_PATH", tmp_path / ".chain_head")
        monkeypatch.setattr(
            mcp_server.agreements, "load_agreement",
            lambda: 'permit actor is "agent-x" and action is "stop_orphan"',
        )
        monkeypatch.setattr(mcp_server.scanner, "scan", lambda: {})
        monkeypatch.setattr(mcp_server.receipts, "snapshot", lambda: {
            "listening_ports": [], "managed_projects": {},
        })

        mcp_server.stop_orphan(port=4242)
        import json
        files = sorted(tmp_path.glob("*.json"))
        assert len(files) == 1
        receipt = json.loads(files[0].read_text())
        assert receipt["actor"]["identity_verified"] is True
        assert receipt["actor"]["agent_hint"] == "agent-x"

    def test_emit_marks_identity_verified_false_without_a_token(self, monkeypatch, tmp_path):
        monkeypatch.delenv("SESHAT_IDENTITY_TOKEN", raising=False)
        monkeypatch.setenv("MCP_AGENT_HINT", "claude-code")
        monkeypatch.setattr(mcp_server.receipts, "RECEIPTS_DIR", tmp_path)
        monkeypatch.setattr(mcp_server.receipts, "LOCK_PATH", tmp_path / ".chain.lock")
        monkeypatch.setattr(mcp_server.receipts, "CHAIN_HEAD_PATH", tmp_path / ".chain_head")
        monkeypatch.setattr(
            mcp_server.agreements, "load_agreement",
            lambda: 'permit actor is "claude-code" and action is "stop_orphan"',
        )
        monkeypatch.setattr(mcp_server.scanner, "scan", lambda: {})
        monkeypatch.setattr(mcp_server.receipts, "snapshot", lambda: {
            "listening_ports": [], "managed_projects": {},
        })

        mcp_server.stop_orphan(port=4242)
        import json
        files = sorted(tmp_path.glob("*.json"))
        receipt = json.loads(files[0].read_text())
        assert receipt["actor"]["identity_verified"] is False


class TestDelegation:
    """Identity-plane Stage 2: attenuate_identity is the one agent-reachable
    identity-issuing verb — narrowing only, never broadening. mint stays
    CLI-only (asserted in test_identity_cli.py's
    test_mint_is_not_an_mcp_tool, extended here to also assert attenuate's
    presence)."""

    def test_attenuate_present_mint_absent_in_mcp_tool_set(self):
        names = {tool.name for tool in mcp_server.mcp._tool_manager.list_tools()}
        assert "attenuate_identity" in names
        assert "mint" not in names
        assert not any(n == "mint" or "mint_identity" in n for n in names)

    def test_agreement_actor_returns_leaf_for_a_delegated_token(self, monkeypatch):
        import identity
        root = identity.mint("agent-root")
        child = identity.attenuate(root, [], delegate_to="agent-child")
        monkeypatch.setenv("SESHAT_IDENTITY_TOKEN", child)
        assert mcp_server._agreement_actor() == "agent-child"

    def test_agreement_actor_returns_root_identifier_when_undelegated(self, monkeypatch):
        import identity
        token = identity.mint("agent-root")
        monkeypatch.setenv("SESHAT_IDENTITY_TOKEN", token)
        assert mcp_server._agreement_actor() == "agent-root"

    def test_attenuate_identity_tool_denied_by_default_without_agreement_permit(self, monkeypatch):
        import identity
        root = identity.mint("agent-root")
        monkeypatch.setenv("SESHAT_IDENTITY_TOKEN", root)
        monkeypatch.setattr(mcp_server.agreements, "load_agreement", lambda: None)

        result = mcp_server.attenuate_identity(token=root, caveats=['forbid action is "wipe_disk"'])
        assert "DENIED" in result

    def test_attenuate_identity_tool_succeeds_when_permitted_and_returns_new_token(self, monkeypatch, tmp_path):
        import identity
        import json as json_module

        root = identity.mint("agent-root")
        monkeypatch.setenv("SESHAT_IDENTITY_TOKEN", root)
        monkeypatch.setattr(
            mcp_server.agreements, "load_agreement",
            lambda: 'permit actor is "agent-root" and action is "attenuate_identity"',
        )
        monkeypatch.setattr(mcp_server.receipts, "RECEIPTS_DIR", tmp_path)
        monkeypatch.setattr(mcp_server.receipts, "LOCK_PATH", tmp_path / ".chain.lock")
        monkeypatch.setattr(mcp_server.receipts, "CHAIN_HEAD_PATH", tmp_path / ".chain_head")
        monkeypatch.setattr(mcp_server.receipts, "snapshot", lambda: {
            "listening_ports": [], "managed_projects": {},
        })

        result = json_module.loads(mcp_server.attenuate_identity(
            token=root, caveats=['forbid action is "wipe_disk"'], delegate_to="agent-child",
        ))
        assert result["status"] == "success"
        new_token = result["token"]
        verified = identity.verify(new_token)
        assert verified is not None
        assert verified.delegation_path == ["agent-root", "agent-child"]

        # The raw new token must never land in the receipt chain — receipts
        # sync externally (seshat receipts sync), so a live bearer
        # capability token must not be persisted there.
        files = sorted(tmp_path.glob("*.json"))
        assert len(files) == 1
        receipt = json_module.loads(files[0].read_text())
        assert new_token not in json_module.dumps(receipt)
        assert receipt["actor"]["identity_verified"] is True

    def test_attenuate_identity_tool_rejects_an_illegal_caveat(self, monkeypatch):
        import identity
        root = identity.mint("agent-root")
        monkeypatch.setenv("SESHAT_IDENTITY_TOKEN", root)
        monkeypatch.setattr(
            mcp_server.agreements, "load_agreement",
            lambda: 'permit actor is "agent-root" and action is "attenuate_identity"',
        )
        monkeypatch.setattr(mcp_server.receipts, "snapshot", lambda: {
            "listening_ports": [], "managed_projects": {},
        })

        import json as json_module
        result = json_module.loads(mcp_server.attenuate_identity(
            token=root, caveats=['remember a string called foo with "bar"'],
        ))
        assert result["status"] == "failure"

    def test_emit_threads_delegation_path_for_a_delegated_caller(self, monkeypatch, tmp_path):
        import identity
        root = identity.mint("agent-root")
        child = identity.attenuate(root, [], delegate_to="agent-child")
        monkeypatch.setenv("SESHAT_IDENTITY_TOKEN", child)
        monkeypatch.setattr(mcp_server.receipts, "RECEIPTS_DIR", tmp_path)
        monkeypatch.setattr(mcp_server.receipts, "LOCK_PATH", tmp_path / ".chain.lock")
        monkeypatch.setattr(mcp_server.receipts, "CHAIN_HEAD_PATH", tmp_path / ".chain_head")
        monkeypatch.setattr(
            mcp_server.agreements, "load_agreement",
            lambda: 'permit actor is "agent-root" and action is "stop_orphan"',
        )
        monkeypatch.setattr(mcp_server.scanner, "scan", lambda: {})
        monkeypatch.setattr(mcp_server.receipts, "snapshot", lambda: {
            "listening_ports": [], "managed_projects": {},
        })

        mcp_server.stop_orphan(port=4242)
        import json as json_module
        files = sorted(tmp_path.glob("*.json"))
        receipt = json_module.loads(files[0].read_text())
        assert receipt["actor"]["delegation_path"] == ["agent-root", "agent-child"]
        assert receipt["actor"]["agent_hint"] == "agent-child"
