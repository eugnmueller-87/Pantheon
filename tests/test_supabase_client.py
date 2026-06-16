"""
Query-shape tests for core/supabase_client helpers used by outcome resolution.
Mocks get_client() (conftest blocks real Supabase env), so no network.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import core.supabase_client as supa


def _mock_select(rows):
    """Patch get_client so table().select().is_().execute() returns `rows`."""
    client = MagicMock()
    (client.table.return_value
        .select.return_value
        .is_.return_value
        .execute.return_value) = MagicMock(data=rows)
    return client


def test_fetch_open_trades_filters_hit_null_and_returns_rows():
    rows = [{"order_id": "5", "symbol": "XOM", "side": "BUY", "fill_price": 141.8}]
    client = _mock_select(rows)
    with patch("core.supabase_client.get_client", return_value=client):
        out = supa.fetch_open_trades()
    assert out == rows
    client.table.assert_called_once_with("trades")
    # filtered on hit IS NULL (the idx_trades_open partial index)
    client.table.return_value.select.return_value.is_.assert_called_once_with("hit", "null")


def test_fetch_open_trades_returns_empty_on_error():
    client = MagicMock()
    client.table.side_effect = RuntimeError("boom")
    with patch("core.supabase_client.get_client", return_value=client):
        out = supa.fetch_open_trades()
    assert out == []


# ── Seniority gate queries (Section 5) ───────────────────────────────────────

def _mock_count(n):
    client = MagicMock()
    client.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(count=n)
    return client


def test_count_hades_compliance_kills():
    client = _mock_count(7)
    with patch("core.supabase_client.get_client", return_value=client):
        assert supa.count_hades_compliance_kills() == 7
    client.table.assert_called_once_with("decision_traces")
    client.table.return_value.select.return_value.eq.assert_called_once_with("killed_at_stage", "hades")


def test_count_hades_compliance_kills_none_on_error():
    client = MagicMock()
    client.table.side_effect = RuntimeError("x")
    with patch("core.supabase_client.get_client", return_value=client):
        assert supa.count_hades_compliance_kills() is None


def _mock_override_rows(rows):
    client = MagicMock()
    client.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(data=rows)
    return client


def test_override_doc_rate_insufficient_sample_returns_none():
    client = _mock_override_rows([{"zeus_override_reason": "x" * 50}] * 3)  # < 5
    with patch("core.supabase_client.get_client", return_value=client):
        assert supa.zeus_override_doc_rate(min_samples=5) is None


def test_override_doc_rate_computes_documented_share():
    rows = (
        [{"zeus_override_reason": "x" * 50}] * 3       # documented (>=40 chars)
        + [{"zeus_override_reason": "short"}] * 2      # undocumented
    )
    client = _mock_override_rows(rows)
    with patch("core.supabase_client.get_client", return_value=client):
        assert supa.zeus_override_doc_rate(min_samples=5) == pytest.approx(0.6)


# ── decision_trace persistence ───────────────────────────────────────────────

def test_insert_decision_trace_keeps_signal_id():
    """signal_id must be persisted (not stripped) so a trace correlates to its
    source signal — the strip was hiding the re-debate leak (same signal_id
    debated 3-5x). decision_traces.signal_id is a nullable uuid, no FK."""
    client = MagicMock()
    with patch("core.supabase_client.get_client", return_value=client):
        supa.insert_decision_trace({
            "trace_id": "t1", "signal_id": "sig-abc", "headline": "x",
        })
    inserted = client.table.return_value.insert.call_args[0][0]
    assert inserted["signal_id"] == "sig-abc"   # not dropped
    client.table.assert_called_once_with("decision_traces")


def test_insert_decision_trace_allows_null_signal_id():
    """The replay/decide() path has no source row → signal_id None is fine."""
    client = MagicMock()
    with patch("core.supabase_client.get_client", return_value=client):
        supa.insert_decision_trace({"trace_id": "t2", "signal_id": None})
    inserted = client.table.return_value.insert.call_args[0][0]
    assert inserted["signal_id"] is None
