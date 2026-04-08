"""
vault.py — encrypted secrets store for Seshat.

Architecture:
  - A random Fernet symmetric key is generated once and stored in macOS
    Keychain via the `keyring` library (Touch ID compatible).
  - The vault data (JSON) is encrypted with that key and stored at
    ~/.seshat/vault.enc.
  - If `keyring` or `cryptography` are unavailable, the vault falls back
    to unencrypted JSON at ~/.seshat/vault.json with a runtime warning.

Vault data structure (decrypted):
  {
    "shared": {
      "ANTHROPIC_API_KEY": "sk-ant-...",
      "SUPABASE_URL":      "https://xxxx.supabase.co"
    },
    "overrides": {
      "SLAPS Prototype": {
        "SUPABASE_URL": "https://yyyy-dev.supabase.co"
      }
    }
  }
"""

import json
from pathlib import Path

SESHAT_DIR    = Path.home() / ".seshat"
VAULT_ENC     = SESHAT_DIR / "vault.enc"
VAULT_PLAIN   = SESHAT_DIR / "vault.json"   # fallback (unencrypted)
SERVICE_NAME  = "seshat"
KEY_ITEM      = "vault_encryption_key"

_EMPTY: dict = {"shared": {}, "overrides": {}}

# ── Optional deps (graceful fallback if not installed) ─────────────────────

try:
    import keyring
    from cryptography.fernet import Fernet, InvalidToken
    _CRYPTO_OK = True
except ImportError:
    _CRYPTO_OK = False


class Vault:
    """Encrypted key-value store for environment secrets."""

    def __init__(self):
        SESHAT_DIR.mkdir(exist_ok=True)

    # ── Encryption helpers ─────────────────────────────────────────────────

    @property
    def encrypted(self) -> bool:
        return _CRYPTO_OK

    def _fernet(self):
        """Return a Fernet instance backed by the Keychain-stored key."""
        raw = keyring.get_password(SERVICE_NAME, KEY_ITEM)
        if not raw:
            key = Fernet.generate_key()
            keyring.set_password(SERVICE_NAME, KEY_ITEM, key.decode())
            raw = key.decode()
        return Fernet(raw.encode())

    # ── Load / save ────────────────────────────────────────────────────────

    def _load(self) -> dict:
        if _CRYPTO_OK and VAULT_ENC.exists():
            try:
                return json.loads(self._fernet().decrypt(VAULT_ENC.read_bytes()))
            except (InvalidToken, Exception):
                pass   # corrupted or wrong key — return empty

        if VAULT_PLAIN.exists():
            try:
                return json.loads(VAULT_PLAIN.read_text())
            except Exception:
                pass

        return dict(_EMPTY)

    def _save(self, data: dict) -> None:
        payload = json.dumps(data, indent=2).encode()
        if _CRYPTO_OK:
            VAULT_ENC.write_bytes(self._fernet().encrypt(payload))
            VAULT_ENC.chmod(0o600)
            # Remove plaintext file if it exists
            if VAULT_PLAIN.exists():
                VAULT_PLAIN.unlink()
        else:
            VAULT_PLAIN.write_text(payload.decode())
            VAULT_PLAIN.chmod(0o600)

    # ── Shared keys ────────────────────────────────────────────────────────

    def list_keys(self) -> list[str]:
        return sorted(self._load().get("shared", {}).keys())

    def get(self, key: str) -> str | None:
        return self._load().get("shared", {}).get(key)

    def set(self, key: str, value: str) -> None:
        data = self._load()
        data.setdefault("shared", {})[key] = value
        self._save(data)

    def delete(self, key: str) -> None:
        data = self._load()
        data.setdefault("shared", {}).pop(key, None)
        # Remove from any project overrides too
        for overrides in data.get("overrides", {}).values():
            overrides.pop(key, None)
        self._save(data)

    # ── Per-project overrides ──────────────────────────────────────────────

    def get_overrides(self, project: str) -> dict:
        return self._load().get("overrides", {}).get(project, {})

    def set_override(self, project: str, key: str, value: str) -> None:
        data = self._load()
        data.setdefault("overrides", {}).setdefault(project, {})[key] = value
        self._save(data)

    def delete_override(self, project: str, key: str) -> None:
        data = self._load()
        if project in data.get("overrides", {}):
            data["overrides"][project].pop(key, None)
            if not data["overrides"][project]:
                del data["overrides"][project]
        self._save(data)

    def clear_project(self, project: str) -> None:
        """Remove all overrides for a deleted project."""
        data = self._load()
        data.setdefault("overrides", {}).pop(project, None)
        self._save(data)

    # ── Resolution (used by runner at start time) ──────────────────────────

    def resolve_for_project(self, project_name: str, env_keys: list[str]) -> dict:
        """
        Return {KEY: value} for each key in env_keys, resolving:
        project-specific override > shared value > (omit if missing).
        """
        data      = self._load()
        shared    = data.get("shared", {})
        overrides = data.get("overrides", {}).get(project_name, {})

        result = {}
        for key in env_keys:
            if key in overrides:
                result[key] = overrides[key]
            elif key in shared:
                result[key] = shared[key]
        return result

    # ── Audit ──────────────────────────────────────────────────────────────

    def audit(self, projects: list[dict]) -> list[dict]:
        """
        Cross-reference vault contents against project env declarations.
        Returns one entry per key (both declared and in vault).
        """
        data   = self._load()
        shared = data.get("shared", {})
        all_overrides = data.get("overrides", {})

        # Map key → projects that declare it
        key_projects: dict[str, list[str]] = {}
        for p in projects:
            for key in p.get("env", []):
                key_projects.setdefault(key, []).append(p["name"])

        # Include vault keys that no project declares
        for key in shared:
            key_projects.setdefault(key, [])

        result = []
        for key in sorted(key_projects):
            declared_by   = key_projects[key]
            in_shared     = key in shared
            overridden_by = [
                proj for proj in declared_by
                if key in all_overrides.get(proj, {})
            ]
            missing_from = [
                proj for proj in declared_by
                if not (in_shared or key in all_overrides.get(proj, {}))
            ]
            result.append({
                "key":           key,
                "in_shared":     in_shared,
                "declared_by":   declared_by,
                "overridden_by": overridden_by,
                "missing_from":  missing_from,
                "unused":        len(declared_by) == 0,
            })

        return result

    # ── Import from .env ───────────────────────────────────────────────────

    def import_dotenv(self, content: str, project: str | None = None) -> dict[str, str]:
        """
        Parse .env file content and store into shared vault or project overrides.
        Returns {key: value} of everything that was imported.
        """
        imported: dict[str, str] = {}
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key   = key.strip()
            value = value.strip()
            # Strip surrounding quotes
            if len(value) >= 2 and value[0] in ('"', "'") and value[0] == value[-1]:
                value = value[1:-1]
            if key:
                imported[key] = value

        if project:
            for k, v in imported.items():
                self.set_override(project, k, v)
        else:
            for k, v in imported.items():
                self.set(k, v)

        return imported

    # ── Summary (non-sensitive) ────────────────────────────────────────────

    def summary(self) -> dict:
        """Return vault metadata without any secret values."""
        data      = self._load()
        shared    = data.get("shared", {})
        overrides = data.get("overrides", {})
        return {
            "encrypted":        _CRYPTO_OK,
            "key_count":        len(shared),
            "keys":             sorted(shared.keys()),
            "project_overrides": {
                proj: sorted(vals.keys())
                for proj, vals in overrides.items()
            },
        }
