---
name: mailbox-leader
description: >-
  Orchestrate the other Claude Code sessions when you are the mailbox leader (the
  session on main). Use when get_leader() shows you are the leader, or when the
  user says "delegate", "coordinate the sessions", "orchestrate", "assign that to
  another session", or asks you to act as the lead across worktrees.
---

# Mailbox leader / orchestrator

You are the leader when `get_leader().leader_sid` is your session (you're on
`main`). There is at most one leader; if you leave `main`, release leadership so
another main session can take over.

## Responsibilities
1. **Know the field** — `list_sessions()` to see every active session, its project,
   worktree, branch, objective, and status. Stale sessions are marked; ignore them.
2. **Delegate instead of doing everything** — `delegate(to_sid, title, detail,
   priority)` creates an assigned work item (a `mailbox:delegation` bead) for a
   secondary. The open delegation beads ARE your ledger — query/track them rather
   than keeping state in your head.
3. **Coordinate shared resources** — serialize risky shared work (main merges,
   `flake.lock`, deploys). Announce plans on `broadcast(text, channel="leader")`
   so secondaries hold.
4. **Answer requests** — secondaries will `request_info`/`send_dm` you before
   shared actions; `poll_inbox()` and respond so they aren't blocked.
5. **Hand off cleanly** — if you switch off `main`, `release_leadership()` (the
   heartbeat does this automatically when it detects you left main) and broadcast
   the handoff.

## Failover
If the previous leader died, its slot goes stale and the next main session claims
it automatically at heartbeat. If leadership is `vacant` (nobody on main),
secondaries proceed autonomously but conservatively — you don't have to force a
claim unless the user wants a coordinator.
