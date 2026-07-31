"""Naming conventions + small helpers mapping mailbox concepts onto beads.

Everything lives in `beads_global`. bd query syntax is `field=value` joined with
AND/OR (NOT `field:value`), e.g. `label=mailbox:session AND status=open`.
"""

from __future__ import annotations

import json
import re
import time

# ── label / type conventions ────────────────────────────────────────────────
L_SESSION = "mailbox:session"  # a session presence bead
L_MESSAGE = "mailbox:message"  # a broadcast or DM (ephemeral event bead)
L_LEADER_SLOT = "mailbox:leader-slot"  # the singleton leadership lock bead
L_DM = "dm"  # marks a message as a direct message
L_DELEGATION = "mailbox:delegation"  # leader → secondary work item
L_REQUEST = "mailbox:request"  # blocking info-request (answered via comment + close)

# state dimensions (bd set-state <id> <dim>=<val>). Reserved for *low-cardinality*
# facts only: set-state mints an event bead and rewrites a `<dim>:<val>` label on
# every call, so a value that changes each beat costs ~3 Dolt commits + one issue
# row per write. That is how heartbeats once grew to 99% of beads_global (21,320
# of 21,353 issues, 85k Dolt commits, 2.9GB) — see K_HB below.
D_STATUS = "status"  # active | idle | blocked | done
D_ROLE = "role"  # leader | secondary
D_LEADER = "leader"  # on the slot bead: <sid> | vacant
D_LEADER_BRANCH = "leader-branch"

# Heartbeats are raw unix-epoch seconds kept in the bead's *description JSON*
# (see meta_of), NOT as a state dimension: a monotonic timestamp has no audit
# value, and `bd update -d` is a single write with no event bead and no label
# churn. The session bead carries K_HB; the leader slot bead carries K_LEADER_HB.
K_HB = "hb"
K_LEADER_HB = "leader_hb"

LEADER_BRANCH = "main"  # only a session on this branch may lead
HB_BUCKET = 60  # heartbeat granularity, seconds
STALE_BEATS = 3  # missed beats before a session/leader is stale
STALE_SECONDS = HB_BUCKET * STALE_BEATS


def hb_now() -> int:
    return int(time.time())


def meta_of(bead: dict) -> dict:
    """Parse a bead's description as the JSON blob mailbox stores there."""
    try:
        meta = json.loads(bead.get("description") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return meta if isinstance(meta, dict) else {}


def hb_of(bead: dict, key: str = K_HB) -> int | None:
    """Read a heartbeat epoch out of a bead's description JSON, or None."""
    v = meta_of(bead).get(key)
    if isinstance(v, bool):  # bool is an int subclass — not a timestamp
        return None
    if isinstance(v, int):
        return v
    return int(v) if isinstance(v, str) and v.isdigit() else None


def hb_age_seconds(hb: int | None) -> float:
    if hb is None:
        return float("inf")
    return time.time() - int(hb)


def is_stale(hb: int | None) -> bool:
    return hb_age_seconds(hb) > STALE_SECONDS


_TOKEN_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def valid_token(s: str) -> bool:
    return bool(s) and bool(_TOKEN_RE.match(s))


def sid_label(sid: str) -> str:
    return f"session:{sid}"


def channel_label(name: str) -> str:
    return f"channel:{name}"


def from_label(sid: str) -> str:
    return f"from:{sid}"
