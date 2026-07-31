"""Thin, typed wrapper around the `bd` (beads) CLI.

By default (`MAILBOX_GLOBAL` unset or truthy) all mailbox state lives in the
shared machine-wide `beads_global` Dolt database: `bd --global` routes there,
but bd still needs a *workspace* (a local `.beads/`) to resolve the
shared-server connection — so every call passes `-C WORKSPACE`, pointing at
this repo's own `.beads/`. That keeps the mailbox reachable no matter which
project directory the Claude session is actually running in.

Set `MAILBOX_GLOBAL=0` (or `false`/`no`) to omit `--global` entirely, so `bd`
resolves against a plain local project database under WORKSPACE via its
default embedded engine instead — the mode used when this server runs as a
standalone HTTP service with its own dedicated local database (see
MAILBOX_TRANSPORT in server.py and the README's HTTP mode section).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


def _default_workspace() -> str:
    """Where `bd -C` points when MAILBOX_WORKSPACE isn't set.

    A source checkout keeps the historical answer: the repo root three levels up
    from src/claude_mailbox/bd.py, whose `.beads/` carries the shared-server
    connection config.

    An *installed* build (nix, brew, plain pip) has no repo around it — there,
    `parents[2]` resolves to something like `<prefix>/lib/python3.13`, which has
    no `.beads/` and is typically read-only, so every `bd -C` call would target a
    nonsense workspace. Those builds fall back to a per-user data directory
    instead, giving the server one stable writable workspace no matter which
    project directory the session runs in. Initialize it once with `bd init`
    (see the README's install sections).
    """
    repo = Path(__file__).resolve().parents[2]
    if (repo / ".beads").is_dir():
        return str(repo)
    xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return str(Path(xdg) / "claude-mailbox")


# Override with MAILBOX_WORKSPACE for tests / relocation / packaging.
WORKSPACE = os.environ.get("MAILBOX_WORKSPACE") or _default_workspace()

BD = shutil.which("bd") or "bd"


class BdError(RuntimeError):
    """A `bd` invocation exited non-zero."""


def use_global() -> bool:
    """Whether to pass `--global` (routes to the shared beads_global DB).

    Controlled by MAILBOX_GLOBAL, default true (current/stdio behavior).
    Set to "0"/"false"/"no" to resolve against a plain local project DB
    under WORKSPACE instead (standalone HTTP mode with its own database).
    """
    return os.environ.get("MAILBOX_GLOBAL", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def run_bd(*args: str, actor: str | None = None, check: bool = True) -> str:
    """Run `bd [--global] -C WORKSPACE [--actor A] <args>` and return stdout.

    Raises BdError on non-zero exit (unless check=False, which returns "").
    """
    cmd = [BD]
    if use_global():
        cmd += ["--global"]
    cmd += ["-C", WORKSPACE]
    if actor:
        cmd += ["--actor", actor]
    cmd += list(args)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        if check:
            raise BdError(f"{' '.join(cmd)}\n{proc.stderr.strip()}")
        return ""
    return proc.stdout.strip()


def create(
    title: str,
    *,
    type: str = "task",
    labels: list[str] | None = None,
    ephemeral: bool = False,
    priority: int | None = None,
    description: str | None = None,
    actor: str | None = None,
) -> str:
    """Create a bead and return its id. Uses `bd create --json` (not `bd q`,
    which lacks --ephemeral). Ephemeral beads are TTL-compacted — used for
    transient messages/DMs."""
    args = ["create", title, "-t", type]
    if labels:
        args += ["-l", ",".join(labels)]
    if priority is not None:
        args += ["-p", str(priority)]
    if ephemeral:
        args += ["--ephemeral"]
    if description is not None:
        args += ["-d", description]
    res = run_bd_json(*args, actor=actor)
    if isinstance(res, dict):
        bid = res.get("id") or res.get("bead", {}).get("id")
        if not bid:
            raise BdError(f"bd create returned no id: {res!r}")
        return bid
    if isinstance(res, list) and res:
        return res[0].get("id")
    raise BdError(f"could not parse created bead id from: {res!r}")


def run_bd_json(*args: str, actor: str | None = None):
    """Run bd with --json appended and parse the result (dict or list)."""
    out = run_bd(*args, "--json", actor=actor)
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        # Some bd subcommands emit a human line before/after JSON; take the
        # widest {...} or [...] span.
        for opener, closer in (("[", "]"), ("{", "}")):
            i, j = out.find(opener), out.rfind(closer)
            if 0 <= i < j:
                try:
                    return json.loads(out[i : j + 1])
                except json.JSONDecodeError:
                    continue
        raise
