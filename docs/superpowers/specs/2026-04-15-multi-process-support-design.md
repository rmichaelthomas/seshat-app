# Multi-Process Support (Fix 2)

**Date:** 2026-04-15
**Status:** Approved

## Problem

Projects like Astroweather require two processes to run (e.g., `npm run server` for the API proxy + `npm run dev` for the Vite frontend). Fix 1 already captures all run commands into `start_all`, but only the first command populates `start`. The second process is silently dropped.

## Design

### Compound Command Approach

When `start_all` has 2+ entries, join them with ` & ` to produce the `start` string. The shell backgrounds the first command and runs the second in the foreground. Both processes end up in the same process group (via `start_new_session=True` in the runner), so `os.killpg(pgid, SIGTERM)` kills them all cleanly.

No changes are needed to the registry schema, runner, state tracking, or API endpoints — the compound command is just a string that the existing infrastructure handles naturally.

### Changes

**`github.py` `scan()`:**
- When building each result dict, if `start_all` has 2+ entries, set `start` to `" & ".join(start_all)`.
- `start_all` remains in the result for display/debugging purposes.

**`local_scanner.py` `scan()`:**
- Same logic: if `start_all` has 2+ entries, set `start` to `" & ".join(start_all)`.

**`local_scanner.py` `_extract()`:**
- Already returns `start_all` (from Fix 1). The join happens in `scan()`, not `_extract()`, to keep extraction and presentation separate.

### Frontend

No UI changes needed. The existing editable start field in the import table shows the compound command string. Users can review and edit it before importing.

### What Does NOT Change

- `registry.py` — schema unchanged, `start` is still a string
- `runner.py` — no changes, `shell=True` already handles `&`
- `seshat.py` — no API changes
- `state.json` — still one PID per project (the shell's PID / process group leader)
- Frontend dashboard — status display unchanged

## Scope

This is Fix 2 of 2. Fix 1 (completed) added the exclusion filter, section-aware parsing, and `start_all` collection. This fix consumes `start_all` to produce compound start commands.
