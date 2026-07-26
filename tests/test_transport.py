"""Unit tests for MAILBOX_TRANSPORT dispatch in server.main(), mocked at the
mcp.run seam (no real socket/stdio needed).

Run: `uv run pytest` (or `pytest tests/`).
"""

from __future__ import annotations

import signal

import pytest

import claude_mailbox.server as srv


def _quiet_signals(monkeypatch):
    # main() installs real signal handlers; keep that a no-op in tests.
    monkeypatch.setattr(signal, "signal", lambda *a, **k: None)


def _no_token(monkeypatch):
    monkeypatch.delenv("MAILBOX_TOKEN", raising=False)
    monkeypatch.delenv("MAILBOX_TOKEN_FILE", raising=False)


def test_main_defaults_to_stdio(monkeypatch):
    monkeypatch.delenv("MAILBOX_TRANSPORT", raising=False)
    _quiet_signals(monkeypatch)
    calls = []
    monkeypatch.setattr(srv.mcp, "run", lambda *a, **k: calls.append((a, k)))
    srv.main()
    assert calls == [((), {})]


def test_main_http_transport_refuses_non_loopback_without_token(monkeypatch):
    monkeypatch.setenv("MAILBOX_TRANSPORT", "http")
    monkeypatch.setenv("MAILBOX_HTTP_HOST", "0.0.0.0")
    monkeypatch.setenv("MAILBOX_HTTP_PORT", "9001")
    _no_token(monkeypatch)
    _quiet_signals(monkeypatch)
    monkeypatch.setattr(srv.mcp, "run", lambda *a, **k: pytest.fail("should not start"))
    with pytest.raises(SystemExit):
        srv.main()


def test_main_http_transport_non_loopback_with_token_starts_with_auth(monkeypatch):
    monkeypatch.setenv("MAILBOX_TRANSPORT", "http")
    monkeypatch.setenv("MAILBOX_HTTP_HOST", "0.0.0.0")
    monkeypatch.setenv("MAILBOX_HTTP_PORT", "9001")
    monkeypatch.setenv("MAILBOX_TOKEN", "secret-token")
    _quiet_signals(monkeypatch)
    calls = []
    monkeypatch.setattr(srv.mcp, "run", lambda *a, **k: calls.append((a, k)))
    srv.main()
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ()
    assert kwargs["transport"] == "http"
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 9001
    assert isinstance(kwargs["auth"], srv._BearerTokenVerifier)


def test_main_http_transport_uses_default_host_and_port(monkeypatch):
    monkeypatch.setenv("MAILBOX_TRANSPORT", "http")
    monkeypatch.delenv("MAILBOX_HTTP_HOST", raising=False)
    monkeypatch.delenv("MAILBOX_HTTP_PORT", raising=False)
    _no_token(monkeypatch)
    _quiet_signals(monkeypatch)
    calls = []
    monkeypatch.setattr(srv.mcp, "run", lambda *a, **k: calls.append((a, k)))
    srv.main()
    assert calls == [
        ((), {"transport": "http", "host": "127.0.0.1", "port": 8000, "auth": None})
    ]


def test_main_http_transport_loopback_with_token_passes_auth(monkeypatch):
    monkeypatch.setenv("MAILBOX_TRANSPORT", "http")
    monkeypatch.delenv("MAILBOX_HTTP_HOST", raising=False)
    monkeypatch.delenv("MAILBOX_HTTP_PORT", raising=False)
    monkeypatch.setenv("MAILBOX_TOKEN", "secret-token")
    _quiet_signals(monkeypatch)
    calls = []
    monkeypatch.setattr(srv.mcp, "run", lambda *a, **k: calls.append((a, k)))
    srv.main()
    assert len(calls) == 1
    _, kwargs = calls[0]
    assert isinstance(kwargs["auth"], srv._BearerTokenVerifier)


def test_load_token_from_env(monkeypatch):
    _no_token(monkeypatch)
    monkeypatch.setenv("MAILBOX_TOKEN", "  abc123  ")
    assert srv._load_token() == "abc123"


def test_load_token_from_file(monkeypatch, tmp_path):
    _no_token(monkeypatch)
    token_file = tmp_path / "token"
    token_file.write_text("filetoken\n")
    monkeypatch.setenv("MAILBOX_TOKEN_FILE", str(token_file))
    assert srv._load_token() == "filetoken"


def test_load_token_env_takes_precedence_over_file(monkeypatch, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("filetoken\n")
    monkeypatch.setenv("MAILBOX_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("MAILBOX_TOKEN", "envtoken")
    assert srv._load_token() == "envtoken"


def test_load_token_missing_returns_none(monkeypatch):
    _no_token(monkeypatch)
    assert srv._load_token() is None


async def test_bearer_token_verifier_accepts_matching_token():
    verifier = srv._BearerTokenVerifier("secret-token")
    result = await verifier.verify_token("secret-token")
    assert result is not None
    assert result.token == "secret-token"


async def test_bearer_token_verifier_rejects_wrong_token():
    verifier = srv._BearerTokenVerifier("secret-token")
    assert await verifier.verify_token("wrong-token") is None
    assert await verifier.verify_token("") is None
