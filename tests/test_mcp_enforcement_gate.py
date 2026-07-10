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


def test_all_nine_tools_are_registered():
    names = {tool.name for tool in mcp_server.mcp._tool_manager.list_tools()}
    assert names == {
        "start_project", "stop_project", "start_group", "stop_group",
        "register_project", "stop_orphan", "set_secret",
        "set_project_override", "amend_agreement",
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
