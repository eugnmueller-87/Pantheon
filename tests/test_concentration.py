"""
Tests for Phase 1 — per-ticker concentration cap + cooldown (zeus.py).

Covers:
  - open position cap reached → killed at "concentration"
  - cooldown active (intra-run same ticker) → killed at "concentration"
  - cooldown active (KB history recent trade) → killed at "concentration"
  - cooldown expired → signal passes concentration check
  - ticker with no history → signal passes concentration check
  - settings values are read from load_settings() (not ZeusConfig hardcoded defaults)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from agents.zeus import ZeusConfig, ZeusOrchestrator
from core.types import (
    FilteredSignal,
    MacroContext,
    MarketRegime,
    RawSignal,
    Severity,
    SignalCategory,
    SizedSignal,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _raw(ticker: str = "AAPL") -> RawSignal:
    return RawSignal(
        signal_id="test-sig-1",
        source_url="https://example.com",
        headline=f"{ticker} earnings beat",
        summary="Beat by 10%",
        published_at=datetime.now(timezone.utc),
        category=SignalCategory.EARNINGS_SURPRISE,
        severity=Severity.HIGH,
        affected_tickers=[ticker],
        supplier=ticker,
    )


def _sized(ticker: str = "AAPL") -> SizedSignal:
    raw = _raw(ticker)
    filtered = FilteredSignal(
        original=raw,
        compliance_score=0.9,
        esg_flag=False,
        ofac_flag=False,
        downgraded=False,
        notes=[],
    )
    macro = MacroContext(
        fetched_at=datetime.now(timezone.utc),
        regime=MarketRegime.BULL,
        vix=15.0,
        sp500_1m_return=0.03,
        suppress=False,
    )
    return SizedSignal(
        original=filtered,
        macro=macro,
        confidence=0.65,
        position_size_pct=0.02,
        skip=False,
    )


def _make_zeus(
    max_per_ticker: int = 1,
    cooldown_hours: float = 48.0,
    max_open: int = 3,
) -> ZeusOrchestrator:
    """Build a ZeusOrchestrator with all external calls mocked."""
    config = ZeusConfig(
        max_open_positions=max_open,
        max_open_positions_per_ticker=max_per_ticker,
        ticker_cooldown_hours=cooldown_hours,
        mock_execution=True,
        paper_trading=True,
        use_llm_reasoning=False,
        default_account_equity=4_000.0,   # explicit — fail-closed gate requires it
    )
    with patch("agents.zeus.KnowledgeBase"), \
         patch("agents.zeus.CircuitBreaker"), \
         patch("agents.zeus.Watchdog"), \
         patch("agents.zeus.MilestoneManager"), \
         patch("agents.zeus.ApolloAgent"), \
         patch("agents.zeus.IcarusAgent"), \
         patch("agents.zeus.HadesAgent"), \
         patch("agents.zeus.ArtemisAgent"), \
         patch("agents.zeus.PythiaAgent"), \
         patch("agents.zeus.AresMockAgent"), \
         patch("agents.zeus.ArgusAgent"), \
         patch("agents.zeus.RedisBridge"), \
         patch("agents.zeus.SeniorityEvaluator"), \
         patch("agents.zeus.Watchdog.start"), \
         patch("agents.zeus.ZeusOrchestrator._run_seniority_evaluation"), \
         patch("config.settings.load_settings", return_value={
             "max_open_positions": max_open,
             "max_open_positions_per_ticker": max_per_ticker,
             "ticker_cooldown_hours": cooldown_hours,
         }):
        zeus = ZeusOrchestrator(config)
    return zeus


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestConcentrationCheck:

    def test_no_history_passes(self):
        """Ticker with no open positions and no KB history should pass."""
        zeus = _make_zeus()
        zeus.argus._state.snapshots = []
        zeus._run_approved_trades = []
        zeus.kb.query_ticker_history = MagicMock(return_value=[])

        result = zeus._check_concentration(_sized("NVDA"))
        assert result is None

    def test_open_position_cap_blocks(self):
        """If ticker already has >= max_per_ticker open positions, block it."""
        zeus = _make_zeus(max_per_ticker=1)

        from agents.argus import PositionSnapshot
        zeus.argus._state.snapshots = [
            PositionSnapshot(
                symbol="AAPL", side="LONG", qty=1, avg_cost=200.0,
                current_price=205.0, unrealized_pnl=5.0, unrealized_pnl_pct=0.025,
            )
        ]
        zeus._run_approved_trades = []
        zeus.kb.query_ticker_history = MagicMock(return_value=[])

        result = zeus._check_concentration(_sized("AAPL"))
        assert result is not None
        assert "concentration" in result
        assert "AAPL" in result

    def test_intra_run_cooldown_blocks(self):
        """If same ticker approved earlier this cycle, block it."""
        zeus = _make_zeus(cooldown_hours=48.0)
        zeus.argus._state.snapshots = []
        zeus._run_approved_trades = [
            {"ticker": "MSFT", "side": "BUY", "sector": "positive_news", "supplier": "MSFT"}
        ]
        zeus.kb.query_ticker_history = MagicMock(return_value=[])

        result = zeus._check_concentration(_sized("MSFT"))
        assert result is not None
        assert "concentration" in result
        assert "MSFT" in result

    def test_kb_cooldown_active_blocks(self):
        """If KB shows a trade on this ticker within cooldown window, block it."""
        zeus = _make_zeus(cooldown_hours=48.0)
        zeus.argus._state.snapshots = []
        zeus._run_approved_trades = []

        recent_ts = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
        zeus.kb.query_ticker_history = MagicMock(return_value=[
            {"outcome": "trade_placed", "timestamp": recent_ts}
        ])

        result = zeus._check_concentration(_sized("TSLA"))
        assert result is not None
        assert "concentration" in result
        assert "TSLA" in result

    def test_kb_cooldown_expired_passes(self):
        """If KB trade is older than cooldown window, signal should pass."""
        zeus = _make_zeus(cooldown_hours=48.0)
        zeus.argus._state.snapshots = []
        zeus._run_approved_trades = []

        old_ts = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
        zeus.kb.query_ticker_history = MagicMock(return_value=[
            {"outcome": "trade_placed", "timestamp": old_ts}
        ])

        result = zeus._check_concentration(_sized("TSLA"))
        assert result is None

    def test_rejected_kb_entry_does_not_trigger_cooldown(self):
        """Only approved/trade_placed outcomes count toward cooldown."""
        zeus = _make_zeus(cooldown_hours=48.0)
        zeus.argus._state.snapshots = []
        zeus._run_approved_trades = []

        recent_ts = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
        zeus.kb.query_ticker_history = MagicMock(return_value=[
            {"outcome": "rejected", "timestamp": recent_ts},
            {"outcome": "zeus_reject", "timestamp": recent_ts},
        ])

        result = zeus._check_concentration(_sized("AMD"))
        assert result is None

    def test_no_ticker_passes(self):
        """Signal with empty affected_tickers should not be blocked by concentration."""
        zeus = _make_zeus()
        zeus.argus._state.snapshots = []
        zeus._run_approved_trades = []
        zeus.kb.query_ticker_history = MagicMock(return_value=[])

        sized = _sized("AAPL")
        sized.original.original.affected_tickers = []

        result = zeus._check_concentration(sized)
        assert result is None

    def test_missing_equity_fails_closed(self):
        """A money-sizing system must REFUSE to start with an unknown balance.
        With no equity in config and none in settings, construction must raise."""
        with patch("config.settings.load_settings", return_value={}), \
             patch("agents.zeus.KnowledgeBase"), \
             patch("agents.zeus.CircuitBreaker"), \
             patch("agents.zeus.Watchdog"), \
             patch("agents.zeus.MilestoneManager"), \
             patch("agents.zeus.RedisBridge"), \
             patch("agents.zeus.SeniorityEvaluator"), \
             patch("agents.zeus.Watchdog.start"):
            config = ZeusConfig(mock_execution=True, paper_trading=True,
                                use_llm_reasoning=False)  # no equity anywhere
            with pytest.raises(ValueError):
                ZeusOrchestrator(config)

    def test_settings_authoritative_for_max_open_positions(self):
        """ZeusConfig.max_open_positions must match what load_settings() returns."""
        with patch("config.settings.load_settings", return_value={
            "max_open_positions": 5,
            "max_open_positions_per_ticker": 1,
            "ticker_cooldown_hours": 24.0,
        }), \
        patch("agents.zeus.KnowledgeBase"), \
        patch("agents.zeus.CircuitBreaker"), \
        patch("agents.zeus.Watchdog"), \
        patch("agents.zeus.MilestoneManager"), \
        patch("agents.zeus.ApolloAgent"), \
        patch("agents.zeus.IcarusAgent"), \
        patch("agents.zeus.HadesAgent"), \
        patch("agents.zeus.ArtemisAgent"), \
        patch("agents.zeus.PythiaAgent"), \
        patch("agents.zeus.AresMockAgent"), \
        patch("agents.zeus.ArgusAgent"), \
        patch("agents.zeus.RedisBridge"), \
        patch("agents.zeus.SeniorityEvaluator"), \
        patch("agents.zeus.Watchdog.start"), \
        patch("agents.zeus.ZeusOrchestrator._run_seniority_evaluation"):
            config = ZeusConfig(mock_execution=True, paper_trading=True, use_llm_reasoning=False,
                                default_account_equity=4_000.0)
            zeus = ZeusOrchestrator(config)

        assert zeus.config.max_open_positions == 5
        assert zeus.config.ticker_cooldown_hours == 24.0
