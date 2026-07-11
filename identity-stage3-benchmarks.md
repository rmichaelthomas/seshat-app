# Identity plane Stage 3 — acceptance benchmarks (§9)

**Captured:** 2026-07-10, branch `feat/identity-plane-stage3-lifecycle`.
**Purpose:** demonstrate, from the real code paths, the §9 acceptance checks — closing the identity-plane arc.

Same environment note as Stages 1–2: this runs inside a sandboxed shell that
cannot reach the interactive macOS Keychain, so `identity._root_key` and
`receipts._mac_key` are substituted with fixed test values. Everything else
is the real, shipped code: `identity.py`'s actual `mint`/`attenuate`/
`revocation_identifiers`, `agreements.check_action`, `cli.py`'s actual
`seshat identity revoke` command (invoked via `click.testing.CliRunner`,
in-process), and `mcp_server.py`.

## Terminology note: "expired" is plain English, not a new `Decision.mode`

§9's first bullet says a past-ttl token is "denied `expired`". The
mechanism (§4) is a `starting "<date>" forbid actor is "<name>"` caveat —
a blanket forbid that activates once its window opens. Since it's a
`forbid`, the resulting `Decision.mode` is `"forbidden"` (the same mode any
other active forbid produces), not a new literal `"expired"` string — there
is no such mode in this codebase. "Expired" describes the observable
behavior in English; the mechanism is the same temporal-forbid machinery
Stage 1 already used for caveat-level windows, just applied blanket-wide
via a predicate that names only the actor fact.

## 1. A token minted with `ttl 0` → denied; within-window → allowed

```
$ python3 -c "
import identity, agreements
expired = identity.mint('agent-root', ttl_hours=0)
d = agreements.check_action('ignored', 'translate',
      agreement_text='permit actor is \"agent-root\" and action is \"translate\"',
      token=expired)
print(d.allowed, d.mode)

within_window = identity.mint('agent-root')  # default 24h
d2 = agreements.check_action('ignored', 'translate',
      agreement_text='permit actor is \"agent-root\" and action is \"translate\"',
      token=within_window)
print(d2.allowed, d2.mode)
"
False forbidden
True permitted
```

## 2. `seshat identity revoke agent-x`; a subsequently-presented valid token for `agent-x` → denied `revoked-identity`. Un-revoked agent unaffected.

```
$ seshat identity revoke agent-x
✓ Revoked: agent-x

$ python3 -c "
import identity, agreements
token = identity.mint('agent-x', ttl_hours=None)
d = agreements.check_action('ignored', 'translate',
      agreement_text='permit actor is \"agent-x\" and action is \"translate\"',
      token=token)
print(d.allowed, d.mode)
"
False revoked-identity

$ python3 -c "
import identity, agreements
token = identity.mint('agent-y', ttl_hours=None)  # never revoked
d = agreements.check_action('ignored', 'translate',
      agreement_text='permit actor is \"agent-y\" and action is \"translate\"',
      token=token)
print(d.allowed, d.mode)
"
True permitted
```

## 3. Two-hop delegation root→child→grandchild; `revoke root` denies the grandchild (path-aware). `revoke child` denies grandchild but not an unrelated root-direct token.

```
$ python3 -c "
import identity
root = identity.mint('agent-root', ttl_hours=None)
child = identity.attenuate(root, [], delegate_to='agent-child')
grandchild = identity.attenuate(child, [], delegate_to='agent-grandchild')
"
$ seshat identity revoke agent-root
$ python3 -c "... check_action(token=grandchild) ..."
False revoked-identity
```

```
# A separate, unrelated chain — root2 is never revoked.
$ seshat identity revoke agent-child2
$ python3 -c "... check_action(token=grandchild2) ..."
False revoked-identity
$ python3 -c "... check_action(token=root2) ..."
True permitted
```

Path-aware by construction: `check_action`'s enforced `actor` is always the
root (the Stage 2 escalation fix), and `identity.revocation_identifiers`
returns the full `[root, ..., leaf]` chain, so revoking any name in that
chain denies — no extra plumbing needed beyond what Stage 2 already built.

## 4. `revoke --token <specific token>` → that token denied by nonce; the same agent's other tokens still work

```
$ python3 -c "
import identity
token_a = identity.mint('agent-z', ttl_hours=None, nonce='nonce-a-demo')
token_b = identity.mint('agent-z', ttl_hours=None, nonce='nonce-b-demo')
"
$ seshat identity revoke --token <token_a>
$ python3 -c "... check_action(token=token_a) ..."
False revoked-identity
$ python3 -c "... check_action(token=token_b) ..."
True permitted
```

## 5. Revocation runs under the F-07 gate: a stale `revocations.limn` denies by default (existing behavior, unchanged)

```
$ python3 -c "
# .last_synced_revocations pointed at a path that was never written
import identity, agreements
token = identity.mint('agent-fresh', ttl_hours=None)
d = agreements.check_action('ignored', 'translate',
      agreement_text='permit actor is \"agent-fresh\" and action is \"translate\"',
      token=token)
print(d.allowed, d.mode)
"
False stale-revocations
```

Confirms the identity-revocation check (this stage) runs *inside* the
existing F-07 staleness-gated block, not around or before it — a stale
file still denies everything by default, exactly as before this stage
existed.

## 6. MCP tool set: `attenuate_identity` present; `mint` and `revoke` absent

```
$ python3 -c "
import mcp_server
print(sorted(t.name for t in mcp_server.mcp._tool_manager.list_tools()))
"
['amend_agreement', 'attenuate_identity', 'register_project', 'set_project_override',
 'set_secret', 'start_group', 'start_project', 'stop_group', 'stop_orphan', 'stop_project']
```

The complete mint/attenuate/revoke authority asymmetry: mint issues
(human-only), attenuate narrows (agent-reachable, since it can only
narrow), revoke kills (human-only) — ten tools, none named `mint` or
containing `revoke`.

## A bug found and fixed along the way: a ~1.5%-chance no-op in every "forged signature" test

While running these benchmarks repeatedly to build confidence in the new
default-random-nonce behavior, one run of the automated suite hit an
intermittent failure in a Stage 2-era test
(`test_inspect_a_forged_token_reports_unverified_without_crashing`) that
had never failed before. Investigation traced it to every "flip the
signature" helper across the identity test suite:

```python
tampered_sig = ("A" if sig_b64[-1] != "A" else "B") + sig_b64[1:]
```

This replaces the **first** character of `sig_b64` but decides the
replacement value by checking the **last** character — an unrelated
condition. Simulated 200,000 random 32-byte signatures: ~1.5% of the time,
the chosen replacement character coincidentally equals the original first
character, making the "forged" token **byte-for-byte identical** to the
original — no tampering happened at all.

This bug was always latent (present since Stage 1), but harmless in
practice as long as `mint()` was fully deterministic: the same fixed test
inputs always produced the same signature, and none of the specific values
already in the suite happened to trigger the ~1.5% collision. This stage's
default random nonce (§6) makes every `mint()` call's signature vary, which
is what surfaced it.

**Fix:** check `sig_b64[0]` (the character actually being replaced), not
`sig_b64[-1]`. Confirmed via the same simulation: 0 collisions in 200,000
trials with the corrected condition. Applied to all six occurrences across
`test_identity.py`, `test_agreements.py`, `test_identity_cli.py`, and
`test_mcp_enforcement_gate.py`. Full suite run 10 times after the fix with
no failures (see below).

## Full test suite

```
$ python3 -m pytest -q
........................................................................ [ 16%]
........................................................................ [ 32%]
........................................................................ [ 49%]
........................................................................ [ 65%]
........................................................................ [ 82%]
........................................................................ [ 98%]
.......                                                                  [100%]
439 passed in 8.20s
```

439 passed, 0 failed — up from 431 (before the forged-signature fix, which
touched existing tests rather than adding new ones) and 412 at the end of
Stage 2. Ran 10 consecutive times after the forged-signature fix with no
failures, confirming the flakiness introduced by the default random nonce
is fully resolved. No regressions in Stage 1–2 identity tests, `test_
receipts.py` (F-01), `test_revocations.py` (F-07 staleness gate, temporal
windows, revocation composition), or `test_mcp_enforcement_gate.py` (F-11).
