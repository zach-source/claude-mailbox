"""Unit tests for the respond_info assignment check (global-5yn): only the
session a request is assigned to may answer it — otherwise any connection
can forge an answer to another agent's blocking request_info call.

Mocked at the run_bd/_bead_assignee seam, same convention as
test_request_response.py — no live bd needed.
"""

from __future__ import annotations

import pytest

import claude_mailbox.server as srv


@pytest.fixture(autouse=True)
def _registered():
    st = srv._current_state()
    saved = st.bead_id
    st.bead_id = "test-bead"
    try:
        yield
    finally:
        st.bead_id = saved


def test_respond_info_rejects_request_not_assigned_to_caller(monkeypatch):
    monkeypatch.setattr(srv, "_bead_assignee", lambda rid: "someone-else")
    calls = []
    monkeypatch.setattr(srv, "run_bd", lambda *a, **k: calls.append(a))

    result = srv.respond_info(request_id="req-1", answer="forged answer")

    assert result == {"ok": False, "error": "request not assigned to you"}
    assert calls == []  # never comments or closes


def test_respond_info_rejects_when_request_has_no_assignee(monkeypatch):
    monkeypatch.setattr(srv, "_bead_assignee", lambda rid: None)
    calls = []
    monkeypatch.setattr(srv, "run_bd", lambda *a, **k: calls.append(a))

    result = srv.respond_info(request_id="req-1", answer="forged answer")

    assert result == {"ok": False, "error": "request not assigned to you"}
    assert calls == []


def test_respond_info_allows_assigned_session(monkeypatch):
    st = srv._current_state()
    monkeypatch.setattr(srv, "_bead_assignee", lambda rid: st.sid)
    calls = []
    monkeypatch.setattr(srv, "run_bd", lambda *a, **k: calls.append(a[0]))

    result = srv.respond_info(request_id="req-1", answer="the real answer")

    assert result == {"ok": True}
    assert calls == ["comment", "close"]
