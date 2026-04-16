# Seshat

A local project registry and process manager for developers running multiple services on one machine. Seshat gives every project a permanent home: a port, a name, an encrypted secrets vault, a folder, and a human-readable `.seshat` hostname — all accessible from a single dashboard at `http://localhost:9000`.

## What it does

- **Registry** — register projects by name, port, and directory. Seshat remembers them across reboots.
- **Process manager** — start and stop projects from the dashboard without touching a terminal. Projects requiring multiple processes (e.g. an API server + frontend) are launched together as a single unit.
- **GitHub import** — scan your GitHub repos and import them with port, start command, and tags pre-filled from the README.
- **Local discovery** — scan a directory for local projects and register them in one click.
- **Port scanner** — detects what's actually running on each registered port and shows live status.
- **Groups** — organize projects into named groups (e.g. "Backend", "Tools").
- **Log viewer** — tail live output and catch errors without opening a separate terminal window.
- **Encrypted secrets vault** — store API keys, tokens, and credentials per project, encrypted with a master password backed by macOS Keychain.
- **Folder organizer** — move projects to recommended directories, preview changes, and roll back if anything goes wrong.
- **Local hostnames** — every project gets a `.seshat` address (e.g. `my-api.seshat`) via Caddy + dnsmasq, so you can stop memorizing port numbers.

## Requirements

- macOS
- Python 3.10+
- [Caddy](https://caddyserver.com/docs/install) (`brew install caddy`) — for the `.seshat` reverse proxy
- [dnsmasq](https://formulae.brew.sh/formula/dnsmasq) (`brew install dnsmasq`) — for wildcard DNS resolution of `*.seshat`

## Setup

```bash
git clone https://github.com/rmichaelthomas/seshat.git
cd seshat
pip install -r requirements.txt
python seshat.py
```

Then open `http://localhost:9000`.

### Local hostnames (optional)

To enable `.seshat` addresses, click **Set Up** in the yellow banner on the dashboard. The guided wizard will:

1. Confirm Caddy is installed
2. Confirm dnsmasq is installed
3. Add `address=/.seshat/127.0.0.1` to your dnsmasq config automatically
4. Walk you through adding a macOS resolver file (one `sudo` command, shown in the UI)

After setup, every registered project is reachable at `http://<project-name>.seshat` automatically.

## Project structure

| File | Purpose |
|---|---|
| `seshat.py` | Flask app, API routes |
| `registry.py` | Project registry (YAML + runtime state) |
| `runner.py` | Process start/stop and log capture |
| `scanner.py` | Port scanner using psutil |
| `vault.py` | Encrypted secrets vault |
| `organizer.py` | Folder move, health checks, rollback |
| `router.py` | Caddy + dnsmasq management, `.seshat` hostnames |
| `github.py` | GitHub API client, README metadata extraction |
| `local_scanner.py` | Local directory project discovery |
| `deps.py` | Dependency detection |
| `templates/` | Dashboard HTML |
| `static/` | CSS and JavaScript |
| `tests/` | pytest test suite |

## Configuration

Seshat stores its data in `~/.seshat/`:

| File | Contents |
|---|---|
| `registry.yaml` | Registered projects |
| `state.json` | Runtime PIDs for managed processes |
| `groups.yaml` | Group assignments |
| `hostnames.yaml` | Custom hostname overrides |
| `Caddyfile` | Generated reverse proxy config |
| `vault/` | Encrypted secrets per project |

## Running tests

```bash
python3 -m pytest tests/
```
