"""Thin, typed wrapper around the `bd` (beads) CLI.

All mailbox state lives in the shared machine-wide `beads_global` Dolt database.
`bd --global` routes there, but bd still needs a *workspace* (a local `.beads/`)
to resolve the shared-server connection — so every call passes `-C WORKSPACE`,
pointing at this repo's own `.beads/`. That keeps the mailbox reachable no matter
which project directory the Claude session is actually running in.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

# The mailbox repo root (…/claude-mailbox). Its .beads/ carries the shared-server
# connection config. Override with MAILBOX_WORKSPACE for tests / relocation.
WORKSPACE = os.environ.get(
    "MAILBOX_WORKSPACE", str(Path(__file__).resolve().parents[2])
)

BD = shutil.which("bd") or "bd"


class BdError(RuntimeError):
    """A `bd` invocation exited non-zero."""


def run_bd(*args: str, actor: str | None = None, check: bool = True) -> str:
    """Run `bd --global -C WORKSPACE [--actor A] <args>` and return stdout.

    Raises BdError on non-zero exit (unless check=False, which returns "").
    """
    cmd = [BD, "--global", "-C", WORKSPACE]
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
