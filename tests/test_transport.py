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


@pytest.fixture(autouse=True)
def _reset_mcp_auth():
    # mcp is a module-level singleton shared across the whole test process —
    # main() mutates mcp.auth as a side effect, so isolate it per test.
    saved = srv.mcp.auth
    try:
        yield
    finally:
        srv.mcp.auth = saved


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
    assert calls == [((), {"transport": "http", "host": "0.0.0.0", "port": 9001})]
    # auth must be set on the server object itself (the only place run_http_async
    # reads it from) — NOT passed as a run() kwarg, which fastmcp 3.4.4 rejects.
    assert isinstance(srv.mcp.auth, srv._BearerTokenVerifier)


def test_main_http_transport_uses_default_host_and_port(monkeypatch):
    monkeypatch.setenv("MAILBOX_TRANSPORT", "http")
    monkeypatch.delenv("MAILBOX_HTTP_HOST", raising=False)
    monkeypatch.delenv("MAILBOX_HTTP_PORT", raising=False)
    _no_token(monkeypatch)
    _quiet_signals(monkeypatch)
    calls = []
    monkeypatch.setattr(srv.mcp, "run", lambda *a, **k: calls.append((a, k)))
    srv.main()
    assert calls == [((), {"transport": "http", "host": "127.0.0.1", "port": 8000})]
    assert srv.mcp.auth is None


def test_main_http_transport_loopback_with_token_sets_auth(monkeypatch):
    monkeypatch.setenv("MAILBOX_TRANSPORT", "http")
    monkeypatch.delenv("MAILBOX_HTTP_HOST", raising=False)
    monkeypatch.delenv("MAILBOX_HTTP_PORT", raising=False)
    monkeypatch.setenv("MAILBOX_TOKEN", "secret-token")
    _quiet_signals(monkeypatch)
    calls = []
    monkeypatch.setattr(srv.mcp, "run", lambda *a, **k: calls.append((a, k)))
    srv.main()
    assert len(calls) == 1
    assert isinstance(srv.mcp.auth, srv._BearerTokenVerifier)


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


def test_load_token_empty_file_refuses(monkeypatch, tmp_path):
    # An empty token file is almost certainly a misconfiguration (operator
    # meant to set a token); treating it as "no token configured" would
    # silently downgrade a non-loopback deployment to refusing to start for
    # the wrong reason, or a loopback one to running unauthenticated.
    _no_token(monkeypatch)
    token_file = tmp_path / "token"
    token_file.write_text("   \n")
    monkeypatch.setenv("MAILBOX_TOKEN_FILE", str(token_file))
    with pytest.raises(SystemExit):
        srv._load_token()


async def test_bearer_token_verifier_accepts_matching_token():
    verifier = srv._BearerTokenVerifier("secret-token")
    result = await verifier.verify_token("secret-token")
    assert result is not None
    assert result.token == "secret-token"


async def test_bearer_token_verifier_rejects_wrong_token():
    verifier = srv._BearerTokenVerifier("secret-token")
    assert await verifier.verify_token("wrong-token") is None
    assert await verifier.verify_token("") is None


async def test_bearer_token_verifier_rejects_non_ascii_token_without_raising():
    # secrets.compare_digest raises TypeError on non-ASCII str input; a naive
    # implementation would turn a garbage bearer token into a 500 instead of
    # a clean 401. Comparing as bytes avoids that.
    verifier = srv._BearerTokenVerifier("secret-token")
    assert await verifier.verify_token("wrong-é-token") is None
