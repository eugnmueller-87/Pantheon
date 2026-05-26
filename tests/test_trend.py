"""
Quality gate — Trend Analyzer (macro context + suppression logic).

If suppression logic is wrong, ZEUS trades in the wrong market environment.
These tests use mocked yfinance to avoid network calls and test the
regime classification and suppression rules in pure isolation.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from agents.artemis import ArtemisAgent as TrendAgent
from core.types import (
    FilteredSignal,
    MacroContext,
    MarketRegime,
    RawSignal,
    Severity,
    SignalCategory,
)


def _filtered(category=SignalCategory.POSITIVE_NEWS) -> FilteredSignal:
    raw = RawSignal(
        signal_id="s-trend",
        source_url="",
        headline="Test",
        summary="",
        published_at=datetime.now(timezone.utc),
        category=category,
        severity=Severity.MEDIUM,
        affected_tickers=["AAPL"],
        raw_text="test",
        supplier="TestCorp",
    )
    return FilteredSignal(original=raw, compliance_score=1.0)


def _mock_history(values: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"Close": values})


# ── Regime classification ──────────────────────────────────────────────────────

class TestRegimeClassification:
    def test_bull_regime(self):
        regime = TrendAgent._classify_regime(sp500_return=0.03, vix=15.0)
        assert regime == MarketRegime.BULL

    def test_bear_regime_by_sp500(self):
        regime = TrendAgent._classify_regime(sp500_return=-0.04, vix=20.0)
        assert regime == MarketRegime.BEAR

    def test_bear_regime_by_extreme_vix(self):
        regime = TrendAgent._classify_regime(sp500_return=0.01, vix=36.0)
        assert regime == MarketRegime.BEAR

    def test_sideways_regime(self):
        regime = TrendAgent._classify_regime(sp500_return=0.01, vix=20.0)
        assert regime == MarketRegime.SIDEWAYS

    def test_bull_threshold_boundary(self):
        assert TrendAgent._classify_regime(0.019, 15.0) == MarketRegime.SIDEWAYS
        assert TrendAgent._classify_regime(0.021, 15.0) == MarketRegime.BULL

    def test_bear_threshold_boundary(self):
        assert TrendAgent._classify_regime(-0.029, 20.0) == MarketRegime.SIDEWAYS
        assert TrendAgent._classify_regime(-0.031, 20.0) == MarketRegime.BEAR


# ── Suppression logic ──────────────────────────────────────────────────────────

class TestSuppression:
    def _macro(self, regime=MarketRegime.BULL, vix=15.0) -> MacroContext:
        return MacroContext(
            fetched_at=datetime.now(timezone.utc),
            regime=regime, vix=vix, sp500_1m_return=0.03,
        )

    def _trend(self) -> TrendAgent:
        return TrendAgent()

    def test_positive_signal_suppressed_in_bear_high_vix(self):
        trend = self._trend()
        ctx = self._macro(regime=MarketRegime.BEAR, vix=28.0)
        result = trend._apply_suppression(ctx, _filtered(SignalCategory.POSITIVE_NEWS))
        assert result.suppress is True
        assert result.suppress_reason is not None

    def test_disruption_not_suppressed_in_bear_high_vix(self):
        """Supply chain disruption (SHORT thesis) should not be suppressed in bear market."""
        trend = self._trend()
        ctx = self._macro(regime=MarketRegime.BEAR, vix=28.0)
        result = trend._apply_suppression(ctx, _filtered(SignalCategory.SUPPLIER_DISRUPTION))
        assert result.suppress is False

    def test_all_signals_suppressed_at_extreme_vix(self):
        trend = self._trend()
        for cat in [SignalCategory.POSITIVE_NEWS, SignalCategory.SUPPLIER_DISRUPTION,
                    SignalCategory.EARNINGS_SURPRISE, SignalCategory.REGULATORY_ACTION]:
            ctx_copy = self._macro(regime=MarketRegime.BULL, vix=36.0)
            result = trend._apply_suppression(ctx_copy, _filtered(cat))
            assert result.suppress is True, f"{cat} should be suppressed at VIX=36"

    def test_positive_signal_passes_in_bull_normal_vix(self):
        trend = self._trend()
        ctx = self._macro(regime=MarketRegime.BULL, vix=15.0)
        result = trend._apply_suppression(ctx, _filtered(SignalCategory.POSITIVE_NEWS))
        assert result.suppress is False

    def test_suppress_reason_mentions_vix(self):
        trend = self._trend()
        ctx = self._macro(regime=MarketRegime.BULL, vix=36.0)
        result = trend._apply_suppression(ctx, _filtered(SignalCategory.EARNINGS_SURPRISE))
        assert "35" in result.suppress_reason or "VIX" in result.suppress_reason


# ── Caching ────────────────────────────────────────────────────────────────────

class TestCaching:
    def test_second_call_uses_cache_not_network(self):
        trend = TrendAgent(cache_ttl_seconds=9999)

        cached_ctx = MacroContext(
            fetched_at=datetime.now(timezone.utc),
            regime=MarketRegime.BULL, vix=12.0, sp500_1m_return=0.05,
        )
        trend._cached    = cached_ctx
        trend._cache_time = datetime.now(timezone.utc)

        with patch.object(trend, '_fetch_macro', side_effect=AssertionError("should not fetch")):
            ctx = trend._get_context()

        assert ctx is cached_ctx

    def test_stale_cache_triggers_refetch(self):
        import time
        trend = TrendAgent(cache_ttl_seconds=0)  # immediately stale

        fetch_calls = []

        def fake_fetch():
            fetch_calls.append(1)
            return MacroContext(
                fetched_at=datetime.now(timezone.utc),
                regime=MarketRegime.SIDEWAYS, vix=20.0, sp500_1m_return=0.0,
            )

        trend._cached     = fake_fetch()
        trend._cache_time = datetime.now(timezone.utc)

        with patch.object(trend, '_fetch_macro', side_effect=fake_fetch):
            trend._get_context()

        assert len(fetch_calls) == 2  # 1 for seed + 1 for stale re-fetch


# ── Fallback on yfinance failure ───────────────────────────────────────────────

class TestFallbackBehaviour:
    def test_vix_fetch_failure_returns_safe_default(self):
        trend = TrendAgent()
        with patch('yfinance.Ticker') as mock_ticker:
            mock_ticker.return_value.history.side_effect = RuntimeError("network error")
            vix = trend._fetch_vix()
        assert vix == pytest.approx(20.0)

    def test_sp500_fetch_failure_returns_zero(self):
        trend = TrendAgent()
        with patch('yfinance.Ticker') as mock_ticker:
            mock_ticker.return_value.history.side_effect = RuntimeError("network error")
            ret = trend._fetch_sp500_return()
        assert ret == pytest.approx(0.0)
