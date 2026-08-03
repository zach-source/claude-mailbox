"""Unit tests for model.valid_token and read_channel's newest-first ordering."""

from __future__ import annotations

import pytest

import claude_mailbox.model as m
import claude_mailbox.server as srv
from claude_mailbox.identity import GitContext


@pytest.fixture(autouse=True)
def _registered():
    # read_channel is gated on registration (global-6ue); these tests exercise
    # its sorting/validation logic directly, not the gate itself.
    st = srv._current_state()
    saved = st.bead_id
    st.bead_id = "test-bead"
    try:
        yield
    finally:
        st.bead_id = saved


def test_valid_token_accepts_safe_chars():
    assert m.valid_token("my-repo_1.x") is True


def test_valid_token_rejects_injection_attempt():
    assert m.valid_token("x OR status=closed") is False


def test_valid_token_rejects_spaces():
    assert m.valid_token("a b") is False


def test_valid_token_rejects_empty():
    assert m.valid_token("") is False


def test_read_channel_sorts_newest_first(monkeypatch):
    rows = [
        {"id": "b-1", "created_at": "2024-01-01T00:00:00Z", "description": "{}"},
        {"id": "b-3", "created_at": "2024-01-03T00:00:00Z", "description": "{}"},
        {"id": "b-2", "created_at": "2024-01-02T00:00:00Z", "description": "{}"},
    ]
    monkeypatch.setattr(srv, "run_bd_json", lambda *a, **k: rows)

    msgs = srv.read_channel(channel="general", limit=10)

    assert [msg["id"] for msg in msgs] == ["b-3", "b-2", "b-1"]
    assert msgs[0]["ts"] == "2024-01-03T00:00:00Z"


def _broadcast_labels(monkeypatch, **kwargs) -> list[str]:
    st = srv._current_state()
    monkeypatch.setattr(
        st, "git", GitContext(project="my-repo", branch="main", worktree="/tmp")
    )
    captured = {}
    monkeypatch.setattr(
        srv, "create", lambda title, **kw: (captured.update(kw), "b-1")[1]
    )
    assert srv.broadcast(**kwargs) == {"message_id": "b-1"}
    return captured["labels"]


def test_broadcast_defaults_to_the_senders_project(monkeypatch):
    labels = _broadcast_labels(monkeypatch, text="hi")
    assert m.channel_label("my-repo") in labels
    assert m.channel_label("general") not in labels


def test_broadcast_reaches_all_projects_only_when_asked(monkeypatch):
    labels = _broadcast_labels(monkeypatch, text="hi", channel="general")
    assert m.channel_label("general") in labels


def test_read_channel_rejects_invalid_channel():
    result = srv.read_channel(channel="x OR y", limit=10)
    assert result == {
        "ok": False,
        "error": "invalid channel: must match [A-Za-z0-9._-]",
    }
