"""Tests for `seshat agreement lint`.

The gap it closes: `permit action is "start_projct"` — one transposed
letter — is a valid Liminate program that silently means deny-forever, and
a silently-denying Agreement is indistinguishable from a working one at
runtime. Enforcement cannot catch this; lint catches it before the file
reaches the enforcement surface.
"""
import pytest
from click.testing import CliRunner

import agreements
import cli


# A fixed vocabulary for the unit tests, so they assert lint's LOGIC rather
# than whatever tools happen to be registered. The live-read path has its
# own test below.
ACTIONS = {"start_project", "stop_project", "start_group", "register_project"}
SCOPES = {"seshat-app", "web-tier"}


def lint(source, **kw):
    kw.setdefault("known_actions", ACTIONS)
    kw.setdefault("known_scopes", SCOPES)
    return cli.lint_agreement(source, **kw)


def errors(findings):
    return [f for f in findings if f.severity == "error"]


def warnings(findings):
    return [f for f in findings if f.severity == "warning"]


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    seshat = tmp_path / ".seshat"
    seshat.mkdir()
    monkeypatch.setattr(agreements, "AGREEMENT_PATH", seshat / "agreement.limn")
    return tmp_path


# ── The headline case ───────────────────────────────────────────────────────

def test_typod_action_warns_with_nearest_match():
    findings = lint('permit actor is "claude-code" and action is "start_projct"')
    warns = warnings(findings)
    assert len(warns) == 1
    assert "start_projct" in warns[0].message
    assert warns[0].suggestion == 'Did you mean "start_project"?'


def test_typod_action_is_a_warning_not_an_error():
    """A typo'd action is a suspicion about intent, not a defect in the
    file — the program is valid, so it must not block an install."""
    findings = lint('permit action is "start_projct"')
    assert errors(findings) == []


def test_valid_agreement_is_clean():
    source = (
        'permit actor is "claude-code" and action is "start_project"\n'
        'permit actor is "claude-code" and action is "stop_project"\n'
        'forbid action is "start_group" because "groups stay manual"\n'
    )
    assert lint(source) == []


def test_the_shipped_starter_agreement_is_clean():
    """The Agreement `seshat agreement init` writes must itself lint clean
    against the REAL registered tools, or the first thing every user sees
    is a warning about the file we just handed them."""
    findings = cli.lint_agreement(cli.AGREEMENT_STARTER, known_scopes=set())
    assert findings == [], [f.message for f in findings]


# ── Unknown facts are errors ────────────────────────────────────────────────

def test_unknown_fact_is_an_error_with_suggestion():
    findings = lint('permit actr is "claude-code" and action is "start_project"')
    errs = errors(findings)
    assert len(errs) == 1
    assert 'unknown fact "actr"' in errs[0].message
    assert errs[0].suggestion == 'Did you mean "actor"?'


@pytest.mark.parametrize("fact", ["actor", "action", "scope"])
def test_legacy_facts_are_known(fact):
    assert errors(lint(f'permit {fact} is "x"')) == []


@pytest.mark.parametrize("fact", agreements.NEW_ENFORCEMENT_FACTS)
def test_every_new_enforcement_fact_is_known(fact):
    """Parity: the linter's known set derives from NEW_ENFORCEMENT_FACTS,
    so a fact added to enforcement is never reported as unknown."""
    assert errors(lint(f'forbid {fact} is "x"')) == []


def test_known_facts_is_exactly_legacy_plus_new():
    assert agreements.KNOWN_FACTS == frozenset(
        agreements.LEGACY_ENFORCEMENT_FACTS + agreements.NEW_ENFORCEMENT_FACTS
    )


def test_includes_and_not_includes_subjects_are_scanned():
    assert errors(lint('forbid actor-teems includes "sre"'))
    assert errors(lint('forbid actor-teems not includes "sre"'))
    assert errors(lint('forbid actor-teams not includes "sre"')) == []


def test_fact_inside_an_unless_clause_is_scanned():
    source = (
        'permit action is "start_project"\n'
        'forbid action is "start_project" unless actor-teems includes "sre"\n'
    )
    errs = errors(lint(source))
    assert len(errs) == 1
    assert errs[0].line == 2
    assert errs[0].suggestion == 'Did you mean "actor-teams"?'


def test_a_fact_the_agreement_remembers_itself_is_known():
    """A name bound by a `remember` in the Agreement resolves at
    enforcement time, so it must not lint as unknown."""
    source = (
        'remember a string called tier with "gold"\n'
        'permit tier is "gold" and action is "start_project"\n'
    )
    assert errors(lint(source)) == []


# ── Things that must NOT produce findings ───────────────────────────────────

def test_rationale_prose_is_not_scanned():
    """`because "..."` is free prose — scanning it would invent facts and
    actions that aren't in the rule."""
    source = 'forbid action is "start_project" because "action is unclear and scope is odd"'
    assert errors(lint(source)) == []
    assert not [w for w in warnings(lint(source)) if "not a registered" in w.message]


def test_quoted_literal_is_not_read_as_a_fact():
    """The string "start_project" sits in value position, not comparison
    position — blanking quoted literals before the fact scan is what keeps
    it from being read as a bare name."""
    assert errors(lint('permit action is "start_project"')) == []


def test_temporal_prefix_does_not_hide_a_permit():
    """`starting "..." permit ...` parses as verb 'other' unless the prefix
    is stripped first — which would wrongly trigger the no-permits warning."""
    findings = lint('starting "2026-01-01" permit action is "start_project"')
    assert findings == []


def test_liminate_comments_are_skipped():
    source = (
        "-- this is a comment mentioning action and scope\n"
        'permit action is "start_project"\n'
    )
    assert lint(source) == []


def test_scope_sentinel_none_never_warns():
    assert lint('permit action is "start_project" and scope is "none"') == []


def test_empty_source_produces_no_findings():
    assert lint("") == []
    assert lint("-- only a comment\n") == []


# ── Warnings ────────────────────────────────────────────────────────────────

def test_agreement_with_no_permits_warns():
    findings = lint('forbid action is "start_project"')
    warns = warnings(findings)
    assert len(warns) == 1
    assert "denies everything" in warns[0].message


def test_agreement_with_a_permit_does_not_warn_about_permits():
    findings = lint('permit action is "start_project"\nforbid action is "stop_project"')
    assert not [w for w in warnings(findings) if "denies everything" in w.message]


def test_unregistered_scope_warns_but_is_not_an_error():
    findings = lint('permit action is "start_project" and scope is "no-such-project"')
    assert errors(findings) == []
    assert len(warnings(findings)) == 1
    assert "no-such-project" in warnings(findings)[0].message


def test_registered_scope_does_not_warn():
    assert lint('permit action is "start_project" and scope is "seshat-app"') == []


def test_empty_known_sets_suppress_warnings_rather_than_inventing_them():
    """An unreadable registry or MCP module must not turn every literal
    into a warning — best-effort means silent, not noisy."""
    findings = cli.lint_agreement(
        'permit action is "anything" and scope is "whatever"',
        known_actions=set(), known_scopes=set(),
    )
    assert findings == []


# ── Interpreter errors surface as lint errors ───────────────────────────────

def test_parse_error_is_a_lint_error():
    findings = lint("permit action is\n")
    assert errors(findings)


# ── The tool list is read live ──────────────────────────────────────────────

def test_known_actions_come_from_the_mcp_server_live():
    import mcp_server

    live = cli._lint_known_actions()
    assert live == mcp_server.enforced_actions()
    assert "start_project" in live


def test_enforced_actions_tracks_a_newly_registered_tool():
    """Add a tool at runtime -> lint sees it. This is what makes the
    hardcoded-list drift impossible rather than merely discouraged."""
    import mcp_server

    before = cli._lint_known_actions()
    assert "brand_new_action" not in before

    @mcp_server._enforced_tool("brand_new_action", lambda a: {})
    def brand_new_action() -> str:
        return "ok"

    try:
        after = cli._lint_known_actions()
        assert "brand_new_action" in after
        # and an Agreement naming it now lints clean
        assert cli.lint_agreement(
            'permit action is "brand_new_action"', known_scopes=set()
        ) == []
    finally:
        mcp_server.mcp._tool_manager._tools.pop("brand_new_action", None)

    assert "brand_new_action" not in cli._lint_known_actions()


def test_enforced_actions_reads_the_action_not_the_function_name():
    """Agreements condition on ACTION. The two coincide by convention
    today, so this pins the distinction before it can silently break."""
    import mcp_server

    @mcp_server._enforced_tool("the_action_string", lambda a: {})
    def a_differently_named_function() -> str:
        return "ok"

    try:
        actions = mcp_server.enforced_actions()
        assert "the_action_string" in actions
        assert "a_differently_named_function" not in actions
    finally:
        mcp_server.mcp._tool_manager._tools.pop("a_differently_named_function", None)


# ── CLI ─────────────────────────────────────────────────────────────────────

def test_lint_command_exits_0_on_a_clean_agreement(isolated_home):
    agreements.AGREEMENT_PATH.write_text('permit action is "start_project"\n')
    result = CliRunner().invoke(cli.cli, ["agreement", "lint"])
    assert result.exit_code == 0
    # Normalize: the rich console hard-wraps the path across lines.
    assert "no issues found" in " ".join(result.output.split())


def test_lint_command_exits_1_on_an_error(isolated_home, tmp_path):
    src = tmp_path / "bad.limn"
    src.write_text('permit actr is "claude-code"\n')
    result = CliRunner().invoke(cli.cli, ["agreement", "lint", "--path", str(src)])
    assert result.exit_code == 1
    assert "unknown fact" in result.output


def test_lint_command_exits_0_on_warnings_only(isolated_home, tmp_path):
    src = tmp_path / "warn.limn"
    src.write_text('permit action is "start_projct"\n')
    result = CliRunner().invoke(cli.cli, ["agreement", "lint", "--path", str(src)])
    assert result.exit_code == 0
    assert "start_project" in result.output


def test_lint_command_hints_when_no_agreement_exists(isolated_home):
    result = CliRunner().invoke(cli.cli, ["agreement", "lint"])
    assert result.exit_code == 0
    assert "seshat agreement init" in " ".join(result.output.split())


def test_install_blocks_on_a_lint_error(isolated_home, tmp_path):
    src = tmp_path / "bad.limn"
    src.write_text('permit actr is "claude-code"\n')
    result = CliRunner().invoke(cli.cli, ["agreement", "install", str(src)])
    assert result.exit_code == 1
    assert "failed lint" in result.output
    assert not agreements.AGREEMENT_PATH.exists()


def test_install_proceeds_on_a_lint_warning(isolated_home, tmp_path):
    src = tmp_path / "warn.limn"
    src.write_text('permit action is "start_projct"\n')
    result = CliRunner().invoke(cli.cli, ["agreement", "install", str(src)])
    assert result.exit_code == 0
    assert agreements.AGREEMENT_PATH.read_text() == src.read_text()
    assert "start_project" in result.output   # the suggestion still printed


def test_install_still_blocks_on_a_parse_error(isolated_home, tmp_path):
    """The pre-existing validation gate is unchanged by the lint wiring."""
    src = tmp_path / "broken.limn"
    src.write_text("permit action is\n")
    result = CliRunner().invoke(cli.cli, ["agreement", "install", str(src)])
    assert result.exit_code == 1
    assert not agreements.AGREEMENT_PATH.exists()


# ── Zero runtime coupling ───────────────────────────────────────────────────

def test_check_action_does_not_consult_the_linter(monkeypatch):
    """Invariant: lint is advisory. An Agreement that lints with errors
    still enforces exactly as before — check_action never calls lint."""
    def explode(*a, **kw):
        raise AssertionError("check_action must not call lint_agreement")

    monkeypatch.setattr(cli, "lint_agreement", explode)
    d = agreements.check_action(
        "claude-code", "start_projct",
        agreement_text='permit actor is "claude-code" and action is "start_projct"',
    )
    assert d.allowed is True
    assert d.mode == "permitted"
