"""Unit tests for the request_info/respond_info/check_request round-trip and
missing-bead robustness, mocked at the run_bd/create seam in server.py.
"""

from __future__ import annotations

import claude_mailbox.server as srv
from claude_mailbox.bd import BdError


def test_request_respond_round_trip(monkeypatch):
    monkeypatch.setattr(srv, "create", lambda *a, **k: "req-1")
    monkeypatch.setattr(srv, "run_bd", lambda *a, **k: "")
    monkeypatch.setattr(srv.time, "sleep", lambda *_: None)

    # First poll: still open. Second poll: closed with an answer posted.
    statuses = iter(["open", "closed"])
    monkeypatch.setattr(srv, "_bead_status", lambda bid: next(statuses))
    monkeypatch.setattr(srv, "_last_answer", lambda bid: "42")

    result = srv.request_info(to_sid="peer", question="what is it?", timeout_s=60)
    assert result["resolved"] is True
    assert result["answer"] == "42"
    assert result["request_id"] == "req-1"


def test_request_info_returns_error_when_assign_fails(monkeypatch):
    monkeypatch.setattr(srv, "create", lambda *a, **k: "req-1")

    def _fail_assign(*a, **k):
        raise BdError("assign boom")

    monkeypatch.setattr(srv, "run_bd", _fail_assign)

    result = srv.request_info(to_sid="peer", question="what is it?", timeout_s=60)
    assert result["resolved"] is False
    assert "assign failed" in result["error"]
    # Must not have entered the blocking poll loop.
    assert result["request_id"] == "req-1"


def test_check_request_missing_bead_returns_gone(monkeypatch):
    monkeypatch.setattr(srv, "_bead_status", lambda bid: None)

    result = srv.check_request(request_id="does-not-exist")
    assert result == {"resolved": False, "answer": None, "gone": True}


def test_bead_status_swallows_bd_error(monkeypatch):
    def _boom(*a, **k):
        raise BdError("gone")

    monkeypatch.setattr(srv, "run_bd_json", _boom)
    assert srv._bead_status("whatever") is None


def test_last_answer_swallows_bd_error(monkeypatch):
    def _boom(*a, **k):
        raise BdError("gone")

    monkeypatch.setattr(srv, "run_bd_json", _boom)
    assert srv._last_answer("whatever") is None


def test_send_dm_reports_delivery_failure(monkeypatch):
    monkeypatch.setattr(srv, "create", lambda *a, **k: "dm-1")

    def _fail_assign(*a, **k):
        raise BdError("assign boom")

    monkeypatch.setattr(srv, "run_bd", _fail_assign)

    result = srv.send_dm(to_sid="peer", text="hi")
    assert result == {"message_id": "dm-1", "delivered": False, "error": "assign boom"}
