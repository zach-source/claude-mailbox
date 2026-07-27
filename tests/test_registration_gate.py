"""Registration gate: acting tools must refuse an unregistered connection
(design: global-4pm P1) — gating only register_session was security theater,
since _current_state() lazily mints a sid for any caller. Every assertion here
short-circuits before touching bd (the gate runs first in each tool), so no
fake bd store or MAILBOX_E2E is needed.
"""

from __future__ import annotations

import claude_mailbox.server as srv

NOT_REGISTERED = {"ok": False, "error": "not registered"}


def _clean_sessions_registry():
    saved = dict(srv._SESSIONS)
    srv._SESSIONS.clear()
    return saved


async def _call(client, name, **kwargs):
    res = await client.call_tool(name, kwargs)
    return res.data


async def test_unregistered_connection_is_rejected_by_every_gated_tool():
    from fastmcp import Client

    saved = _clean_sessions_registry()
    try:
        async with Client(srv.mcp) as client:
            assert await _call(client, "broadcast", text="hi") == NOT_REGISTERED
            assert (
                await _call(client, "read_channel", channel="general") == NOT_REGISTERED
            )
            assert (
                await _call(client, "send_dm", to_sid="peer", text="hi")
                == NOT_REGISTERED
            )
            assert (
                await _call(client, "delegate", to_sid="peer", title="t")
                == NOT_REGISTERED
            )
            assert (
                await _call(client, "respond_info", request_id="req-1", answer="a")
                == NOT_REGISTERED
            )
            assert (
                await _call(
                    client,
                    "request_info",
                    to_sid="peer",
                    question="q",
                    timeout_s=0,
                )
                == NOT_REGISTERED
            )
            assert await _call(client, "claim_leadership") == NOT_REGISTERED
            assert await _call(client, "set_status", status="idle") == NOT_REGISTERED
            assert await _call(client, "poll_inbox") == NOT_REGISTERED
    finally:
        srv._SESSIONS.clear()
        srv._SESSIONS.update(saved)


def test_require_registered_returns_none_when_registered():
    st = srv._SessionState("conn-x")
    st.bead_id = "bd-1"
    assert srv._require_registered(st) is None


def test_require_registered_returns_error_when_not_registered():
    st = srv._SessionState("conn-y")
    assert srv._require_registered(st) == NOT_REGISTERED
