"""F-09: the Flask dashboard (seshat.py, bound 127.0.0.1:9000) had no
Origin/Host validation on state-changing routes — a page loaded from any
other origin in the user's browser could POST/PUT/PATCH/DELETE against it
purely because the browser sends cookies/requests to localhost
automatically. Mutating requests whose Origin header is present and
doesn't match the dashboard's own origin are now rejected; GETs and
requests with no Origin header at all (non-browser clients like curl, run
directly by the user) are unaffected.
"""
import pytest

import seshat


@pytest.fixture
def client():
    seshat.app.config["TESTING"] = True
    return seshat.app.test_client()


class TestCrossOriginMutationRejected:
    def test_cross_origin_post_to_vault_keys_is_rejected(self, client):
        resp = client.post(
            "/api/vault/keys",
            json={"key": "FOO", "value": "bar"},
            headers={"Origin": "https://evil.example"},
        )
        assert resp.status_code == 403

    def test_cross_origin_post_to_project_stop_is_rejected(self, client):
        """Rejected before the route handler ever runs — a nonexistent
        project would otherwise 404, not 403, proving the before_request
        gate fires first."""
        resp = client.post(
            "/api/projects/some-project/stop",
            headers={"Origin": "http://attacker.example"},
        )
        assert resp.status_code == 403

    def test_cross_origin_post_to_router_setup_is_rejected(self, client, monkeypatch):
        def _boom(*a, **k):
            raise AssertionError("must not reach the handler")
        monkeypatch.setattr(seshat.router, "configure_dnsmasq", _boom)

        resp = client.post(
            "/api/router/setup/dnsmasq",
            headers={"Origin": "https://evil.example"},
        )
        assert resp.status_code == 403


class TestSameOriginAndNonBrowserRequestsUnaffected:
    def test_matching_origin_post_reaches_the_handler(self, client, monkeypatch):
        seshat_dir_calls = []
        monkeypatch.setattr(seshat.vault, "set", lambda k, v: seshat_dir_calls.append((k, v)))

        resp = client.post(
            "/api/vault/keys",
            json={"key": "FOO", "value": "bar"},
            headers={"Origin": "http://localhost"},
        )
        assert resp.status_code == 200
        assert seshat_dir_calls == [("FOO", "bar")]

    def test_matching_origin_on_a_non_default_port_reaches_the_handler(self, client, monkeypatch):
        """seshat serve --port <n> accepts an arbitrary port and binds all
        interfaces — the check must compare against the request's own Host,
        not a hardcoded 127.0.0.1:9000, or a dashboard accessed via a
        non-default port/LAN address would reject its own same-origin
        requests."""
        calls = []
        monkeypatch.setattr(seshat.vault, "set", lambda k, v: calls.append((k, v)))

        resp = client.post(
            "/api/vault/keys",
            json={"key": "FOO", "value": "bar"},
            headers={"Origin": "http://10.23.1.86:9091", "Host": "10.23.1.86:9091"},
        )
        assert resp.status_code == 200
        assert calls == [("FOO", "bar")]

    def test_no_origin_header_is_allowed(self, client, monkeypatch):
        """A direct curl-style request (no browser, no Origin header) is the
        user's own deliberate local action, not a CSRF vector — unaffected."""
        calls = []
        monkeypatch.setattr(seshat.vault, "set", lambda k, v: calls.append((k, v)))

        resp = client.post("/api/vault/keys", json={"key": "FOO", "value": "bar"})
        assert resp.status_code == 200
        assert calls == [("FOO", "bar")]

    def test_get_requests_are_never_blocked(self, client):
        resp = client.get("/api/vault/keys", headers={"Origin": "https://evil.example"})
        assert resp.status_code == 200
