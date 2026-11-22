"""Tests for how the dashboard is served: forwarded-client-IP trust.

The rootless-Podman published-port path rewrites every inbound peer address to
the container's own bridge IP, so the only header that can name a client is
``X-Forwarded-For`` — and trusting it without a proxy in front would let any
client forge its own source address. These tests pin the shipped default (off)
and the exact address ProxyFix selects when an operator opts in.

The selection rule (measured against werkzeug 3.1, not assumed): ``x_for=N``
takes the Nth address **counted from the right** of the header, because each
trusting proxy APPENDS the address it saw.
"""
import pytest
from flask import Flask, request
from werkzeug.middleware.proxy_fix import ProxyFix

from scraper.server import _apply_proxy_fix, _trusted_proxy_hops


def _probe() -> Flask:
    """Tiny app that reports the address Flask believes the client has."""
    probe = Flask("probe")

    @probe.route("/ip")
    def _ip():  # pragma: no cover - exercised through test_client
        return request.remote_addr or ""

    probe.config["TESTING"] = True
    return probe


class TestTrustedProxyHops:
    def test_default_is_zero(self, monkeypatch):
        monkeypatch.delenv("INVESTCLOSURE_TRUST_PROXY", raising=False)
        assert _trusted_proxy_hops() == 0

    @pytest.mark.parametrize("raw,expected", [
        ("0", 0), ("", 0), ("   ", 0), ("not-a-number", 0), ("-1", 0),
        ("-7", 0), ("1", 1), ("2", 2),
    ])
    def test_parsing_never_yields_a_trusting_default(self, monkeypatch, raw, expected):
        monkeypatch.setenv("INVESTCLOSURE_TRUST_PROXY", raw)
        assert _trusted_proxy_hops() == expected


class TestProxyFixGating:
    def test_dashboard_ships_without_proxy_fix(self, monkeypatch):
        """Import-time default: the real app does not read X-Forwarded-For.

        Asserted with isinstance, not identity — in Flask 3.1 ``app.wsgi_app``
        resolves to a bound method, so every access is a fresh object and an
        ``is`` comparison can never hold.
        """
        from scraper.server import app
        monkeypatch.delenv("INVESTCLOSURE_TRUST_PROXY", raising=False)
        assert _apply_proxy_fix(app) is app
        assert not isinstance(app.wsgi_app, ProxyFix)

    def test_forged_header_ignored_when_off(self, monkeypatch):
        monkeypatch.delenv("INVESTCLOSURE_TRUST_PROXY", raising=False)
        probe = _probe()
        _apply_proxy_fix(probe)
        got = probe.test_client().get("/ip", headers={"X-Forwarded-For": "203.0.113.9"}).text
        assert got == "127.0.0.1", "a client must not be able to invent its own address"

    def test_single_appending_proxy_names_the_client(self, monkeypatch):
        monkeypatch.setenv("INVESTCLOSURE_TRUST_PROXY", "1")
        probe = _probe()
        _apply_proxy_fix(probe)
        assert probe.test_client().get("/ip", headers={
            "X-Forwarded-For": "203.0.113.9"}).text == "203.0.113.9"

    def test_client_forged_leftmost_entry_loses(self, monkeypatch):
        """A client sending its own X-Forwarded-For is overridden by the proxy."""
        monkeypatch.setenv("INVESTCLOSURE_TRUST_PROXY", "1")
        probe = _probe()
        _apply_proxy_fix(probe)
        got = probe.test_client().get("/ip", headers={
            "X-Forwarded-For": "1.2.3.4, 203.0.113.9"}).text
        assert got == "203.0.113.9", "one trusted hop = the rightmost (proxy-written) address"

    def test_two_hop_chain_needs_two(self, monkeypatch):
        """Two proxies in front: x_for=1 credits the inner proxy instead."""
        hdr = {"X-Forwarded-For": "1.2.3.4, 203.0.113.9, 10.0.0.1"}
        monkeypatch.setenv("INVESTCLOSURE_TRUST_PROXY", "2")
        outer = _probe()
        _apply_proxy_fix(outer)
        assert outer.test_client().get("/ip", headers=hdr).text == "203.0.113.9"
        monkeypatch.setenv("INVESTCLOSURE_TRUST_PROXY", "1")
        one = _probe()
        _apply_proxy_fix(one)
        assert one.test_client().get("/ip", headers=hdr).text == "10.0.0.1"

    def test_overcounted_hops_fall_back_to_the_real_peer(self, monkeypatch):
        """x_for larger than the header is not trusted into a wrong address."""
        monkeypatch.setenv("INVESTCLOSURE_TRUST_PROXY", "2")
        probe = _probe()
        _apply_proxy_fix(probe)
        assert probe.test_client().get("/ip", headers={
            "X-Forwarded-For": "203.0.113.9"}).text == "127.0.0.1"

    def test_no_header_keeps_the_real_peer(self, monkeypatch):
        monkeypatch.setenv("INVESTCLOSURE_TRUST_PROXY", "1")
        probe = _probe()
        _apply_proxy_fix(probe)
        assert probe.test_client().get("/ip").text == "127.0.0.1"

    def test_applying_twice_does_not_nest(self, monkeypatch):
        """Werkzeug refuses a second ProxyFix — re-running must be a no-op."""
        monkeypatch.setenv("INVESTCLOSURE_TRUST_PROXY", "1")
        probe = _probe()
        _apply_proxy_fix(probe)
        once = probe.wsgi_app
        _apply_proxy_fix(probe)  # must not raise, must not wrap again
        assert probe.wsgi_app is once
        assert probe.test_client().get("/ip", headers={
            "X-Forwarded-For": "203.0.113.9"}).text == "203.0.113.9"
