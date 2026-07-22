"""Unit tests for leadership election logic with bd mocked out.

Covers: only-main-can-claim, vacant claim, tiebreak (smallest sid wins), and
state parsing from labels. Run: `uv run pytest` (or `pytest tests/`).
"""

from __future__ import annotations

import claude_mailbox.leader as L


def test_states_of_parses_dimension_labels():
    bead = {
        "labels": [
            "mailbox:session",
            "session:h-1-ab",
            "status:blocked",
            "role:leader",
            "hb:42",
            "channel:general",
        ]
    }
    st = L.states_of(bead)
    assert st["status"] == "blocked"
    assert st["role"] == "leader"
    assert st["hb"] == "42"
    # non-state x:y labels (session:, channel:) are ignored
    assert "session" not in st and "channel" not in st


def test_non_main_cannot_claim(monkeypatch):
    monkeypatch.setattr(L, "ensure_slot", lambda actor: "slot-1")
    out = L.claim("h-1-aaa", branch="feature/x", actor="h-1-aaa")
    assert out["granted"] is False
    assert "not on main" in out["reason"]


def test_vacant_claim_succeeds(monkeypatch):
    writes = []
    monkeypatch.setattr(L, "ensure_slot", lambda actor: "slot-1")
    monkeypatch.setattr(L, "_write_leader", lambda *a, **k: writes.append(a))
    monkeypatch.setattr(L.time, "sleep", lambda *_: None)
    states = iter(
        [
            {"vacant": True, "leader_sid": None, "stale": True},  # pre-claim read
            {"vacant": False, "leader_sid": "h-1-aaa", "stale": False},  # read-back
        ]
    )
    monkeypatch.setattr(L, "read_leader", lambda actor: next(states))
    out = L.claim("h-1-aaa", branch="main", actor="h-1-aaa")
    assert out["granted"] is True
    assert writes  # a leader write happened


def test_tiebreak_smaller_sid_wins(monkeypatch):
    monkeypatch.setattr(L, "ensure_slot", lambda actor: "slot-1")
    monkeypatch.setattr(L, "_write_leader", lambda *a, **k: None)
    monkeypatch.setattr(L.time, "sleep", lambda *_: None)
    # I'm "h-1-aaa"; read-back shows a larger sid won the write → I should reclaim.
    states = iter(
        [
            {"vacant": True, "leader_sid": None, "stale": True},
            {"vacant": False, "leader_sid": "h-1-zzz", "stale": False},
        ]
    )
    monkeypatch.setattr(L, "read_leader", lambda actor: next(states))
    out = L.claim("h-1-aaa", branch="main", actor="h-1-aaa")
    assert out["granted"] is True
    assert out["reason"] == "won tiebreak"


def test_tiebreak_larger_sid_yields(monkeypatch):
    monkeypatch.setattr(L, "ensure_slot", lambda actor: "slot-1")
    monkeypatch.setattr(L, "_write_leader", lambda *a, **k: None)
    monkeypatch.setattr(L.time, "sleep", lambda *_: None)
    states = iter(
        [
            {"vacant": True, "leader_sid": None, "stale": True},
            {"vacant": False, "leader_sid": "h-1-aaa", "stale": False},
        ]
    )
    monkeypatch.setattr(L, "read_leader", lambda actor: next(states))
    out = L.claim("h-1-zzz", branch="main", actor="h-1-zzz")
    assert out["granted"] is False
    assert out["reason"] == "lost tiebreak"
