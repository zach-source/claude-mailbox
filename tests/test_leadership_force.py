"""Unit tests for restricting claim_leadership(force=True) to stdio
(global-tvr): an HTTP caller force-claiming leadership would gain
delegate() over every session.

Mocked at the L.claim seam, same convention as test_authz.py — no live bd
needed.
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


def test_claim_leadership_force_rejected_over_http(monkeypatch):
    monkeypatch.setenv("MAILBOX_TRANSPORT", "http")
    monkeypatch.setattr(
        srv.L, "claim", lambda *a, **k: pytest.fail("must not reach L.claim")
    )

    result = srv.claim_leadership(force=True)

    assert result == {"granted": False, "reason": "force requires stdio transport"}


def test_claim_leadership_force_allowed_over_stdio(monkeypatch):
    monkeypatch.delenv("MAILBOX_TRANSPORT", raising=False)
    calls = []
    monkeypatch.setattr(
        srv.L,
        "claim",
        lambda sid, branch, actor, force=False: calls.append(force)
        or {"granted": True, "reason": "leader"},
    )

    result = srv.claim_leadership(force=True)

    assert result == {"granted": True, "reason": "leader"}
    assert calls == [True]


def test_claim_leadership_non_force_allowed_over_http(monkeypatch):
    monkeypatch.setenv("MAILBOX_TRANSPORT", "http")
    calls = []
    monkeypatch.setattr(
        srv.L,
        "claim",
        lambda sid, branch, actor, force=False: calls.append(force)
        or {"granted": False, "reason": "not on main"},
    )

    result = srv.claim_leadership(force=False)

    assert result == {"granted": False, "reason": "not on main"}
    assert calls == [False]
