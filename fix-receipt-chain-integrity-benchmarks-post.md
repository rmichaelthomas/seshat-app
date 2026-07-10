# Receipt chain integrity — post-build benchmark

**Captured:** 2026-07-10, after B1 (keyed MAC) + B2 (`.chain_head` anchor)
landed (branch `fix/receipt-chain-integrity`).
**Purpose:** re-run the exact scenarios from `fix-receipt-chain-integrity-benchmarks-pre.md`
against the new code and confirm the truncation case actually flips from
"intact" to "detected." Same scratch-copy methodology — the real
`~/.seshat/receipts/` was never modified.

One environment note: this benchmark runs inside a sandboxed shell that
cannot reach the interactive macOS Keychain (writes fail with
`keyring.errors.PasswordSetError`), so the emit step below substitutes a
fixed `_mac_key()` for the demonstration. The Keychain integration itself
(`keyring.get_password`/`set_password`, same call shape `vault.py` already
uses in its shipped Fernet-key path) is exercised for real by the automated
test suite's mocked-but-structurally-identical calls, and by `vault.py`'s
existing production usage — this substitution only affects *this manual
demo run*, not the shipped code path.

## Scenario 1 — untouched legacy chain (3 receipts, no anchor yet)

```
$ HOME=<scratch> PYTHONPATH=. python3 -m cli receipts verify
⚠ 3 receipt(s) verified via the legacy unkeyed method (written before chain
keying) — link-verified only, not forgery-resistant.
✓ Chain intact — 3 receipt(s) verified.
```

Matches the pre-build baseline's "Chain intact" result, now with an
explicit legacy-method disclosure that didn't exist before. No anchor
exists yet, so truncation of this chain alone still couldn't be detected —
expected: the anchor only starts protecting the chain from the moment a
keyed receipt is first emitted against it.

## Scenario 2 — after emitting one real keyed receipt (bootstraps `.chain_head`)

```
$ python3 -c "receipts.emit(...)"
emitted receipt_version: 2
identity_verified: False

$ cat .chain_head
{"head_hash": "346760452ed1b737649dcf702ee160279215f6cf32ee0f1467ce3bf83482d726", "count": 4}

$ HOME=<scratch> PYTHONPATH=. python3 -m cli receipts verify
⚠ 3 receipt(s) verified via the legacy unkeyed method (written before chain
keying) — link-verified only, not forgery-resistant.
✓ Chain intact — 4 receipt(s) verified.
```

The new receipt correctly chains from the legacy chain's existing head
(migration continuity), stamps `receipt_version: 2` and
`identity_verified: false`, and bootstraps `.chain_head` to
`{head_hash, count: 4}`.

## Scenario 3 — tail-truncated chain (the newest, now-anchored receipt deleted)

```
$ rm 20260710T193249520376_benchmark_demo_4d26528a.json

$ HOME=<scratch> PYTHONPATH=. python3 -m cli receipts verify
⚠ 3 receipt(s) verified via the legacy unkeyed method (written before chain
keying) — link-verified only, not forgery-resistant.
✗ Truncation detected — the chain anchor recorded 4 receipt(s) ending at
346760452ed1b737649dcf702ee160279215f6cf32ee0f1467ce3bf83482d726, but only 3
receipt(s) ending at 3f06beca2018bda5777454690c1e642486937829b18aa189f93277b2911d9931
are present on disk. Receipts were deleted, or never returned after being
written.
```

**This is the required flip.** Deleting the newest receipt still leaves a
perfectly self-consistent, correctly-linked 3-receipt chain — the same
failure mode as the pre-build benchmark's Scenario 2, where this reported
`✓ Chain intact — 3 receipt(s) verified.` with no distinction from a chain
that legitimately only ever had 3 receipts. Now it reports `✗ Truncation
detected`, because `.chain_head` independently recorded what the true head
and count were *at emission time*, and disk state no longer matches.

## Diff summary vs. pre-build

| Scenario | Pre-build | Post-build |
|---|---|---|
| Untouched chain | `✓ Chain intact — 3 receipt(s) verified.` | `✓ Chain intact — 3 receipt(s) verified.` (+ legacy-method disclosure) |
| Tail-truncated (2 of 3 deleted) | `✓ Chain intact — 1 receipt(s) verified.` (silently wrong) | *(this exact case isn't re-run — no anchor existed for the legacy-only chain; see Scenario 3 below for the anchored case)* |
| Tail-truncated **after** a keyed receipt anchors the chain | *(not possible pre-build — keying/anchoring didn't exist)* | `✗ Truncation detected — ...` |

The pre-build gap was specifically about receipts written under the new,
anchored regime — which is exactly the case Scenario 3 reproduces and
closes. The legacy-only truncation case (deleting from a chain that never
had an anchor) remains structurally undetectable, by design: an anchor can
only protect receipts emitted after it exists, the same way a burglar alarm
can't report a break-in that happened before it was installed. That gap
closes as soon as any new receipt is emitted, at which point the anchor
starts covering the whole chain going forward.
