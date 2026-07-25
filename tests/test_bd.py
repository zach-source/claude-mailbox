"""Unit tests for bd.py's MAILBOX_GLOBAL-conditional `--global` flag, mocked
at the subprocess.run seam (no live database needed).

Run: `uv run pytest` (or `pytest tests/`).
"""

from __future__ import annotations

import subprocess

import claude_mailbox.bd as bd


class _FakeCompleted:
    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.returncode = 0
        self.stdout = ""
        self.stderr = ""


def _capture(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _FakeCompleted(cmd)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_run_bd_includes_global_by_default(monkeypatch):
    monkeypatch.delenv("MAILBOX_GLOBAL", raising=False)
    calls = _capture(monkeypatch)
    bd.run_bd("query", "label=foo")
    assert calls[0] == [bd.BD, "--global", "-C", bd.WORKSPACE, "query", "label=foo"]


def test_run_bd_omits_global_when_disabled(monkeypatch):
    monkeypatch.setenv("MAILBOX_GLOBAL", "0")
    calls = _capture(monkeypatch)
    bd.run_bd("query", "label=foo")
    assert calls[0] == [bd.BD, "-C", bd.WORKSPACE, "query", "label=foo"]


def test_run_bd_omits_global_for_false_and_no(monkeypatch):
    calls = _capture(monkeypatch)
    for value in ("false", "False", "no", "NO"):
        monkeypatch.setenv("MAILBOX_GLOBAL", value)
        calls.clear()
        bd.run_bd("query")
        assert "--global" not in calls[0]


def test_run_bd_includes_global_for_truthy_values(monkeypatch):
    calls = _capture(monkeypatch)
    for value in ("1", "true", "yes", "anything-else"):
        monkeypatch.setenv("MAILBOX_GLOBAL", value)
        calls.clear()
        bd.run_bd("query")
        assert "--global" in calls[0]


def test_use_global_default_true(monkeypatch):
    monkeypatch.delenv("MAILBOX_GLOBAL", raising=False)
    assert bd.use_global() is True


def test_use_global_false_when_disabled(monkeypatch):
    monkeypatch.setenv("MAILBOX_GLOBAL", "0")
    assert bd.use_global() is False


def test_run_bd_actor_and_extra_args_order_preserved(monkeypatch):
    monkeypatch.setenv("MAILBOX_GLOBAL", "0")
    calls = _capture(monkeypatch)
    bd.run_bd("set-state", "bd-1", "status=active", actor="sid-1")
    assert calls[0] == [
        bd.BD,
        "-C",
        bd.WORKSPACE,
        "--actor",
        "sid-1",
        "set-state",
        "bd-1",
        "status=active",
    ]
