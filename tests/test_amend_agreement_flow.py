# tests/test_amend_agreement_flow.py
#
# TI-Q7 (v1.0k §55-57) end-to-end integration: amend_agreement (MCP propose)
# -> agreement amend --apply (CLI, human write) -> entrench (CLI, human-only
# mutation of entrenched.limn). Isolates agreement.limn/entrenched.limn/
# receipts to a tmp dir so tests never touch the real ~/.seshat/*.
import json

import pytest
from click.testing import CliRunner

import agreements
import cli
import mcp_server
import receipts as receipts_module

_PERMIT_AMEND = 'permit actor is "claude-code" and action is "amend_agreement"\n'
_FORBID_STOP_ORPHAN = 'forbid action is "stop_orphan" because "orphan termination stays in the dashboard"\n'


@pytest.fixture
def seshat_home(tmp_path, monkeypatch):
    seshat_dir = tmp_path / ".seshat"
    receipts_dir = seshat_dir / "receipts"
    receipts_dir.mkdir(parents=True)

    monkeypatch.setattr(agreements, "AGREEMENT_PATH", seshat_dir / "agreement.limn")
    monkeypatch.setattr(agreements, "ENTRENCHED_PATH", seshat_dir / "entrenched.limn")

    monkeypatch.setattr(receipts_module, "RECEIPTS_DIR", receipts_dir)
    monkeypatch.setattr(receipts_module, "LOCK_PATH", receipts_dir / ".chain.lock")
    monkeypatch.setattr(
        receipts_module, "snapshot",
        lambda: {"listening_ports": [], "managed_projects": {}},
    )

    monkeypatch.setenv("MCP_AGENT_HINT", "claude-code")
    return seshat_dir


def _install_agreement(seshat_dir, text):
    seshat_dir.joinpath("agreement.limn").write_text(text)


def _runner():
    return CliRunner()


def test_amend_agreement_never_writes_agreement_path(seshat_home):
    _install_agreement(seshat_home, _PERMIT_AMEND + _FORBID_STOP_ORPHAN)
    before = agreements.load_agreement()

    mcp_server.amend_agreement(additions=['forbid action is "delete_prod"'], removals=[])

    assert agreements.load_agreement() == before


def test_amend_agreement_denied_without_permit(seshat_home):
    _install_agreement(seshat_home, _FORBID_STOP_ORPHAN)  # no permit for amend_agreement
    result = mcp_server.amend_agreement(additions=['forbid action is "delete_prod"'], removals=[])
    assert result.startswith("DENIED by Agreement")


def test_amend_agreement_classifies_monotonic_addition(seshat_home):
    _install_agreement(seshat_home, _PERMIT_AMEND)
    result = json.loads(mcp_server.amend_agreement(additions=['forbid action is "delete_prod"'], removals=[]))
    assert result["classification"] == "monotonic"
    assert "receipt_id" in result


def test_amend_agreement_classifies_entrenched_violation(seshat_home):
    _install_agreement(seshat_home, _PERMIT_AMEND + _FORBID_STOP_ORPHAN)
    seshat_home.joinpath("entrenched.limn").write_text('forbid stop_orphan is protected because "x"\n')

    result = json.loads(mcp_server.amend_agreement(additions=[], removals=[_FORBID_STOP_ORPHAN.strip()]))
    assert result["classification"] == "entrenched-violation"
    assert result["violations"] == [["forbid", "stop_orphan"]]


def test_apply_refuses_entrenched_violation(seshat_home):
    _install_agreement(seshat_home, _PERMIT_AMEND + _FORBID_STOP_ORPHAN)
    seshat_home.joinpath("entrenched.limn").write_text('forbid stop_orphan is protected because "x"\n')

    propose = json.loads(mcp_server.amend_agreement(additions=[], removals=[_FORBID_STOP_ORPHAN.strip()]))
    before = agreements.load_agreement()

    result = _runner().invoke(cli.cli, ["agreement", "amend", "--apply", propose["receipt_id"]])
    assert result.exit_code != 0
    assert "entrenched" in result.output.lower()
    assert agreements.load_agreement() == before


def test_apply_writes_monotonic_amendment_and_emits_receipt(seshat_home):
    _install_agreement(seshat_home, _PERMIT_AMEND)
    propose = json.loads(mcp_server.amend_agreement(additions=['forbid action is "delete_prod"'], removals=[]))

    result = _runner().invoke(cli.cli, ["agreement", "amend", "--apply", propose["receipt_id"]])
    assert result.exit_code == 0, result.output
    assert "delete_prod" in agreements.load_agreement()

    receipts = receipts_module.load(limit=10)
    apply_receipts = [r for r in receipts if r["action"] == "apply_amendment"]
    assert len(apply_receipts) == 1
    assert apply_receipts[0]["result"]["classification"] == "monotonic"
    assert apply_receipts[0]["target"]["proposal_receipt_id"] == propose["receipt_id"]


def test_apply_refuses_deescalation_without_flag_then_succeeds_with_it(seshat_home):
    _install_agreement(seshat_home, _PERMIT_AMEND)
    propose = json.loads(mcp_server.amend_agreement(
        additions=['permit actor is "claude-code" and action is "wipe_disk"'], removals=[]
    ))

    result = _runner().invoke(cli.cli, ["agreement", "amend", "--apply", propose["receipt_id"]])
    assert result.exit_code != 0
    assert "de-escalating" in result.output.lower() or "deescalation" in result.output.lower()
    assert "wipe_disk" not in agreements.load_agreement()

    result = _runner().invoke(
        cli.cli, ["agreement", "amend", "--apply", propose["receipt_id"], "--allow-deescalation"]
    )
    assert result.exit_code == 0, result.output
    assert "wipe_disk" in agreements.load_agreement()


def test_apply_reclassifies_at_apply_time_against_current_entrenched(seshat_home):
    """A key entrenched AFTER the proposal was made must still block --apply
    (invariant 5: never trust the receipt's stored classification)."""
    _install_agreement(seshat_home, _PERMIT_AMEND + _FORBID_STOP_ORPHAN)
    propose = json.loads(mcp_server.amend_agreement(additions=[], removals=[_FORBID_STOP_ORPHAN.strip()]))
    assert propose["classification"] == "de-escalating"  # not yet entrenched at propose time

    # Entrench the key after the proposal, before apply.
    seshat_home.joinpath("entrenched.limn").write_text('forbid stop_orphan is protected because "x"\n')

    result = _runner().invoke(
        cli.cli, ["agreement", "amend", "--apply", propose["receipt_id"], "--allow-deescalation"]
    )
    assert result.exit_code != 0
    assert "entrenched" in result.output.lower()


def test_apply_unknown_receipt_id_fails(seshat_home):
    _install_agreement(seshat_home, _PERMIT_AMEND)
    result = _runner().invoke(cli.cli, ["agreement", "amend", "--apply", "nonexistent"])
    assert result.exit_code != 0


def test_entrench_add_show_remove_round_trip(seshat_home):
    runner = _runner()

    result = runner.invoke(cli.cli, ["entrench", "add", "forbid", "stop_orphan"], input="forbid stop_orphan\n")
    assert result.exit_code == 0, result.output
    assert agreements.entrenched_keys() == {("forbid", "stop_orphan")}

    result = runner.invoke(cli.cli, ["entrench", "show"])
    assert "forbid" in result.output and "stop_orphan" in result.output

    result = runner.invoke(cli.cli, ["entrench", "remove", "forbid", "stop_orphan"], input="wrong\n")
    assert result.exit_code != 0
    assert agreements.entrenched_keys() == {("forbid", "stop_orphan")}

    result = runner.invoke(
        cli.cli, ["entrench", "remove", "forbid", "stop_orphan"], input="forbid stop_orphan\n"
    )
    assert result.exit_code == 0, result.output
    assert agreements.entrenched_keys() == set()


def test_entrench_add_emits_receipt(seshat_home):
    runner = _runner()
    runner.invoke(cli.cli, ["entrench", "add", "forbid", "stop_orphan"], input="forbid stop_orphan\n")

    receipts = receipts_module.load(limit=10)
    entrench_receipts = [r for r in receipts if r["action"] == "entrench"]
    assert len(entrench_receipts) == 1
    assert entrench_receipts[0]["target"] == {"operation": "add", "verb": "forbid", "subject": "stop_orphan"}
