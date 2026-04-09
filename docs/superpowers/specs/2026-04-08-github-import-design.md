# GitHub Import — Design Spec
**Date:** 2026-04-08
**Status:** Approved

---

## Overview

Allow users to scan their own GitHub repos, extract project metadata from READMEs and repo data, detect local clones automatically, and register projects via a confirmation table — without leaving Seshat.

---

## Architecture

### New module: `github.py`
A self-contained class (`GitHubImporter`) responsible for:
- GitHub API communication (token auth, pagination)
- README fetching and field extraction
- Local path detection
- Cross-referencing against the existing Seshat registry

No new third-party dependencies. Uses Python's built-in `urllib` for HTTP calls.

### Token storage
GitHub token stored in the Vault under a reserved `__seshat__` project key, encrypted alongside other secrets. This keeps credential handling consistent with the rest of Seshat.

### New routes in `seshat.py`
| Method | Route | Purpose |
|--------|-------|---------|
| GET | `/api/github/status` | Is a token configured? |
| POST | `/api/github/token` | Save/update token |
| GET | `/api/github/scan` | Run full scan, return table data |

### Frontend entry point
**"Import from GitHub"** button added to the top nav alongside "+ Register Project". First click checks `/api/github/status`:
- No token → opens token setup modal
- Token present → runs scan and opens import table modal

---

## GitHub Scanning & Extraction

### Repo fetch
- Calls `GET /user/repos` with pagination (all pages)
- Filters to repos owned by the authenticated user
- Sorted by last pushed (most recent first)
- Forks included but flagged (`is_fork: true`) for optional filtering

### Local path detection
Searches these directories in order for each repo:
`~`, `~/Projects`, `~/Developer`, `~/Code`, `~/dev`, `~/src`

For each candidate path, checks that `<dir>/<repo-name>/.git/config` exists and contains the repo's GitHub URL. Also tries common name variations (`my-app` → `my_app`, `MyApp`). Returns the most recently modified match, or `null` if not found locally.

### README extraction
Fetches README via GitHub API (base64 decoded). Applies patterns in order:

**Port** — first match of:
- `PORT[=\s:]+(\d{4,5})`
- `localhost:(\d{4,5})`
- `:\s*(\d{4,5})`

**Start command** — first code block line matching any of:
`python3?`, `npm`, `node`, `uvicorn`, `flask`, `yarn`, `cargo run`, `go run`, `./gradlew`, `make`

**Notes** — first non-heading paragraph of README, capped at 300 characters.

**Tags** — GitHub repo topics + primary language (lowercased), deduplicated.

### Confidence signals
Each extracted field carries a confidence hint:
- `detected` — pattern matched, value pre-filled
- `missing` — no match found, field left blank and highlighted amber in the UI

### Registry cross-reference
Each repo is marked `registered: true` if its name or detected local path matches an existing Seshat project (matched case-insensitively).

---

## Import Table UI

Full-screen modal following the existing Organize modal pattern.

### Columns
`[checkbox]` | Repo | Local Path | Port | Start Command | Tags | Status

### Behavior
- Already-registered rows are greyed out with checkboxes disabled
- `missing`-confidence fields highlighted in amber
- All editable cells (Port, Local Path, Start Command, Tags) support inline editing before import
- Repos with no detected local path show a text input — cannot be imported until a path is provided
- "Select all new" checkbox in header selects all unregistered rows at once
- **Import Selected** button registers checked rows sequentially, showing per-row success/fail inline
- Row-level errors (duplicate port, missing required field) surface on the row itself, not as a blocking modal

### Scan UX
- "Import from GitHub" click triggers a loading state: "Scanning your repos…"
- If scan exceeds 3 seconds: "Found N repos so far…"
- Top-level errors (bad token, rate limit) shown as a banner at the top of the modal

---

## Token Setup Modal

Shown on first use when no token is configured.

- Single text input for Personal Access Token
- Required GitHub scope noted inline: `repo` (includes private) or `public_repo` (public only)
- "Test & Save" button — calls `GET /user` to validate, saves to Vault on success, shows GitHub's error message on failure
- "Manage token" link in import modal footer for future updates

---

## Error Handling

| Scenario | Behaviour |
|----------|-----------|
| Rate limited | Show reset time countdown, "Retry after Xs" button |
| Repo has no README | All fields `missing`, row still shown |
| README too large | Skip README fetch, extract from repo description only |
| Network failure mid-scan | Return partial results with banner noting incomplete fetch |
| Multiple local path candidates | Pick most recently modified, tooltip notes ambiguity |

---

## What's Not Included

- README parsing is heuristic only — no Claude API call
- Scan results are not cached to disk; re-scanning always fetches live data
- No support for org repos or repos owned by others (own repos only)
- No automatic cloning of repos not found locally
