# Fix README Start-Command Extraction

**Date:** 2026-04-15
**Status:** Approved

## Problem

Seshat's GitHub import and local scanner extract a project's start command from its README by matching any line starting with `npm`, `python`, etc. via `_START_PATTERNS`. This causes two bugs:

1. **False positives:** `npm install` in a "Setup" section matches before `npm run dev` in a "Run" section, because the parser takes the first linear match.
2. **No section awareness:** The parser treats the entire README as flat text, ignoring heading structure that distinguishes setup from run instructions.

Additionally, projects requiring multiple processes (e.g., `npm run server` + `npm run dev`) only get one command captured. While full multi-process support is deferred to Fix 2, the extraction layer should capture all run commands now.

## Design

### 1. Exclusion Filter

A new module-level regex `_SETUP_COMMANDS` rejects lines that match install/setup commands:

- `npm install`, `npm ci`, `npm init`, `npm link`, `npm uninstall`
- `pip install`, `pip3 install`
- `yarn add`, `yarn install`
- `cargo build` (but not `cargo run`)
- `go get`, `go install` (but not `go run`)
- `make install`, `make build` (but not `make run/start/serve/dev/up`)
- `bundle install`

Applied as a post-filter: after `_START_PATTERNS` matches a line, reject it if `_SETUP_COMMANDS` also matches. This keeps the two concerns cleanly separated — "looks like a command" vs "is actually a setup command."

### 2. Section-Aware Parsing

**`_split_sections(readme: str) -> list[tuple[str, str]]`** — splits a README on heading lines (`^#{1,3}\s+(.+)`, up to h3). Returns `(heading_text, body_text)` tuples. Content before the first heading gets heading `""` (preamble).

**Heading classifiers** (case-insensitive):

- **Run-like:** heading contains any of: `run`, `start`, `usage`, `develop`, `launch`, `getting started`, `quick start`
- **Setup-like:** heading contains any of: `setup`, `install`, `prerequisites`, `requirements`, `build`, `dependencies`

**`_extract_start_commands(readme: str) -> list[str]`** — the core extraction function:

1. Split README into sections via `_split_sections()`
2. Collect all `_START_PATTERNS` matches (post-filtered by `_SETUP_COMMANDS`) from **run-like** sections
3. If none found, collect from **unclassified** sections (preamble, headings matching neither category)
4. If still none, collect from **setup-like** sections as last resort
5. Return all collected matches as a list

Within each section, fenced code blocks are searched first, then bare lines — preserving the existing preference order.

### 3. Changes to Return Types and Affected Files

**`github.py`:**

- Add `_SETUP_COMMANDS` regex at module level
- Add `_split_sections()` helper
- Add `_extract_start_commands()` implementing section-aware filtered extraction
- `_extract_fields()` calls `_extract_start_commands()` instead of inline logic. Returns `"start"` as first match (str | None) and `"start_all"` as full list
- `scan()` result dict gets `"start"` (first command, str | None) and `"start_all"` (list[str]) for Fix 2

**`local_scanner.py`:**

- `_find_start()` updated to `_find_starts()`, returns `list[str]` using the same exclusion filter
- README fallback (step 3 of start extraction) uses `_extract_start_commands()` from github.py for section awareness
- `_extract()` returns `"start"` (first match, str | None) and `"start_all"` (list[str])

**No changes to:** registry, runner, seshat.py, or frontend. Those remain single-command until Fix 2.

## Scope

This is Fix 1 of 2. Fix 2 will handle multi-process support by consuming `start_all` in the registry, runner, and UI.
