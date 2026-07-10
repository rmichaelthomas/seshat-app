# tests/test_platform_client.py
"""Tests for the TI-Q4 platform-query client (seshat_tui/platform_client.py)
and its wiring into SeshatApp's best-effort verdict fetch (§A.5/A.6).

Read-only, offline-additive: every failure mode (no key, no hashes, network
error, non-200) must degrade to an empty map, never raise past the caller.
"""
from __future__ import annotations

import httpx
import pytest

from seshat_tui import platform_client
from seshat_tui.app import SeshatApp


class _FakeVault:
    def __init__(self, key: str | None) -> None:
        self._key = key

    def get(self, name: str) -> str | None:
        return self._key


def test_fetch_sentinel_verdicts_posts_hashes_and_returns_map(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return httpx.Response(
            200,
            json={"verdicts": {"a" * 64: {"verdict": "holding"}}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    result = platform_client.fetch_sentinel_verdicts("https://example.test", "key123", ["a" * 64])
    assert result == {"a" * 64: {"verdict": "holding"}}
    assert captured["url"] == "https://example.test/api/v1/sentinels/verdicts-by-agreement"
    assert captured["json"] == {"agreement_hashes": ["a" * 64]}
    assert captured["headers"]["Authorization"] == "Bearer key123"


def test_fetch_sentinel_verdicts_empty_hashes_short_circuits(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("should not make a network call with no hashes")

    monkeypatch.setattr(httpx, "post", fail_if_called)
    assert platform_client.fetch_sentinel_verdicts("https://example.test", "key", []) == {}


def test_fetch_sentinel_verdicts_raises_on_http_error(monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None):
        return httpx.Response(500, json={"detail": "boom"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(httpx.HTTPStatusError):
        platform_client.fetch_sentinel_verdicts("https://example.test", "key", ["a" * 64])


# ── SeshatApp._fetch_sentinel_verdicts — best-effort degrade (§A.5) ────────


def test_app_fetch_degrades_to_empty_with_no_api_key(monkeypatch):
    import seshat_tui.app as app_mod

    monkeypatch.setattr(app_mod, "_vault", _FakeVault(None))
    app = SeshatApp()
    result = app._fetch_sentinel_verdicts([{"agreement_hash": "a" * 64}])
    assert result == {}


def test_app_fetch_degrades_to_empty_with_no_agreement_hashes(monkeypatch):
    import seshat_tui.app as app_mod

    monkeypatch.setattr(app_mod, "_vault", _FakeVault("some-key"))

    def fail_if_called(*args, **kwargs):
        raise AssertionError("should not fetch when no receipt carries agreement_hash")

    monkeypatch.setattr(app_mod.platform_client, "fetch_sentinel_verdicts", fail_if_called)
    app = SeshatApp()
    result = app._fetch_sentinel_verdicts([{"action": "start_project"}])
    assert result == {}


def test_app_fetch_degrades_to_empty_on_network_failure(monkeypatch):
    import seshat_tui.app as app_mod

    monkeypatch.setattr(app_mod, "_vault", _FakeVault("some-key"))

    def raise_error(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(app_mod.platform_client, "fetch_sentinel_verdicts", raise_error)
    app = SeshatApp()
    result = app._fetch_sentinel_verdicts([{"agreement_hash": "a" * 64}])
    assert result == {}


def test_app_fetch_returns_verdicts_on_success(monkeypatch):
    import seshat_tui.app as app_mod

    monkeypatch.setattr(app_mod, "_vault", _FakeVault("some-key"))
    monkeypatch.setattr(
        app_mod.platform_client,
        "fetch_sentinel_verdicts",
        lambda api_base, api_key, hashes: {hashes[0]: {"verdict": "holding"}},
    )
    app = SeshatApp()
    result = app._fetch_sentinel_verdicts([{"agreement_hash": "a" * 64}])
    assert result == {"a" * 64: {"verdict": "holding"}}
