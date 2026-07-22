"""Leadership election over a single well-known 'leader-slot' bead.

Invariant: at most one leader; the session on `main` wins. The slot bead's
state dimensions (leader / leader-branch / leader-hb) are the source of truth —
`bd set-state` is atomic and event-backed, so a losing racer is always
detectable on read-back. Tiebreak on simultaneous claims: lexicographically
smallest sid wins.
"""

from __future__ import annotations

import time

from . import model as m
from .bd import run_bd, run_bd_json

# Stable identity for the singleton slot so concurrent first-use converges.
_SLOT_REF = "mailbox-leader-slot"


def _labels(bead: dict) -> list[str]:
    return bead.get("labels") or []


def states_of(bead: dict) -> dict[str, str]:
    """Extract state dimensions from a bead's `dimension:value` labels."""
    known = {m.D_STATUS, m.D_ROLE, m.D_HB, m.D_LEADER, m.D_LEADER_BRANCH, m.D_LEADER_HB}
    out: dict[str, str] = {}
    for lbl in _labels(bead):
        if ":" in lbl:
            dim, _, val = lbl.partition(":")
            if dim in known:
                out[dim] = val
    return out


def _find_slot() -> dict | None:
    rows = run_bd_json("query", f"label={m.L_LEADER_SLOT}") or []
    return rows[0] if rows else None


def ensure_slot(actor: str) -> str:
    """Return the slot bead id, creating it if absent."""
    slot = _find_slot()
    if slot:
        return slot["id"]
    bid = run_bd(
        "q",
        "[mailbox] leader-slot",
        "-t",
        "task",
        "-l",
        m.L_LEADER_SLOT,
        actor=actor,
    ).strip()
    run_bd(
        "set-state", bid, f"{m.D_LEADER}=vacant", "--reason", "slot init", actor=actor
    )
    return bid


def read_leader(actor: str) -> dict:
    slot = _find_slot()
    if not slot:
        return {"vacant": True, "leader_sid": None, "branch": None, "stale": True}
    st = states_of(slot)
    leader = st.get(m.D_LEADER, "vacant")
    hb = st.get(m.D_LEADER_HB)
    hb_int = int(hb) if hb and hb.isdigit() else None
    vacant = leader == "vacant" or not leader
    return {
        "vacant": vacant,
        "leader_sid": None if vacant else leader,
        "branch": st.get(m.D_LEADER_BRANCH),
        "last_hb_age_s": None if vacant else m.hb_age_seconds(hb_int),
        "stale": (not vacant) and m.is_stale(hb_int),
    }


def claim(sid: str, branch: str, actor: str, force: bool = False) -> dict:
    """Try to become leader. Only main-branch sessions claim (unless force)."""
    if branch != m.LEADER_BRANCH and not force:
        return {"granted": False, "reason": f"not on {m.LEADER_BRANCH}"}

    bid = ensure_slot(actor)
    cur = read_leader(actor)
    if not cur["vacant"] and not cur["stale"] and not force:
        if cur["leader_sid"] == sid:
            _refresh(bid, sid, branch, actor)
            return {"granted": True, "reason": "already leader"}
        return {
            "granted": False,
            "reason": "leader active",
            "current_leader": cur["leader_sid"],
        }

    # Claim, then settle + read back for split-brain detection.
    _write_leader(bid, sid, branch, actor, reason="claim")
    time.sleep(1.0)
    after = read_leader(actor)
    winner = after["leader_sid"]
    if winner and winner != sid:
        # Someone else's write landed after ours. Smallest-sid tiebreak.
        if sid < winner:
            _write_leader(bid, sid, branch, actor, reason="tiebreak-win")
            return {"granted": True, "reason": "won tiebreak"}
        return {"granted": False, "reason": "lost tiebreak", "current_leader": winner}
    return {"granted": True, "reason": "claimed"}


def _write_leader(bid: str, sid: str, branch: str, actor: str, reason: str):
    run_bd("set-state", bid, f"{m.D_LEADER}={sid}", "--reason", reason, actor=actor)
    run_bd(
        "set-state",
        bid,
        f"{m.D_LEADER_BRANCH}={branch}",
        "--reason",
        reason,
        actor=actor,
    )
    _refresh(bid, sid, branch, actor)


def _refresh(bid: str, sid: str, branch: str, actor: str):
    run_bd(
        "set-state",
        bid,
        f"{m.D_LEADER_HB}={m.hb_now()}",
        "--reason",
        "leader hb",
        actor=actor,
    )


def heartbeat_leader(sid: str, branch: str, actor: str) -> dict:
    """Called each heartbeat by a session. Maintains or (re)acquires leadership
    when on main; yields it when off main."""
    cur = read_leader(actor)
    if branch == m.LEADER_BRANCH:
        if cur["leader_sid"] == sid:
            bid = ensure_slot(actor)
            _refresh(bid, sid, branch, actor)
            return {"role": "leader", **cur, "leader_sid": sid}
        if cur["vacant"] or cur["stale"]:
            return {
                "role": (
                    "leader" if claim(sid, branch, actor)["granted"] else "secondary"
                ),
                **read_leader(actor),
            }
    elif cur["leader_sid"] == sid:
        # We led but left main — vacate.
        release(sid, actor)
        return {"role": "secondary", **read_leader(actor)}
    return {"role": "secondary", **cur}


def release(sid: str, actor: str) -> dict:
    slot = _find_slot()
    if not slot:
        return {"ok": True}
    st = states_of(slot)
    if st.get(m.D_LEADER) == sid:
        run_bd(
            "set-state",
            slot["id"],
            f"{m.D_LEADER}=vacant",
            "--reason",
            "release",
            actor=actor,
        )
    return {"ok": True}
