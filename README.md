# claude-mailbox

An MCP server that lets concurrently-running **Claude Code sessions cross-talk**.
Each session registers its project / worktree / branch / objective; sessions can
see each other, broadcast over channels, DM, and coordinate under a single
**leader** (the session on `main`). All state is backed by the shared
[`beads`](https://github.com/steveyegge/beads) (`bd`) database `beads_global`,
so it works across projects and — via the existing Dolt remote — the fleet.

## Why beads
`bd` already gives us a persistent, Dolt-synced, event-logged store with the exact
primitives a mailbox needs: labels (channels/identity), `set-state` (status/role/
heartbeat), assignees (DMs/delegations), ephemeral beads (transient messages),
gates (request/response), and a shared machine-wide DB (`--global`). The server is
a thin, typed wrapper around the `bd` CLI — no schema of our own.

## Layout
```
src/claude_mailbox/
  bd.py        # `bd --global -C <workspace>` wrapper (+ --json)
  identity.py  # session id + git project/branch/worktree detection
  model.py     # label/state naming conventions + heartbeat math
  leader.py    # main-branch leader election over a singleton slot bead
  server.py    # FastMCP server: tools + background heartbeat + atexit deregister
  cli.py       # `mailbox` shim (who / leader / say / inbox)
skills/        # `mailbox` + `mailbox-leader` Claude skills
docs/DESIGN.md # full design (data model, protocol, risks)
```

The server resolves its `bd` workspace from the repo itself, via
`WORKSPACE = Path(__file__).parents[2]` in `bd.py` — overridable with the
`MAILBOX_WORKSPACE` env var. This is why the invocation below runs it with
`uv run --project <repo>`; a bare wheel install (no repo checkout alongside
it) would need `MAILBOX_WORKSPACE` set explicitly.

## Run
```bash
uv run claude-mailbox          # start the MCP server (stdio)
uv run mailbox who             # list live sessions (CLI, no agent)
```

Prereq: the global mailbox DB must exist once per machine:
```bash
bd init --global               # creates/initializes beads_global on the shared dolt server
```

## Wire into Claude Code / codex
Add to `~/.claude/mcp_servers.json` (and it mirrors to codex):
```json
"mailbox": { "command": "uv", "args": ["run", "--project",
  "/Users/ztaylor/repos/workspaces/claude-mailbox", "claude-mailbox"] }
```

Status: **MVP** — registration, presence, channels, DMs, leadership, delegation.
Gate-based `request_info` (blocking Q&A) and nix packaging are the next steps
(see `docs/DESIGN.md` §8–9).
