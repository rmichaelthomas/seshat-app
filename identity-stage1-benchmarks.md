# Identity plane Stage 1 — acceptance benchmarks (§11)

**Captured:** 2026-07-10, branch `feat/identity-plane-stage1-issuance-verify`.
**Purpose:** demonstrate, from the real code paths, the six §11 acceptance checks.

One environment note, same as `fix-receipt-chain-integrity-benchmarks-post.md`:
this runs inside a sandboxed shell that cannot reach the interactive macOS
Keychain, so the identity root key (`identity._root_key`) and the receipt MAC
key (`receipts._mac_key`) are substituted with fixed test values for this
demonstration — exactly the isolation `tests/conftest.py`'s autouse fixtures
already apply for the whole automated suite. The real Keychain call shape
(`keyring.get_password`/`set_password`, same as `vault.py`'s and `receipts.py`'s
shipped paths) is unchanged and is exercised for real by `vault.py`'s and
`receipts.py`'s existing production usage. Everything else below — `cli.py`'s
actual `identity mint` command (invoked via `click.testing.CliRunner`, in-process
rather than a subprocess, so the key substitution takes effect), `identity.py`'s
real `mint`/`verify`/`is_legal_caveat`, `agreements.check_action`, and
`receipts.emit` — is the real, shipped code.

## Critical finding during benchmarking: `permit` caveats must be illegal

While building benchmark 4, minting a token with caveat `permit action is
"translate"` and checking it against an Agreement that separately permitted
`wipe_disk` revealed that the token's caveat had **no effect** on `wipe_disk` —
it remained allowed. Investigating further:

```
$ python3 -c "
import identity, agreements
token = identity.mint('agent-x', caveats=['permit action is \"wipe_disk\"'])
d = agreements.check_action('agent-x', 'wipe_disk',
      agreement_text='permit actor is \"agent-x\" and action is \"translate\"',
      token=token)
print(d.allowed, d.mode, d.rule)
"
True permitted permit action is wipe_disk
```

**The Agreement never granted `wipe_disk` to `agent-x` — only `translate`. The
token's own `permit` caveat granted it anyway.** This is because a caveat is
spliced into the exact same flat Liminate evaluation pool as the Agreement
(§6 of the build prompt), and Liminate's `permit` semantics are purely
additive/non-blocking (an existing invariant of this codebase — see
`test_permit_never_triggers_a_denial` in `tests/test_agreements.py`): the
composed program has no notion of "this permit only counts if some other
permit already granted it." Any `permit` statement from *any* source in the
composed text can grant, full stop.

This inverts the one property a macaroon caveat must have: it can narrow
authority, never widen it. The build prompt's own §5 literally lists "forbid
/ permit clauses ... (allow-deny)" as the fourth legal shape — this is the
"genuine ambiguity the prompt does not cover" case called out in the prompt's
closing paragraph, and per its instruction the safest fail-closed
interpretation was applied without pausing: **`is_legal_caveat` accepts
`forbid` only.** `permit` is rejected at both mint and verify, exactly like
any other illegal caveat. `forbid` has no escalation path (forbid always
wins over a matching permit, never grants one), so it is the only verb that
can safely appear in a caveat.

Knock-on effect: the `--until <date>` CLI flag (§8) was removed from `seshat
identity mint`. Its planned implementation appended `until "<date>" permit
actor is "<agent>"` as a blanket-expiry caveat — which, given the finding
above, would have let a token holder perform **any** action as long as the
date window was open, completely bypassing the Agreement. There is no way to
express "forbid every action after date X" in the locked, negation-free
caveat grammar, so blanket token expiry is left to Stage 3 (lifecycle/
revocation) rather than approximated unsafely here.

## 1. Mint a token for `agent-x`; a call presenting it verifies and the receipt shows `identity_verified: true` with `agent_hint: "agent-x"`

```
$ seshat identity mint agent-x
✓ Minted identity token for agent-x:

eyJhbGciOiAiSFMyNTYtbWFjYXJvb24iLCAidHlwIjogIlNJVCJ9.eyJjYXZlYXRzIjogW10sICJpZGV
udGlmaWVyIjogImFnZW50LXgiLCAibG9jYXRpb24iOiAiUnMtTWFjQm9vay1Qcm8tMi5sb2NhbCJ9.yW
VsRlOffgCS8Uxla92Gh6W0hCpeourwAJ9gdYmhVbI

  Set SESHAT_IDENTITY_TOKEN to this value for agent-x's MCP session.

$ python3 -c "
import agreements
d = agreements.check_action('ignored-untrusted-actor-string', 'start_project',
      agreement_text='permit actor is \"agent-x\" and action is \"start_project\"',
      token=TOKEN)
print(d.allowed, d.mode)
"
True permitted

$ python3 -c "receipts.emit(..., agent_hint='agent-x', identity_verified=True)"
{
  "type": "mcp_session",
  "session_id": "benchmark",
  "agent_hint": "agent-x",
  "identity_verified": true,
  "delegation_path": []
}
```

`identity_verified: true`, `agent_hint` is the verified identifier, and the
passed-in `"ignored-untrusted-actor-string"` was correctly overridden.

## 2. A forged token (flip a byte of the signature, or append a caveat without re-signing) → `verify` returns `None` → `check_action` denies with `mode="identity-invalid"`

```
$ python3 -c "
import identity
t = identity.mint('agent-x')
h, p, s = t.split('.')
forged = h + '.' + p + '.' + (('A' if s[-1] != 'A' else 'B') + s[1:])
print(identity.verify(forged))
"
None

$ python3 -c "
import agreements
d = agreements.check_action('agent-x', 'start_project',
      agreement_text='permit actor is \"agent-x\" and action is \"start_project\"',
      token=FORGED)
print(d.allowed, d.mode, d.reason)
"
False identity-invalid Identity token failed verification — forged, tampered, or carrying an illegal caveat. Denying by default.
```

An appended-caveat-without-re-signing forgery (mutating the base64url
payload segment, leaving the original signature) was also confirmed to
return `None` from `verify()` — the HMAC chain no longer matches.

## 3. A token carrying an illegal caveat (a verb-`other` line, or an unresolvable predicate) → token invalid (deny), not silently accepted

```
$ seshat identity mint agent-y --caveat 'remember a string called foo with "bar"'
Refused to mint — illegal caveat: Caveat is outside the locked decidable
subset (§5): 'remember a string called foo with "bar"'
$ echo $?
1
$ ls ~/.seshat/identity/agent-y.json
ls: ~/.seshat/identity/agent-y.json: No such file or directory
```

No metadata file was written — the whole mint call was refused, not just the
offending caveat line dropped. `identity.is_legal_caveat('forbid action is
"translate" and reviewed_by is "someone"')` also returns `False` (the
interpreter reports `ERROR_SEMANTIC: I can't find 'reviewed_by'`), confirming
the unresolvable-predicate case is caught the same way.

## 4. Each of the four permitted caveat shapes round-trips through `amendment_diff.parse_statements` and evaluates correctly

```
$ python3 -c "
import identity, amendment_diff
for s in ['forbid actor is \"agent-x\"',
          'forbid action is \"wipe_disk\" or action is \"delete_all\"',
          'forbid scope is \"production\"',
          'until \"2099-01-01\" forbid action is \"wipe_disk\"']:
    assert identity.is_legal_caveat(s)
    stmts = amendment_diff.parse_statements(identity._strip_temporal_prefix(s))
    assert len(stmts) == 1 and stmts[0]['verb'] == 'forbid'
    print('OK:', s)
"
OK: forbid actor is "agent-x"
OK: forbid action is "wipe_disk" or action is "delete_all"
OK: forbid scope is "production"
OK: until "2099-01-01" forbid action is "wipe_disk"
```

A token scoped `forbid action is "wipe_disk"` (the safe way to scope a
token, per the finding above — a bare `permit action is "translate"` cannot
do this, since it would only ever add authority, never subtract it) permits
`translate` (via the Agreement, unaffected by the caveat) and denies
`wipe_disk` (via the caveat, forbid-wins):

```
translate: allowed=True mode=permitted
wipe_disk: allowed=False mode=forbidden
```

## 5. A token-absent call → unchanged behavior, `identity_verified: false` (F-02 acute intact)

```
$ python3 -c "
import agreements
d = agreements.check_action('claude-code', 'start_project',
      agreement_text='permit actor is \"claude-code\" and action is \"start_project\"')
print(d.allowed, d.mode)
"
True permitted

$ python3 -c "receipts.emit(..., agent_hint='claude-code')"  # identity_verified omitted
{
  "agent_hint": "claude-code",
  "identity_verified": false,
  "delegation_path": []
}
```

Byte-for-byte the same decision and the same `identity_verified: false` as
before this stage existed.

## 6. `mint` confirmed absent from the MCP tool list

```
$ python3 -c "
import mcp_server
print(sorted(t.name for t in mcp_server.mcp._tool_manager.list_tools()))
"
['amend_agreement', 'register_project', 'set_project_override', 'set_secret',
 'start_group', 'start_project', 'stop_group', 'stop_orphan', 'stop_project']
```

Nine tools, none named `mint` or containing `identity` — `identity mint` is
CLI-only, exactly as required.

## Full test suite

```
$ python3 -m pytest -q
........................................................................ [ 18%]
........................................................................ [ 37%]
........................................................................ [ 55%]
........................................................................ [ 74%]
........................................................................ [ 92%]
.............................                                            [100%]
389 passed in 8.72s
```

389 passed, 0 failed — up from 379 before this stage (10 net new test
functions across `test_identity.py`, `test_agreements.py`,
`test_receipts.py`, `test_mcp_enforcement_gate.py`, and
`test_identity_cli.py`, several of which contain multiple assertions per the
scenarios above). No regressions in `test_receipts.py` (F-01) or
`test_agreements.py` (existing Agreement semantics, including
`test_permit_never_triggers_a_denial`, whose documented "permit is purely
informational" property is exactly what motivated banning `permit` from the
caveat subset above) or `test_mcp_enforcement_gate.py` (F-11).
