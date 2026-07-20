# Identity plane — ID-Q4 Phase 1 acceptance benchmarks (§10)

**Captured:** 2026-07-20, branch `feat/ed25519-identity-plane`.
**Purpose:** demonstrate, from the real shipped code, the §10 acceptance checks — the Ed25519 next-key block chain replacing the HMAC macaroon chain as `identity.py`'s issuance path, with the legacy path preserved.

Same environment note as Stages 1–3: this runs inside a sandboxed shell that
cannot reach the interactive macOS Keychain (see the standing memory note on
`seshat vault set` hanging headless off a TTY), so `identity._root_signing_key`
/ `identity._root_public_key` are substituted with a fixed test keypair —
**the exact same substitution `tests/conftest.py`'s
`_test_identity_root_signing_key` fixture performs for the whole suite.**
Everything else is the real, shipped code: `identity.py`'s actual
`mint`/`verify`/`attenuate`/`root_public_key_hex`, and `cli.py`'s actual
`identity keys show` / `identity keys export` / `identity mint` / `identity
attenuate` commands (invoked via `click.testing.CliRunner`, in-process).

---

## A design decision made mid-build, flagged for the manual gate

Before the six benchmarks: `mcp_server.py`'s `attenuate_identity` tool is
byte-identical to `main` (confirmed below). Its signature —
`attenuate_identity(token, caveats, delegate_to)` — has **no parameter**
through which a calling agent could pass a holder private key. Since every
Ed25519 token now *requires* `holder_private_key` to attenuate (nobody else
can produce a valid signature), a literal reading of "don't touch
mcp_server.py" would have made `attenuate_identity` **permanently
non-functional** for every real minted token — a regression in a currently
working, tested, agent-reachable delegation feature.

This was surfaced to Rob mid-build rather than resolved silently. Resolution
(his call): `identity.attenuate()` falls back to a new
`SESHAT_IDENTITY_HOLDER_KEY` environment variable when `holder_private_key`
isn't passed explicitly — mirroring exactly how `SESHAT_IDENTITY_TOKEN`
already reaches `mcp_server.py` today (a human provisions both env vars for
an agent's MCP session; the agent narrows its own session token). Zero
`mcp_server.py` changes; an explicit `holder_private_key` argument still
always takes precedence over the env var; attenuation still fails closed
(`IllegalCaveatError`) if neither is set. See `identity.HOLDER_KEY_ENV_VAR`
and `attenuate()`'s docstring. Exercised in
`tests/test_identity.py::TestHolderKeyEnvFallback` and
`tests/test_mcp_enforcement_gate.py::TestDelegation::test_attenuate_identity_tool_succeeds_when_permitted_and_returns_new_token`
/ `test_attenuate_identity_tool_fails_closed_without_the_holder_key_env_var`.

A second, smaller consequence of the frozen file: since
`identity.attenuate()` now returns `(token, holder_private_key)` and
`mcp_server.py`'s unmodified `new_token = identity.attenuate(...)` binds the
whole tuple, `attenuate_identity`'s JSON response shape changes from
`"token": "<jwt>"` to `"token": ["<jwt>", "<holder_key_hex_or_null>"]` — the
holder key rides through the *same* channel the token itself already used,
deliberately, with no new code. Demonstrated live below.

---

## 1. Holder-side attenuation without the root key

```
$ python3 -c "
import identity
token, holder_key = identity.mint('agent-root', caveats=['forbid action is \"wipe_disk\"'], ttl_hours=None)

calls = []
identity._root_signing_key = lambda: calls.append(True) or (_ for _ in ()).throw(RuntimeError('keychain locked'))

child, child_key = identity.attenuate(
    token, ['forbid action is \"delete_all\"'], delegate_to='agent-child',
    holder_private_key=holder_key,
)
print('root_signing_key calls during attenuate():', len(calls))
verified = identity.verify(child)
print(verified.identifier, verified.delegation_path, verified.caveats)
"
root_signing_key calls during attenuate(): 0
agent-root ['agent-root', 'agent-child'] ['forbid action is "wipe_disk"', 'forbid action is "delete_all"']
```

`identity._root_signing_key` was broken *before* `attenuate()` ran and was
never called (`0` invocations) — attenuation is holder-side-only, not merely
by convention. The resulting token still verifies, still carries the
delegation hop, and still resolves `identifier` to the root.

## 2. Forgery rejected

```
$ python3 -c "
import identity
from cryptography.hazmat.primitives.asymmetric import ed25519
import base64, json

token, _ = identity.mint('agent-root')
header_b64, payload_b64, _sig_b64 = token.split('.')
payload = json.loads(base64.urlsafe_b64decode(payload_b64 + '=' * (-len(payload_b64) % 4)))

unrelated_key = ed25519.Ed25519PrivateKey.generate()
forged_sig = unrelated_key.sign(identity._canonical_block(payload['blocks'][0]))
sig_part = base64.urlsafe_b64encode(json.dumps([base64.urlsafe_b64encode(forged_sig).rstrip(b'=').decode()]).encode()).rstrip(b'=').decode()
forged = f'{header_b64}.{payload_b64}.{sig_part}'
print(identity.verify(forged))
"
None
```

A block 0 signed by a freshly generated, wholly unrelated Ed25519 key fails
verification outright.

## 3. Caveat removal detected

```
$ python3 -c "
import identity, base64, json

token, _ = identity.mint('agent-root', caveats=['forbid action is \"wipe_disk\"'], ttl_hours=None)
header_b64, payload_b64, sig_b64 = token.split('.')
payload = json.loads(base64.urlsafe_b64decode(payload_b64 + '=' * (-len(payload_b64) % 4)))
print('before:', payload['blocks'][0]['caveats'])
payload['blocks'][0]['caveats'] = []
new_payload_b64 = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True).encode()).rstrip(b'=').decode()
tampered = f'{header_b64}.{new_payload_b64}.{sig_b64}'
print('verify(tampered):', identity.verify(tampered))
"
before: ['forbid action is "wipe_disk"']
verify(tampered): None
```

Stripping a caveat from a signed block (keeping the now-mismatched original
signature) is detected — the signature covers the exact block content,
`next_key` included.

## 4. Root anchoring after two delegation hops

```
$ python3 -c "
import identity
root_token, root_key = identity.mint('agent-root')
child_token, child_key = identity.attenuate(root_token, [], delegate_to='agent-child', holder_private_key=root_key)
grandchild_token, _ = identity.attenuate(
    child_token, ['forbid action is \"wipe_disk\"'], delegate_to='agent-grandchild',
    holder_private_key=child_key,
)
verified = identity.verify(grandchild_token)
print('delegation_path:', verified.delegation_path)
print('identifier:', verified.identifier)
"
delegation_path: ['agent-root', 'agent-child', 'agent-grandchild']
identifier: agent-root
```

At delegation depth 2, `VerifiedIdentity.identifier` still resolves to
`agent-root` — block 0's identifier, never the self-chosen leaf (v1.0n
build-time catch #2: leaf-rename escalation).

## 5. Legacy interop

```
$ python3 -c "
import identity, secrets, json, base64

caveats = ['forbid action is \"wipe_disk\"']
sig = identity._chain_signature('agent-legacy', caveats, nonce='legacy-nonce-demo')
legacy_token = identity._serialize('agent-legacy', 'legacy-host', caveats, sig, nonce='legacy-nonce-demo')
print('alg header:', json.loads(base64.urlsafe_b64decode(legacy_token.split(\".\")[0] + '==')))

verified = identity.verify(legacy_token)
print('identifier:', verified.identifier, 'caveats:', verified.caveats, 'nonce:', verified.token_nonce)

child, holder_key = identity.attenuate(legacy_token, ['forbid action is \"delete_all\"'])
print('attenuate() holder key (must be None):', holder_key)
print('child verifies:', identity.verify(child) is not None)
"
alg header: {'alg': 'HS256-macaroon', 'typ': 'SIT'}
identifier: agent-legacy caveats: ['forbid action is "wipe_disk"'] nonce: legacy-nonce-demo
attenuate() holder key (must be None): None
child verifies: True
```

A token hand-constructed exactly as the pre-ID-Q4 `mint()` body did — carrying
`alg: HS256-macaroon` — verifies with the same field semantics as before, and
still attenuates via the unchanged root-key path (`holder_private_key` is
`None`, since no holder keypair exists in that model).

## 6. Public-key-only verification (the cross-org property) — proven unambiguous

```
$ python3 -c "
import identity

token, holder_key = identity.mint('agent-root', caveats=['forbid action is \"wipe_disk\"'], ttl_hours=None)
print('minted while the root private key is still reachable.')

def _boom():
    raise RuntimeError('keychain locked -- simulating an unavailable root private key')
identity._root_signing_key = _boom
print('identity._root_signing_key is now BROKEN and unconditionally raises.')
print('(this is the auditor / cross-org verifier scenario: only')
print(' root_public_key_hex() was ever handed to them, never the private key)')

verified = identity.verify(token)
print('verify() result:', verified.identifier, verified.caveats)
"
minted while the root private key is still reachable.
identity._root_signing_key is now BROKEN and unconditionally raises.
(this is the auditor / cross-org verifier scenario: only
 root_public_key_hex() was ever handed to them, never the private key)
verify() result: agent-root ['forbid action is "wipe_disk"']
```

`identity._root_signing_key` — the function that would fetch the root
**private** key — was broken (unconditionally raises) *before*
`identity.verify()` ran, and `verify()` still succeeded, returning the
correct identifier and caveats. `verify()`'s EdDSA-chain path calls
`_require_root_public_key()` exclusively; it never calls
`_root_signing_key()` at all, at any point, for any reason. This is the
whole thesis of ID-Q4 Phase 1: **an auditor or enterprise SIEM, holding only
`root_public_key_hex()`, can independently verify a delegation chain without
ever being handed a forging key.**

---

## CLI demonstrations

```
$ seshat identity keys show
03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8

  This is the ROOT PUBLIC key for this install — safe to share. Hand it to a
third party so they can independently verify tokens this install mints, without
ever needing this install's private key.
```

64 hex chars, no private material — confirmed programmatically (the private
key's own hex string does not appear anywhere in the captured output).

```
$ seshat identity keys export --out root_pub.hex
✓ Wrote root public key to /.../root_pub.hex
(file contents: 03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8)
```

```
$ seshat identity mint agent-demo --ttl 24
✓ Minted identity token for agent-demo:

eyJhbGciOiAiRWREU0EtY2hhaW4iLCAidHlwIjogIlNJVCJ9. ... (Ed25519 block-chain token, truncated)

  Set SESHAT_IDENTITY_TOKEN to this value for agent-demo's MCP session.

metadata file mode: 600 (must be 600)
holder_private_key IS present in the metadata JSON file on disk (0600, expected — §8)
holder_private_key was NOT printed to stdout above (checking...)
confirmed: holder private key never appears in console output.
```

```
$ seshat identity attenuate <token> --caveat 'forbid action is "wipe_disk"' --as agent-demo-child
✓ Narrowed identity token:

eyJhbGciOiAiRWREU0EtY2hhaW4iLCAidHlwIjogIlNJVCJ9. ... (extended block chain, truncated)

  Set SESHAT_IDENTITY_TOKEN to this value for agent-demo-child's MCP session.

verified.delegation_path = ['agent-demo', 'agent-demo-child']
confirmed: the CHILD's holder private key was also never printed to stdout.
```

`identity attenuate` looked up `agent-demo`'s holder key from
`~/.seshat/identity/agent-demo.json` (written by the preceding `mint`) —
neither command ever printed a private key to the terminal, and both
metadata files were written mode `0600`.

## MCP tool: the holder-key env-var fallback, live

```
$ python3 -c "
import os, json, identity, mcp_server

root, root_key = identity.mint('agent-root')
os.environ['SESHAT_IDENTITY_TOKEN'] = root
os.environ['SESHAT_IDENTITY_HOLDER_KEY'] = root_key
mcp_server.agreements.load_agreement = lambda: 'permit actor is \"agent-root\" and action is \"attenuate_identity\"'

result = json.loads(mcp_server.attenuate_identity(
    token=root, caveats=['forbid action is \"wipe_disk\"'], delegate_to='agent-child',
))
print('status:', result['status'])
new_token, new_holder_key = result['token']
verified = identity.verify(new_token)
print('delegation_path:', verified.delegation_path)
print('holder key present in response:', bool(new_holder_key))
"
status: success
delegation_path: ['agent-root', 'agent-child']
holder key present in response: True
```

Confirms the response-shape change documented above: `result['token']` is
now `[token, holder_key]`, produced by **zero changes** to `mcp_server.py` —
the tuple `identity.attenuate()` returns rides through unmodified.

---

## Full test suite

```
$ python3 -m pytest -q
........................................................................ [ 12%]
........................................................................ [ 25%]
........................................................................ [ 38%]
........................................................................ [ 51%]
........................................................................ [ 64%]
........................................................................ [ 76%]
........................................................................ [ 89%]
..........................................................               [100%]
562 passed in 9.49s
```

**562 passed, 0 failed, 0 skipped** — up from a pre-build baseline of **531
passed** (recorded at base SHA `763d9459d0118a156410329bbcd681611d9c0f21`,
before branching). Net **+31 tests**, zero regressions. Ran 4 consecutive
times with identical results (no flakiness from the randomness inherent in
fresh Ed25519 keypair generation on every `mint()`/`attenuate()` call).

```
$ git diff --stat main -- agreements.py mcp_server.py receipts.py requirements.txt
(empty)
```

`agreements.py`, `mcp_server.py`, `receipts.py`, and `requirements.txt` are
byte-identical to `main` — confirmed by an empty diff.

---

## Invariants exercised by tests (§9)

| # | Invariant | Where exercised |
|---|---|---|
| 1 | Fail closed, never unkeyed | `TestEd25519RootKeyManagement` (mint/verify/root_public_key_hex all raise `IdentityKeyUnavailableError`); `TestLegacyHmacInterop::test_legacy_verify_fails_closed_when_root_key_unavailable` |
| 2 | Identifier is always the ROOT | `TestEd25519HolderSideAttenuation::test_root_anchoring_survives_two_delegation_hops`; `TestAttenuation::test_delegate_to_cannot_escalate_via_an_unrelated_agreement_actor_rule` |
| 3 | `permit` is never a legal caveat | `TestCaveatLegality::test_permit_verb_is_illegal` (unchanged, alg-agnostic) |
| 4 | Attenuation only narrows | `TestAttenuation::test_attenuate_refuses_a_non_monotonic_classification` |
| 5 | No agent-reachable enforcement-surface write | structural — `identity.py` has no reference to `agreement.limn`/`revocations.limn`/`invariant.limn`/`entrenched.limn`/`teams.limn` anywhere (grep-verifiable); `cli.py`'s identity commands remain human-only, unchanged in that respect |
| 6 | `agreements.py`, `mcp_server.py`, `receipts.py` byte-identical to main | `git diff --stat` above |
| 7 | Zero new dependencies | `requirements.txt` byte-identical to main (same diff) |
| 8 | Legacy tokens verify unchanged | `TestLegacyHmacInterop` (whole class) |
| 9 | Holder private keys never reach a receipt / logged output / stdout | `test_mcp_enforcement_gate.py::TestDelegation::test_attenuate_identity_tool_succeeds_when_permitted_and_returns_new_token` (receipt exclusion); CLI demonstrations above (stdout exclusion) |
| 10 | Suite grows monotonically, zero failures | 531 → 562, 0 failures |
