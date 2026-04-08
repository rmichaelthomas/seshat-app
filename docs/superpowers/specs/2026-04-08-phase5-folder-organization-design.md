# Phase 5 — Folder Organization Design

**Date:** 2026-04-08
**Feature:** Folder map, recommended structure, safe migration assistant, rollback
**Status:** Approved

---

## Overview

Phase 5 adds a dedicated **Organize** tab to the Seshat dashboard. It shows where all registered projects currently live on disk, suggests a clean folder hierarchy based on project tags, and provides a safe migration assistant that moves projects with registry updates, full Git verification, and a health check. Every move is logged permanently and can be rolled back.

---

## Architecture

**Approach:** Single new `organizer.py` module + routes added to `seshat.py` under an `# ── Organize ──` block. Follows the existing pattern (registry.py, runner.py, vault.py, deps.py, scanner.py) exactly. No new patterns introduced.

**New files:**
- `organizer.py` — all folder-organization business logic

**Modified files:**
- `seshat.py` — six new API routes
- `templates/index.html` — Organize tab HTML
- `static/app.js` — Organize tab JS
- `static/style.css` — Organize tab styles

**New data file:**
- `~/.seshat/moves.log` — YAML file recording full move history

---

## Data Model

`~/.seshat/moves.log` — YAML, appended on each migration:

```yaml
moves:
  - id: "20260408-143022-vault"
    project: VAULT
    from: ~/Desktop/vault
    to: ~/Projects/infrastructure/vault
    timestamp: "2026-04-08T14:30:22"
    git_verified: true
    health_verified: true
    rolled_back: false
```

Each record has a stable `id` (timestamp + project slug) for targeted rollback. The `rolled_back` flag marks undone moves — records are never deleted (full history). Rolled-back records are visually dimmed in the UI.

---

## `organizer.py` Module

### Folder map

```python
folder_map() -> list[dict]
```
Iterates registered projects, expands each `directory` path, groups projects by parent directory. Returns `[{parent, projects[]}]` sorted by parent path. Read-only — no filesystem writes.

### Recommendations

```python
recommend_structure(projects, root="~/Projects") -> list[dict]
```
Derives a suggested path for each project from its first matching tag using a fixed mapping:

```python
TAG_DIRS = {
    "infrastructure": "infrastructure",
    "games":          "games",
    "creative":       "creative",
    "civic":          "civic",
    "rag":            "infrastructure",
}
```

Projects with no matching tag go to `~/Projects/misc/`. The `slug` is the project name lowercased with spaces replaced by hyphens. Returns `[{project_name, current, suggested, slug}]`. The user can override `suggested` in the UI before confirming a move.

### Migration

```python
migrate(project_name, destination, registry, force=False) -> dict
```

Execution order:
1. Expand and validate `destination` (parent must exist; destination must not already exist)
2. If project is running and `force` is False: return `{"warning": "project_running"}` — no move performed
3. `shutil.move(current, destination)`
4. Update registry `directory` via `registry.update()`
5. **Git verification:**
   - `.git` directory present at new location
   - `git status` exits without error
   - `git remote -v` shows at least one remote
   - `git fetch --dry-run` confirms remote is reachable
6. **Health check:** verify start command prerequisites exist:
   - `package.json` for `npm`-based commands
   - `requirements.txt` or `pyproject.toml` for Python commands
   - `Cargo.toml` for Rust/Cargo commands
   - (passes silently for unknown command types)
7. Append record to `moves.log`
8. Return `{ok, move_id, git_result, health_result}`

**Failure handling:** If step 3 (`shutil.move`) fails, nothing is written to registry or log — clean failure. If steps 4–6 fail after the move has completed, the record is still written with `git_verified: false` / `health_verified: false` so the user can roll back.

### Rollback

```python
rollback(move_id, registry) -> dict
```

1. Find record by `id` in `moves.log`
2. `shutil.move(to, from)` — move folder back
3. Update registry `directory` to the original path
4. Run Git verification at restored location
5. Mark `rolled_back: true` in the log (record is preserved)
6. Return `{ok, git_result}`

---

## API Routes

All routes under `/api/organize/`:

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/organize/map` | Folder map — projects grouped by parent directory |
| `GET` | `/api/organize/recommendations` | Suggested destinations for all projects. Accepts `?root=~/Projects` |
| `POST` | `/api/organize/migrate` | Run a migration. Body: `{project, destination, force}` |
| `GET` | `/api/organize/history` | Full move history, newest first |
| `POST` | `/api/organize/rollback` | Roll back a specific move. Body: `{move_id}` |
| `GET` | `/api/organize/history/<project_name>` | Move history for a single project |

**Running-project flow:** `POST /api/organize/migrate` with `force: false` returns `{"warning": "project_running"}` without moving anything. The UI prompts the user to confirm, then re-sends with `force: true`.

---

## UI Design

### Tab placement

"Organize" is a peer tab alongside All Projects, Running, Stopped, Conflicts & Orphans, and tag-filter tabs. Built in vanilla JS consistent with the existing `static/app.js` pattern.

### Section 1: Folder Map

Read-only. Projects grouped by parent directory — each group shows the parent path as a header, with project rows beneath (name, port, tags). Makes filesystem scatter immediately visible.

### Section 2: Recommended Structure

- **Root folder** input at top (default `~/Projects`) — changing it recalculates all suggestions
- Table columns: **Project** | **Current Location** | **Suggested Location** (editable inline) | **Status**
- Status values: unmoved / moved / running (amber warning icon)
- **Move** button per row; **Move All** button: first collects any running-project warnings and shows one consolidated confirmation ("N projects are currently running — move them anyway?"), then runs all pending migrations in sequence, stops on first hard failure (permission error, destination conflict, etc.)
- Running-project confirmation: *"VAULT is currently running. Moving it won't affect the running process, but the next start will use the new location. Continue?"* — Cancel and Move Anyway buttons

### Section 3: Move History

Table columns: **Project** | **From** | **To** | **Date** | **Git** | **Health** | **Status**

- Status: moved / rolled back
- **Roll Back** button per row (disabled if already rolled back)
- Rolled-back rows visually dimmed

---

## Error Handling

| Scenario | Behavior |
|----------|----------|
| `shutil.move` fails (permissions, disk full) | Return error; registry and log unchanged |
| Destination already exists | Reject before move; return descriptive error |
| Parent of destination doesn't exist | Reject before move; return descriptive error |
| Git not initialized in project | `git_verified: false`; migration proceeds; user can roll back |
| No internet / remote unreachable | `git_verified: false`; migration proceeds; user can roll back |
| Project is running (`force: false`) | Return warning; no move performed |
| Project is running (`force: true`) | Move proceeds; running process unaffected until next start |

---

## Out of Scope

- Moving projects to a network drive or external volume (no special handling)
- Merging two projects into one folder
- Renaming projects (only directory moves)
- Automatic reorganization without user approval
