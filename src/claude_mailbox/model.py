"""Naming conventions + small helpers mapping mailbox concepts onto beads.

Everything lives in `beads_global`. bd query syntax is `field=value` joined with
AND/OR (NOT `field:value`), e.g. `label=mailbox:session AND status=open`.
"""

from __future__ import annotations

import time

# ── label / type conventions ────────────────────────────────────────────────
L_SESSION = "mailbox:session"  # a session presence bead
L_MESSAGE = "mailbox:message"  # a broadcast or DM (ephemeral event bead)
L_LEADER_SLOT = "mailbox:leader-slot"  # the singleton leadership lock bead
L_DM = "dm"  # marks a message as a direct message
L_DELEGATION = "mailbox:delegation"  # leader → secondary work item

# state dimensions (bd set-state <id> <dim>=<val>)
D_STATUS = "status"  # active | idle | blocked | done
D_ROLE = "role"  # leader | secondary
D_HB = "hb"  # heartbeat: unix-epoch // HB_BUCKET
D_LEADER = "leader"  # on the slot bead: <sid> | vacant
D_LEADER_BRANCH = "leader-branch"
D_LEADER_HB = "leader-hb"

LEADER_BRANCH = "main"  # only a session on this branch may lead
HB_BUCKET = 30  # heartbeat granularity, seconds
STALE_BEATS = 3  # missed beats before a session/leader is stale
STALE_SECONDS = HB_BUCKET * STALE_BEATS


def hb_now() -> int:
    return int(time.time()) // HB_BUCKET


def hb_age_seconds(hb_bucket: int | None) -> float:
    if hb_bucket is None:
        return float("inf")
    return time.time() - (int(hb_bucket) * HB_BUCKET)


def is_stale(hb_bucket: int | None) -> bool:
    return hb_age_seconds(hb_bucket) > STALE_SECONDS


def sid_label(sid: str) -> str:
    return f"session:{sid}"


def channel_label(name: str) -> str:
    return f"channel:{name}"


def from_label(sid: str) -> str:
    return f"from:{sid}"
