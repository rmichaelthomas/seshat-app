# Local Project Discovery — Design Spec
**Date:** 2026-04-08
**Status:** Approved

---

## Overview

Allow users to scan a local directory for projects that are not yet registered in Seshat. The scanner walks one level deep into a target directory, identifies project candidates by the presence of known code/config files, attempts to extract port and start command from local files, cross-references against the existing registry, and presents results in an editable confirmation table — identical flow to GitHub import but distinct modal.

---

## Architecture

### New module: `local_scanner.py`

A self-contained `LocalScanner` class responsible for:
- Walking one level deep into a given directory
- Identifying project candidates by signal files
- Extracting port and start command from local files
- Cross-referencing against the existing Seshat registry

No new dependencies. Uses Python's built-in `pathlib` and `json`.

Reuses `_PORT_PATTERNS` and `_START_PATTERNS` from `github.py` (imported directly).

### New route in `seshat.py`

| Method | Route | Purpose |
|--------|-------|---------|
| POST | `/api/local-scan` | Scan a directory, return candidate list |

Request body: `{ "directory": "~/Projects" }`

### Frontend

**`⌖ Discover`** button added to the top nav between `⚿ Vault` and `⇩ GitHub`. Opens the Discover modal. No token or auth required.

---

## Scanner Logic

### Signal files (any one of these = candidate)

```
package.json, pyproject.toml, setup.py, setup.cfg,
Cargo.toml, go.mod, Gemfile, requirements.txt,
Makefile, *.xcodeproj, .git/
```

A directory is a candidate if it contains at least one signal file. Plain empty directories or non-project folders are skipped.

### Extraction (per candidate)

Attempts extraction in this priority order, stopping at first match:

**Port:**
1. `.env` / `.env.local` / `.env.example` — look for `PORT=NNNN`
2. `package.json` — look for port in `scripts.start`, `scripts.dev`
3. `Makefile` — scan for `PORT` assignment or `localhost:NNNN`
4. Any `*.py` file in root — apply `_PORT_PATTERNS` to first 200 lines
5. `README.md` / `README.rst` — apply `_PORT_PATTERNS`

**Start command:**
1. `package.json` → `scripts.dev` preferred, then `scripts.start`
2. `Makefile` → first target matching `_START_PATTERNS`
3. `pyproject.toml` / `setup.cfg` → look for entry point or run script
4. `README.md` — apply `_START_PATTERNS` to first code block

**Name:** Directory name (as-is).

**Notes:** Empty — not extracted locally.

### Registry cross-reference

Each candidate is marked `registered: true` if its directory path or name matches an existing Seshat project (case-insensitive path comparison).

### Result shape (per candidate)

```json
{
  "name": "my-app",
  "directory": "/Users/me/Projects/my-app",
  "port": "3000",
  "start": "npm run dev",
  "registered": false
}
```

---

## Discover Modal UI

### Directory input

- Text field pre-filled with `~/Projects`
- Quick-select chips: `~`, `~/Projects`, `~/Developer`, `~/Code`, `~/dev`, `~/src`
- Clicking a chip replaces the text field value
- `Scan` button triggers the scan

### Results table

Columns: **[checkbox]** | **Name** | **Directory** | **Port** | **Start Command** | **Status**

- Already-registered rows greyed out, checkboxes disabled, Status = "Registered"
- Missing port or start command: cell highlighted amber
- Port and Start Command cells are editable inputs (synced back before import)
- Name is read-only (directory name)
- Directory is read-only (full path, displayed shortened with `~`)
- "Select all new" checkbox in header
- **Import Selected** in footer — registers rows sequentially, shows per-row ✓ / error inline
- Rows missing port are not pre-checked (same rule as GitHub import)

### Scan UX

- Button shows "Scanning…" while in-flight
- Top-level errors (directory not found, permission denied) shown as a banner
- Empty results: "No new projects found in `<dir>`." message

---

## Error Handling

| Scenario | Behaviour |
|----------|-----------|
| Directory doesn't exist | 400 with `{"error": "Directory not found"}` |
| Permission denied on root | 400 with `{"error": "Cannot read directory"}` |
| Permission denied on subdirectory | Skip silently, continue scan |
| Unreadable file during extraction | Skip that file, continue with next source |
| Duplicate port on import | Row-level error inline, not blocking |
| Missing required field on import | Row-level "Missing fields" error, skip row |

---

## What's Not Included

- Recursive scan beyond depth 1 (user can point scanner at a subdirectory directly)
- Auto-detection of the start command beyond the listed sources
- Notes or tags extraction (imported with empty notes, no tags)
- Watching for new projects (scan is always on-demand)
