"""`mailbox` convenience CLI — same bd-backed operations as the MCP tools, for
use from herdr panes, shell hooks, and debugging (no agent required).

Note: unlike the MCP server, the CLI is stateless (one-shot); it does not run a
heartbeat. `mailbox register` is meant for a SessionStart hook that also arranges
periodic `mailbox heartbeat` (or just let the MCP server own liveness).
"""

from __future__ import annotations

import argparse
import json
import sys

from . import leader as L
from . import model as m
from .bd import create, run_bd, run_bd_json
from .identity import detect_git, hostname, new_sid


def _who(_args) -> int:
    q = f"label={m.L_SESSION} AND status=open"
    rows = run_bd_json("query", q) or []
    if not rows:
        print("no live sessions")
        return 0
    for r in rows:
        st = L.states_of(r)
        try:
            meta = json.loads(r.get("description") or "{}")
        except (json.JSONDecodeError, TypeError):
            meta = {}
        print(
            f"{st.get(m.D_ROLE,'?'):9} {st.get(m.D_STATUS,'?'):8} "
            f"{meta.get('project','?')}@{meta.get('branch','?')}  "
            f"{meta.get('objective','')}  ({meta.get('sid','?')})"
        )
    return 0


def _leader(_args) -> int:
    print(json.dumps(L.read_leader("cli"), indent=2))
    return 0


def _say(args) -> int:
    if not m.valid_token(args.channel):
        print("error: invalid channel: must match [A-Za-z0-9._-]", file=sys.stderr)
        return 2
    sid = f"cli-{hostname()}"
    payload = json.dumps({"text": args.text, "from": sid, "channel": args.channel})
    mid = create(
        f"[msg] {args.channel}: {args.text}"[:200],
        type="event",
        labels=[m.L_MESSAGE, m.channel_label(args.channel), m.from_label(sid)],
        ephemeral=True,
        description=payload,
        actor=sid,
    )
    print(mid)
    return 0


def _inbox(args) -> int:
    sid = args.sid or f"cli-{hostname()}"
    if not m.valid_token(sid):
        print("error: invalid sid: must match [A-Za-z0-9._-]", file=sys.stderr)
        return 2
    rows = run_bd_json("query", f"assignee={sid} AND status=open") or []
    for r in rows:
        print(f"{r['id']}  {r.get('title','')}")
        if args.ack and m.L_DM in (r.get("labels") or []):
            run_bd("close", r["id"], actor=sid, check=False)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="mailbox", description="Claude Code mailbox")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("who", help="list live sessions").set_defaults(fn=_who)
    sub.add_parser("leader", help="show current leader").set_defaults(fn=_leader)

    say = sub.add_parser("say", help="broadcast to a channel")
    say.add_argument("text")
    say.add_argument("-c", "--channel", default="general")
    say.set_defaults(fn=_say)

    inb = sub.add_parser("inbox", help="show messages addressed to a sid")
    inb.add_argument("--sid")
    inb.add_argument("--ack", action="store_true", help="close after reading")
    inb.set_defaults(fn=_inbox)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
