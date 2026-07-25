"""Per-connection session isolation: two concurrent connections must never
share sid/bead_id/objective, and an abandoned HTTP-mode connection must not
heartbeat forever. Mocked at the run_bd/run_bd_json/create seam (a small
in-memory fake bd store), same convention as tests/test_leader.py's
FakeBdStore — no live bd needed.
"""

from __future__ import annotations

import itertools

import pytest

import claude_mailbox.server as srv


class FakeBd:
    """In-memory fake standing in for `bd`, covering the subcommands server.py
    issues across register/heartbeat/update_objective/list_sessions/deregister."""

    def __init__(self) -> None:
        self._id_seq = itertools.count(1)
        self.beads: dict[str, dict] = {}

    def _new_id(self) -> str:
        return f"bd-{next(self._id_seq)}"

    @staticmethod
    def _labels(args) -> list[str]:
        return list(args[args.index("-l") + 1].split(",")) if "-l" in args else []

    def _create(self, title: str, args) -> str:
        bid = self._new_id()
        desc = args[args.index("-d") + 1] if "-d" in args else "{}"
        self.beads[bid] = {
            "id": bid,
            "title": title,
            "labels": self._labels(args),
            "status": "open",
            "description": desc,
            "assignee": None,
            "created_at": "2024-01-01T00:00:00Z",
        }
        return bid

    def run_bd(self, *args, actor=None, check=True):
        cmd = args[0]
        if cmd == "q":
            return self._create(args[1], args)
        if cmd == "update":
            bead = self.beads.get(args[1])
            if bead is None:
                return ""
            if "-d" in args:
                bead["description"] = args[args.index("-d") + 1]
            if "--title" in args:
                bead["title"] = args[args.index("--title") + 1]
            return ""
        if cmd == "set-state":
            bead = self.beads.get(args[1])
            if bead is None:
                return ""
            dim, _, _ = args[2].partition("=")
            bead["labels"] = [
                lbl for lbl in bead["labels"] if not lbl.startswith(f"{dim}:")
            ]
            bead["labels"].append(args[2].replace("=", ":", 1))
            return ""
        if cmd == "close":
            bead = self.beads.get(args[1])
            if bead is not None:
                bead["status"] = "closed"
            return ""
        if cmd == "assign":
            bead = self.beads.get(args[1])
            if bead is not None:
                bead["assignee"] = args[2]
            return ""
        if cmd in ("note", "comment"):
            return ""
        raise NotImplementedError(f"fake bd: unsupported run_bd command {cmd!r}")

    def run_bd_json(self, *args, actor=None):
        cmd = args[0]
        if cmd == "query":
            return [b for b in self.beads.values() if self._matches(b, args[1])]
        if cmd == "create":
            return {"id": self._create(args[1], args)}
        if cmd == "show":
            bead = self.beads.get(args[1])
            return [bead] if bead else []
        if cmd == "comments":
            return []
        raise NotImplementedError(f"fake bd: unsupported json command {cmd!r}")

    @staticmethod
    def _matches(bead: dict, expr: str) -> bool:
        for clause in expr.split(" AND "):
            field, _, val = clause.partition("=")
            field, val = field.strip(), val.strip()
            if field == "label":
                if val not in bead["labels"]:
                    return False
            elif field == "status":
                if bead["status"] != val:
                    return False
            elif field == "assignee":
                if bead.get("assignee") != val:
                    return False
            else:
                return False
        return True


@pytest.fixture
def fake_bd(monkeypatch):
    fake = FakeBd()
    monkeypatch.setattr(srv, "run_bd", fake.run_bd)
    monkeypatch.setattr(srv, "run_bd_json", fake.run_bd_json)
    monkeypatch.setattr(
        srv,
        "create",
        lambda *a, actor=None, **k: fake.run_bd_json("create", *a, actor=actor)["id"],
    )
    return fake


@pytest.fixture(autouse=True)
def _clean_sessions_registry():
    """_SESSIONS is a module-level dict shared across the whole test process —
    isolate each test's view of it instead of leaking entries between tests."""
    saved = dict(srv._SESSIONS)
    srv._SESSIONS.clear()
    try:
        yield
    finally:
        srv._SESSIONS.clear()
        srv._SESSIONS.update(saved)


async def _register(client, objective: str) -> dict:
    res = await client.call_tool("register_session", {"objective": objective})
    return res.data


async def _call(client, name: str, **kwargs) -> dict:
    res = await client.call_tool(name, kwargs)
    return res.data


@pytest.mark.usefixtures("fake_bd")
async def test_two_concurrent_connections_never_leak_identity():
    """Two concurrent in-process MCP clients, driven through the real protocol
    layer (each gets its own Context.session_id from fastmcp) — sid, bead_id,
    and objective must never cross over."""
    from fastmcp import Client

    async with Client(srv.mcp) as c1, Client(srv.mcp) as c2:
        r1 = await _register(c1, "alpha's objective")
        r2 = await _register(c2, "beta's objective")

        assert r1["sid"] != r2["sid"]
        assert r1["bead_id"] != r2["bead_id"]
        assert len(srv._SESSIONS) == 2

        # A second register call (idempotent refresh) on c1 must not touch c2.
        await _register(c1, "alpha updated")
        sessions = {s["sid"]: s for s in await _call(c1, "list_sessions")}
        assert sessions[r1["sid"]]["objective"] == "alpha updated"
        assert sessions[r2["sid"]]["objective"] == "beta's objective"

        # heartbeat/update_objective/set_status all resolve to the calling
        # connection's own state, not whichever connection registered last.
        await _call(c2, "update_objective", objective="beta updated")
        await _call(c1, "set_status", status="blocked")
        sessions = {s["sid"]: s for s in await _call(c1, "list_sessions")}
        assert sessions[r1["sid"]]["objective"] == "alpha updated"
        assert sessions[r1["sid"]]["status"] == "blocked"
        assert sessions[r2["sid"]]["objective"] == "beta updated"
        assert sessions[r2["sid"]]["status"] == "active"

        await _call(c1, "deregister")
        await _call(c2, "deregister")


@pytest.mark.usefixtures("fake_bd")
def test_direct_calls_without_context_share_one_fallback_state():
    """Existing unit tests (test_request_response.py etc.) call tool functions
    directly as plain Python calls with no MCP Context at all. That must keep
    resolving to a single shared state, exactly like the pre-fix singleton —
    otherwise every one of those tests would silently start failing."""
    st1 = srv._current_state()
    st2 = srv._current_state()
    assert st1 is st2
    assert st1.conn_id == srv._DEFAULT_CONN_ID


@pytest.mark.usefixtures("fake_bd")
def test_idle_http_connection_is_reaped_without_a_real_wait(monkeypatch):
    """Drives the staleness clock like test_leader.py's fake store does for
    L.claim, rather than sleeping: back-date last_activity past the reuse
    threshold and run one heartbeat tick directly."""
    monkeypatch.setenv("MAILBOX_TRANSPORT", "http")

    st = srv._state_for("conn-abandoned")
    srv._register_impl(st, "will be abandoned")
    assert st.bead_id is not None
    bead_id = st.bead_id

    st.last_activity -= srv._CONN_IDLE_SECONDS + 1
    srv._hb_tick_once()

    assert "conn-abandoned" not in srv._SESSIONS
    # reuses the existing deregister path: bead closed, not left dangling.
    assert srv.run_bd_json("show", bead_id)[0]["status"] == "closed"


@pytest.mark.usefixtures("fake_bd")
def test_active_http_connection_keeps_heartbeating():
    """A connection touched recently must NOT be reaped, even in HTTP mode."""
    st = srv._state_for("conn-active")
    srv._register_impl(st, "still working")
    bead_id = st.bead_id

    srv._hb_tick_once()

    assert "conn-active" in srv._SESSIONS
    assert srv.run_bd_json("show", bead_id)[0]["status"] == "open"


@pytest.mark.usefixtures("fake_bd")
def test_idle_connection_is_never_reaped_outside_http_mode(monkeypatch):
    """stdio mode must be zero-behavior-change: no idle reap, ever, no matter
    how long since the last tool call — the background thread always keeps a
    stdio process's single connection heartbeating, exactly like before this
    fix."""
    monkeypatch.delenv("MAILBOX_TRANSPORT", raising=False)

    st = srv._state_for("conn-stdio")
    srv._register_impl(st, "long idle but still connected")
    bead_id = st.bead_id

    st.last_activity -= srv._CONN_IDLE_SECONDS + 1
    srv._hb_tick_once()

    assert "conn-stdio" in srv._SESSIONS
    assert srv.run_bd_json("show", bead_id)[0]["status"] == "open"


def test_channel_capture_is_scoped_per_connection_state():
    """register_session must not resurrect the removed global _CH slot — the
    captured channel state lives on the connection's own _SessionState."""
    st1 = srv._SessionState("a")
    st2 = srv._SessionState("b")
    assert st1.channel is None
    assert st2.channel is None
    assert not hasattr(srv.ch, "_CH")
