"""Pure-logic tests for the claude/channel layer. The full push path (a real
notification on the wire) is covered by the raw JSON-RPC wire test in the repo's
scratchpad harness; here we cover capability injection, meta sanitization, and the
no-live-session guard without needing a running server."""

from __future__ import annotations

from claude_mailbox import channel as ch


def test_enable_channel_injects_capability():
    from fastmcp import FastMCP

    m = FastMCP("t")
    ch.enable_channel(m)
    opts = m._mcp_server.create_initialization_options()
    assert "claude/channel" in (opts.capabilities.experimental or {})


def test_enable_channel_preserves_other_experimental_caps():
    from fastmcp import FastMCP

    m = FastMCP("t2")
    ch.enable_channel(m)
    opts = m._mcp_server.create_initialization_options(
        experimental_capabilities={"other/cap": {"x": 1}}
    )
    exp = opts.capabilities.experimental or {}
    assert "claude/channel" in exp and "other/cap" in exp


def test_sanitize_meta_drops_bad_keys_and_none():
    out = ch._sanitize_meta(
        {"kind": "dm", "from_sid": "peer-1", "bad-key": "x", "n": None, "num": 5}
    )
    assert out == {"kind": "dm", "from_sid": "peer-1", "num": "5"}  # str-coerced
    assert "bad-key" not in out and "n" not in out


def test_push_noop_without_live_session(monkeypatch):
    # Fresh state: no captured session → push is a no-op returning False.
    monkeypatch.setattr(ch._CH, "session", None)
    monkeypatch.setattr(ch._CH, "loop", None)
    assert ch.is_live() is False
    assert ch.push("hello", {"kind": "dm"}) is False
