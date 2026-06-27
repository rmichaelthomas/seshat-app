# Seshat

Local environmental agent harness for developers running multiple services on one machine.

Seshat makes your local environment legible: what's running, what's broken, what an agent touched, and what it's allowed to touch. It exposes that knowledge through a dashboard at `http://localhost:9000` and an MCP server that AI coding agents (Claude Code, Cursor, Windsurf) can query before they act.

The name comes from the Egyptian goddess of writing, measurement, and record-keeping. Seshat measured the foundation before anything was built.

---

## What it does

**Registry and process management**

Register projects by name, port, and directory. Seshat remembers them across reboots. Start and stop services from the dashboard. Projects with multiple processes launch as a single unit.

**MCP server**

Exposes your full local environment to AI coding agents: registered projects, port assignments, running processes, vault keys (resolved, never raw), dependency state, and agent session history. Agents can query the machine before they act on it.

**Machine-action Receipts**

Every action an AI agent takes through Seshat is recorded as a Receipt: what ran, what changed, which agent session initiated it. Attribution is tracked at the process level, not inferred after the fact.

**Encrypted secrets vault**

API keys, tokens, and credentials stored per project, encrypted with a master password backed by macOS Keychain. Agents get resolved values for the keys they're permitted to access. They cannot enumerate the vault.

**Port scanner and conflict detection**

Live scan of what's actually running on each registered port. Conflicts surface immediately. Processes Seshat didn't start are identified and shown separately.

**Local hostnames**

Every registered project gets a `.seshat` address (`my-api.seshat`) via Caddy and dnsmasq. Stop memorizing port numbers.

**Supporting tools**

- GitHub import: scan your repos and import with port, start command, and tags pre-filled from the README
- Local discovery: scan a directory for projects and register them in one click
- Log viewer: tail live output without opening a separate terminal
- Groups: organize projects into named groups
- Folder organizer: move projects to recommended directories, preview changes, roll back if anything goes wrong

---

## Requirements

- macOS
- Python 3.10+
- [Caddy](https://caddyserver.com/docs/install) (`brew install caddy`) for the `.seshat` reverse proxy
- [dnsmasq](https://formulae.brew.sh/formula/dnsmasq) (`brew install dnsmasq`) for wildcard DNS resolution of `*.seshat`

---

## Setup

```bash
git clone https://github.com/rmichaelthomas/seshat-app.git
cd seshat-app
pip3 install -r requirements.txt
python3 seshat.py
```

Open `http://localhost:9000`.

### Local hostnames

To enable `.seshat` addresses, click **Set Up** in the banner on the dashboard. The wizard will:

1. Confirm Caddy is installed
2. Confirm dnsmasq is installed
3. Add `address=/.seshat/127.0.0.1` to your dnsmasq config
4. Walk through adding a macOS resolver file (one `sudo` command, shown in the UI)

After setup, every registered project is reachable at `http://<project-name>.seshat`.

### MCP server

Seshat runs an MCP server alongside the dashboard. To connect it to Claude Code:

```bash
# Add to your Claude Code MCP config
seshat mcp
```

Once connected, Claude Code can query your local environment before running commands, check port availability, resolve vault keys, and record Receipts for actions it takes.

---

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
| `mcp_server.py` | MCP server for agent access |
| `receipts.py` | Machine-action Receipt recording and retrieval |
| `templates/` | Dashboard HTML |
| `static/` | CSS and JavaScript |
| `tests/` | pytest test suite |

---

## Data

Seshat stores everything in `~/.seshat/`:

| File | Contents |
|---|---|
| `registry.yaml` | Registered projects |
| `state.json` | Runtime PIDs for managed processes |
| `groups.yaml` | Group assignments |
| `hostnames.yaml` | Custom hostname overrides |
| `Caddyfile` | Generated reverse proxy config |
| `vault/` | Encrypted secrets per project |
| `receipts/` | Machine-action Receipt log |

---

## Tests

```bash
python3 -m pytest tests/
```
