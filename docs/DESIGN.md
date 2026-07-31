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
  State: `leader=<sid>|vacant`, `leader-branch`. Its `leader_hb` is description
  JSON, not state — see §4.
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
Server-driven heartbeat every 60s, not model-driven. The `hb` epoch is written
into the bead's **description JSON** via `bd update`, on a bead marked
`--no-history`; the session and leader-slot beads are the only ones written at
this cadence.

> Not `bd set-state`. set-state mints an event bead *and* rewrites a
> `<dim>:<val>` label per call — measured at ~3.8 Dolt commits + 1 issue row per
> write, versus 0 commits for a description write on a `--no-history` bead.
> Heartbeating through set-state grew `beads_global` to 21,320 heartbeat event
> beads out of 21,353 issues (85k Dolt commits, 2.9GB) in eight days. Reserve
> set-state for genuinely low-cardinality facts (`status`, `role`, `leader`);
> anything that changes every beat belongs in the description.

`stale = age > 180s` (3 missed beats). Sessions >10× stale are reaped
(`status=done` + `close`) opportunistically on any register. Messages are
`--ephemeral`; bd TTL compaction cleans them. `atexit`/SIGTERM → clean deregister.

## 5. Stack
Python + FastMCP (thin subprocess wrapper; simple background heartbeat thread;
covered by the repo `mcp-builder` skill). Server shells out to `bd`, never to
Dolt directly.

Session identity is per-connection (`_SessionState`, keyed by FastMCP's
`Context.session_id`), not per-process. Under stdio there's exactly one
connection for the process's life, so this is unchanged from a single
in-memory instance; under `MAILBOX_TRANSPORT=http` a single process can serve
several concurrent connections, each with its own sid/git-context/bead_id/
objective/captured channel session. One shared heartbeat thread and one shared
channel-delivery thread iterate every tracked connection each tick — not one
thread pair per connection — since a periodic sweep over a dict is simpler
than per-connection thread lifecycle management and equally correct. What those
two threads are allowed to cost is §6.

## 6. Poll cost budget
The server's steady-state cost is *entirely* background polling, and it is paid
in `bd` subprocesses. One server runs per agent session, and a busy machine
really does run many — 20 concurrent were measured on one laptop, all sharing
one Dolt server. So the per-session budget is multiplied by session count, and
it has to stay small.

Measured `bd` calls (see `tests/test_channel_delivery.py`, which pins the
channel figure):

| Path | Before | Now |
|---|---|---|
| Channel poll | 4 (`1 + len(subscribed_channels)`) | **1** |
| Heartbeat tick | 5 | **3** |
| Poll interval | flat 4s | 15s, backing off to 60s while idle |
| Sustained, per session | ~1.08 calls/s | **~0.07 calls/s idle, ~0.12 busy** |

At 20 sessions that is ~22 calls/s → ~1.4 calls/s. The old figure was enough to
saturate the shared Dolt server: `bd query` against `beads_global` was measured
at 33–146s per call under that load, all of it queueing on the schema-migration
lock, with the server pinned at ~140% CPU.

Three rules hold the budget:
1. **One query per poll.** `_inbound_query` covers the assigned-to-us inbox and
   every subscribed channel in a single expression using bd's `OR`/paren
   grouping. Channel names are `valid_token`-filtered before interpolation —
   with one shared expression, a single malformed name would break delivery for
   every channel at once rather than just its own.
2. **Every query is explicitly bounded.** `bd query` silently defaults to
   `--limit 50`. That default was a *correctness* bug, not just a cost one: an
   unbounded channel scan meant new messages could hide behind 50 older rows
   forever, and `read_channel` was sorting whichever arbitrary 50 rows came
   back. Broadcasts are additionally bounded by `created>` so the scan doesn't
   grow with channel history. Tradeoff: a process suspended past the lookback
   (laptop sleep) can miss a broadcast on resume; the pull tools still surface
   it.
3. **Idle costs less than busy.** The delivery loop backs off to
   `CHANNEL_POLL_MAX_SECONDS` when a pass pushes nothing and snaps back to
   `CHANNEL_POLL_SECONDS` the moment traffic appears. Most sessions are idle
   most of the time.

Failure isolation belongs here too: the delivery sweep catches per session, not
around the whole loop. `run_bd` raises on its 30s subprocess timeout, and at the
latencies above that fires routinely — a single wrapping `try` meant one slow
session aborted delivery for every other session in the same pass.

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
- **HTTP-mode idle-connection reap is time-based, not a transport liveness
  check**: a connection that disconnects without calling `deregister` is only
  noticed once it's gone `_CONN_IDLE_SECONDS` (reuses the existing 10×-stale
  threshold, 900s) without a tool call — there's no probe of the underlying
  socket/stream. A connection that stays open but is genuinely idle that long
  is indistinguishable from an abandoned one and gets reaped too. Gated to
  `MAILBOX_TRANSPORT=http` only, so stdio's single long-lived connection is
  never affected.
- **set-state race semantics** on the shared Dolt server drive the claim/read-back
  tiebreak; verify empirically under two simultaneous main sessions.
- **Fleet sync is pull-based** (`bd dolt push/pull`): cross-machine visibility &
  failover lag one sync cycle. Recommend machine-local guarantees + best-effort
  periodic sync; leave as a user decision.
- **Identity is unauthenticated** (`--actor`/sid are self-asserted) — fine for a
  single-user fleet; no auth.
- **Query volume** — see §6; the original "~2 bd calls/min/session, fine <20
  sessions" estimate was wrong by ~30×, and the correction is now a budget with
  a regression test rather than an estimate. Write *volume* still matters more
  than call count: keep steady-state writes off `set-state` and on
  `--no-history` beads, or the DB grows without bound.
- **`bd` subprocess is the read transport, and it's the standing cost ceiling.**
  Every read pays ~0.3s of Go startup (measured, `bd version`, before any DB
  work) plus a DB open plus a machine-wide schema-migration lock that serializes
  across *all* bd processes. §6's budget keeps that affordable; it doesn't make
  it cheap. The escape hatch, if the budget stops holding, is a persistent
  MySQL connection to the Dolt sql-server for reads (~1–5ms, measured 0.7s for
  a whole `dolt sql` client round-trip including spawn), keeping `bd` for
  writes where the audit trail and wisp semantics are the point. That trades
  the "never touch Dolt directly" rule in §5 for coupling to beads' table
  layout — deliberately not taken yet.
- **Blocking `request_info`** would hold a model turn up to its timeout; prefer an
  async `check_request` escape hatch when implemented.
- **register via SessionStart hook** (guaranteed) vs skill (probabilistic):
  recommend a hook calling `mailbox register` once the story is proven.
