"""End-to-end test through the real MCP protocol layer.

Uses FastMCP's in-memory Client, which performs an actual MCP initialize +
tools/list + tools/call handshake against the server object — the same path a
Claude Code session uses, minus the stdio transport. This DOES hit the live
`bd --global` database, so it is opt-in: set MAILBOX_E2E=1 to run.

    MAILBOX_E2E=1 uv run pytest tests/test_mcp_e2e.py -q
"""

from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("MAILBOX_E2E") != "1",
    reason="live bd e2e; set MAILBOX_E2E=1 to run",
)


async def _call(client, name, **args):
    res = await client.call_tool(name, args)
    # FastMCP returns structured content when the tool returns a dict/list.
    data = getattr(res, "data", None)
    if data is not None:
        return data
    block = res.content[0]
    return (
        json.loads(block.text)
        if block.text.strip().startswith(("{", "["))
        else block.text
    )


@pytest.mark.asyncio
async def test_full_handshake_and_flow():
    from fastmcp import Client

    import claude_mailbox.server as srv

    async with Client(srv.mcp) as client:
        # 1. tools are discoverable over the protocol
        tools = {t.name for t in await client.list_tools()}
        assert {
            "register_session",
            "list_sessions",
            "broadcast",
            "read_channel",
            "get_leader",
            "deregister",
        } <= tools

        # 2. register → we appear in list_sessions
        reg = await _call(client, "register_session", objective="e2e test")
        sid = reg["sid"]
        assert reg["bead_id"]
        sessions = await _call(client, "list_sessions")
        assert any(s["sid"] == sid for s in sessions)

        # 3. broadcast → read back on the channel
        await _call(client, "broadcast", text="hello e2e", channel="e2e")
        msgs = await _call(client, "read_channel", channel="e2e", limit=10)
        assert any(mm["text"] == "hello e2e" for mm in msgs)

        # 4. leader reflects our branch context (leader or vacant, never crash)
        lead = await _call(client, "get_leader")
        assert "vacant" in lead

        # 5. clean up
        out = await _call(client, "deregister")
        assert out["ok"] is True
        sessions = await _call(client, "list_sessions")
        assert not any(s["sid"] == sid for s in sessions)
