"""Unit tests for leadership election logic, mocked at the run_bd/run_bd_json
seam (a small in-memory fake bd store), not by monkeypatching leader's own
functions. This exercises the real claim()/ensure_slot()/_find_slot() logic
against concurrent racers.

Run: `uv run pytest` (or `pytest tests/`).
"""

from __future__ import annotations

import itertools

import pytest

import claude_mailbox.leader as L
import claude_mailbox.model as m


class FakeBdStore:
    """In-memory fake standing in for the shared beads_global DB, backing the
    handful of bd subcommands leader.py issues (query/q/set-state/close)."""

    def __init__(self) -> None:
        self._id_seq = itertools.count(1)
        self.beads: dict[str, dict] = {}

    def _new_id(self) -> str:
        return f"bd-{next(self._id_seq)}"

    def create(self, title: str, labels: list[str]) -> str:
        bid = self._new_id()
        self.beads[bid] = {
            "id": bid,
            "title": title,
            "labels": list(labels),
            "status": "open",
        }
        return bid

    def query(self, label: str) -> list[dict]:
        return [
            b
            for b in self.beads.values()
            if label in b["labels"] and b["status"] == "open"
        ]

    def set_state(self, bid: str, dim_val: str) -> None:
        bead = self.beads[bid]
        dim, _, _val = dim_val.partition("=")
        bead["labels"] = [l for l in bead["labels"] if not l.startswith(f"{dim}:")]
        bead["labels"].append(dim_val.replace("=", ":", 1))

    def close(self, bid: str) -> None:
        if bid in self.beads:
            self.beads[bid]["status"] = "closed"

    def run_bd(self, *args, actor=None, check=True):
        cmd = args[0]
        if cmd == "q":
            title = args[1]
            labels = args[args.index("-l") + 1].split(",") if "-l" in args else []
            return self.create(title, labels)
        if cmd == "set-state":
            bid, dim_val = args[1], args[2]
            self.set_state(bid, dim_val)
            return ""
        if cmd == "close":
            self.close(args[1])
            return ""
        raise NotImplementedError(f"fake bd store: unsupported command {cmd!r}")

    def run_bd_json(self, *args, actor=None):
        cmd = args[0]
        if cmd == "query":
            _field, _, label = args[1].partition("=")
            return self.query(label)
        raise NotImplementedError(f"fake bd store: unsupported json command {cmd!r}")


@pytest.fixture
def store(monkeypatch):
    fake = FakeBdStore()
    monkeypatch.setattr(L, "run_bd", fake.run_bd)
    monkeypatch.setattr(L, "run_bd_json", fake.run_bd_json)
    monkeypatch.setattr(L.time, "sleep", lambda *_: None)
    return fake


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


def test_non_main_cannot_claim(store):
    out = L.claim("h-1-aaa", branch="feature/x", actor="h-1-aaa")
    assert out["granted"] is False
    assert "not on main" in out["reason"]


def test_vacant_claim_succeeds(store):
    out = L.claim("h-1-aaa", branch="main", actor="h-1-aaa")
    assert out["granted"] is True
    leader = L.read_leader("h-1-aaa")
    assert leader["leader_sid"] == "h-1-aaa"


def test_two_actor_convergence(store):
    """Two sids both call claim(..., "main", ...) against the SAME shared
    fake store; exactly one ends as holder, and it's the smaller sid — both
    agree via read_leader."""
    out_small = L.claim("h-1-aaa", branch="main", actor="h-1-aaa")
    out_large = L.claim("h-1-zzz", branch="main", actor="h-1-zzz")

    leader_from_small = L.read_leader("h-1-aaa")
    leader_from_large = L.read_leader("h-1-zzz")
    assert leader_from_small["leader_sid"] == leader_from_large["leader_sid"]
    assert leader_from_small["leader_sid"] == "h-1-aaa"

    assert out_small["granted"] is True
    assert out_large["granted"] is False
    assert out_large["current_leader"] == "h-1-aaa"


def test_release_vacates_only_when_sid_is_current_leader(store):
    """Drives the leader-vacate helper server._reap_stale relies on: releasing a
    sid that isn't the current leader is a no-op, but releasing the actual
    leader sid vacates the slot. (server._reap_stale itself imports FastMCP, so
    it's exercised indirectly here by covering the L.release(check=False) path
    it calls after reaping a stale leader's session bead.)"""
    L.claim("h-1-aaa", branch="main", actor="h-1-aaa")
    assert L.read_leader("h-1-aaa")["leader_sid"] == "h-1-aaa"

    # Releasing a non-leader sid (e.g. some other reaped session) is a no-op.
    L.release("h-1-zzz", "h-1-zzz", check=False)
    assert L.read_leader("h-1-aaa")["leader_sid"] == "h-1-aaa"

    # Releasing the reaped leader's own sid vacates the slot.
    L.release("h-1-aaa", "h-1-aaa", check=False)
    assert L.read_leader("h-1-aaa")["vacant"] is True


def test_slot_dedup(store):
    """Pre-seed two slot beads; ensure_slot converges to the min-id one and
    closes the extra."""
    older = store.create("[mailbox] leader-slot", [m.L_LEADER_SLOT])
    newer = store.create("[mailbox] leader-slot", [m.L_LEADER_SLOT])
    assert older < newer  # sanity: fake ids are monotonically increasing

    bid = L.ensure_slot("actor")
    assert bid == older
    assert store.beads[newer]["status"] == "closed"
    assert store.beads[older]["status"] == "open"
