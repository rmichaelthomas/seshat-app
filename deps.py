"""
deps.py — dependency health checks for Seshat.

Supported dependency types / providers:
  tunnel   → ngrok, cloudflare
  database → supabase, postgres, postgresql, mysql
  api      → any URL (generic HTTP check)
  hosting  → any URL (generic HTTP check)

Each check returns a status dict:
  {status: "connected"|"disconnected"|"unknown", detail: str, public_url: str}

Results are cached per-project for CACHE_TTL seconds. Background checks are
kicked off asynchronously so they never block the main request thread.
"""

import json
import socket
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

CACHE_TTL = 30   # seconds between re-checks

_cache: dict[str, tuple[float, list]] = {}   # {name: (timestamp, results)}
_lock  = threading.Lock()


# ── Cache API ──────────────────────────────────────────────────────────────


def get_cached(project_name: str) -> list | None:
    """Return cached dep results if still fresh, else None."""
    with _lock:
        entry = _cache.get(project_name)
        if entry and time.time() - entry[0] < CACHE_TTL:
            return entry[1]
    return None


def store(project_name: str, results: list) -> None:
    with _lock:
        _cache[project_name] = (time.time(), results)


def invalidate(project_name: str) -> None:
    with _lock:
        _cache.pop(project_name, None)


# ── Public check interface ─────────────────────────────────────────────────


def check_all(project_name: str, deps: list) -> list:
    """Check all deps synchronously and update the cache. Returns results."""
    results = [_check_one(dep) for dep in deps]
    store(project_name, results)
    return results


def check_all_async(project_name: str, deps: list) -> None:
    """Kick off dep checks in a daemon thread (non-blocking)."""
    threading.Thread(
        target=check_all,
        args=(project_name, deps),
        daemon=True,
    ).start()


# ── Dispatcher ─────────────────────────────────────────────────────────────


def _check_one(dep: dict) -> dict:
    """Dispatch a single dependency to the right checker."""
    dep_type = dep.get("type", "").lower()
    provider = dep.get("provider", "").lower()
    url      = dep.get("url", "")

    try:
        if dep_type == "tunnel":
            if provider in ("ngrok", "ngrok-stable", "ngrok-edge"):
                result = _check_ngrok()
            elif provider in ("cloudflare", "cloudflared"):
                result = _check_cloudflare()
            else:
                result = _check_http(url) if url else _unknown("No URL configured")

        elif dep_type == "database":
            if provider == "supabase":
                result = _check_supabase(url)
            elif provider in ("postgres", "postgresql"):
                result = _check_postgres(url)
            elif provider == "mysql":
                result = _check_tcp_from_url(url, 3306)
            else:
                result = _check_http(url) if url else _unknown("No URL configured")

        elif dep_type in ("api", "hosting"):
            result = _check_http(url) if url else _unknown("No URL configured for this dependency")

        else:
            result = _unknown(f"Unsupported dependency type: '{dep_type}'")

    except Exception as e:
        result = _disconnected(str(e))

    return {**dep, **result}


# ── Tunnel checkers ────────────────────────────────────────────────────────


def _check_ngrok() -> dict:
    """Query ngrok's local admin API at localhost:4040."""
    try:
        req = Request("http://localhost:4040/api/tunnels",
                      headers={"User-Agent": "Seshat/1.0"})
        with urlopen(req, timeout=2) as resp:
            data    = json.loads(resp.read())
            tunnels = data.get("tunnels", [])

        if not tunnels:
            return _disconnected("ngrok is running but no tunnels are open")

        # Prefer HTTPS tunnel if both exist
        https_tunnels = [t for t in tunnels if t.get("public_url", "").startswith("https")]
        best = https_tunnels[0] if https_tunnels else tunnels[0]
        public_url = best.get("public_url", "")
        name       = best.get("name", "")
        proto      = best.get("proto", "")
        detail     = f"{proto} tunnel — {public_url}" if public_url else "tunnel active"
        return _connected(detail, public_url)

    except (ConnectionRefusedError, OSError):
        return _disconnected("ngrok not running (no response on localhost:4040)")
    except Exception as e:
        return _disconnected(f"ngrok check failed: {e}")


def _check_cloudflare() -> dict:
    """Check if a cloudflared process is running."""
    try:
        import psutil
        for proc in psutil.process_iter(["name", "cmdline"]):
            try:
                if "cloudflared" in (proc.info["name"] or "").lower():
                    return _connected("cloudflared process is running")
                if proc.info["cmdline"] and any(
                    "cloudflared" in arg for arg in proc.info["cmdline"]
                ):
                    return _connected("cloudflared process is running")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return _disconnected("cloudflared process not found")
    except ImportError:
        return _unknown("psutil not available for process check")
    except Exception as e:
        return _unknown(f"Could not check cloudflared: {e}")


# ── Database checkers ──────────────────────────────────────────────────────


def _check_supabase(url: str) -> dict:
    """
    Check Supabase reachability via its REST health endpoint.
    Any HTTP response (including auth errors) means the server is up.
    """
    if not url:
        return _unknown("No Supabase URL configured (add SUPABASE_URL to vault)")

    base = url.rstrip("/")
    try:
        req = Request(
            f"{base}/rest/v1/",
            headers={"apikey": "seshat-health-check", "User-Agent": "Seshat/1.0"},
        )
        with urlopen(req, timeout=4) as resp:
            return _connected(f"Supabase reachable (HTTP {resp.status})")
    except HTTPError as e:
        # 4xx = server responded (it's up), auth/permission error is expected
        if 400 <= e.code < 500:
            return _connected(f"Supabase reachable (HTTP {e.code})")
        return _disconnected(f"Supabase returned HTTP {e.code}")
    except URLError as e:
        reason = str(e.reason) if hasattr(e, "reason") else str(e)
        return _disconnected(f"Supabase unreachable — {reason}")
    except Exception as e:
        return _disconnected(str(e))


def _check_postgres(url: str) -> dict:
    """Check Postgres reachability via TCP."""
    if not url:
        return _unknown("No Postgres URL configured")
    return _check_tcp_from_url(url, default_port=5432)


def _check_tcp_from_url(url: str, default_port: int) -> dict:
    """Parse a DB URL and check TCP connectivity to host:port."""
    try:
        parsed = urlparse(url)
        host   = parsed.hostname or "localhost"
        port   = parsed.port or default_port
        return _check_tcp(host, port)
    except Exception as e:
        return _disconnected(f"Could not parse URL: {e}")


# ── Generic checkers ───────────────────────────────────────────────────────


def _check_http(url: str, timeout: int = 4) -> dict:
    """
    Generic HTTP reachability check.
    2xx and 4xx responses all mean the server is up.
    """
    if not url:
        return _unknown("No URL configured")

    try:
        req = Request(url, headers={"User-Agent": "Seshat/1.0"}, method="HEAD")
        with urlopen(req, timeout=timeout) as resp:
            return _connected(f"Reachable (HTTP {resp.status})", url)
    except HTTPError as e:
        if 400 <= e.code < 500:
            return _connected(f"Reachable (HTTP {e.code})", url)
        return _disconnected(f"HTTP {e.code} from {url}")
    except URLError as e:
        reason = str(e.reason) if hasattr(e, "reason") else str(e)
        return _disconnected(f"Unreachable — {reason}")
    except Exception as e:
        return _disconnected(str(e))


def _check_tcp(host: str, port: int, timeout: int = 3) -> dict:
    """Check that a TCP port is accepting connections."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return _connected(f"{host}:{port} reachable")
    except socket.timeout:
        return _disconnected(f"{host}:{port} — connection timed out")
    except ConnectionRefusedError:
        return _disconnected(f"{host}:{port} — connection refused")
    except OSError as e:
        return _disconnected(f"{host}:{port} — {e}")


# ── Status constructors ────────────────────────────────────────────────────


def _connected(detail: str = "", public_url: str = "") -> dict:
    return {"status": "connected",    "detail": detail, "public_url": public_url}


def _disconnected(detail: str = "") -> dict:
    return {"status": "disconnected", "detail": detail, "public_url": ""}


def _unknown(detail: str = "") -> dict:
    return {"status": "unknown",      "detail": detail, "public_url": ""}
