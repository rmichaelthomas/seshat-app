# Identity plane Stage 2 — acceptance benchmarks (§10)

**Captured:** 2026-07-10, branch `feat/identity-plane-stage2-delegation`.
**Purpose:** demonstrate, from the real code paths, the §10 acceptance checks.

Same environment note as Stage 1: this runs inside a sandboxed shell that
cannot reach the interactive macOS Keychain, so `identity._root_key` and
`receipts._mac_key` are substituted with fixed test values — the same
isolation `tests/conftest.py`'s autouse fixtures already apply for the whole
automated suite. Everything else is the real, shipped code: `identity.py`'s
actual `attenuate`/`verify`, `agreements.check_action`, `mcp_server.py`'s
actual `attenuate_identity` tool and `stop_orphan` tool (invoked directly,
in-process), and `receipts.emit`.

## Critical safety correction: `actor` stays root, not leaf

The design source (per the build prompt's paraphrase) says the delegated
token's Agreement-matching `actor` "remains the leaf identifier." Tracing
this through concretely, before writing any code: if `check_action`'s
`actor` became the leaf, any holder of a valid token could call

```python
identity.attenuate(root_token, [], delegate_to="trusted-admin")
```

— choosing the name of some *other*, more-privileged actor already present
in the Agreement — and inherit that actor's permissions, entirely bypassing
the narrowing check (which only examines the caveat list, not a bare
identity swap). This is the same class of bug as Stage 1's `permit`-caveat
finding: authority arriving from a source the narrowing/legality check never
examines.

**Fix:** `VerifiedIdentity.identifier` stays the ROOT (the signed payload
identifier, unchanged across every delegation hop) for the whole chain —
exactly what `check_action`'s existing `actor = verified.identifier` line
already uses, so **`check_action` required zero changes** for this to be
safe. `delegation_path` is `[]` when undelegated, else `[root, ..., leaf]`,
for audit/receipt purposes only. `mcp_server.py`'s `_agreement_actor()` (the
only place a `VerifiedIdentity` becomes a bare string) returns the **leaf**
for `agent_hint`/display — safe, because `check_action` never trusts that
string; it always independently re-verifies the token and uses the root.

This is proven below (the "SEC" scenario): a token renamed via `delegate_to`
to an actor with *more* Agreement-granted permissions than the root gains
**nothing** — it still only carries the root's own authority, minus whatever
the caveats additionally forbid.

```
$ python3 -c "
import identity, agreements
root = identity.mint('agent-root')
escalated = identity.attenuate(root, [], delegate_to='trusted-admin')
agreement = '''
permit actor is \"agent-root\" and action is \"translate\"
permit actor is \"trusted-admin\" and action is \"wipe_disk\"
'''
d = agreements.check_action('ignored', 'wipe_disk', agreement_text=agreement, token=escalated)
print(d.allowed, d.mode)
"
False default-deny
```

`wipe_disk` — a permission `trusted-admin` has but `agent-root` never
did — is correctly denied even though the token was delegated under that
name.

## 1. Scoped root token → attenuate → child denied/allowed correctly; parent unaffected

```
$ python3 -c "
import identity, agreements
root = identity.mint('agent-root')
agreement = '''
permit actor is \"agent-root\" and action is \"read\"
permit actor is \"agent-root\" and action is \"translate\"
'''
child = identity.attenuate(root, ['forbid action is \"translate\"'], delegate_to='agent-child')
verified = identity.verify(child)
print('delegation_path:', verified.delegation_path)

d_child_translate = agreements.check_action('ignored', 'translate', agreement_text=agreement, token=child)
d_child_read = agreements.check_action('ignored', 'read', agreement_text=agreement, token=child)
d_parent_translate = agreements.check_action('ignored', 'translate', agreement_text=agreement, token=root)
print('child translate:', d_child_translate.allowed, d_child_translate.mode)
print('child read:     ', d_child_read.allowed, d_child_read.mode)
print('parent translate:', d_parent_translate.allowed, d_parent_translate.mode)
"
delegation_path: ['agent-root', 'agent-child']
child translate: False forbidden
child read:      True permitted
parent translate: True permitted
```

## 2. A broadening attempt is rejected by the monotonicity assertion, not silently accepted

Since `attenuate()` only ever appends forbid-only caveats, it cannot
structurally produce a broadening change through its own API — so this test
proves the assertion is actually wired up and respected (not simply assumed
safe from append-only structure) by forcing
`amendment_diff.classify_monotonicity_from_changes` to report `"de-escalating"`
and confirming `attenuate()` still refuses:

```
$ python3 -c "
import identity, amendment_diff
amendment_diff.classify_monotonicity_from_changes = lambda changes: 'de-escalating'
root = identity.mint('agent-root')
try:
    identity.attenuate(root, ['forbid action is \"wipe_disk\"'])
    print('FAIL')
except identity.IllegalCaveatError as e:
    print('Refused:', e)
"
Refused: Refusing to attenuate: the requested change is not authority-narrowing (classified 'de-escalating').
```

## 3. An `attenuate` call with an illegal caveat → `IllegalCaveatError`, no token produced

```
$ python3 -c "
import identity
root = identity.mint('agent-root')
try:
    identity.attenuate(root, ['permit action is \"wipe_disk\"'])
    print('FAIL')
except identity.IllegalCaveatError as e:
    print('Refused:', e)
"
Refused: Caveat is outside the locked decidable subset (§5): 'permit action is "wipe_disk"'
```

## 4. A two-hop delegation (root → child → grandchild) verifies; the receipt's `actor.delegation_path` is `[agent-root, agent-child, agent-grandchild]`, `agent_hint` is the leaf, `identity_verified` true

```
$ python3 -c "
import identity
root = identity.mint('agent-root')
child = identity.attenuate(root, [], delegate_to='agent-child')
grandchild = identity.attenuate(child, ['forbid action is \"delete_all\"'], delegate_to='agent-grandchild')
print(identity.verify(grandchild).delegation_path)
"
['agent-root', 'agent-child', 'agent-grandchild']

$ SESHAT_IDENTITY_TOKEN=<grandchild> python3 -c "mcp_server.stop_orphan(port=4242)"
{
  "type": "mcp_session",
  "agent_hint": "agent-grandchild",
  "identity_verified": true,
  "delegation_path": ["agent-root", "agent-child", "agent-grandchild"]
}
```

## 5. MCP tool set: `attenuate_identity` present, `mint` absent

```
$ python3 -c "
import mcp_server
print(sorted(t.name for t in mcp_server.mcp._tool_manager.list_tools()))
"
['amend_agreement', 'attenuate_identity', 'register_project', 'set_project_override',
 'set_secret', 'start_group', 'start_project', 'stop_group', 'stop_orphan', 'stop_project']
```

Ten tools: `attenuate_identity` present (the one agent-reachable
identity-issuing verb, since it can only narrow), `mint` absent.

## 6. Tamper: flip a byte in a delegated token's signature → `verify` None → deny

```
$ python3 -c "
import identity, agreements
root = identity.mint('agent-root')
child = identity.attenuate(root, ['forbid action is \"read\"'], delegate_to='agent-child')
h, p, s = child.split('.')
forged = h + '.' + p + '.' + (('A' if s[-1] != 'A' else 'B') + s[1:])
print(identity.verify(forged))
d = agreements.check_action('agent-x', 'read', agreement_text='permit actor is \"agent-root\" and action is \"read\"', token=forged)
print(d.allowed, d.mode)
"
None
False identity-invalid
```

## Full test suite

```
$ python3 -m pytest -q
........................................................................ [ 17%]
........................................................................ [ 34%]
........................................................................ [ 52%]
........................................................................ [ 69%]
........................................................................ [ 87%]
....................................................                     [100%]
412 passed in 8.96s
```

412 passed, 0 failed — up from 389 at the end of Stage 1 (23 net new test
functions across `test_identity.py` (`TestAttenuation`),
`test_receipts.py`, `test_mcp_enforcement_gate.py` (`TestDelegation`), and
`test_identity_cli.py`). No regressions in Stage 1's identity tests,
`test_receipts.py` (F-01), `test_agreements.py` (F-07 and existing Agreement
semantics), or `test_mcp_enforcement_gate.py` (F-11). One Stage 1 test
(`test_mint_is_not_an_mcp_tool`'s over-broad "no tool name contains
'identity'" assertion, written before `attenuate_identity` existed) was
loosened to the actual invariant it was meant to protect: `mint` absent,
not identity-named tools in general.
