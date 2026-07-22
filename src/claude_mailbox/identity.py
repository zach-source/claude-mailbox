"""Session identity + git context detection.

A session id (`sid`) is `<host>-<pid>-<rand8>`, stable for the process lifetime.
Project / branch / worktree are read from git so a session self-describes without
the model having to supply them.
"""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
from dataclasses import dataclass


def _git(*args: str, cwd: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5
        )
        return out.stdout.strip() if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


@dataclass(frozen=True)
class GitContext:
    project: str  # repo name (basename of toplevel)
    branch: str  # current branch (or "DETACHED")
    worktree: str  # absolute worktree path (or cwd if not a repo)


def detect_git(cwd: str | None = None) -> GitContext:
    cwd = cwd or os.getcwd()
    top = _git("rev-parse", "--show-toplevel", cwd=cwd)
    if not top:
        return GitContext(
            project=os.path.basename(cwd) or "unknown", branch="none", worktree=cwd
        )
    branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd) or "DETACHED"
    return GitContext(project=os.path.basename(top), branch=branch, worktree=top)


def new_sid() -> str:
    host = socket.gethostname().split(".")[0]
    return f"{host}-{os.getpid()}-{secrets.token_hex(4)}"


def hostname() -> str:
    return socket.gethostname().split(".")[0]
