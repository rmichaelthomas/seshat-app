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
