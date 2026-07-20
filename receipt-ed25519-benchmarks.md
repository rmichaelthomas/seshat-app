# Receipts plane — ID-Q4 Phase 2 acceptance benchmarks (§10)

**Captured:** 2026-07-20, branch `feat/ed25519-receipt-signatures`.
**Purpose:** demonstrate, from the real shipped code, the §10 acceptance checks — Ed25519 receipt signatures (`RECEIPT_VERSION` 2 → 3) replacing HMAC hashing as `receipts.py`'s per-receipt integrity mechanism, with the HMAC path preserved for existing version-2 receipts, plus the ID-Q5 response-shape fix to `mcp_server.py`'s `attenuate_identity`.

Same environment note as the identity-plane benchmarks: this runs inside a
sandboxed shell that cannot reach the interactive macOS Keychain, so
`receipts._receipt_signing_key` / `receipts._receipt_public_key` are
substituted with a fixed test keypair — **the exact same substitution
`tests/conftest.py`'s `_test_receipt_signing_key` fixture performs for the
whole suite** (a distinct keypair from identity's, by construction: derived
from `bytes(range(32, 64))` vs. identity's `bytes(range(32))`). `_mac_key` is
substituted with the same fixed HMAC secret `_test_mac_key` uses
(`test-only-mac-key-not-for-real-use`), only where a benchmark needs to
hand-construct a version-2 receipt. Everything else is the real, shipped
code: `receipts.py`'s actual `emit`/`_signed_hash`/`receipt_public_key_hex`,
and `cli.py`'s actual `receipts verify` / `receipts verify --pubkey` /
`receipts keys show` commands (invoked via `click.testing.CliRunner`,
in-process). Each `RECEIPTS_DIR` below is a fresh temp directory — no real
`~/.seshat/receipts/` state is read or written by these demonstrations.

---

## §A5 key-separation decision, flagged for the manual gate

**Decision: the receipt signing key is a distinct Ed25519 keypair from the
identity root key, in its own Keychain item** (`receipt_signing_key` /
`receipt_signing_public_key`, vs. identity's `identity_root_signing_key` /
`identity_root_signing_public_key`) — the recommendation in the build prompt,
adopted as-is. Nothing in the code made it wrong:

- Identity tokens and receipts are different artifacts with different
  lifetimes (a token is a short-lived bearer capability, `DEFAULT_TTL_HOURS =
  24`; a receipt is a permanent audit record) and different audiences (a
  token's holder narrows their own authority; a receipt's public key is
  handed to an auditor who never holds any bearer capability at all).
- Compromising one must not compromise the other — an install may reasonably
  want to publish its receipt verification key to an auditor or SIEM more
  freely than its identity root, since the receipt key can only ever verify
  past receipts, never mint new authority.
- This matches the existing codebase shape exactly: `receipt_mac_key`
  (`receipts.py`) and `identity_root_key` (`identity.py`) were already
  separate Keychain items under the same `MAC_SERVICE_NAME`, for the
  equivalent HMAC-era reason. The Ed25519 upgrade preserves that separation
  rather than collapsing it.

## §A12 ingest-compatibility finding

Confirmed **by reading**, not by changing, `liminate-dev`'s live
`app/main.py` (blob `62aa98f`, matching the pinned SHA) and `app/db.py`:

- `MachineActionReceipt.receipt_version: int | None = None` — no upper
  bound, so `3` parses without any Pydantic change.
- The hash-verify branch is `if receipt.receipt_version is None or
  receipt.receipt_version < 2:` (`app/main.py:695`) — a version-3 receipt
  skips this branch entirely, exactly as a version-2 receipt already does.
  Both are trusted on harness-attestation alone, per the endpoint's
  documented trust model.
- `receipt_hash: str` (`app/main.py:628`) — no `min_length`/`max_length`
  constraint, so the widened 128-hex-char signature is accepted unchanged.
- Storage: `receipt_hash TEXT NOT NULL UNIQUE` (`app/db.py:130`), and
  `save_machine_action_receipt`/`machine_action_receipt_exists` key
  idempotency off that same column (`app/db.py:1348-1377`) — a `TEXT`
  column has no length limit, and uniqueness/idempotency both continue to
  work unchanged for a longer string.

**Expected result confirmed: version-3 receipts ingest with zero platform
changes.** No `liminate-dev` file was modified from this branch.

---

## 1. Public-key-only verification (the thesis)

```
$ python3 -c "
import receipts
fixed_private = ed25519.Ed25519PrivateKey.from_private_bytes(bytes(range(32, 64)))
receipts._receipt_signing_key = lambda: fixed_private
receipts._receipt_public_key = lambda: fixed_private.public_key()

r0 = receipts.emit(action='start_project', ...)
r1 = receipts.emit(action='stop_project', ...)
print('emitted 2 version-3 receipts. receipt_version:', r0['receipt_version'], r1['receipt_version'])
print('receipt_hash length (hex chars):', len(r0['receipt_hash']))

def _boom():
    raise RuntimeError('keychain locked -- simulating an unavailable receipt private key')
receipts._receipt_signing_key = _boom
print('receipts._receipt_signing_key is now BROKEN and unconditionally raises.')

result = CliRunner().invoke(cli.cli, ['receipts', 'verify'])
print('exit_code:', result.exit_code); print(result.output)
"
emitted 2 version-3 receipts. receipt_version: 3 3
receipt_hash length (hex chars): 128
receipts._receipt_signing_key is now BROKEN and unconditionally raises.
exit_code: 0
✓ Chain intact — 2 receipt(s) verified.
```

`receipts._receipt_signing_key` — the function that would fetch the
**private** key — was broken (unconditionally raises) *before*
`receipts verify` ran, and verification still succeeded. The verify path
calls `_require_receipt_public_key()` exclusively; it never calls
`_receipt_signing_key()` at all, at any point. `receipt_hash` is 128 hex
chars (a 64-byte Ed25519 signature), not 64 (a sha256/HMAC digest) — the
width failure mode (§11 #2) has no client-side assumption to break, verified
by grep (see §9 invariants table below).

## 2. Mixed-version chain (§A8 migration — the primary test)

```
$ python3 -c "
# Hand-write two version-2 (HMAC) receipts simulating a pre-upgrade chain,
# then emit() a real version-3 receipt chaining from the v2 tail.
h0 = make_v2('..._v2_00000000.json', 'start_project', None)
h1 = make_v2('..._v2_00000001.json', 'stop_project', h0)
receipts._write_chain_head(h1, 2)
r2 = receipts.emit(action='start_group', ...)
print('v2 prefix hashes:', h0[:16] + '...', h1[:16] + '...')
print('v3 suffix receipt_version:', r2['receipt_version'], 'previous_hash matches v2 tail:', r2['previous_hash'] == h1)
result = CliRunner().invoke(cli.cli, ['receipts', 'verify'])
print('exit_code:', result.exit_code); print(result.output)
"
v2 prefix hashes: c243b450295bc0a9... a7bf3b56d5f37f12...
v3 suffix receipt_version: 3 previous_hash matches v2 tail: True
exit_code: 0
✓ Chain intact — 3 receipt(s) verified.
```

A chain with a version-2 (HMAC) prefix followed by a version-3 (Ed25519)
suffix verifies end-to-end in one `receipts verify` run — the version-2
receipts via `_keyed_hash` recompute, the version-3 receipt via Ed25519
signature check, both walked by the same link-chain loop. No re-signing of
history, no flag day.

## 3. Tamper detection

```
$ python3 -c "
r0 = receipts.emit(action='start_project', ...)
receipt = json.loads(files[0].read_text())
print('before tamper, action:', receipt['action'])
receipt['action'] = 'TAMPERED'
files[0].write_text(json.dumps(receipt, indent=2))
result = CliRunner().invoke(cli.cli, ['receipts', 'verify'])
print('exit_code:', result.exit_code); print(result.output)
"
before tamper, action: start_project
exit_code: 0
✗ 20260720T234703536339_start_project_55a33dc0.json — signature verification
failed (receipt was modified)

Chain broken at receipt 1 of 1.
```

Modifying a version-3 receipt's body (keeping the now-mismatched original
signature) is detected — the Ed25519 signature covers the exact canonical
receipt bytes, same discipline `_keyed_hash` already used.

## 4. Downgrade rejected

```
$ python3 -c "
r0 = receipts.emit(action='start_project', ...)  # version 3
print('r0 receipt_version:', r0['receipt_version'])
# ...inject a rogue version-2 (HMAC) receipt chained from r0's hash...
print('injected a rogue version-2 receipt after the version-3 receipt.')
result = CliRunner().invoke(cli.cli, ['receipts', 'verify'])
print('exit_code:', result.exit_code); print(result.output)
"
r0 receipt_version: 3
injected a rogue version-2 receipt after the version-3 receipt.
exit_code: 0
✗ 99999999T999999_rogue_ffffffff.json — downgrade detected: a version-2 receipt
appears after the chain had already graduated to version-3 (Ed25519-signed)
receipts

Chain broken at receipt 2 of 2.
```

A version-2 receipt appearing after a version-3 receipt is rejected as a
downgrade — the chain-never-downgrades guard, previously only covering
unversioned-after-keyed, now also covers this direction (§A7).

## 5. Legacy unchanged

```
$ python3 -c "
# Write a version-2-only chain — no Ed25519 involved at all.
h0 = make_v2('..._v2_00000000.json', 'start_project', None)
h1 = make_v2('..._v2_00000001.json', 'stop_project', h0)
receipts._write_chain_head(h1, 2)
print('wrote a version-2-only chain of 2 receipts (no Ed25519 involved at all).')
result = CliRunner().invoke(cli.cli, ['receipts', 'verify'])
print('exit_code:', result.exit_code); print(result.output)
"
wrote a version-2-only chain of 2 receipts (no Ed25519 involved at all).
exit_code: 0
✓ Chain intact — 2 receipt(s) verified.
```

A chain that never touched Ed25519 at all — pure version-2, HMAC-only —
verifies exactly as it did before Phase 2, byte-for-byte identical code path
(`_keyed_hash`, untouched).

## 6. Auditor path — `seshat receipts verify --pubkey`

```
$ python3 -c "
r0 = receipts.emit(action='start_project', ...)
r1 = receipts.emit(action='stop_project', ...)
pubkey_hex = receipts.receipt_public_key_hex()
print('exported public key (this is all the auditor ever receives):', pubkey_hex)

# Simulate the auditor's machine: it never had ANY Keychain item for this install.
def _boom(*a, **k):
    raise RuntimeError('no such Keychain item on this machine')
receipts._receipt_signing_key = _boom
receipts._receipt_public_key = _boom
print('receipts._receipt_signing_key AND _receipt_public_key are now BOTH broken.')

result = CliRunner().invoke(cli.cli, ['receipts', 'verify', '--pubkey', pubkey_hex])
print('exit_code:', result.exit_code); print(result.output)
"
exported public key (this is all the auditor ever receives): 29acbae141bccaf0b22e1a94d34d0bc7361e526d0bfe12c89794bc9322966dd7
receipts._receipt_signing_key AND _receipt_public_key are now BOTH broken.
(this is the auditor scenario: only the hex string above was ever handed to them)
exit_code: 0
✓ Chain intact — 2 receipt(s) verified.
```

`seshat receipts verify --pubkey <hex>` verified a real version-3 chain with
**both** Keychain accessors broken — not just the private key (benchmark 1),
but the local public-key accessor too. Nothing about this verification
touched Keychain state at all; the supplied hex string was the sole source
of truth. This is the cross-org property Phase 2 exists to produce: an
auditor who has only ever been handed `receipt_public_key_hex()`'s output can
independently verify a chain.

**Supporting demonstration — non-Ed25519 tiers under `--pubkey` are reported
unverifiable, not failures:**

```
$ python3 -c "
# A chain with a legacy (unversioned) receipt, a version-2 (HMAC) receipt,
# and a version-3 (Ed25519) receipt, verified via --pubkey.
result = CliRunner().invoke(cli.cli, ['receipts', 'verify', '--pubkey', pubkey_hex])
print('exit_code:', result.exit_code); print(result.output)
"
exit_code: 0
? 10000000T000000_legacy_00000000.json — unverifiable-by-this-method: a legacy,
unkeyed (plain sha256) receipt has no Ed25519 signature to check against
--pubkey. Only chain linkage was checked.
? 10000001T000000_v2_00000001.json — unverifiable-by-this-method: a version-2,
HMAC-keyed receipt has no Ed25519 signature to check against --pubkey. Only
chain linkage was checked.
⚠ 1 receipt(s) verified via the legacy unkeyed method (written before chain
keying) — link-verified only, not forgery-resistant.
⚠ 2 receipt(s) are unverifiable-by-this-method under --pubkey (version-2 HMAC or
legacy plain-hash) — chain linkage was checked, signatures were not.
✓ Chain intact — 3 receipt(s) verified.
```

Version-2 and unversioned receipts are reported `unverifiable-by-this-method`
under `--pubkey` — plainly labeled, not silently passed and not treated as
failures — while the version-3 receipt in the same chain is fully verified
and chain linkage is checked for all three.

## 7. ID-Q5 fixed — `attenuate_identity`'s response shape

```
$ python3 -c "
root, root_key = identity.mint('agent-root')
os.environ['SESHAT_IDENTITY_TOKEN'] = root
os.environ['SESHAT_IDENTITY_HOLDER_KEY'] = root_key
mcp_server.agreements.load_agreement = lambda: 'permit actor is \"agent-root\" and action is \"attenuate_identity\"'

raw = mcp_server.attenuate_identity(
    token=root, caveats=['forbid action is \"wipe_disk\"'], delegate_to='agent-child',
)
print('raw JSON response:'); print(raw)
result = json.loads(raw)
print('type(result[\"token\"]):', type(result['token']).__name__)
print('type(result[\"holder_key\"]):', type(result['holder_key']).__name__)
"
raw JSON response:
{"status": "success", "token": "eyJhbGciOiAiRWREU0EtY2hhaW4iLCAidHlwIjogIlNJVCJ9...(truncated)...", "holder_key": "bec032a1309d00be64d6e6fcbd251b5efb7dac300ab270f5d8833bcb38f58ea8"}

type(result["token"]): str
type(result["holder_key"]): str
delegation_path: ['agent-root', 'agent-child']
new token leaked into receipt: False
holder key leaked into receipt: False
```

Before the fix, `result["token"]` was a 2-element list (`[token,
holder_key]`) because `mcp_server.py`'s unmodified call site bound
`new_token` to the whole tuple `identity.attenuate()` returns. After the
two-line fix, `token` is a plain string and `holder_key` is its own named
field — the response shape an agent reading `result["token"]` as a string
expects. The existing receipt-exclusion rule (neither the token nor the
holder key ever reaches a receipt) still holds, unweakened.

---

## `seshat receipts keys show`

```
$ seshat receipts keys show
29acbae141bccaf0b22e1a94d34d0bc7361e526d0bfe12c89794bc9322966dd7

  This is the RECEIPT PUBLIC key for this install — safe to share. Hand it to a
third party so they can independently verify receipts this install emits,
without ever needing this install's private key.
```

64 hex chars (32-byte public key), no private material — confirmed
programmatically (the private key's own hex string does not appear anywhere
in the captured output).

---

## Full test suite

```
$ python3 -m pytest -q
........................................................................ [ 12%]
........................................................................ [ 25%]
........................................................................ [ 37%]
........................................................................ [ 50%]
........................................................................ [ 63%]
........................................................................ [ 75%]
........................................................................ [ 88%]
...................................................................      [100%]
571 passed in 9.48s
```

**571 passed, 0 failed, 0 skipped** — up from a pre-build baseline of **562
passed** (recorded at base SHA `b106b60b9d7e22a2cd196dd8a4d06d3aa94fbd93`,
before branching — the tip of Phase 1's merged PR #32). Net **+9 tests**
(+1 Phase 0 regression guard, +8 Phase 2/3/4 receipt-signing and
verification-ladder tests), zero regressions.

```
$ git diff --stat main -- agreements.py identity.py requirements.txt
(empty)
```

`agreements.py`, `identity.py`, and `requirements.txt` are byte-identical to
`main` — confirmed by an empty diff.

```
$ git diff main -- mcp_server.py
--- a/mcp_server.py
+++ b/mcp_server.py
@@ -858,7 +858,7 @@ def attenuate_identity(token: str, caveats: list[str] | None = None, delegate_to
     try:
-        new_token = identity.attenuate(token, caveats, delegate_to=delegate_to)
+        new_token, holder_key = identity.attenuate(token, caveats, delegate_to=delegate_to)
     except identity.IllegalCaveatError as e:
@@ -882,7 +882,7 @@ def attenuate_identity(token: str, caveats: list[str] | None = None, delegate_to
-    return json.dumps({"status": "success", "token": new_token})
+    return json.dumps({"status": "success", "token": new_token, "holder_key": holder_key})
```

`mcp_server.py`'s diff against `main` is exactly the two-line Phase 0 unpack
in `attenuate_identity` — nothing else in the file changed.

---

## Invariants exercised by tests (§9)

| # | Invariant | Where exercised |
|---|---|---|
| 1 | Fail closed, never unsigned | `TestKeyedChain::test_emit_fails_closed_when_signing_key_unavailable` |
| 2 | Canonical serialization byte-identical (`sort_keys`, `receipt_hash` excluded) | `_signed_hash` and `_keyed_hash` both receive the identical `canonical` string built once in `emit()` — same call site, unchanged since before Phase 2 |
| 3 | Chain linkage unchanged (`previous_hash`, `.chain_head`, lock, filename format) | `TestVerifyAnchorTruncation`, `TestConcurrentEmission::test_two_threads_produce_linear_chain` (unchanged locking/anchor code, only the hash tier under it changed) |
| 4 | Version-2 and unversioned receipts still verify | `TestVerifyLegacyChain::test_legacy_only_chain_verifies_with_a_warning`; benchmark 5 above |
| 5 | Downgrade detection (version 2 after version 3) | `TestVerifyDowngrade::test_v2_after_v3_is_rejected`; benchmark 4 above |
| 6 | `agreements.py`, `identity.py`, `requirements.txt` byte-identical to main; `mcp_server.py` diff scoped to two lines | `git diff --stat`/`git diff` above |
| 7 | Zero new dependencies | `requirements.txt` byte-identical to main (same diff) |
| 8 | `--pubkey` verification works without the private key present | `TestVerifyPubkeyAuditor` (whole class); benchmark 6 above |
| 9 | Holder private keys still never reach a receipt | `test_mcp_enforcement_gate.py::TestDelegation::test_attenuate_identity_tool_succeeds_when_permitted_and_returns_new_token` (unmodified receipt-exclusion assertion, still passing) |
| 10 | Suite grows monotonically, zero failures | 562 → 571, 0 failures |

Signature-length grep (§11 failure mode #2 — nothing assumes a fixed 64-char
`receipt_hash`): every `receipt_hash[:N]` site in the codebase
(`seshat_tui/graph.py`, `seshat_tui/domains/receipts.py`,
`seshat_tui/domains/invariant.py`) truncates for *display* only (`[:8]`,
`[:14]`, `[:16]`) — none compares against or asserts a full-length value, so
the widened 128-char signature changes nothing there. The platform's
`receipt_hash: str` column is `TEXT` with no length constraint (§A12 above).
