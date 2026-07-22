"""Naming conventions + small helpers mapping mailbox concepts onto beads.

Everything lives in `beads_global`. bd query syntax is `field=value` joined with
AND/OR (NOT `field:value`), e.g. `label=mailbox:session AND status=open`.
"""

from __future__ import annotations

import re
import time

# ── label / type conventions ────────────────────────────────────────────────
L_SESSION = "mailbox:session"  # a session presence bead
L_MESSAGE = "mailbox:message"  # a broadcast or DM (ephemeral event bead)
L_LEADER_SLOT = "mailbox:leader-slot"  # the singleton leadership lock bead
L_DM = "dm"  # marks a message as a direct message
L_DELEGATION = "mailbox:delegation"  # leader → secondary work item
L_REQUEST = "mailbox:request"  # blocking info-request (answered via comment + close)

# state dimensions (bd set-state <id> <dim>=<val>)
D_STATUS = "status"  # active | idle | blocked | done
D_ROLE = "role"  # leader | secondary
D_HB = "hb"  # heartbeat: raw unix-epoch seconds
D_LEADER = "leader"  # on the slot bead: <sid> | vacant
D_LEADER_BRANCH = "leader-branch"
D_LEADER_HB = "leader-hb"

LEADER_BRANCH = "main"  # only a session on this branch may lead
HB_BUCKET = 30  # heartbeat granularity, seconds
STALE_BEATS = 3  # missed beats before a session/leader is stale
STALE_SECONDS = HB_BUCKET * STALE_BEATS


def hb_now() -> int:
    return int(time.time())


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
