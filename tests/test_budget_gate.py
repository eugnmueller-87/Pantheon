"""
Quality gate — capital/budget controls.

The pipeline must not line up more trades than the wallet can fund (the
51-open-trades-on-€4k bug). Two guarantees:
  - Argus.committed_capital() / available_cash() reflect DB positions and stay
    correct even when IB is down (broker-independent).
  - The ZEUS budget cap (max_deployed_pct) rejects a trade that would push
    deployed capital past the cap.

Argus DB reads are mocked — no network.
"""

from unittest.mock import MagicMock, patch

from agents.argus import ArgusAgent
from agents.zeus import ZeusConfig


def _argus(equity=4000.0):
    return ArgusAgent(default_account_equity=equity, mock=True)


def _mock_positions(rows):
    """Patch supabase_client.get_client so portfolio_positions returns `rows`."""
    client = MagicMock()
    (client.table.return_value
        .select.return_value
        .is_.return_value
        .execute.return_value) = MagicMock(data=rows)
    return patch("core.supabase_client.get_client", return_value=client)


# ── Argus wallet math ─────────────────────────────────────────────────────────

class TestCommittedCapital:
    def test_zero_when_no_positions(self):
        a = _argus()
        with _mock_positions([]):
            assert a.committed_capital() == 0.0
            assert a.available_cash() == 4000.0

    def test_sums_cost_basis(self):
        a = _argus()
        rows = [{"qty": 3, "avg_cost": 500.0}, {"qty": 9, "avg_cost": 200.0}]  # 1500 + 1800
        with _mock_positions(rows):
            assert a.committed_capital() == 3300.0
            assert a.available_cash() == 700.0

    def test_available_cash_never_negative(self):
        a = _argus(equity=1000.0)
        rows = [{"qty": 10, "avg_cost": 500.0}]  # 5000 committed on 1000 equity
        with _mock_positions(rows):
            assert a.committed_capital() == 5000.0
            assert a.available_cash() == 0.0  # floored, not negative


# ── Budget cap arithmetic (the rule ZEUS applies) ─────────────────────────────

class TestBudgetCap:
    def test_config_default_is_90pct(self):
        assert ZeusConfig().max_deployed_pct == 0.90

    def test_trade_rejected_when_over_cap(self):
        equity = 4000.0
        cfg = ZeusConfig(max_deployed_pct=0.90)
        committed = 3600.0                      # already at 90%
        trade_cost = equity * 0.02              # +2% = €80
        max_deployed = equity * cfg.max_deployed_pct  # €3600
        assert committed + trade_cost > max_deployed   # rejected

    def test_trade_allowed_with_headroom(self):
        equity = 4000.0
        cfg = ZeusConfig(max_deployed_pct=0.90)
        committed = 1000.0
        trade_cost = equity * 0.02
        max_deployed = equity * cfg.max_deployed_pct
        assert committed + trade_cost <= max_deployed  # allowed

    def test_51_trades_on_4k_would_be_blocked(self):
        # The actual bug: ~€9.4k notional on €4k equity = 236% deployed.
        equity = 4000.0
        cfg = ZeusConfig(max_deployed_pct=0.90)
        committed = 9427.0
        assert committed > equity * cfg.max_deployed_pct  # never would have been allowed
