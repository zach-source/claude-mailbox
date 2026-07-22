# Claude Code Mailbox — Design

> Designed with the Fable model; implemented against verified `bd` behavior.
> This is the condensed authority; §"bd integration notes" records the
> corrections found while wiring it up.

## 1. Concept & data model
Everything is a bead in the shared `beads_global` DB. Three kinds:

- **Session bead** — one per live session. Type `task`; closed on deregister.
  Labels: `mailbox:session`, `session:<sid>`, `project:<p>`, `branch:<b>`,
  `machine:<host>`. Description = JSON `{sid, project, worktree, branch,
  objective, machine}`. State dimensions via `bd set-state`:
  `status=active|idle|blocked|done`, `role=leader|secondary`, `hb=<epoch//30>`.
  `sid = <host>-<pid>-<rand8>`.
- **Message bead** — a broadcast or DM. Type `event`, `--ephemeral` (TTL-compacted).
  Labels: `mailbox:message`, `channel:<name>` (broadcast) or `dm` + assignee=`<sid>`
  (DM), `from:<sid>`. Text carried in the JSON description.
- **Leader-slot bead** — a singleton (`mailbox:leader-slot`) that is the lock.
  State: `leader=<sid>|vacant`, `leader-branch`, `leader-hb`.
- **Delegation bead** — leader→secondary work. Type `task`, label
  `mailbox:delegation`, assignee=target. Not ephemeral (real work).

## 2. MCP tool surface
`register_session`, `heartbeat`, `update_objective`, `set_status`, `deregister`,
`list_sessions`, `get_leader`, `claim_leadership`, `release_leadership`,
`broadcast`, `read_channel`, `send_dm`, `poll_inbox`, `delegate`.
All read-only + self-scoped writes are auto-approve; `delegate` and
`claim_leadership(force=True)` warrant a prompt. (Blocking `request_info`/
`respond_info` via bd gates is designed but deferred — see §9.)

## 3. Leadership protocol
Invariant: ≤1 leader; the `main`-branch session wins. On register + every
heartbeat, a main-branch session reads the slot; if vacant or the leader's `hb`
is stale (>90s), it claims via `set-state leader=<sid>`, then settles 1s and
reads back. On a detected race, **smallest sid wins** (deterministic tiebreak);
the loser sets `role=secondary`. Leaving `main` → release + broadcast on
`channel:leader`. Clean exit → `release` inside `deregister`. Vacant leadership is
allowed; secondaries then act autonomously but conservatively.

## 4. Presence & liveness
Server-driven heartbeat every 30s (`set-state hb=<epoch//30>`), not model-driven.
`stale = age > 90s` (3 missed beats). Sessions >10× stale are reaped
(`status=done` + `close`) opportunistically on any register. Messages are
`--ephemeral`; bd TTL compaction cleans them. `atexit`/SIGTERM → clean deregister.

## 5. Stack
Python + FastMCP (thin subprocess wrapper; simple background heartbeat thread;
covered by the repo `mcp-builder` skill). Server shells out to `bd`, never to
Dolt directly.

## bd integration notes (verified — differ from the first-draft design)
1. **`bd --global` needs a local `.beads/` workspace** for the shared-server
   connection config. The server always passes `-C <mailbox-repo>` (see `bd.py`)
   so the mailbox is reachable regardless of the session's cwd. The mailbox DB
   itself (`beads_global`) is created once per machine with `bd init --global`.
2. **Query syntax is `field=value`** joined with `AND`/`OR` — e.g.
   `label=mailbox:session AND status=open`. NOT `label:value`.
3. **State is stored as `dimension:value` labels** (+ an event bead); we read
   current state by parsing those labels off `bd show/query --json` output.
4. **Gates** use `bd gate create --type=<t> --blocks <id> [--await-id ...]`
   (`--blocks` is the gated issue). Bead gates: `--type=bead --await-id <rig>:<id>`.
5. Ephemeral TTL default is unverified; if channel history vanishes too fast for
   slow readers, switch broadcasts to `--defer`-based expiry.

## 9. Risks / open questions
- **set-state race semantics** on the shared Dolt server drive the claim/read-back
  tiebreak; verify empirically under two simultaneous main sessions.
- **Fleet sync is pull-based** (`bd dolt push/pull`): cross-machine visibility &
  failover lag one sync cycle. Recommend machine-local guarantees + best-effort
  periodic sync; leave as a user decision.
- **Identity is unauthenticated** (`--actor`/sid are self-asserted) — fine for a
  single-user fleet; no auth.
- **Query volume**: ~4 bd calls/min/session (heartbeat + inbox). Fine <20 sessions.
- **Blocking `request_info`** would hold a model turn up to its timeout; prefer an
  async `check_request` escape hatch when implemented.
- **register via SessionStart hook** (guaranteed) vs skill (probabilistic):
  recommend a hook calling `mailbox register` once the story is proven.
