"""Claude Code Mailbox — FastMCP server.

Lets concurrent Claude Code sessions register, discover each other, cross-talk
over channels, DM, and coordinate under a single main-branch leader. All state
lives in the shared `beads_global` Dolt DB via the `bd` CLI (see bd.py).

A background thread heartbeats every HB_BUCKET seconds so liveness/leadership
don't depend on the model remembering to call anything.

Session identity is per-connection, not per-process (see `_SessionState` /
`_SESSIONS` below) — a single HTTP-mode server process can serve multiple
concurrent Claude Code connections, each with its own sid/git-context/bead_id.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import secrets
import signal
import sys
import threading
import time

from fastmcp import Context, FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_context

from . import channel as ch
from . import leader as L
from . import model as m
from .bd import BdError, create, run_bd, run_bd_json
from .identity import GitContext, detect_git, hostname, new_sid

logger = logging.getLogger("claude_mailbox")

# Channels: peer messages arrive as <channel source="mailbox" ...> events pushed
# into the session (see channel.py) — the model doesn't have to poll.
_CHANNEL_INSTRUCTIONS = (
    "You share a mailbox with other Claude Code sessions. Messages from peers "
    'arrive pushed as <channel source="mailbox" kind="..." from_sid="..."> events '
    "(kind is dm | request | delegation | broadcast). React to them: for kind="
    '"request" answer with the respond_info tool passing the request_id attribute; '
    "for a dm reply with send_dm to the from_sid; for a broadcast just take it into "
    "account. Register once at session start with register_session so peers can see "
    "you and reach you. There is a single leader (the session on the main branch); "
    "if you are secondary, defer to it. Broadcast sparingly — it interrupts every "
    "peer; prefer send_dm when one session needs to know. broadcast defaults to "
    'your own project\'s channel; only pass channel="general" when sessions in '
    "other repos genuinely need the message."
)

mcp = FastMCP("claude-mailbox", instructions=_CHANNEL_INSTRUCTIONS)
ch.enable_channel(mcp)

# How often the channel-delivery thread checks for new inbound to push (seconds).
# Faster than the HB_BUCKET heartbeat so peer messages feel like interrupts, but
# every poll costs a `bd` subprocess (~0.3s of Go startup before any DB work,
# and a machine-wide schema-migration lock), and a busy machine runs one server
# per agent session — measured 20 concurrent on one laptop. At the old flat 4s
# with one query per subscribed channel that was ~1 spawn/sec/session in
# perpetuity, enough to saturate the shared Dolt server. Now: one query per poll
# (see _inbound_query) on an idle backoff, so a quiet mailbox costs a spawn a
# minute and a busy one still reacts within CHANNEL_POLL_SECONDS.
CHANNEL_POLL_SECONDS = 15
CHANNEL_POLL_MAX_SECONDS = 60

# Bounds on the single inbound query. The lookback exists because `bd query`
# silently defaults to `--limit 50`: an unbounded scan of a channel's whole
# history meant a busy channel could hide its newest messages behind 50 older
# rows forever, so this is a correctness bound as much as a cost one.
_INBOUND_LOOKBACK = "1h"  # bd relative-duration syntax
_INBOUND_LIMIT = 200

# Cap on the per-connection delivered-ids set. These processes live for weeks;
# the set used to be unbounded and kept every id ever seen.
_SEEN_MAX = 2000


class _SessionState:
    """Per-connection session identity: everything that used to live on the
    single module-global `_State` singleton, now scoped to one caller."""

    def __init__(self, conn_id: str) -> None:
        self.conn_id = conn_id
        self.sid = new_sid()
        self.git: GitContext = detect_git()
        self.bead_id: str | None = None
        self.meta: dict = {}
        self.channel: ch.ChannelState | None = None  # captured per-connection
        # Message bead ids already pushed to this connection. A dict used as an
        # insertion-ordered set so the oldest can be evicted — see mark_seen.
        self._seen: dict[str, None] = {}
        self._lock = threading.Lock()
        self.last_activity = time.time()  # last real tool call from this connection
        self.role: str | None = None  # last role written; skips no-op set-state beats

    def subscribed_channels(self) -> list[str]:
        # Filtered through valid_token because these names are interpolated into
        # a single compound bd query (_inbound_query): one malformed name — a
        # repo directory with a space in it, say — would otherwise break
        # delivery for every channel at once, not just its own.
        return [c for c in ("general", self.git.project, "leader") if m.valid_token(c)]

    def has_seen(self, rid: str) -> bool:
        return rid in self._seen

    def mark_seen(self, rid: str) -> None:
        """Record a delivered id, evicting the oldest past _SEEN_MAX. Safe to
        forget old ids because the inbound query is lookback-bounded, so a
        re-push would need a bead both older than the window and still
        unseen."""
        self._seen[rid] = None
        while len(self._seen) > _SEEN_MAX:
            self._seen.pop(next(iter(self._seen)))


# ── per-connection session registry ─────────────────────────────────────────
# Keyed by fastmcp's Context.session_id, which is stable for the life of one
# connection on every transport: for HTTP it's the `mcp-session-id` header; for
# stdio/SSE/in-memory, fastmcp generates a UUID once and caches it on the
# ServerSession object, so a stdio process (one connection for its whole life)
# still gets exactly one entry here — identical to the old single-singleton
# behavior. `_DEFAULT_CONN_ID` is only a fallback for the rare case there's no
# active MCP context at all (e.g. unit tests calling tool functions directly as
# plain Python calls); it reproduces the pre-fix shared-singleton behavior those
# tests already rely on.
_DEFAULT_CONN_ID = "default"
_SESSIONS: dict[str, _SessionState] = {}
_SESSIONS_LOCK = threading.Lock()

# How long an HTTP-mode connection can go without a real tool call before we
# treat it as abandoned (see _hb_tick_once). Reuses the exact reap threshold
# `_reap_stale` already uses for cross-process crash cleanup.
_CONN_IDLE_SECONDS = m.STALE_SECONDS * 10

_hb_thread: threading.Thread | None = None
_ch_thread: threading.Thread | None = None
_threads_lock = threading.Lock()
_stop = threading.Event()


def _http_mode() -> bool:
    return os.environ.get("MAILBOX_TRANSPORT", "stdio").strip().lower() == "http"


def _resolve_conn_id() -> str:
    try:
        return get_context().session_id
    except RuntimeError:
        return _DEFAULT_CONN_ID


def _state_for(conn_id: str) -> _SessionState:
    with _SESSIONS_LOCK:
        st = _SESSIONS.get(conn_id)
        if st is None:
            st = _SessionState(conn_id)
            _SESSIONS[conn_id] = st
        st.last_activity = time.time()
        return st


def _current_state() -> _SessionState:
    """Resolve the calling connection's per-session state. Every sync/async
    tool call goes through this instead of touching a shared global."""
    return _state_for(_resolve_conn_id())


_NOT_REGISTERED = {"ok": False, "error": "not registered"}


def _require_registered(st: _SessionState) -> dict | None:
    """Single choke point (design: global-4pm P1): tools that act on behalf of
    a session must not run on a connection that never called register_session
    — otherwise gating register_session alone is security theater, since
    _current_state() lazily mints a sid for anyone. Returns an error dict to
    short-circuit the caller, or None if the connection is registered."""
    if not st.bead_id:
        return dict(_NOT_REGISTERED)
    return None


# ── helpers ──────────────────────────────────────────────────────────────────
def _find_session(sid: str) -> dict | None:
    rows = run_bd_json("query", f"label={m.sid_label(sid)}") or []
    return rows[0] if rows else None


def _session_view(bead: dict) -> dict:
    st = L.states_of(bead)
    meta = m.meta_of(bead)
    hb_int = m.hb_of(bead)
    return {
        "sid": meta.get("sid"),
        "project": meta.get("project"),
        "branch": meta.get("branch"),
        "worktree": meta.get("worktree"),
        "objective": meta.get("objective"),
        "status": st.get(m.D_STATUS, "unknown"),
        "role": st.get(m.D_ROLE, "secondary"),
        "last_hb_age_s": round(m.hb_age_seconds(hb_int)),
        "stale": m.is_stale(hb_int),
    }


def _heartbeat_once(st: _SessionState) -> dict:
    if not st.bead_id:
        return {"ok": False}
    st.git = detect_git()
    st.meta["branch"] = st.git.branch
    st.meta["worktree"] = st.git.worktree
    st.meta["project"] = st.git.project
    st.meta[m.K_HB] = m.hb_now()
    # One description write carries both the heartbeat and any git drift. This
    # deliberately is NOT `bd set-state` — see model.py K_HB for why (event-bead
    # + label churn per beat is what bloated beads_global).
    run_bd("update", st.bead_id, "-d", json.dumps(st.meta), actor=st.sid)
    with st._lock:
        lead = L.heartbeat_leader(st.sid, st.git.branch, st.sid)
        # Only on an actual role *change*. Role is low-cardinality (86 changes
        # across the whole history), so set-state is the right home for it — but
        # calling it unconditionally every beat spends a bd subprocess to
        # rediscover that nothing changed, and leans on bd's no-op dedup to keep
        # it from minting an event bead. Decide here instead.
        if lead["role"] != st.role:
            ok = run_bd(
                "set-state",
                st.bead_id,
                f"{m.D_ROLE}={lead['role']}",
                "--reason",
                "role sync",
                actor=st.sid,
                check=False,
            )
            if ok:  # check=False returns "" on failure — retry on the next beat
                st.role = lead["role"]
    return {"ok": True, "role": lead["role"]}


def _hb_tick_once() -> None:
    """One pass over every tracked connection: heartbeat it, unless it's an
    HTTP-mode connection that's gone idle past _CONN_IDLE_SECONDS, in which
    case treat it as abandoned and clean it up instead (see module docstring
    in channel.py and README/DESIGN for the tradeoff this makes)."""
    for st in list(_SESSIONS.values()):
        if not st.bead_id:
            continue
        idle_for = time.time() - st.last_activity
        if (
            _http_mode()
            and st.conn_id != _DEFAULT_CONN_ID
            and idle_for > _CONN_IDLE_SECONDS
        ):
            _deregister_state(st)
            with _SESSIONS_LOCK:
                _SESSIONS.pop(st.conn_id, None)
            continue
        try:
            _heartbeat_once(st)
        except (
            Exception
        ) as exc:  # never let the daemon thread die on a transient bd error
            logger.warning("heartbeat failed for %s: %s", st.sid, exc)


def _hb_loop() -> None:
    # One shared thread iterating every live connection, not one thread per
    # connection: HTTP mode can have many concurrent connections and spawning
    # (and tearing down) a thread per connection is unnecessary complexity for
    # what's fundamentally a periodic sweep — a single loop over a dict is
    # simpler and just as correct, and it's what already reaps stale entries.
    while not _stop.wait(m.HB_BUCKET):
        try:
            _hb_tick_once()
        except (
            Exception
        ) as exc:  # never let the daemon thread die on a transient bd error
            logger.warning("heartbeat tick failed: %s", exc)


def _reap_stale(actor_sid: str) -> None:
    """Close session beads whose heartbeat is >10x stale (crashed sessions)."""
    for row in run_bd_json("query", f"label={m.L_SESSION} AND status=open") or []:
        if m.hb_age_seconds(m.hb_of(row)) > m.STALE_SECONDS * 10:
            run_bd(
                "set-state",
                row["id"],
                f"{m.D_STATUS}=done",
                "--reason",
                "reaped: stale",
                actor=actor_sid,
                check=False,
            )
            run_bd("close", row["id"], actor=actor_sid, check=False)
            reaped_sid = next(
                (
                    lbl.removeprefix("session:")
                    for lbl in (row.get("labels") or [])
                    if lbl.startswith("session:")
                ),
                None,
            )
            if reaped_sid and L.read_leader(actor_sid).get("leader_sid") == reaped_sid:
                # The reaped session was still holding the leader slot — vacate it
                # so a live session can fail over instead of waiting for the next
                # main-branch heartbeat to notice staleness.
                L.release(reaped_sid, actor_sid, check=False)


def _channel_loop() -> None:
    delay = CHANNEL_POLL_SECONDS
    while not _stop.wait(delay):
        pushed = 0
        for st in list(_SESSIONS.values()):
            # Per session, not around the whole sweep: `run_bd` raises on a 30s
            # subprocess timeout, and a single wrapping try meant one slow bd
            # call aborted delivery for every *other* session in that pass.
            try:
                pushed += _deliver_channel(st)
            except Exception as exc:  # keep the daemon alive on transient errors
                logger.warning("channel delivery failed for %s: %s", st.sid, exc)
        # Back off while nothing is moving; snap back the moment traffic appears.
        delay = (
            CHANNEL_POLL_SECONDS if pushed else min(delay * 2, CHANNEL_POLL_MAX_SECONDS)
        )


def _inbound_query(st: _SessionState) -> str:
    """The single bd expression the channel poller runs.

    Covers both halves of a session's inbound in one subprocess: items assigned
    to us (DMs, info-requests, delegations) and recent broadcasts on subscribed
    channels. This used to be `1 + len(subscribed_channels)` separate `bd`
    spawns per poll.

    The broadcast half is bounded by `created>` so the scan doesn't grow with a
    channel's whole history — and because `bd query` silently defaults to
    `--limit 50`, that bound is what keeps new messages from hiding behind 50
    older rows. Tradeoff: a process suspended longer than the lookback (laptop
    sleep) can miss a broadcast on resume; poll_inbox/read_channel still surface
    it on demand.
    """
    mine = f"assignee={st.sid} AND status=open"
    chans = " OR ".join(f"label={m.channel_label(c)}" for c in st.subscribed_channels())
    if not chans:
        return mine
    return (
        f"({mine}) OR "
        f"(label={m.L_MESSAGE} AND ({chans}) AND created>{_INBOUND_LOOKBACK})"
    )


def _fetch_inbound(st: _SessionState) -> list[dict]:
    return run_bd_json("query", _inbound_query(st), "-n", str(_INBOUND_LIMIT)) or []


def _kind_of(st: _SessionState, row: dict, labels: list[str]) -> str | None:
    """Classify a row from _inbound_query, or None if it isn't deliverable."""
    if row.get("assignee") == st.sid:
        if m.L_REQUEST in labels:
            return "request"
        if m.L_DELEGATION in labels:
            return "delegation"
        return "dm" if m.L_DM in labels else None
    return "broadcast" if m.L_MESSAGE in labels else None


def _seed_seen(st: _SessionState) -> None:
    """Mark everything the poller can currently see as already delivered, so the
    channel pushes only messages that arrive AFTER we join (no startup backlog
    flood). The pull tools still surface any pre-existing items.

    Runs the *same* query as _deliver_channel so seeding and delivery can never
    disagree about what "currently present" means — they used to issue different
    queries, and with bd's implicit --limit 50 that let a busy channel under-seed
    and then flood on the first pass.
    """
    for r in _fetch_inbound(st):
        if r.get("id"):
            st.mark_seen(r["id"])


def _deliver_channel(st: _SessionState) -> int:
    """Push new inbound to the session as claude/channel events. Delivery-once via
    st._seen. DMs are closed after push (delivered = read); requests stay open so
    the model can respond_info to them. Returns the number pushed."""
    if not st.bead_id or not ch.is_live(st.channel):
        return 0
    pushed = 0
    for r in _fetch_inbound(st):
        rid = r.get("id")
        if not rid or st.has_seen(rid):
            continue
        labels = r.get("labels") or []
        if m.from_label(st.sid) in labels:
            st.mark_seen(rid)  # our own broadcast — don't echo it back to us
            continue
        kind = _kind_of(st, r, labels)
        if kind is None:
            continue
        meta = m.meta_of(r)
        if ch.push(
            st.channel,
            meta.get("text") or r.get("title") or "",
            {
                "kind": kind,
                "from_sid": meta.get("from"),
                "channel": meta.get("channel") if kind == "broadcast" else None,
                "request_id": rid if kind == "request" else None,
                "bead_id": rid,
            },
        ):
            st.mark_seen(rid)
            pushed += 1
            if kind == "dm":
                run_bd("close", rid, actor=st.sid, check=False)
    return pushed


def _ensure_background_threads() -> None:
    global _hb_thread, _ch_thread
    with _threads_lock:
        if not _hb_thread:
            _hb_thread = threading.Thread(target=_hb_loop, daemon=True)
            _hb_thread.start()
        if not _ch_thread:
            _ch_thread = threading.Thread(target=_channel_loop, daemon=True)
            _ch_thread.start()


# ── tools ────────────────────────────────────────────────────────────────────
@mcp.tool
async def register_session(objective: str, ctx: Context) -> dict:
    """Register this Claude session in the mailbox and start heartbeating.

    project/branch/worktree are auto-detected from git. Auto-claims leadership
    if on the main branch. Idempotent for the connection's lifetime (one
    process per session under stdio; one entry per connection under HTTP).
    Also captures the live session so peer messages can be pushed as
    claude/channel events.
    """
    st = _state_for(ctx.session_id)
    if st.channel is None:
        st.channel = ch.capture(
            ctx
        )  # async tool → on the server loop; grabs session+loop
    return await asyncio.to_thread(_register_impl, st, objective)


def _register_impl(st: _SessionState, objective: str) -> dict:
    g = st.git = detect_git()
    if st.bead_id:  # already registered — just refresh objective
        return _update_objective(st, objective)
    meta = {
        "sid": st.sid,
        "project": g.project,
        "branch": g.branch,
        "worktree": g.worktree,
        "objective": objective,
        "machine": hostname(),
        m.K_HB: m.hb_now(),
    }
    st.meta = meta
    title = f"[session] {g.project}@{g.branch} — {objective}"[:200]
    st.bead_id = run_bd(
        "q",
        title,
        "-t",
        "task",
        "-l",
        ",".join(
            [
                m.L_SESSION,
                m.sid_label(st.sid),
                f"project:{g.project}",
                f"branch:{g.branch}",
                f"machine:{hostname()}",
            ]
        ),
        actor=st.sid,
    ).strip()
    # --no-history: this bead's description is rewritten every HB_BUCKET for the
    # session's whole life, and none of those revisions are worth a Dolt commit.
    # Measured: 0 commits per heartbeat with the flag, ~1 without.
    #
    # Caveat: bd implements this by demoting the bead to a *wisp* (verified:
    # "bd: demote <id> to wisp" in dolt_log, and the row moves to the `wisps`
    # table). Verified that `bd query label=... AND status=open` still returns it
    # with its description intact, so list_sessions/_reap_stale are unaffected —
    # and mailbox already depends on that for its ephemeral message beads. What
    # is NOT established is whether wisp TTL compaction can reap a *live*
    # session's bead; if presence ever starts dropping sessions that are still
    # heartbeating, this flag is the first thing to suspect.
    run_bd("update", st.bead_id, "--no-history", actor=st.sid, check=False)
    run_bd("update", st.bead_id, "-d", json.dumps(meta), actor=st.sid, check=False)
    run_bd(
        "set-state",
        st.bead_id,
        f"{m.D_STATUS}=active",
        "--reason",
        "start",
        actor=st.sid,
    )
    with st._lock:
        lead = L.heartbeat_leader(st.sid, g.branch, st.sid)
        run_bd(
            "set-state",
            st.bead_id,
            f"{m.D_ROLE}={lead['role']}",
            "--reason",
            "start",
            actor=st.sid,
            check=False,
        )
        st.role = lead["role"]
    _reap_stale(st.sid)
    _seed_seen(st)  # only push messages that arrive after we join
    _ensure_background_threads()
    return {
        "sid": st.sid,
        "bead_id": st.bead_id,
        "role": lead["role"],
        "leader": L.read_leader(st.sid),
        "channel": "live" if ch.is_live(st.channel) else "not-registered-as-channel",
    }


@mcp.tool
def heartbeat() -> dict:
    """Manually pump a heartbeat and return role + inbox (the background
    thread heartbeats automatically; call it to force a fresh read)."""
    st = _current_state()
    r = _heartbeat_once(st)
    return {
        **r,
        "leader": L.read_leader(st.sid),
        "inbox": _poll_inbox(st, mark_read=False),
    }


def _update_objective(st: _SessionState, objective: str) -> dict:
    if not st.bead_id:
        return {"ok": False, "error": "not registered"}
    g = st.git
    st.meta["objective"] = objective
    run_bd(
        "update",
        st.bead_id,
        "--title",
        f"[session] {g.project}@{g.branch} — {objective}"[:200],
        "-d",
        json.dumps(st.meta),
        actor=st.sid,
    )
    run_bd("note", st.bead_id, f"objective: {objective}", actor=st.sid, check=False)
    return {"ok": True}


@mcp.tool
def update_objective(objective: str) -> dict:
    """Update this session's advertised objective."""
    return _update_objective(_current_state(), objective)


@mcp.tool
def set_status(status: str) -> dict:
    """Set this session's status: active | idle | blocked | done."""
    if status not in ("active", "idle", "blocked", "done"):
        return {"ok": False, "error": "bad status"}
    st = _current_state()
    if (err := _require_registered(st)) is not None:
        return err
    run_bd(
        "set-state",
        st.bead_id,
        f"{m.D_STATUS}={status}",
        "--reason",
        "set_status",
        actor=st.sid,
    )
    return {"ok": True}


@mcp.tool
def list_sessions(include_stale: bool = False, project: str | None = None) -> list:
    """List other live Claude sessions: who is working, on what, where."""
    if project is not None and not m.valid_token(project):
        return {"ok": False, "error": "invalid project: must match [A-Za-z0-9._-]"}
    q = f"label={m.L_SESSION} AND status=open"
    if project:
        q += f" AND label=project:{project}"
    # Explicit -n: bd's default is --limit 50, and a busy machine really does run
    # dozens of sessions (measured 20 concurrent), so the default would silently
    # truncate the roster.
    out = [_session_view(r) for r in (run_bd_json("query", q, "-n", "200") or [])]
    return [v for v in out if include_stale or not v["stale"]]


@mcp.tool
def get_leader() -> dict:
    """Who is the current leader/orchestrator (the session on main)?"""
    return L.read_leader(_current_state().sid)


@mcp.tool
def claim_leadership(force: bool = False) -> dict:
    """Attempt to become leader. Only succeeds on the main branch unless force.
    force is restricted to stdio (global-tvr) — an HTTP caller force-claiming
    leadership would gain delegate() over every session."""
    st = _current_state()
    if (err := _require_registered(st)) is not None:
        return err
    if force and _http_mode():
        return {"granted": False, "reason": "force requires stdio transport"}
    with st._lock:
        return L.claim(st.sid, st.git.branch, st.sid, force=force)


@mcp.tool
def release_leadership() -> dict:
    """Voluntarily give up leadership."""
    st = _current_state()
    with st._lock:
        return L.release(st.sid, st.sid)


@mcp.tool
def broadcast(text: str, channel: str | None = None) -> dict:
    """Broadcast a message to a channel. Defaults to this session's own project
    channel, so only sessions in the same repo are interrupted. Pass
    channel="general" to reach every project on the machine — do that only when
    the other projects genuinely need to know."""
    st = _current_state()
    if (err := _require_registered(st)) is not None:
        return err
    channel = channel or st.git.project
    if not m.valid_token(channel):
        return {"ok": False, "error": "invalid channel: must match [A-Za-z0-9._-]"}
    payload = json.dumps({"text": text, "from": st.sid, "channel": channel})
    mid = create(
        f"[msg] {channel}: {text}"[:200],
        type="event",
        labels=[m.L_MESSAGE, m.channel_label(channel), m.from_label(st.sid)],
        ephemeral=True,
        description=payload,
        actor=st.sid,
    )
    return {"message_id": mid}


@mcp.tool
def read_channel(channel: str, limit: int = 20) -> list | dict:
    """Read recent messages on a channel (newest first)."""
    if not m.valid_token(channel):
        return {"ok": False, "error": "invalid channel: must match [A-Za-z0-9._-]"}
    if (err := _require_registered(_current_state())) is not None:
        return err
    # Sort and limit in bd, not just in Python: bd's default --limit 50 meant we
    # were sorting whichever arbitrary 50 rows it happened to return, so on a
    # channel with more than 50 messages "newest first" could miss the newest.
    # The Python sort stays as a deterministic tiebreak over the rows we get.
    rows = (
        run_bd_json(
            "query",
            f"label={m.L_MESSAGE} AND label={m.channel_label(channel)}",
            "--sort",
            "created",
            "-r",
            "-n",
            str(max(1, limit)),
        )
        or []
    )
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    msgs = []
    for r in rows[:limit]:
        try:
            meta = json.loads(r.get("description") or "{}")
        except (json.JSONDecodeError, TypeError):
            meta = {}
        msgs.append(
            {
                "id": r["id"],
                "from": meta.get("from"),
                "text": meta.get("text") or r.get("title"),
                "ts": r.get("created_at"),
            }
        )
    return msgs


@mcp.tool
def send_dm(to_sid: str, text: str) -> dict:
    """Send a direct message to a specific session."""
    if not m.valid_token(to_sid):
        return {"ok": False, "error": "invalid to_sid: must match [A-Za-z0-9._-]"}
    st = _current_state()
    if (err := _require_registered(st)) is not None:
        return err
    payload = json.dumps({"text": text, "from": st.sid})
    mid = create(
        f"[dm] to {to_sid}: {text}"[:200],
        type="event",
        labels=[m.L_MESSAGE, m.L_DM, m.from_label(st.sid)],
        ephemeral=True,
        description=payload,
        actor=st.sid,
    )
    try:
        run_bd("assign", mid, to_sid, actor=st.sid, check=True)
    except BdError as e:
        return {"message_id": mid, "delivered": False, "error": str(e)}
    return {"message_id": mid, "delivered": True}


def _poll_inbox(st: _SessionState, mark_read: bool = True) -> dict:
    rows = run_bd_json("query", f"assignee={st.sid} AND status=open", "-n", "200") or []
    dms, delegations, requests = [], [], []
    for r in rows:
        labels = r.get("labels") or []
        try:
            meta = json.loads(r.get("description") or "{}")
        except (json.JSONDecodeError, TypeError):
            meta = {}
        item = {
            "id": r["id"],
            "from": meta.get("from"),
            "text": meta.get("text") or r.get("title"),
        }
        if m.L_REQUEST in labels:
            # An info-request: the sender is gate-blocked. Answer with respond_info.
            requests.append(item)
        elif m.L_DELEGATION in labels:
            delegations.append(item)
        elif m.L_DM in labels:
            dms.append(item)
            if mark_read:
                run_bd("close", r["id"], actor=st.sid, check=False)
    return {"dms": dms, "delegations": delegations, "requests": requests}


@mcp.tool
def poll_inbox(mark_read: bool = True) -> dict:
    """Read messages/delegations addressed to this session. Closes DMs when
    mark_read is true (a closed DM = read)."""
    st = _current_state()
    if (err := _require_registered(st)) is not None:
        return err
    return _poll_inbox(st, mark_read=mark_read)


def _bead_status(bead_id: str) -> str | None:
    try:
        rows = run_bd_json("show", bead_id)
    except BdError:
        return None
    bead = rows[0] if isinstance(rows, list) and rows else rows
    return bead.get("status") if isinstance(bead, dict) else None


def _bead_assignee(bead_id: str) -> str | None:
    try:
        rows = run_bd_json("show", bead_id)
    except BdError:
        return None
    bead = rows[0] if isinstance(rows, list) and rows else rows
    return bead.get("assignee") if isinstance(bead, dict) else None


def _last_answer(bead_id: str) -> str | None:
    try:
        comments = run_bd_json("comments", bead_id) or []
    except BdError:
        return None
    return comments[-1].get("text") if comments else None


def _bead_from(bead_id: str) -> str | None:
    try:
        rows = run_bd_json("show", bead_id)
    except BdError:
        return None
    bead = rows[0] if isinstance(rows, list) and rows else rows
    if not isinstance(bead, dict):
        return None
    try:
        meta = json.loads(bead.get("description") or "{}")
    except (json.JSONDecodeError, TypeError):
        return None
    return meta.get("from")


@mcp.tool
def request_info(to_sid: str, question: str, timeout_s: int = 60) -> dict:
    """Ask another session a question and block (up to timeout_s) for its answer.

    Creates a request bead (not ephemeral — an unanswered question must not
    evaporate) assigned to the target (surfaces in their poll_inbox); they
    reply via respond_info, which comments the answer and closes the bead.
    Returns {request_id, answer, resolved, timed_out}. If it times out, keep the
    request_id and poll later with check_request — the request stays open.
    """
    if not m.valid_token(to_sid):
        return {"ok": False, "error": "invalid to_sid: must match [A-Za-z0-9._-]"}
    st = _current_state()
    if (err := _require_registered(st)) is not None:
        return err
    payload = json.dumps({"text": question, "from": st.sid})
    rid = create(
        f"[req] to {to_sid}: {question}"[:200],
        type="task",
        labels=[m.L_REQUEST, m.from_label(st.sid)],
        ephemeral=False,
        description=payload,
        actor=st.sid,
    )
    try:
        run_bd("assign", rid, to_sid, actor=st.sid, check=True)
    except BdError as e:
        return {"request_id": rid, "resolved": False, "error": f"assign failed: {e}"}
    deadline = time.time() + max(0, timeout_s)
    # Exponential backoff rather than a flat 3s: every probe is a fresh `bd show`
    # subprocess (~0.3s of Go startup before it even opens the DB), so a 60s
    # request cost ~20 spawns while blocking. Starts tighter than the old 3s, so
    # a fast answer is actually noticed sooner, then decays.
    delay = 1.0
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(delay, remaining))
        if _bead_status(rid) == "closed":
            return {
                "request_id": rid,
                "answer": _last_answer(rid),
                "resolved": True,
                "timed_out": False,
            }
        delay = min(delay * 1.7, 15.0)
    return {"request_id": rid, "answer": None, "resolved": False, "timed_out": True}


@mcp.tool
def respond_info(request_id: str, answer: str) -> dict:
    """Answer an info-request (from poll_inbox 'requests'): comment + close,
    which unblocks the asking session. Only the session the request is
    assigned to may answer it (global-5yn) — otherwise any connection could
    forge an answer to another agent's blocking request_info call."""
    st = _current_state()
    if (err := _require_registered(st)) is not None:
        return err
    if _bead_assignee(request_id) != st.sid:
        return {"ok": False, "error": "request not assigned to you"}
    run_bd("comment", request_id, answer, actor=st.sid)
    run_bd("close", request_id, actor=st.sid, check=False)
    return {"ok": True}


@mcp.tool
def check_request(request_id: str) -> dict:
    """Non-blocking: has an info-request been answered yet? Only the session
    that created the request may poll it — otherwise any connection could
    read another agent's answer by guessing/enumerating request_ids."""
    st = _current_state()
    if _bead_from(request_id) != st.sid:
        return {"resolved": False, "answer": None, "gone": True}
    status = _bead_status(request_id)
    if status is None:
        return {"resolved": False, "answer": None, "gone": True}
    resolved = status == "closed"
    return {
        "resolved": resolved,
        "answer": _last_answer(request_id) if resolved else None,
    }


@mcp.tool
def delegate(to_sid: str, title: str, detail: str = "", priority: int = 2) -> dict:
    """Leader-only: assign a work item to a secondary session."""
    if not m.valid_token(to_sid):
        return {"ok": False, "error": "invalid to_sid: must match [A-Za-z0-9._-]"}
    st = _current_state()
    if (err := _require_registered(st)) is not None:
        return err
    # Re-read the leader slot on every call (not cached) to guard against a stale
    # leadership belief — e.g. this session lost leadership since it last checked.
    lead = L.read_leader(st.sid)
    if lead.get("leader_sid") != st.sid:
        return {"ok": False, "error": "not the leader"}
    tid = run_bd(
        "q",
        title[:200],
        "-t",
        "task",
        "-p",
        str(priority),
        "-l",
        ",".join([m.L_DELEGATION, m.from_label(st.sid)]),
        actor=st.sid,
    ).strip()
    if detail:
        run_bd("update", tid, "-d", detail, actor=st.sid, check=False)
    run_bd("assign", tid, to_sid, actor=st.sid, check=False)
    return {"ok": True, "task_id": tid}


@mcp.tool
def deregister() -> dict:
    """Cleanly leave the mailbox: release leadership, mark done, close bead."""
    return _deregister_state(_current_state())


def _deregister_state(st: _SessionState) -> dict:
    if not st.bead_id:
        return {"ok": True}
    with st._lock:
        L.release(st.sid, st.sid)
    run_bd(
        "set-state",
        st.bead_id,
        f"{m.D_STATUS}=done",
        "--reason",
        "exit",
        actor=st.sid,
        check=False,
    )
    run_bd("close", st.bead_id, actor=st.sid, check=False)
    st.bead_id = None
    return {"ok": True}


def _deregister_all() -> None:
    """Process-shutdown path: deregister every tracked connection. For stdio
    there's exactly one, so this is the same cleanup as before; for HTTP it
    covers every connection this process still knows about."""
    _stop.set()
    for st in list(_SESSIONS.values()):
        _deregister_state(st)


atexit.register(_deregister_all)


def _sig(_signum, _frame):
    _deregister_all()
    sys.exit(0)


# ── HTTP bearer-token auth (design: global-4pm P0) ──────────────────────────
# stdio inherits the launching OS user's trust already, so it's exempt; HTTP
# binds a TCP port any local (or LAN) process can reach, so it's gated.
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _load_token() -> str | None:
    if "MAILBOX_TOKEN" in os.environ:
        token = os.environ["MAILBOX_TOKEN"].strip()
        if not token:
            raise SystemExit("MAILBOX_TOKEN is set but empty")
        return token
    path = os.environ.get("MAILBOX_TOKEN_FILE")
    if path:
        with open(path) as f:
            token = f.read().strip()
        if not token:
            raise SystemExit(f"MAILBOX_TOKEN_FILE={path!r} is empty")
        return token
    return None


class _BearerTokenVerifier(TokenVerifier):
    """Single shared deployment token, constant-time compare."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token.encode()

    async def verify_token(self, token: str) -> AccessToken | None:
        # compare as bytes: secrets.compare_digest rejects non-ASCII str input,
        # which would otherwise turn a garbage bearer token into a 500 instead
        # of a clean 401.
        if not secrets.compare_digest(token.encode(), self._token):
            return None
        return AccessToken(token=token, client_id="mailbox", scopes=[])


def main() -> None:
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    if os.environ.get("MAILBOX_TRANSPORT", "stdio") == "http":
        host = os.environ.get("MAILBOX_HTTP_HOST", "127.0.0.1")
        token = _load_token()
        if not token and host not in _LOOPBACK_HOSTS:
            raise SystemExit(
                f"refusing to start: MAILBOX_HTTP_HOST={host!r} is not loopback and "
                "no MAILBOX_TOKEN/MAILBOX_TOKEN_FILE is set — set one of them or bind "
                "to 127.0.0.1"
            )
        if not token:
            logger.warning(
                "MAILBOX_TRANSPORT=http with no MAILBOX_TOKEN set — any local process "
                "on this machine can reach the mailbox. Set MAILBOX_TOKEN before "
                "relying on this beyond a trusted single-user machine."
            )
        # `auth` is a FastMCP constructor attribute, not a run()/run_http_async()
        # kwarg — it must be set on the server object before run() builds the
        # ASGI app, or HTTP auth is silently never enforced.
        mcp.auth = _BearerTokenVerifier(token) if token else None
        mcp.run(
            transport="http",
            host=host,
            port=int(os.environ.get("MAILBOX_HTTP_PORT", "8000")),
        )
    else:
        mcp.run()


if __name__ == "__main__":
    main()
