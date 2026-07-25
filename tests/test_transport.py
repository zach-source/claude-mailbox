"""Unit tests for MAILBOX_TRANSPORT dispatch in server.main(), mocked at the
mcp.run seam (no real socket/stdio needed).

Run: `uv run pytest` (or `pytest tests/`).
"""

from __future__ import annotations

import signal

import claude_mailbox.server as srv


def _quiet_signals(monkeypatch):
    # main() installs real signal handlers; keep that a no-op in tests.
    monkeypatch.setattr(signal, "signal", lambda *a, **k: None)


def test_main_defaults_to_stdio(monkeypatch):
    monkeypatch.delenv("MAILBOX_TRANSPORT", raising=False)
    _quiet_signals(monkeypatch)
    calls = []
    monkeypatch.setattr(srv.mcp, "run", lambda *a, **k: calls.append((a, k)))
    srv.main()
    assert calls == [((), {})]


def test_main_http_transport_passes_host_and_port(monkeypatch):
    monkeypatch.setenv("MAILBOX_TRANSPORT", "http")
    monkeypatch.setenv("MAILBOX_HTTP_HOST", "0.0.0.0")
    monkeypatch.setenv("MAILBOX_HTTP_PORT", "9001")
    _quiet_signals(monkeypatch)
    calls = []
    monkeypatch.setattr(srv.mcp, "run", lambda *a, **k: calls.append((a, k)))
    srv.main()
    assert calls == [((), {"transport": "http", "host": "0.0.0.0", "port": 9001})]


def test_main_http_transport_uses_default_host_and_port(monkeypatch):
    monkeypatch.setenv("MAILBOX_TRANSPORT", "http")
    monkeypatch.delenv("MAILBOX_HTTP_HOST", raising=False)
    monkeypatch.delenv("MAILBOX_HTTP_PORT", raising=False)
    _quiet_signals(monkeypatch)
    calls = []
    monkeypatch.setattr(srv.mcp, "run", lambda *a, **k: calls.append((a, k)))
    srv.main()
    assert calls == [((), {"transport": "http", "host": "127.0.0.1", "port": 8000})]
