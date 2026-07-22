"""Unit tests for model.valid_token and read_channel's newest-first ordering."""

from __future__ import annotations

import claude_mailbox.model as m
import claude_mailbox.server as srv


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


def test_read_channel_rejects_invalid_channel():
    result = srv.read_channel(channel="x OR y", limit=10)
    assert result == {
        "ok": False,
        "error": "invalid channel: must match [A-Za-z0-9._-]",
    }
