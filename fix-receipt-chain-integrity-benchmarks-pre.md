# Receipt chain integrity — pre-build benchmark

**Captured:** 2026-07-10, before any B1/B2 changes (repo at `9d5ddea`).
**Purpose:** record the current, pre-fix behavior of `seshat receipts verify`
against (a) the real local chain untouched, and (b) a tail-truncated copy —
so the post-build benchmark can prove the truncation case actually flips
from "intact" to "detected".

Both runs use a scratch `$HOME` pointed at a copy of the real
`~/.seshat/receipts/` directory (3 real receipts from this machine's prior
`seshat` usage), invoked as `HOME=<scratch> PYTHONPATH=. python3 -m cli
receipts verify` from the repo root. The real `~/.seshat/receipts/` was never
modified — every mutation happened on a throwaway copy.

## Scenario 1 — untouched chain (3 receipts)

```
$ HOME=<scratch> PYTHONPATH=. python3 -m cli receipts verify
✓ Chain intact — 3 receipt(s) verified.
```

Expected and correct: nothing was tampered with.

## Scenario 2 — tail-truncated chain (newest 2 of 3 receipts deleted)

Copied the same 3 receipts to a second scratch `$HOME`, then deleted the two
newest files by filename-sort order (simulating an attacker — or a bug —
silently dropping the tail of the chain, e.g. `rm` on the two most recent
receipt files):

```
$ ls
20260707T223523465335_stop_project_26362924.json   (kept — oldest)
20260707T223631494895_start_project_479bb306.json  (deleted)
20260707T223708173710_set_secret_367adad3.json      (deleted)

$ HOME=<scratch-truncated> PYTHONPATH=. python3 -m cli receipts verify
✓ Chain intact — 1 receipt(s) verified.
```

**This is the F-01 finding, reproduced.** `receipts_verify` walks
`sorted(RECEIPTS_DIR.glob("*.json"))` — filename order — and checks
`previous_hash` linkage plus a hash recompute. The remaining single receipt
(the oldest one, whose `previous_hash` is `null`, i.e. genesis) verifies
against itself just fine in isolation. There is no persisted record of how
many receipts *should* exist or what the chain's head *should* be, so nothing
in this code path can distinguish "the chain has always had 1 receipt" from
"2 receipts were deleted off the end." The tool reports the exact same
`✓ Chain intact` message either way.

## What must change (post-build)

After B1 (keyed MAC) + B2 (persisted `.chain_head` anchor + count), Scenario
2 must report a truncation distinctly from "intact" — the `.chain_head`
pointer will record the head hash and count *at the time each receipt was
emitted*, independent of what files happen to be sitting in the directory
when `verify` runs later. Re-run in `fix-receipt-chain-integrity-benchmarks-post.md`.
