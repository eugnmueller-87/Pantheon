"""
Quality gate — MockExecutionAgent (position sizing, bracket math, direction).

The mock is used during all paper trading. If it calculates wrong quantities
or wrong stop/take-profit prices, the Pattern agent records bad outcome data
and the learning layer trains on garbage. The math must be exact.
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import patch
import pandas as pd

from core.types import (
    FilteredSignal, MacroContext, MarketRegime,
    RawSignal, Severity, SignalCategory, SizedSignal,
)
from agents.ares_mock import AresMockAgent as MockExecutionAgent

_ACCOUNT_EQUITY = 100_000.0   # matches default — used in quantity assertions


def _filtered(category=SignalCategory.POSITIVE_NEWS, tickers=None) -> FilteredSignal:
    raw = RawSignal(
        signal_id="ex-001",
        source_url="",
        headline="Test",
        summary="",
        published_at=datetime.now(timezone.utc),
        category=category,
        severity=Severity.HIGH,
        affected_tickers=["AAPL"] if tickers is None else tickers,
        raw_text="test",
        supplier="TestCorp",
    )
    return FilteredSignal(original=raw, compliance_score=1.0)


def _macro() -> MacroContext:
    return MacroContext(
        fetched_at=datetime.now(timezone.utc),
        regime=MarketRegime.BULL, vix=15.0, sp500_1m_return=0.03,
    )


def _sized(category=SignalCategory.POSITIVE_NEWS, size_pct=0.02, tickers=None) -> SizedSignal:
    return SizedSignal(
        original=_filtered(category, tickers),
        macro=_macro(),
        confidence=0.65,
        position_size_pct=size_pct,
    )


def _mock_price(price: float):
    """Patch yfinance to return a fixed price."""
    hist = pd.DataFrame({"Close": [price]})
    return patch('yfinance.Ticker', return_value=type('T', (), {'history': lambda *a, **kw: hist})())


# ── Trade direction ────────────────────────────────────────────────────────────

class TestTradeDirection:
    def test_positive_news_is_long(self):
        with _mock_price(150.0):
            agent = MockExecutionAgent()
            result = agent.place(_sized(SignalCategory.POSITIVE_NEWS))
        assert result.side == "BUY"

    def test_supplier_disruption_is_short(self):
        with _mock_price(150.0):
            agent = MockExecutionAgent()
            result = agent.place(_sized(SignalCategory.SUPPLIER_DISRUPTION))
        assert result.side == "SELL"

    def test_earnings_surprise_is_long(self):
        with _mock_price(150.0):
            agent = MockExecutionAgent()
            result = agent.place(_sized(SignalCategory.EARNINGS_SURPRISE))
        assert result.side == "BUY"

    def test_regulatory_action_is_long(self):
        with _mock_price(150.0):
            agent = MockExecutionAgent()
            result = agent.place(_sized(SignalCategory.REGULATORY_ACTION))
        assert result.side == "BUY"


# ── Position quantity calculation ──────────────────────────────────────────────

class TestQuantityCalculation:
    def test_qty_at_2pct_of_100k_at_100_price(self):
        """2% of €100k = €2000. At price €100 → 20 shares."""
        with _mock_price(100.0):
            agent = MockExecutionAgent(slippage_bps=0)
            result = agent.place(_sized(size_pct=0.02))
        assert result.qty == 20

    def test_qty_minimum_is_1(self):
        """Even at tiny position size and high price, must place at least 1 share."""
        with _mock_price(100_000.0):
            agent = MockExecutionAgent(slippage_bps=0)
            result = agent.place(_sized(size_pct=0.0001))
        assert result.qty >= 1

    def test_larger_size_pct_gives_more_shares(self):
        with _mock_price(100.0):
            agent = MockExecutionAgent(slippage_bps=0)
            small = agent.place(_sized(size_pct=0.01))
            large = agent.place(_sized(size_pct=0.04))
        assert large.qty > small.qty


# ── Bracket order prices ───────────────────────────────────────────────────────

class TestBracketPrices:
    def test_long_stop_loss_is_3pct_below_entry(self):
        with _mock_price(100.0):
            agent = MockExecutionAgent(slippage_bps=0)
            result = agent.place(_sized(SignalCategory.POSITIVE_NEWS))
        assert result.stop_loss_price == pytest.approx(result.fill_price * 0.97, rel=0.01)

    def test_long_take_profit_is_6pct_above_entry(self):
        with _mock_price(100.0):
            agent = MockExecutionAgent(slippage_bps=0)
            result = agent.place(_sized(SignalCategory.POSITIVE_NEWS))
        assert result.take_profit_price == pytest.approx(result.fill_price * 1.06, rel=0.01)

    def test_short_stop_loss_is_3pct_above_entry(self):
        with _mock_price(100.0):
            agent = MockExecutionAgent(slippage_bps=0)
            result = agent.place(_sized(SignalCategory.SUPPLIER_DISRUPTION))
        assert result.stop_loss_price == pytest.approx(result.fill_price * 1.03, rel=0.01)

    def test_short_take_profit_is_6pct_below_entry(self):
        with _mock_price(100.0):
            agent = MockExecutionAgent(slippage_bps=0)
            result = agent.place(_sized(SignalCategory.SUPPLIER_DISRUPTION))
        assert result.take_profit_price == pytest.approx(result.fill_price * 0.94, rel=0.01)

    def test_rr_ratio_is_2to1(self):
        """Reward-to-risk must be 2:1 — core trading rule."""
        with _mock_price(100.0):
            agent = MockExecutionAgent(slippage_bps=0)
            result = agent.place(_sized(SignalCategory.POSITIVE_NEWS))
        entry = result.fill_price
        risk   = abs(entry - result.stop_loss_price)
        reward = abs(result.take_profit_price - entry)
        assert reward / risk == pytest.approx(2.0, rel=0.05)


# ── No tickers → skipped ───────────────────────────────────────────────────────

class TestEdgeCases:
    def test_no_tickers_returns_skipped_result(self):
        agent = MockExecutionAgent()
        result = agent.place(_sized(tickers=[]))
        assert result.status == "skipped"
        assert result.qty == 0

    def test_price_fetch_failure_returns_skipped(self):
        with patch('yfinance.Ticker', side_effect=RuntimeError("network down")):
            agent = MockExecutionAgent()
            result = agent.place(_sized())
        assert result.status == "skipped"

    def test_status_is_simulated_on_success(self):
        with _mock_price(100.0):
            agent = MockExecutionAgent()
            result = agent.place(_sized())
        assert result.status == "simulated"

    def test_order_id_is_unique(self):
        with _mock_price(100.0):
            agent = MockExecutionAgent()
            ids = {agent.place(_sized()).order_id for _ in range(5)}
        assert len(ids) == 5  # all unique


# ── Cancel pending ─────────────────────────────────────────────────────────────

class TestCancelPending:
    def test_cancel_clears_pending_list(self):
        with _mock_price(100.0):
            agent = MockExecutionAgent()
            agent.place(_sized())
            agent.place(_sized())
        assert len(agent._pending) == 2
        agent.cancel_all_pending()
        assert len(agent._pending) == 0
