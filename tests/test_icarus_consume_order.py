"""
Icarus claims signals consumed BEFORE mapping/returning them, so an overlapping
ZEUS cycle (~900s scheduler) cannot re-fetch and re-debate the same row. This
was a real cost leak: the same signal was debated 3-5x (~5K-char Sonnet output
each) before the old post-mapping consume-mark landed.

Behaviours locked here:
  1. mark_signals_consumed is called with the batch ids BEFORE _map_supabase_rows.
  2. A consume-claim failure aborts the cycle (returns []) rather than handing
     ZEUS rows it couldn't lock (which would be re-debated next cycle).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import core.supabase_client as supa_mod
from agents.icarus import IcarusAgent


@pytest.fixture
def supa_env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc")


def _rows():
    return [
        {"signal_id": "s1", "headline": "Acme beats", "supplier": "Acme",
         "affected_tickers": ["ACME"], "category": "earnings_surprise"},
        {"signal_id": "s2", "headline": "Beta deal", "supplier": "Beta",
         "affected_tickers": ["BETA"], "category": "positive_news"},
    ]


def test_consume_claim_happens_before_mapping(supa_env):
    agent = IcarusAgent(api_key="k")
    call_order: list[str] = []

    with patch.object(supa_mod, "get_unconsumed_signals", return_value=_rows()), \
         patch.object(supa_mod, "mark_signals_consumed",
                      side_effect=lambda ids: call_order.append(("mark", ids)) or 2), \
         patch.object(agent, "_map_supabase_rows",
                      side_effect=lambda rows, **kw: (call_order.append(("map", None)) or ([], []))):
        agent._fetch_from_supabase()

    # The claim must precede the (expensive) mapping step.
    assert [c[0] for c in call_order] == ["mark", "map"], call_order
    assert call_order[0][1] == ["s1", "s2"]   # claimed both ids up front


def test_claim_failure_aborts_cycle(supa_env):
    agent = IcarusAgent(api_key="k")
    mapped = MagicMock()

    with patch.object(supa_mod, "get_unconsumed_signals", return_value=_rows()), \
         patch.object(supa_mod, "mark_signals_consumed", side_effect=RuntimeError("rpc down")), \
         patch.object(agent, "_map_supabase_rows", mapped):
        out = agent._fetch_from_supabase()

    assert out == []                 # cycle skipped
    mapped.assert_not_called()       # never mapped → never handed to ZEUS → no re-debate


def test_no_rows_no_claim(supa_env):
    agent = IcarusAgent(api_key="k")

    with patch.object(supa_mod, "get_unconsumed_signals", return_value=[]), \
         patch.object(supa_mod, "mark_signals_consumed") as mark:
        out = agent._fetch_from_supabase()

    assert out == []
    mark.assert_not_called()
