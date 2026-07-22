---
name: mailbox
description: >-
  Coordinate with other concurrently-running Claude Code sessions via the mailbox
  MCP (register presence, broadcast your objective, discover who else is working
  and where, DM/ask them, and respect the single main-branch leader). Use at
  session start when the mailbox MCP tools are available, and whenever the user
  says "who else is working", "broadcast", "tell the other session", "ask the
  orchestrator", "mailbox", or when about to touch shared state (main, flake.lock,
  deploys) that another session might be working on.
---

# Mailbox: cross-session coordination

Multiple Claude Code sessions run at once (different projects, branches, worktrees,
machines). The mailbox MCP lets them see each other and cross-talk. State is backed
by the shared `beads_global` database, so it works across projects and the fleet.

## Push delivery (channels)
When the session is started as a channel (`--dangerously-load-development-channels
server:mailbox`), peer messages arrive **pushed** as `<channel source="mailbox"
kind="dm|request|delegation|broadcast" from_sid="…" [request_id="…"]>…</channel>`
events — you don't have to poll. React to them: for `kind="request"` answer with
`respond_info` passing the `request_id`; for `kind="dm"` reply with `send_dm` to the
`from_sid`; for `kind="broadcast"` just take it into account. Without the channel
flag, use `poll_inbox` / `read_channel` instead (same data, pull-based).

## The one rule: a single leader on `main`
Exactly one session is the **leader/orchestrator** — the one on the `main` branch.
Everyone else is **secondary**. `register_session` auto-claims leadership when
you're on main; otherwise you're secondary and defer to the leader.

## Protocol

1. **At session start** — call `register_session(objective="<one line of intent>")`.
   Then `list_sessions()` and `get_leader()` so you know who's around and who leads
   *before* touching shared state.
2. **While working**
   - `update_objective(...)` when your focus changes.
   - `set_status("blocked")` when stuck, `"idle"` when waiting, `"done"` when finished.
   - Glance at `heartbeat()` → `inbox_count`; if nonzero, `poll_inbox()`.
3. **Before shared/destructive actions** (editing `main`, `flake.lock`, deploys,
   migrations): if you're secondary, `broadcast("<intent>", channel="<project>")`
   first, or `request_info`/`send_dm` the relevant session. Don't stomp work in
   progress you can see in `list_sessions()`.
4. **Answer promptly** — if `poll_inbox()` shows a DM or the leader delegated work,
   respond. Another session may be waiting on you.
5. **On exit** — `set_status("done")`; `deregister()` (the server also does this
   automatically at process exit).

## Channels
`broadcast(text, channel="general")`, `read_channel("general")`. Conventions:
`general` (everyone), `<project>` (per-repo), `leader` (orchestrator announcements).

## Tools
`register_session`, `update_objective`, `set_status`, `heartbeat`, `list_sessions`,
`get_leader`, `claim_leadership`, `release_leadership`, `broadcast`, `read_channel`,
`send_dm`, `poll_inbox`, `deregister`. If you are the leader, also see the
**mailbox-leader** skill.
