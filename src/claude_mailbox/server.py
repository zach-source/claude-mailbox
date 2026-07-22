"""Claude Code Mailbox — FastMCP server.

Lets concurrent Claude Code sessions register, discover each other, cross-talk
over channels, DM, and coordinate under a single main-branch leader. All state
lives in the shared `beads_global` Dolt DB via the `bd` CLI (see bd.py).

A background thread heartbeats every HB_BUCKET seconds so liveness/leadership
don't depend on the model remembering to call anything.
"""

from __future__ import annotations

import atexit
import json
import threading
import time

from fastmcp import FastMCP

from . import leader as L
from . import model as m
from .bd import create, run_bd, run_bd_json
from .identity import GitContext, detect_git, hostname, new_sid

mcp = FastMCP("claude-mailbox")


class _State:
    def __init__(self) -> None:
        self.sid = new_sid()
        self.git: GitContext = detect_git()
        self.bead_id: str | None = None
        self._hb_thread: threading.Thread | None = None
        self._stop = threading.Event()


S = _State()


# ── helpers ──────────────────────────────────────────────────────────────────
def _find_session(sid: str) -> dict | None:
    rows = run_bd_json("query", f"label={m.sid_label(sid)}") or []
    return rows[0] if rows else None


def _session_view(bead: dict) -> dict:
    st = L.states_of(bead)
    desc = bead.get("description") or "{}"
    try:
        meta = json.loads(desc)
    except (json.JSONDecodeError, TypeError):
        meta = {}
    hb = st.get(m.D_HB)
    hb_int = int(hb) if hb and str(hb).isdigit() else None
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


def _heartbeat_once() -> dict:
    if not S.bead_id:
        return {"ok": False}
    S.git = detect_git()
    run_bd(
        "set-state", S.bead_id, f"{m.D_HB}={m.hb_now()}", "--reason", "hb", actor=S.sid
    )
    lead = L.heartbeat_leader(S.sid, S.git.branch, S.sid)
    run_bd(
        "set-state",
        S.bead_id,
        f"{m.D_ROLE}={lead['role']}",
        "--reason",
        "role sync",
        actor=S.sid,
        check=False,
    )
    return {"ok": True, "role": lead["role"]}


def _hb_loop() -> None:
    while not S._stop.wait(m.HB_BUCKET):
        try:
            _heartbeat_once()
        except Exception:  # never let the daemon thread die on a transient bd error
            pass


def _reap_stale() -> None:
    """Close session beads whose heartbeat is >10x stale (crashed sessions)."""
    for row in run_bd_json("query", f"label={m.L_SESSION} AND status=open") or []:
        st = L.states_of(row)
        hb = st.get(m.D_HB)
        hb_int = int(hb) if hb and str(hb).isdigit() else None
        if m.hb_age_seconds(hb_int) > m.STALE_SECONDS * 10:
            run_bd(
                "set-state",
                row["id"],
                f"{m.D_STATUS}=done",
                "--reason",
                "reaped: stale",
                actor=S.sid,
                check=False,
            )
            run_bd("close", row["id"], actor=S.sid, check=False)


# ── tools ────────────────────────────────────────────────────────────────────
@mcp.tool
def register_session(objective: str) -> dict:
    """Register this Claude session in the mailbox and start heartbeating.

    project/branch/worktree are auto-detected from git. Auto-claims leadership
    if on the main branch. Idempotent for the process lifetime.
    """
    g = S.git = detect_git()
    if S.bead_id:  # already registered — just refresh objective
        return update_objective(objective)
    meta = {
        "sid": S.sid,
        "project": g.project,
        "branch": g.branch,
        "worktree": g.worktree,
        "objective": objective,
        "machine": hostname(),
    }
    title = f"[session] {g.project}@{g.branch} — {objective}"[:200]
    S.bead_id = run_bd(
        "q",
        title,
        "-t",
        "task",
        "-l",
        ",".join(
            [
                m.L_SESSION,
                m.sid_label(S.sid),
                f"project:{g.project}",
                f"branch:{g.branch}",
                f"machine:{hostname()}",
            ]
        ),
        actor=S.sid,
    ).strip()
    run_bd("update", S.bead_id, "-d", json.dumps(meta), actor=S.sid, check=False)
    run_bd(
        "set-state", S.bead_id, f"{m.D_STATUS}=active", "--reason", "start", actor=S.sid
    )
    run_bd(
        "set-state",
        S.bead_id,
        f"{m.D_HB}={m.hb_now()}",
        "--reason",
        "start",
        actor=S.sid,
    )
    lead = L.heartbeat_leader(S.sid, g.branch, S.sid)
    run_bd(
        "set-state",
        S.bead_id,
        f"{m.D_ROLE}={lead['role']}",
        "--reason",
        "start",
        actor=S.sid,
        check=False,
    )
    _reap_stale()
    if not S._hb_thread:
        S._hb_thread = threading.Thread(target=_hb_loop, daemon=True)
        S._hb_thread.start()
    return {
        "sid": S.sid,
        "bead_id": S.bead_id,
        "role": lead["role"],
        "leader": L.read_leader(S.sid),
    }


@mcp.tool
def heartbeat() -> dict:
    """Manually pump a heartbeat and return role + inbox count (the background
    thread does this automatically; call it to force a fresh read)."""
    r = _heartbeat_once()
    inbox = poll_inbox(mark_read=False)
    return {
        **r,
        "leader": L.read_leader(S.sid),
        "inbox_count": sum(len(v) for v in inbox.values() if isinstance(v, list)),
    }


@mcp.tool
def update_objective(objective: str) -> dict:
    """Update this session's advertised objective."""
    if not S.bead_id:
        return {"ok": False, "error": "not registered"}
    g = S.git
    run_bd(
        "update",
        S.bead_id,
        "--title",
        f"[session] {g.project}@{g.branch} — {objective}"[:200],
        actor=S.sid,
    )
    run_bd("note", S.bead_id, f"objective: {objective}", actor=S.sid, check=False)
    return {"ok": True}


@mcp.tool
def set_status(status: str) -> dict:
    """Set this session's status: active | idle | blocked | done."""
    if status not in ("active", "idle", "blocked", "done"):
        return {"ok": False, "error": "bad status"}
    if not S.bead_id:
        return {"ok": False, "error": "not registered"}
    run_bd(
        "set-state",
        S.bead_id,
        f"{m.D_STATUS}={status}",
        "--reason",
        "set_status",
        actor=S.sid,
    )
    return {"ok": True}


@mcp.tool
def list_sessions(include_stale: bool = False, project: str | None = None) -> list:
    """List other live Claude sessions: who is working, on what, where."""
    q = f"label={m.L_SESSION} AND status=open"
    if project:
        q += f" AND label=project:{project}"
    out = [_session_view(r) for r in (run_bd_json("query", q) or [])]
    return [v for v in out if include_stale or not v["stale"]]


@mcp.tool
def get_leader() -> dict:
    """Who is the current leader/orchestrator (the session on main)?"""
    return L.read_leader(S.sid)


@mcp.tool
def claim_leadership(force: bool = False) -> dict:
    """Attempt to become leader. Only succeeds on the main branch unless force."""
    return L.claim(S.sid, S.git.branch, S.sid, force=force)


@mcp.tool
def release_leadership() -> dict:
    """Voluntarily give up leadership."""
    return L.release(S.sid, S.sid)


@mcp.tool
def broadcast(text: str, channel: str = "general") -> dict:
    """Broadcast a message to a channel that all sessions can read."""
    payload = json.dumps({"text": text, "from": S.sid, "channel": channel})
    mid = create(
        f"[msg] {channel}: {text}"[:200],
        type="event",
        labels=[m.L_MESSAGE, m.channel_label(channel), m.from_label(S.sid)],
        ephemeral=True,
        description=payload,
        actor=S.sid,
    )
    return {"message_id": mid}


@mcp.tool
def read_channel(channel: str, limit: int = 20) -> list:
    """Read recent messages on a channel (newest first)."""
    rows = (
        run_bd_json(
            "query", f"label={m.L_MESSAGE} AND label={m.channel_label(channel)}"
        )
        or []
    )
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
            }
        )
    return msgs


@mcp.tool
def send_dm(to_sid: str, text: str) -> dict:
    """Send a direct message to a specific session."""
    payload = json.dumps({"text": text, "from": S.sid})
    mid = create(
        f"[dm] to {to_sid}: {text}"[:200],
        type="event",
        labels=[m.L_MESSAGE, m.L_DM, m.from_label(S.sid)],
        ephemeral=True,
        description=payload,
        actor=S.sid,
    )
    run_bd("assign", mid, to_sid, actor=S.sid, check=False)
    return {"message_id": mid}


@mcp.tool
def poll_inbox(mark_read: bool = True) -> dict:
    """Read messages/delegations addressed to this session. Closes DMs when
    mark_read is true (a closed DM = read)."""
    rows = run_bd_json("query", f"assignee={S.sid} AND status=open") or []
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
                run_bd("close", r["id"], actor=S.sid, check=False)
    return {"dms": dms, "delegations": delegations, "requests": requests}


def _bead_status(bead_id: str) -> str | None:
    rows = run_bd_json("show", bead_id)
    bead = rows[0] if isinstance(rows, list) and rows else rows
    return bead.get("status") if isinstance(bead, dict) else None


def _last_answer(bead_id: str) -> str | None:
    comments = run_bd_json("comments", bead_id) or []
    return comments[-1].get("text") if comments else None


@mcp.tool
def request_info(to_sid: str, question: str, timeout_s: int = 60) -> dict:
    """Ask another session a question and block (up to timeout_s) for its answer.

    Creates a request bead assigned to the target (surfaces in their poll_inbox);
    they reply via respond_info, which comments the answer and closes the bead.
    Returns {request_id, answer, resolved, timed_out}. If it times out, keep the
    request_id and poll later with check_request — the request stays open.
    """
    payload = json.dumps({"text": question, "from": S.sid})
    rid = create(
        f"[req] to {to_sid}: {question}"[:200],
        type="task",
        labels=[m.L_REQUEST, m.from_label(S.sid)],
        ephemeral=True,
        description=payload,
        actor=S.sid,
    )
    run_bd("assign", rid, to_sid, actor=S.sid, check=False)
    deadline = time.time() + max(0, timeout_s)
    while time.time() < deadline:
        time.sleep(3)
        if _bead_status(rid) == "closed":
            return {
                "request_id": rid,
                "answer": _last_answer(rid),
                "resolved": True,
                "timed_out": False,
            }
    return {"request_id": rid, "answer": None, "resolved": False, "timed_out": True}


@mcp.tool
def respond_info(request_id: str, answer: str) -> dict:
    """Answer an info-request (from poll_inbox 'requests'): comment + close,
    which unblocks the asking session."""
    run_bd("comment", request_id, answer, actor=S.sid)
    run_bd("close", request_id, actor=S.sid, check=False)
    return {"ok": True}


@mcp.tool
def check_request(request_id: str) -> dict:
    """Non-blocking: has an info-request been answered yet?"""
    resolved = _bead_status(request_id) == "closed"
    return {
        "resolved": resolved,
        "answer": _last_answer(request_id) if resolved else None,
    }


@mcp.tool
def delegate(to_sid: str, title: str, detail: str = "", priority: int = 2) -> dict:
    """Leader-only: assign a work item to a secondary session."""
    lead = L.read_leader(S.sid)
    if lead.get("leader_sid") != S.sid:
        return {"ok": False, "error": "not the leader"}
    tid = run_bd(
        "q",
        title[:200],
        "-t",
        "task",
        "-p",
        str(priority),
        "-l",
        ",".join([m.L_DELEGATION, m.from_label(S.sid)]),
        actor=S.sid,
    ).strip()
    if detail:
        run_bd("update", tid, "-d", detail, actor=S.sid, check=False)
    run_bd("assign", tid, to_sid, actor=S.sid, check=False)
    return {"ok": True, "task_id": tid}


@mcp.tool
def deregister() -> dict:
    """Cleanly leave the mailbox: release leadership, mark done, close bead."""
    return _deregister()


def _deregister() -> dict:
    S._stop.set()
    if not S.bead_id:
        return {"ok": True}
    L.release(S.sid, S.sid)
    run_bd(
        "set-state",
        S.bead_id,
        f"{m.D_STATUS}=done",
        "--reason",
        "exit",
        actor=S.sid,
        check=False,
    )
    run_bd("close", S.bead_id, actor=S.sid, check=False)
    S.bead_id = None
    return {"ok": True}


atexit.register(_deregister)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
