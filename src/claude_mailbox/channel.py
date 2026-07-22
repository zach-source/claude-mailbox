"""Claude Code *channel* integration.

Claude Code "channels" (https://code.claude.com/docs/en/channels-reference) let an
MCP server *push* events into a live session: the server declares the
`claude/channel` capability and emits `notifications/claude/channel`, which arrive
in the session as `<channel source="mailbox" ...>body</channel>` tags — no polling
by the model. This module turns the mailbox into such a channel so a peer session's
DM / info-request / delegation / broadcast proactively interrupts the target
session instead of waiting for a `poll_inbox` call.

Mechanics (grounded in the installed MCP SDK):
  * `experimental_capabilities={"claude/channel": {}}` is injected into the
    low-level server's InitializationOptions (declares the capability).
  * A live `ServerSession` + its event loop are captured on the first tool call.
  * `push()` sends a raw `JSONRPCNotification` (method `notifications/claude/channel`)
    onto the session's write stream, scheduled onto the server loop from any thread.

Note: during the research preview custom channels aren't allowlisted, so a session
must be started with `--dangerously-load-development-channels server:mailbox` (and
the org's `channelsEnabled` policy must allow it) for these pushes to register.
"""

from __future__ import annotations

import asyncio
import re
import threading

from mcp.shared.session import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification

CHANNEL_CAP = "claude/channel"
CHANNEL_METHOD = "notifications/claude/channel"

# Channel meta keys must be identifiers (letters/digits/underscore); other chars
# are silently dropped by Claude Code. We sanitize keys and stringify values.
_KEY_RE = re.compile(r"^[A-Za-z0-9_]+$")


class _ChannelState:
    def __init__(self) -> None:
        self.session = None  # mcp ServerSession, captured on first tool call
        self.loop: asyncio.AbstractEventLoop | None = None
        self.enabled = False  # capability declared on this server
        self._lock = threading.Lock()


_CH = _ChannelState()


def enable_channel(mcp) -> None:
    """Declare the `claude/channel` capability on a FastMCP server by wrapping the
    low-level server's create_initialization_options to inject it. Idempotent."""
    low = mcp._mcp_server
    orig = low.create_initialization_options

    def patched(notification_options=None, experimental_capabilities=None, **kw):
        caps = dict(experimental_capabilities or {})
        caps.setdefault(CHANNEL_CAP, {})
        return orig(notification_options, caps, **kw)

    low.create_initialization_options = patched
    _CH.enabled = True


def _unwrap_session(obj):
    """Find the raw mcp ServerSession (the thing with `_write_stream`) from a
    FastMCP Context / session wrapper."""
    seen = set()
    for cand in (obj, getattr(obj, "session", None), getattr(obj, "_session", None)):
        if cand is None or id(cand) in seen:
            continue
        seen.add(id(cand))
        if hasattr(cand, "_write_stream"):
            return cand
    return None


def capture(ctx) -> None:
    """Capture the live ServerSession + running loop from an async tool's Context
    so the background poller can push notifications. MUST be called from an async
    tool (so `asyncio.get_running_loop()` returns the server loop). Best-effort —
    never raises into the tool; only the first successful capture matters."""
    if _CH.session is not None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # not on the event loop (sync tool worker thread) — cannot capture
    rc = getattr(ctx, "request_context", None)
    raw = _unwrap_session(getattr(rc, "session", None)) or _unwrap_session(
        getattr(ctx, "session", None)
    )
    if raw is None:
        return
    with _CH._lock:
        if _CH.session is None:
            _CH.session = raw
            _CH.loop = loop


def is_live() -> bool:
    return _CH.enabled and _CH.session is not None and _CH.loop is not None


def _sanitize_meta(meta: dict) -> dict:
    out = {}
    for k, v in (meta or {}).items():
        if v is None:
            continue
        if _KEY_RE.match(str(k)):
            out[str(k)] = str(v)
    return out


def push(content: str, meta: dict | None = None) -> bool:
    """Emit a `notifications/claude/channel` event into the captured session from
    any thread. Returns True if the send was scheduled, False if no live session
    (e.g. the client isn't running us as a channel). Never raises."""
    if not is_live():
        return False
    note = JSONRPCNotification(
        jsonrpc="2.0",
        method=CHANNEL_METHOD,
        params={"content": content, "meta": _sanitize_meta(meta or {})},
    )
    msg = SessionMessage(message=JSONRPCMessage(note))

    async def _send():
        try:
            await _CH.session._write_stream.send(msg)
        except Exception:
            pass  # session torn down / not a channel — drop silently, like the spec

    try:
        asyncio.run_coroutine_threadsafe(_send(), _CH.loop)
        return True
    except Exception:
        return False
