# tests/conftest.py
import pytest

import agreements
import receipts as receipts_mod


@pytest.fixture(autouse=True)
def _test_mac_key(monkeypatch):
    """Isolate every test from the real macOS Keychain for the receipt MAC
    key (F-01), mirroring _no_revocations_by_default/_no_invariant_by_default
    below. Without this, every emit() call in the whole test suite would
    read/write the developer's real Keychain entry — slow, environment-
    dependent, and it pollutes real Keychain state with test key material."""
    monkeypatch.setattr(receipts_mod, "_mac_key", lambda: b"test-only-mac-key-not-for-real-use")


@pytest.fixture(autouse=True)
def _no_revocations_by_default(monkeypatch):
    """Isolate every test from whatever may actually exist at
    ~/.seshat/revocations.limn on the host machine. check_action() loads
    revocations independently of the agreement_text override, so without
    this, tests would silently depend on host state. Tests that need
    specific revocations content override load_revocations explicitly in
    the test body, which runs after fixture setup and so takes precedence.
    """
    monkeypatch.setattr(agreements, "load_revocations", lambda: None)


@pytest.fixture(autouse=True)
def _no_invariant_by_default(monkeypatch):
    """Isolate every test from whatever may actually exist at
    ~/.seshat/invariant.limn on the host machine, mirroring
    _no_revocations_by_default above. Tests that need specific Invariant
    contract content override load_invariant explicitly in the test body."""
    monkeypatch.setattr(agreements, "load_invariant", lambda: None)


@pytest.fixture(autouse=True)
def _no_teams_by_default(monkeypatch):
    """Isolate every test from whatever may actually exist at
    ~/.seshat/teams.limn on the host machine, mirroring
    _no_revocations_by_default above. check_action() resolves teams
    independently of the agreement_text override, so without this every
    check_action call in the suite would read the developer's real teams
    file. Tests that need specific teams content override load_teams
    explicitly in the test body."""
    monkeypatch.setattr(agreements, "load_teams", lambda: None)


@pytest.fixture(autouse=True)
def _test_identity_root_key(monkeypatch):
    """Isolate every test from the real macOS Keychain for the identity
    root key, mirroring _test_mac_key above. Without this, mint()/verify()
    calls in the test suite would read/write the developer's real Keychain
    entry. Legacy HS256-macaroon path only (§7)."""
    import identity as identity_mod
    monkeypatch.setattr(
        identity_mod, "_root_key", lambda: b"test-only-identity-root-key-not-for-real-use"
    )


@pytest.fixture(autouse=True)
def _test_identity_root_signing_key(monkeypatch):
    """Isolate every test from the real macOS Keychain for the Ed25519 root
    signing key (ID-Q4 Phase 1), mirroring _test_identity_root_key above.
    A single fixed keypair for the whole run — not per-test-random — so a
    token minted in one test still verifies if checked from another, the
    same way the real Keychain-backed key would be stable across calls.
    Patches both accessors directly (never real keyring get/set) so a test
    that breaks _root_signing_key specifically (simulating "the private
    key is unavailable") leaves _root_public_key work — this is exactly
    what proves verification needs only the public half (§10 benchmark 6)."""
    import identity as identity_mod
    from cryptography.hazmat.primitives.asymmetric import ed25519

    fixed_private = ed25519.Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    fixed_public = fixed_private.public_key()
    monkeypatch.setattr(identity_mod, "_root_signing_key", lambda: fixed_private)
    monkeypatch.setattr(identity_mod, "_root_public_key", lambda: fixed_public)
