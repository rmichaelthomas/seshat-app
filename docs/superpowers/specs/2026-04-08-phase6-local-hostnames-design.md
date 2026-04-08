# Phase 6 — Local Hostnames Design

**Date:** 2026-04-08
**Feature:** Human-readable local addresses via `.seshat` TLD
**Status:** Approved

---

## Overview

Phase 6 gives every registered Seshat project a friendly local hostname (e.g., `vault.seshat` instead of `localhost:5001`). This serves two purposes: typing a friendly name in the browser instead of memorising port numbers, and stable inter-service addressing so projects can reference each other by name rather than hardcoded ports. Vault entries can store `http://vault.seshat` instead of `http://localhost:5001` — port changes stop breaking anything.

The stack: **dnsmasq** resolves all `*.seshat` names to `127.0.0.1`; **Caddy** reverse-proxies each hostname to the correct `localhost:PORT`. Seshat owns and regenerates both config files whenever projects are added, removed, or renamed.

`.seshat` is chosen deliberately over `.local` — macOS reserves `.local` for Bonjour/mDNS and causes slow DNS lookups for custom entries.

---

## Architecture

**New file:**
- `router.py` — all routing business logic (`Router` class)

**Modified files:**
- `seshat.py` — seven new API routes under `# ── Router ──` block; also calls `router._reload_caddy()` after any project registration or deletion so the Caddyfile stays current automatically
- `templates/index.html` — hostname chip on shelf row, hostname field in detail view, setup banner, setup modal
- `static/app.js` — hostname display/edit/setup logic, vault hint
- `static/style.css` — hostname chip and setup modal styles

**Managed config files** (Seshat owns, never edited by hand):
- `~/.seshat/hostnames.yaml` — project → hostname mapping
- `~/.seshat/Caddyfile` — generated reverse proxy config, rewritten on any change

**One-time system files** (written during setup, never touched again):
- `/usr/local/etc/dnsmasq.conf` — Seshat appends `address=/.seshat/127.0.0.1`
- `/etc/resolver/seshat` — tells macOS to use dnsmasq for `.seshat` lookups; requires `sudo`

---

## Data Model

`~/.seshat/hostnames.yaml`:

```yaml
hostnames:
  VAULT: vault.seshat
  MY-API: my-api.seshat
```

Keys are project names exactly as stored in the registry. Values are the full hostname including `.seshat` suffix. Missing entries fall back to the auto-generated slug at runtime but are not persisted until the user explicitly sets or saves them.

**Auto-generated slug rule:** project name lowercased; spaces and underscores replaced with hyphens. `"My Vault"` → `my-vault.seshat`. Stored on first explicit save or override.

**Caddyfile format** (fully regenerated on every change):

```
vault.seshat {
    reverse_proxy localhost:5001
}

my-api.seshat {
    reverse_proxy localhost:3000
}
```

All registered projects with a known port appear. Projects with no port configured are omitted.

---

## `router.py` Module

### `Router` class

```python
Router(registry)
```

| Method | Description |
|--------|-------------|
| `setup_status() -> dict` | Returns `{caddy_installed, dnsmasq_installed, caddy_running, dnsmasq_running, resolver_configured, caddyfile_exists}` |
| `configure_dnsmasq() -> dict` | Appends `address=/.seshat/127.0.0.1` to dnsmasq config (idempotent), restarts service via `brew services restart dnsmasq` |
| `start_caddy() -> dict` | Generates initial Caddyfile from all registered projects, runs `caddy start --config ~/.seshat/Caddyfile` |
| `all_hostnames() -> list[dict]` | Returns `[{project_name, hostname, port}]` for all registered projects; auto-generates missing slugs on the fly without persisting |
| `set_hostname(project_name, hostname) -> dict` | Validates hostname, saves to `hostnames.yaml`, regenerates and reloads Caddyfile |
| `reset_hostname(project_name) -> dict` | Removes override from `hostnames.yaml`, reverts to auto-generated slug, regenerates and reloads Caddyfile |
| `_generate_caddyfile() -> str` | Builds full Caddyfile string from all registered projects and their hostnames |
| `_reload_caddy() -> dict` | Writes Caddyfile, runs `caddy reload --config ~/.seshat/Caddyfile` (or `caddy start` if not running). Returns `{ok, error}` |

### Hostname Validation Rules

- Must end in `.seshat`
- Subdomain part: lowercase alphanumeric and hyphens only; no leading or trailing hyphens
- Must be unique — rejected if already claimed by another project

### Idempotency

`configure_dnsmasq()` checks whether `address=/.seshat/127.0.0.1` is already present in the dnsmasq config before appending. Safe to call multiple times. `_reload_caddy()` uses `caddy reload` if Caddy is running, `caddy start` if not — no manual state tracking needed.

---

## Setup Wizard

A guided four-step modal walks the user through first-time configuration.

**Step 1 — Check Caddy:** Runs `which caddy`. If missing, shows `brew install caddy` in a copyable code block. User installs in their terminal, clicks **Check Again**. Passes automatically if found.

**Step 2 — Check dnsmasq:** Same pattern with `brew install dnsmasq`.

**Step 3 — Configure dnsmasq:** Fully automated. Seshat calls `POST /api/router/setup/dnsmasq`, which appends the wildcard rule and restarts dnsmasq. Shows spinner, then green check or error.

**Step 4 — Configure macOS resolver:** Requires `sudo`. Seshat displays the exact two commands to run in Terminal:
```
sudo mkdir -p /etc/resolver
sudo tee /etc/resolver/seshat <<< "nameserver 127.0.0.1"
```
Seshat polls `GET /api/router/status` until `resolver_configured` is `true`, then marks step green automatically.

Once all four steps are green, Seshat calls `POST /api/router/setup/caddy-start`, generates the initial Caddyfile, starts Caddy, and closes the modal. The setup banner disappears permanently.

**Re-entry:** On each dashboard load, `GET /api/router/status` is called. If `caddy_running` or `dnsmasq_running` is false, the banner reappears with a **Restart Services** button that calls the appropriate service restart — no need to redo the full wizard.

---

## API Routes

All routes under `/api/router/`:

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/router/status` | Setup status — all six flags from `setup_status()` |
| `POST` | `/api/router/setup/dnsmasq` | Configure dnsmasq (automated step 3) |
| `POST` | `/api/router/setup/caddy-start` | Generate initial Caddyfile and start Caddy |
| `GET` | `/api/router/hostnames` | All hostnames with project name and port |
| `PUT` | `/api/router/hostnames/<project>` | Set or override hostname. Body: `{hostname}` |
| `DELETE` | `/api/router/hostnames/<project>` | Reset hostname to auto-generated slug |
| `POST` | `/api/router/reload` | Regenerate Caddyfile and reload Caddy (manual trigger) |

---

## UI Design

### Setup Banner

A slim yellow bar at the very top of the dashboard (above the tab row), shown only when `setup_status()` reports any flag as false. Text: *"Local hostnames not configured — projects could be reachable at vault.seshat and friends."* with a **Set Up** link. Hidden once fully configured.

### Setup Modal

Four-step checklist. Each step shows a spinner while running and a green check or red error message when done. Manual steps (install Caddy, install dnsmasq) show a copyable `brew install` command and a **Check Again** button. Automated steps run on modal open and show their result immediately. A **Done** button appears once all steps are green.

### Shelf Row

Each project row gains a hostname chip between the port badge and the action buttons. Renders as a small pill showing `vault.seshat`. Clicking the chip opens `http://vault.seshat` in a new tab. If routing is not set up yet, the chip is greyed out and non-clickable.

### Inline Detail / Edit View

The project's expanded detail panel gains a **Local Address** field below the port field. It shows the current hostname with an **Edit** button. Clicking Edit replaces it with a text input pre-filled with the current value, plus **Save** and **Reset to default** buttons:
- **Save** — calls `PUT /api/router/hostnames/<project>`, updates chip and field on success
- **Reset to default** — calls `DELETE /api/router/hostnames/<project>`, reverts to auto-generated slug

### Vault Integration Hint

No changes to `vault.py`. When the project detail panel shows a vault entry whose value matches `http://localhost:<PORT>` and that port belongs to a project with a configured hostname, a hint appears beneath the value: *"You can also use http://vault.seshat"* with a one-click **Use hostname** button that updates the vault entry in place. This is display-only logic in `app.js` — no new backend routes required.

---

## Error Handling

| Scenario | Behavior |
|----------|----------|
| Caddy not installed | Setup wizard step 1 shows install instructions; hostname chips greyed out |
| dnsmasq not installed | Setup wizard step 2 shows install instructions |
| Caddy not running on dashboard load | Banner reappears with Restart Services button |
| `caddy reload` fails | Return error from `_reload_caddy()`; Caddyfile written but caddy left in previous state |
| Hostname already taken | `set_hostname()` returns `{error: "hostname_taken"}`; UI shows inline error |
| Invalid hostname format | `set_hostname()` returns `{error: "invalid_hostname"}`; UI shows inline error |
| Project has no port | Omitted from Caddyfile; hostname chip not shown on shelf row |

---

## Out of Scope

- HTTPS / TLS certificates for `.seshat` domains
- Multiple hostnames per project
- Subpath routing (e.g., `local.seshat/vault`)
- Windows or Linux support (macOS only for dnsmasq + `/etc/resolver`)
- Hostname persistence across registry deletions (removing a project removes its hostname entry)
