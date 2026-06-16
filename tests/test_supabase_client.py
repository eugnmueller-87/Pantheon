"""
Query-shape tests for core/supabase_client helpers used by outcome resolution.
Mocks get_client() (conftest blocks real Supabase env), so no network.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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
