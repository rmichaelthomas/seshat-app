"""Tests for the delegation facts, group-membership closure, and the probe
parity invariant (design session 2026-07-20).

Four new harness-bound facts — actor-groups, delegation-path,
delegation-depth, token-nonce — reach the interpreter via
liminate.run(inject=...) as inert data, never text composition. The three
legacy facts (actor/action/scope) stay text-composed byte-for-byte (F-02).
"""
import pytest
from click.testing import CliRunner

import agreements
import cli
import identity
from agreements import check_action, resolve_groups


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def groups(monkeypatch):
    """Install groups.limn text for the duration of one test. conftest's
    autouse _no_groups_by_default already isolates from the host file;
    this overrides it with specific content."""
    def _install(text):
        monkeypatch.setattr(agreements, "load_groups", lambda: text)
    return _install


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Point the groups/agreement enforcement paths at a tmp dir so CLI
    tests never touch the developer's real ~/.seshat (failure mode 7)."""
    seshat = tmp_path / ".seshat"
    seshat.mkdir()
    monkeypatch.setattr(agreements, "GROUPS_PATH", seshat / "groups.limn")
    monkeypatch.setattr(agreements, "AGREEMENT_PATH", seshat / "agreement.limn")
    return tmp_path


# ── Resolver ────────────────────────────────────────────────────────────────

def test_resolve_groups_direct_membership():
    text = 'remember a list called eng-members with "alice"\n'
    assert resolve_groups("alice", text) == ["eng"]


def test_resolve_groups_transitive_through_two_parent_levels():
    text = (
        'remember a list called eng-members with "alice"\n'
        'remember a list called eng-parents with "rnd"\n'
        'remember a list called rnd-parents with "company"\n'
    )
    assert resolve_groups("alice", text) == ["company", "eng", "rnd"]


def test_resolve_groups_cyclic_file_terminates():
    """Required gate: a -> b -> a must not hang, and must return both."""
    text = (
        'remember a list called a-members with "alice"\n'
        'remember a list called a-parents with "b"\n'
        'remember a list called b-parents with "a"\n'
    )
    assert resolve_groups("alice", text) == ["a", "b"]


def test_resolve_groups_missing_file_is_empty(monkeypatch):
    monkeypatch.setattr(agreements, "load_groups", lambda: None)
    assert resolve_groups("alice") == []


def test_resolve_groups_erroring_file_is_empty():
    """Fail-safe on the grant side: a broken groups.limn withholds every
    group rather than resolving to something arbitrary."""
    assert resolve_groups("alice", 'forbid actor is "x"') == []


def test_resolve_groups_parse_error_is_empty():
    assert resolve_groups("alice", "remember a list called broken with\n") == []


def test_resolve_groups_actor_in_no_groups_is_empty():
    text = 'remember a list called eng-members with "alice"\n'
    assert resolve_groups("bob", text) == []


def test_resolve_groups_is_sorted_for_determinism():
    text = (
        'remember a list called zeta-members with "alice"\n'
        'remember a list called alpha-members with "alice"\n'
        'remember a list called middle-members with "alice"\n'
    )
    assert resolve_groups("alice", text) == ["alpha", "middle", "zeta"]


def test_resolve_groups_ignores_non_membership_symbols():
    text = (
        'remember a list called eng-members with "alice"\n'
        'remember a string called some-note with "alice"\n'
        'remember a list called unrelated with "alice"\n'
    )
    assert resolve_groups("alice", text) == ["eng"]


# ── check_action integration: the four legacy benchmark scenarios ───────────

STARTER = (
    'permit actor is "claude-code" and action is "start_project"\n'
    'permit actor is "claude-code" and action is "stop_project"\n'
    'forbid action is "stop_orphan" because "orphan termination stays in the dashboard"\n'
)


@pytest.mark.parametrize("actor,action,allowed,mode", [
    ("claude-code", "start_project", True, "permitted"),
    ("claude-code", "stop_orphan", False, "forbidden"),
    ("claude-code", "delete_everything", False, "default-deny"),
    ("unknown-agent", "start_project", False, "default-deny"),
])
def test_legacy_decisions_unchanged_by_new_facts(actor, action, allowed, mode):
    """The §3 benchmark scenarios. The new facts are injected on every
    call, but the starter Agreement never references them — so every
    legacy decision must be identical (F-02, invariant 2)."""
    d = check_action(actor, action, agreement_text=STARTER)
    assert d.allowed is allowed
    assert d.mode == mode


# ── check_action integration: the new facts ─────────────────────────────────

GROUP_PERMIT = 'permit action is "start_project" and actor-groups includes "engineering"\n'


def test_group_permit_allows_with_membership(groups):
    groups('remember a list called engineering-members with "claude-code"\n')
    d = check_action("claude-code", "start_project", agreement_text=GROUP_PERMIT)
    assert d.allowed is True
    assert d.mode == "permitted"


def test_group_permit_default_denies_without_membership(groups):
    groups('remember a list called engineering-members with "someone-else"\n')
    d = check_action("claude-code", "start_project", agreement_text=GROUP_PERMIT)
    assert d.allowed is False
    assert d.mode == "default-deny"


def test_group_permit_default_denies_with_no_groups_file():
    d = check_action("claude-code", "start_project", agreement_text=GROUP_PERMIT)
    assert d.allowed is False
    assert d.mode == "default-deny"


def test_group_permit_allows_through_transitive_parent(groups):
    groups(
        'remember a list called sre-members with "claude-code"\n'
        'remember a list called sre-parents with "engineering"\n'
    )
    d = check_action("claude-code", "start_project", agreement_text=GROUP_PERMIT)
    assert d.allowed is True


UNLESS_AGREEMENT = (
    'permit action is "deploy"\n'
    'forbid action is "deploy" unless actor-groups includes "sre"\n'
)


def test_forbid_unless_group_membership_takes_the_exception(groups):
    groups('remember a list called sre-members with "claude-code"\n')
    d = check_action("claude-code", "deploy", agreement_text=UNLESS_AGREEMENT)
    assert d.allowed is True
    assert d.mode == "permitted"


def test_forbid_unless_group_membership_fires_without_it(groups):
    groups('remember a list called sre-members with "someone-else"\n')
    d = check_action("claude-code", "deploy", agreement_text=UNLESS_AGREEMENT)
    assert d.allowed is False
    assert d.mode == "forbidden"


def test_empty_actor_groups_is_fail_closed():
    """An empty injected list is fail-closed: `not includes` prohibitions
    fire against it rather than silently passing."""
    agreement = 'permit action is "go"\nforbid actor-groups not includes "x"\n'
    d = check_action("claude-code", "go", agreement_text=agreement)
    assert d.allowed is False
    assert d.mode == "forbidden"


# ── Delegation facts ────────────────────────────────────────────────────────

DEPTH_AGREEMENT = 'permit action is "go"\nforbid delegation-depth is above 3\n'


def _delegated_token(root, hops):
    token = identity.mint(root, ttl_hours=None)
    for hop in hops:
        token = identity.attenuate(token, delegate_to=hop)
    return token


def test_delegation_depth_forbid_does_not_fire_tokenless():
    """Tokenless calls normalize to delegation-path [actor], depth 1."""
    d = check_action("claude-code", "go", agreement_text=DEPTH_AGREEMENT)
    assert d.allowed is True
    assert d.mode == "permitted"


def test_delegation_depth_forbid_fires_over_the_limit():
    token = _delegated_token("root-agent", ["sub-a", "sub-b", "sub-c"])
    assert len(identity.verify(token).delegation_path) == 4
    d = check_action("ignored", "go", agreement_text=DEPTH_AGREEMENT, token=token)
    assert d.allowed is False
    assert d.mode == "forbidden"


def test_delegation_depth_forbid_does_not_fire_under_the_limit():
    token = _delegated_token("root-agent", ["sub-a"])
    d = check_action("ignored", "go", agreement_text=DEPTH_AGREEMENT, token=token)
    assert d.allowed is True
    assert d.mode == "permitted"


def test_delegation_path_membership_is_enforceable():
    agreement = 'permit action is "go"\nforbid delegation-path includes "sub-b"\n'
    token = _delegated_token("root-agent", ["sub-a", "sub-b"])
    d = check_action("ignored", "go", agreement_text=agreement, token=token)
    assert d.allowed is False
    assert d.mode == "forbidden"


def test_token_nonce_is_none_sentinel_when_tokenless():
    """A fact must never be unbound: tokenless calls bind "none"."""
    agreement = 'permit action is "go" and token-nonce is "none"\n'
    d = check_action("claude-code", "go", agreement_text=agreement)
    assert d.allowed is True


def test_groups_resolve_against_root_not_the_delegated_leaf(groups):
    """Failure mode 5: group resolution keys off `actor`, which after
    verification is always the ROOT identifier — never the self-chosen
    leaf. A delegate must not inherit groups by renaming itself."""
    groups('remember a list called engineering-members with "root-agent"\n')
    token = _delegated_token("root-agent", ["engineering-impostor"])
    d = check_action("ignored", "start_project", agreement_text=GROUP_PERMIT, token=token)
    assert d.allowed is True  # resolved from root-agent's real membership

    groups('remember a list called engineering-members with "engineering-impostor"\n')
    token = _delegated_token("outsider", ["engineering-impostor"])
    d = check_action("ignored", "start_project", agreement_text=GROUP_PERMIT, token=token)
    assert d.allowed is False  # the leaf's name buys nothing


# ── Injection inertness ─────────────────────────────────────────────────────

# A payload that, if it were ever parsed as program text rather than
# bound as data, would break out of its quoted string and introduce a
# rule that flips the decision under test.
HOSTILE = 'x" \nforbid action is "start_project'


def test_inject_never_parses_values_as_program_text():
    """The mechanism the whole build rests on, stated directly: values
    handed to liminate.run(inject=...) are bound as inert data. If the
    payload below were parsed as program text it would introduce
    `forbid action is "start_project"` and flip this to a prohibition."""
    import liminate

    result = liminate.run(
        'permit action is "start_project"\n',
        enter_phase2=False, auto_confirm_amber=True,
        inject={"action": "start_project", "actor-groups": [HOSTILE]},
    )
    assert [r.status.name for r in result.results] == ["SUCCESS"]


def test_hostile_group_name_is_inert_at_the_inject_boundary(monkeypatch):
    """Whatever resolve_groups returns crosses into the interpreter as
    data. A group name carrying quotes, a newline and a whole embedded
    statement cannot introduce a rule or alter another rule's outcome."""
    monkeypatch.setattr(
        agreements, "resolve_groups", lambda actor: ["engineering", HOSTILE]
    )
    d = check_action("claude-code", "start_project", agreement_text=GROUP_PERMIT)
    assert d.allowed is True
    assert d.mode == "permitted"   # the smuggled forbid never became a rule


def test_hostile_groups_file_fails_safe_to_empty(groups):
    """Upstream of the inject boundary: a groups.limn whose quoted string
    breaks out into a second statement is an *erroring* file, so it
    resolves to [] rather than executing the smuggled statement. The
    resulting decision is a deny — grants fail closed (invariant 3)."""
    groups(
        'remember a list called engineering-members with "claude-code"\n'
        f'remember a list called hostile-members with "{HOSTILE}"\n'
    )
    assert resolve_groups("claude-code") == []
    d = check_action("claude-code", "start_project", agreement_text=GROUP_PERMIT)
    assert d.allowed is False
    assert d.mode == "default-deny"


def test_hostile_delegate_to_is_rejected_at_attenuate_time():
    """Defense in depth ahead of the inject boundary: a delegation marker
    naming hostile characters is not a legal caveat, so attenuate refuses
    to mint it — the hostile hop never reaches delegation-path at all."""
    token = identity.mint("root-agent", ttl_hours=None)
    with pytest.raises(identity.IllegalCaveatError):
        identity.attenuate(token, delegate_to=HOSTILE)


def test_hostile_delegation_path_is_inert_at_the_inject_boundary(monkeypatch):
    """And if a hostile hop ever did reach delegation-path — a future
    minting path, a hand-built token — it is still only data."""
    real_verify = identity.verify

    def spy(token):
        v = real_verify(token)
        if v is not None:
            v.delegation_path = ["root-agent", HOSTILE]
        return v

    monkeypatch.setattr(identity, "verify", spy)
    token = _delegated_token("root-agent", ["sub-a"])
    d = check_action("ignored", "start_project", agreement_text='permit action is "start_project"\n', token=token)
    assert d.allowed is True
    assert d.mode == "permitted"


# ── The probe parity invariant (invariant 1) ────────────────────────────────

def test_probe_values_cover_exactly_the_enforcement_facts():
    """Invariant 1, enforced rather than trusted: a fact added to
    NEW_ENFORCEMENT_FACTS but not to the probe (or vice versa) breaks
    here, not silently at enforcement time."""
    assert set(agreements.new_fact_probe_values().keys()) == set(
        agreements.NEW_ENFORCEMENT_FACTS
    )


def test_probe_value_types_match_enforcement_binding_types():
    """Shapes must match real binding: list / list / number / string, in
    NEW_ENFORCEMENT_FACTS order."""
    probe = agreements.new_fact_probe_values()
    expected = (list, list, int, str)
    for name, want in zip(agreements.NEW_ENFORCEMENT_FACTS, expected):
        assert isinstance(probe[name], want), f"{name} should be {want.__name__}"


def test_check_action_injects_exactly_the_enforcement_facts(monkeypatch):
    """The other half of parity: what check_action actually hands the
    interpreter is the same key set the probe declares."""
    import liminate

    captured = {}
    real_run = liminate.run

    def spy(source, **kwargs):
        captured.update(kwargs.get("inject") or {})
        return real_run(source, **kwargs)

    monkeypatch.setattr(liminate, "run", spy)
    check_action("claude-code", "start_project", agreement_text=STARTER)
    assert set(captured.keys()) == set(agreements.NEW_ENFORCEMENT_FACTS)


@pytest.mark.parametrize("line,legal", [
    ('forbid actor-groups includes "contractors"', True),
    ('forbid delegation-depth is above 3', True),
    ('forbid delegation-path includes "sub-c"', True),
    ('starting "2026-01-01" forbid delegation-depth is above 2', True),
    ('forbid unbound-thing is above 3', False),
    ('permit actor-groups includes "x"', False),
    ('forbid token-nonce is "abc"', True),
])
def test_caveat_legality_over_the_new_facts(line, legal):
    assert identity.is_legal_caveat(line) is legal


def test_forbid_only_rule_still_holds_for_new_facts():
    """Invariant 4: membership is expressible in a caveat only as
    prohibition — a permit caveat could widen authority."""
    assert identity.is_legal_caveat('permit delegation-depth is above 3') is False
    assert identity.is_legal_caveat('permit delegation-path includes "x"') is False


def test_mint_with_new_caveat_denies_after_delegation_hops():
    """End-to-end: a token carrying a depth caveat denies once the chain
    grows past the limit, with the Agreement itself saying nothing about
    delegation."""
    agreement = 'permit action is "go"\n'
    token = identity.mint(
        "root-agent", caveats=['forbid delegation-depth is above 2'], ttl_hours=None
    )

    d = check_action("ignored", "go", agreement_text=agreement, token=token)
    assert d.allowed is True, "undelegated: depth 1, under the caveat's limit"

    one_hop = identity.attenuate(token, delegate_to="sub-a")
    d = check_action("ignored", "go", agreement_text=agreement, token=one_hop)
    assert d.allowed is True, "one hop: depth 2, still at the limit"

    two_hops = identity.attenuate(one_hop, delegate_to="sub-b")
    d = check_action("ignored", "go", agreement_text=agreement, token=two_hops)
    assert d.allowed is False
    assert d.mode == "forbidden"


# ── CLI ─────────────────────────────────────────────────────────────────────

def test_groups_init_then_check_resolves_the_starter_group(isolated_home, monkeypatch):
    monkeypatch.setattr(
        agreements, "load_groups", lambda: agreements.GROUPS_PATH.read_text()
    )
    runner = CliRunner()
    assert runner.invoke(cli.cli, ["groups", "init"]).exit_code == 0
    result = runner.invoke(cli.cli, ["groups", "check", "claude-code"])
    assert result.exit_code == 0
    assert "engineering" in result.output


def test_groups_init_refuses_to_overwrite_without_force(isolated_home):
    runner = CliRunner()
    runner.invoke(cli.cli, ["groups", "init"])
    result = runner.invoke(cli.cli, ["groups", "init"])
    assert result.exit_code == 1
    assert "already exists" in result.output


def test_groups_show_hints_when_absent(isolated_home):
    runner = CliRunner()
    result = runner.invoke(cli.cli, ["groups", "show"])
    assert result.exit_code == 0
    # Normalize: the rich console hard-wraps the hint across lines.
    assert "seshat groups init" in " ".join(result.output.split())


def test_groups_install_rejects_a_non_remember_statement(isolated_home, tmp_path):
    src = tmp_path / "bad.limn"
    src.write_text('remember a list called x-members with "a"\nshow x-members\n')
    runner = CliRunner()
    result = runner.invoke(cli.cli, ["groups", "install", str(src)])
    assert result.exit_code == 1
    assert "only remember statements are allowed" in result.output
    assert not agreements.GROUPS_PATH.exists()


def test_groups_install_rejects_a_resolving_deontic_statement(isolated_home, tmp_path):
    """A forbid whose predicate resolves cleanly still doesn't belong —
    it would execute on every resolution run."""
    src = tmp_path / "bad.limn"
    src.write_text('remember a string called actor with "x"\nforbid actor is "x"\n')
    runner = CliRunner()
    result = runner.invoke(cli.cli, ["groups", "install", str(src)])
    assert result.exit_code == 1
    assert not agreements.GROUPS_PATH.exists()


def test_groups_install_rejects_a_parse_error(isolated_home, tmp_path):
    src = tmp_path / "bad.limn"
    src.write_text("remember a list called broken with\n")
    runner = CliRunner()
    result = runner.invoke(cli.cli, ["groups", "install", str(src)])
    assert result.exit_code == 1
    assert not agreements.GROUPS_PATH.exists()


def test_groups_install_rejects_unbound_reference_no_forgiveness(isolated_home, tmp_path):
    """Unlike an Agreement, a groups file references no enforcement-time
    facts — so unbound-reference errors are blocking here."""
    src = tmp_path / "bad.limn"
    src.write_text('remember a list called x-members with "a"\nforbid actor is "x"\n')
    runner = CliRunner()
    result = runner.invoke(cli.cli, ["groups", "install", str(src)])
    assert result.exit_code == 1
    assert not agreements.GROUPS_PATH.exists()


def test_groups_install_writes_a_valid_file(isolated_home, tmp_path):
    src = tmp_path / "good.limn"
    src.write_text(
        "-- a comment\n"
        'remember a list called sre-members with "claude-code"\n'
        'remember a list called sre-parents with "engineering"\n'
    )
    runner = CliRunner()
    result = runner.invoke(cli.cli, ["groups", "install", str(src)])
    assert result.exit_code == 0
    assert agreements.GROUPS_PATH.read_text() == src.read_text()


def test_groups_install_refuses_to_overwrite_without_force(isolated_home, tmp_path):
    src = tmp_path / "good.limn"
    src.write_text('remember a list called sre-members with "claude-code"\n')
    runner = CliRunner()
    runner.invoke(cli.cli, ["groups", "install", str(src)])
    result = runner.invoke(cli.cli, ["groups", "install", str(src)])
    assert result.exit_code == 1
    assert "already exists" in result.output


def test_groups_check_reports_no_groups_for_an_unknown_actor(isolated_home, monkeypatch):
    monkeypatch.setattr(
        agreements, "load_groups", lambda: agreements.GROUPS_PATH.read_text()
    )
    runner = CliRunner()
    runner.invoke(cli.cli, ["groups", "init"])
    result = runner.invoke(cli.cli, ["groups", "check", "nobody"])
    assert result.exit_code == 0
    assert "no groups" in result.output
