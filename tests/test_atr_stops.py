"""
Tests for Phase 3 — ATR-based volatility-aware stops (ares_mock.py / ares.py).

Covers:
  - use_atr_stops=False → fixed percentage stops used
  - use_atr_stops=True, ATR available → ATR-derived stop used
  - ATR stop clamped to min_stop_pct when ATR is tiny
  - ATR stop clamped to max_stop_pct when ATR is huge
  - reward_risk_ratio respected (take_profit = stop_distance × ratio)
  - ATR fetch failure falls back gracefully to fixed stops
  - is_long=False (short side) → stops flip correctly
  - settings values are read from load_settings() (not hardcoded)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from agents.ares_mock import AresMockAgent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent(stop_loss_pct: float = 0.03, take_profit_pct: float = 0.06) -> AresMockAgent:
    return AresMockAgent(
        account_equity=4_000.0,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
    )


def _make_history_df(highs, lows, closes) -> pd.DataFrame:
    return pd.DataFrame({"High": highs, "Low": lows, "Close": closes})


def _atr_settings(
    use_atr: bool = True,
    atr_period: int = 14,
    atr_stop_multiple: float = 2.0,
    min_stop_pct: float = 0.03,
    max_stop_pct: float = 0.12,
    reward_risk_ratio: float = 2.0,
) -> dict:
    return {
        "use_atr_stops":      use_atr,
        "atr_period":         atr_period,
        "atr_stop_multiple":  atr_stop_multiple,
        "min_stop_pct":       min_stop_pct,
        "max_stop_pct":       max_stop_pct,
        "reward_risk_ratio":  reward_risk_ratio,
        "stop_loss_pct":      0.03,
        "take_profit_pct":    0.06,
    }


def _constant_history(n: int = 20, price: float = 100.0, range_: float = 2.0) -> pd.DataFrame:
    """Constant OHLC bars — ATR = range_ for every bar."""
    return _make_history_df(
        highs  = [price + range_ / 2] * n,
        lows   = [price - range_ / 2] * n,
        closes = [price] * n,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAtrStops:

    def test_fixed_stops_when_atr_disabled(self):
        agent = _make_agent(stop_loss_pct=0.03, take_profit_pct=0.06)
        settings = _atr_settings(use_atr=False)

        with patch("config.settings.load_settings", return_value=settings):
            stop, tp = agent._compute_stops("AAPL", fill_price=100.0, is_long=True)

        assert abs(stop - 97.0) < 0.01,  f"Expected stop ~97.00, got {stop}"
        assert abs(tp   - 106.0) < 0.01, f"Expected TP ~106.00, got {tp}"

    def test_atr_stop_used_when_enabled(self):
        """ATR of 2.0 on a $100 stock = 2% raw, × 2.0 multiple = 4% stop (above 3% floor)."""
        agent    = _make_agent()
        settings = _atr_settings(use_atr=True, atr_stop_multiple=2.0, min_stop_pct=0.03, max_stop_pct=0.12, reward_risk_ratio=2.0)
        hist     = _constant_history(n=20, price=100.0, range_=2.0)  # ATR ≈ 2.0

        mock_ticker = MagicMock()
        mock_ticker.history.return_value = hist

        with patch("config.settings.load_settings", return_value=settings), \
             patch("yfinance.Ticker", return_value=mock_ticker):
            stop, tp = agent._compute_stops("AAPL", fill_price=100.0, is_long=True)

        # ATR=2.0, fill=100, multiple=2.0 → raw_stop_pct=0.04 (4%), above 3% floor
        expected_stop = round(100.0 * (1 - 0.04), 4)   # 96.0
        expected_tp   = round(100.0 * (1 + 0.08), 4)   # 108.0  (0.04 × 2.0 rr)
        assert abs(stop - expected_stop) < 0.02, f"Expected stop ~{expected_stop}, got {stop}"
        assert abs(tp   - expected_tp)   < 0.02, f"Expected TP ~{expected_tp}, got {tp}"

    def test_atr_stop_clamped_to_min(self):
        """ATR=0.1 on $100 → raw=0.1%, below 3% floor → clamped to 3%."""
        agent    = _make_agent()
        settings = _atr_settings(use_atr=True, atr_stop_multiple=2.0, min_stop_pct=0.03, max_stop_pct=0.12, reward_risk_ratio=2.0)
        hist     = _constant_history(n=20, price=100.0, range_=0.1)  # ATR ≈ 0.1

        mock_ticker = MagicMock()
        mock_ticker.history.return_value = hist

        with patch("config.settings.load_settings", return_value=settings), \
             patch("yfinance.Ticker", return_value=mock_ticker):
            stop, tp = agent._compute_stops("AAPL", fill_price=100.0, is_long=True)

        # Clamped to min 3%
        assert abs(stop - 97.0) < 0.02, f"Expected stop ~97.0 (min clamp), got {stop}"

    def test_atr_stop_clamped_to_max(self):
        """ATR=20 on $100 → raw=40%, above 12% ceiling → clamped to 12%."""
        agent    = _make_agent()
        settings = _atr_settings(use_atr=True, atr_stop_multiple=2.0, min_stop_pct=0.03, max_stop_pct=0.12, reward_risk_ratio=2.0)
        hist     = _constant_history(n=20, price=100.0, range_=20.0)  # ATR ≈ 20

        mock_ticker = MagicMock()
        mock_ticker.history.return_value = hist

        with patch("config.settings.load_settings", return_value=settings), \
             patch("yfinance.Ticker", return_value=mock_ticker):
            stop, tp = agent._compute_stops("AAPL", fill_price=100.0, is_long=True)

        # Clamped to max 12%
        assert abs(stop - 88.0) < 0.02, f"Expected stop ~88.0 (max clamp), got {stop}"

    def test_reward_risk_ratio_applied(self):
        """ATR stop = 4%, rr=3.0 → TP = 12%."""
        agent    = _make_agent()
        settings = _atr_settings(use_atr=True, atr_stop_multiple=2.0, min_stop_pct=0.03, max_stop_pct=0.12, reward_risk_ratio=3.0)
        hist     = _constant_history(n=20, price=100.0, range_=2.0)  # ATR ≈ 2 → 4% stop

        mock_ticker = MagicMock()
        mock_ticker.history.return_value = hist

        with patch("config.settings.load_settings", return_value=settings), \
             patch("yfinance.Ticker", return_value=mock_ticker):
            stop, tp = agent._compute_stops("AAPL", fill_price=100.0, is_long=True)

        # 4% stop, 3.0 rr → 12% TP
        assert abs(tp - 112.0) < 0.02, f"Expected TP ~112.0, got {tp}"

    def test_atr_fetch_failure_falls_back_to_fixed(self):
        """If yfinance raises during ATR fetch, fall back to fixed stops silently."""
        agent    = _make_agent(stop_loss_pct=0.03, take_profit_pct=0.06)
        settings = _atr_settings(use_atr=True)

        with patch("config.settings.load_settings", return_value=settings), \
             patch("yfinance.Ticker", side_effect=RuntimeError("network error")):
            stop, tp = agent._compute_stops("AAPL", fill_price=100.0, is_long=True)

        # Should fall back to fixed 3% / 6%
        assert abs(stop - 97.0) < 0.02,  f"Expected fixed stop ~97.0, got {stop}"
        assert abs(tp   - 106.0) < 0.02, f"Expected fixed TP ~106.0, got {tp}"

    def test_short_side_stop_flips(self):
        """For a short position, stop is ABOVE entry (buy to cover at a loss)."""
        agent    = _make_agent(stop_loss_pct=0.03, take_profit_pct=0.06)
        settings = _atr_settings(use_atr=False)

        with patch("config.settings.load_settings", return_value=settings):
            stop, tp = agent._compute_stops("AAPL", fill_price=100.0, is_long=False)

        assert stop > 100.0, f"Short stop should be above entry, got {stop}"
        assert tp   < 100.0, f"Short TP should be below entry, got {tp}"
        assert abs(stop - 103.0) < 0.02, f"Expected short stop ~103.0, got {stop}"
        assert abs(tp   - 94.0)  < 0.02, f"Expected short TP ~94.0, got {tp}"

    def test_settings_authoritative_for_atr_params(self):
        """Changing atr_stop_multiple in settings changes the derived stop."""
        agent    = _make_agent()
        hist     = _constant_history(n=20, price=100.0, range_=2.0)  # ATR ≈ 2

        mock_ticker = MagicMock()
        mock_ticker.history.return_value = hist

        # 1× multiple → raw=2%, above 3% floor → clamped to 3%
        settings_1x = _atr_settings(use_atr=True, atr_stop_multiple=1.0, min_stop_pct=0.03)
        # 4× multiple → raw=8%, between 3%-12% → 8%
        settings_4x = _atr_settings(use_atr=True, atr_stop_multiple=4.0, min_stop_pct=0.03)

        with patch("config.settings.load_settings", return_value=settings_1x), \
             patch("yfinance.Ticker", return_value=mock_ticker):
            stop_1x, _ = agent._compute_stops("AAPL", fill_price=100.0, is_long=True)

        with patch("config.settings.load_settings", return_value=settings_4x), \
             patch("yfinance.Ticker", return_value=mock_ticker):
            stop_4x, _ = agent._compute_stops("AAPL", fill_price=100.0, is_long=True)

        assert stop_4x < stop_1x, (
            f"4× multiple should give wider stop than 1×: got {stop_4x} vs {stop_1x}"
        )
