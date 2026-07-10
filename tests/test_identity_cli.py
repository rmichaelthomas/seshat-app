"""Tests for `seshat identity mint/list/show` — human-only CLI surface,
never MCP-reachable (§9.4)."""
import json

from click.testing import CliRunner

import cli
import identity


def test_mint_prints_a_serialized_token_and_writes_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "IDENTITY_DIR", tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli.cli, ["identity", "mint", "agent-x"])
    assert result.exit_code == 0
    assert result.output.count(".") >= 2  # the three-part token got printed

    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    meta = json.loads(files[0].read_text())
    assert meta["identifier"] == "agent-x"
    verified = identity.verify(meta["token"])
    assert verified is not None
    assert verified.identifier == "agent-x"


def test_mint_with_multiple_forbid_caveats(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "IDENTITY_DIR", tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli.cli, [
        "identity", "mint", "agent-x",
        "--caveat", 'forbid action is "wipe_disk"',
        "--caveat", 'until "2099-01-01" forbid action is "delete_all"',
    ])
    assert result.exit_code == 0
    meta = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert 'forbid action is "wipe_disk"' in meta["caveats"]
    assert any(c.startswith('until "2099-01-01"') for c in meta["caveats"])


def test_mint_rejects_a_permit_caveat(tmp_path, monkeypatch):
    """The critical security case at the CLI surface: a permit caveat can
    grant authority the Agreement never gave, so it must be refused here
    exactly like any other illegal caveat (§5)."""
    monkeypatch.setattr(identity, "IDENTITY_DIR", tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli.cli, [
        "identity", "mint", "agent-x",
        "--caveat", 'permit action is "wipe_disk"',
    ])
    assert result.exit_code != 0
    assert list(tmp_path.glob("*.json")) == []


def test_mint_rejects_an_illegal_caveat(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "IDENTITY_DIR", tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli.cli, [
        "identity", "mint", "agent-x",
        "--caveat", 'remember a string called foo with "bar"',
    ])
    assert result.exit_code != 0
    assert list(tmp_path.glob("*.json")) == []


def test_list_shows_minted_identities(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "IDENTITY_DIR", tmp_path)
    runner = CliRunner()
    runner.invoke(cli.cli, ["identity", "mint", "agent-x"])
    runner.invoke(cli.cli, ["identity", "mint", "agent-y"])
    result = runner.invoke(cli.cli, ["identity", "list"])
    assert result.exit_code == 0
    assert "agent-x" in result.output
    assert "agent-y" in result.output


def test_show_prints_the_token_for_an_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "IDENTITY_DIR", tmp_path)
    runner = CliRunner()
    runner.invoke(cli.cli, ["identity", "mint", "agent-x"])
    result = runner.invoke(cli.cli, ["identity", "show", "agent-x"])
    assert result.exit_code == 0
    assert "agent-x" in result.output


def test_show_unknown_agent_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "IDENTITY_DIR", tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli.cli, ["identity", "show", "no-such-agent"])
    assert result.exit_code != 0


def test_mint_is_not_an_mcp_tool():
    """§9.4: mint is never agent-reachable."""
    import mcp_server
    names = {tool.name for tool in mcp_server.mcp._tool_manager.list_tools()}
    assert "mint" not in names
    assert not any("identity" in n for n in names)
