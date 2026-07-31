"""Channel delivery: one bd query per poll, correct classification, and
delivery-once.

The poller used to issue `1 + len(subscribed_channels)` separate `bd` spawns
every CHANNEL_POLL_SECONDS — the dominant cost of the whole server. These tests
pin the collapsed single-query behavior (server._inbound_query) so it can't
silently regress back into a fan-out.
"""

from __future__ import annotations

import pytest

import claude_mailbox.server as srv


class _FakeChannel:
    """Stands in for a live claude/channel: records what got pushed."""

    def __init__(self) -> None:
        self.pushed: list[tuple[str, dict]] = []


@pytest.fixture
def st(monkeypatch):
    """A registered session with a live channel, pointed at a fixed project so
    subscribed_channels() is deterministic."""
    state = srv._SessionState("conn-delivery")
    state.bead_id = "bead-self"
    state.channel = _FakeChannel()
    state.git = srv.GitContext(project="proj", branch="main", worktree="/tmp/proj")

    monkeypatch.setattr(srv.ch, "is_live", lambda c: True)
    monkeypatch.setattr(
        srv.ch,
        "push",
        lambda c, text, meta: (c.pushed.append((text, meta)), True)[1],
    )
    return state


def _row(rid, labels, *, assignee=None, text="hi", frm="peer", channel=None):
    import json

    payload = {"text": text, "from": frm}
    if channel:
        payload["channel"] = channel
    return {
        "id": rid,
        "labels": labels,
        "assignee": assignee,
        "description": json.dumps(payload),
        "title": text,
    }


def test_one_query_per_poll_regardless_of_channel_count(st, monkeypatch):
    """The whole point of the change: a poll is ONE bd spawn, not one per
    subscribed channel plus one for the inbox."""
    calls: list[tuple] = []
    monkeypatch.setattr(
        srv, "run_bd_json", lambda *a, **k: (calls.append(a), [])[1] or []
    )

    assert len(st.subscribed_channels()) == 3  # general, proj, leader
    srv._deliver_channel(st)

    assert len(calls) == 1, f"expected a single bd query, got {len(calls)}: {calls}"
    expr = calls[0][1]
    # ...and that one query really does cover the inbox and every channel.
    assert f"assignee={st.sid}" in expr
    for chan in st.subscribed_channels():
        assert f"label=channel:{chan}" in expr
    assert f"created>{srv._INBOUND_LOOKBACK}" in expr


def test_inbound_query_is_bounded(st):
    """bd defaults to --limit 50; an unbounded channel scan let new messages
    hide behind 50 older rows. The lookback + explicit -n is the guard."""
    assert f"created>{srv._INBOUND_LOOKBACK}" in srv._inbound_query(st)
    assert srv._INBOUND_LIMIT > 50


def test_classifies_each_kind_and_skips_our_own_broadcast(st, monkeypatch):
    rows = [
        _row("r-req", [srv.m.L_REQUEST], assignee=st.sid, text="question?"),
        _row("r-del", [srv.m.L_DELEGATION], assignee=st.sid, text="do this"),
        _row("r-dm", [srv.m.L_MESSAGE, srv.m.L_DM], assignee=st.sid, text="psst"),
        _row(
            "r-cast",
            [srv.m.L_MESSAGE, "channel:general"],
            text="all hands",
            channel="general",
        ),
        # our own broadcast must never echo back to us
        _row(
            "r-mine",
            [srv.m.L_MESSAGE, "channel:general", srv.m.from_label(st.sid)],
            text="mine",
            channel="general",
        ),
    ]
    monkeypatch.setattr(srv, "run_bd_json", lambda *a, **k: rows)
    closed: list[str] = []
    monkeypatch.setattr(
        srv,
        "run_bd",
        lambda *a, **k: (closed.append(a[1]) if a[0] == "close" else None),
    )

    pushed = srv._deliver_channel(st)

    assert pushed == 4
    kinds = {meta["bead_id"]: meta["kind"] for _, meta in st.channel.pushed}
    assert kinds == {
        "r-req": "request",
        "r-del": "delegation",
        "r-dm": "dm",
        "r-cast": "broadcast",
    }
    # a request carries the id the model needs for respond_info; others don't
    assert dict(st.channel.pushed[0][1])["request_id"] == "r-req"
    # a delivered DM is closed (delivered = read); a request stays open
    assert closed == ["r-dm"]
    # our own broadcast was marked seen, not pushed
    assert st.has_seen("r-mine")


def test_delivery_is_once_only(st, monkeypatch):
    rows = [_row("r-1", [srv.m.L_MESSAGE, "channel:general"], channel="general")]
    monkeypatch.setattr(srv, "run_bd_json", lambda *a, **k: rows)
    monkeypatch.setattr(srv, "run_bd", lambda *a, **k: "")

    assert srv._deliver_channel(st) == 1
    assert srv._deliver_channel(st) == 0  # same row, already seen
    assert len(st.channel.pushed) == 1


def test_seed_seen_suppresses_the_startup_backlog(st, monkeypatch):
    """_seed_seen and _deliver_channel must run the *same* query, or seeding
    misses rows that delivery then floods on the first pass."""
    rows = [
        _row(f"r-{i}", [srv.m.L_MESSAGE, "channel:general"], channel="general")
        for i in range(5)
    ]
    queries: list[str] = []
    monkeypatch.setattr(
        srv, "run_bd_json", lambda *a, **k: (queries.append(a[1]), rows)[1]
    )

    srv._seed_seen(st)
    assert srv._deliver_channel(st) == 0  # everything pre-existing was seeded
    assert st.channel.pushed == []
    assert queries[0] == queries[1]  # identical expression, not merely similar


def test_seen_set_is_bounded(st):
    """These processes run for weeks; the id set used to grow forever."""
    for i in range(srv._SEEN_MAX + 250):
        st.mark_seen(f"r-{i}")

    assert len(st._seen) == srv._SEEN_MAX
    assert st.has_seen(f"r-{srv._SEEN_MAX + 249}")  # newest kept
    assert not st.has_seen("r-0")  # oldest evicted


def test_malformed_project_name_cannot_break_the_whole_query(monkeypatch):
    """One bad channel name used to break only its own query; now every channel
    shares one expression, so a name that can't be interpolated must be dropped
    rather than corrupting delivery for all of them."""
    state = srv._SessionState("conn-badproj")
    state.git = srv.GitContext(
        project="my repo OR status=closed", branch="main", worktree="/tmp/x"
    )

    assert state.subscribed_channels() == ["general", "leader"]
    assert "my repo" not in srv._inbound_query(state)
