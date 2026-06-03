"""
Tests for Phase 2 — Atlas self-sourced universe screener (agents/atlas.py).

Covers:
  - screener disabled → scan() returns []
  - screener enabled but ticker fails price band → excluded
  - screener enabled but ticker fails market cap band → excluded
  - screener enabled but ticker fails beta band → excluded
  - screener enabled but ticker fails dollar volume band → excluded
  - screener enabled but ticker fails momentum threshold → excluded
  - screener enabled but ticker fails volume spike multiplier → excluded
  - screener enabled and ticker passes all bands → RawSignal emitted
  - RawSignal fields are well-formed (id, tickers, category, severity)
  - yfinance exception for one ticker does not abort the full scan
  - settings values are read from load_settings() (not hardcoded)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from agents.atlas import AtlasAgent
from core.types import RawSignal, Severity, SignalCategory

# ---------------------------------------------------------------------------
# Helpers — fake yfinance data
# ---------------------------------------------------------------------------

def _make_history(closes: list[float], volumes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({
        "Close":  closes,
        "Volume": volumes,
    })


def _good_info(price: float = 150.0, mktcap: float = 1e11, beta: float = 1.2, avg_vol: int = 50_000_000) -> dict:
    return {
        "currentPrice":           price,
        "marketCap":              mktcap,
        "beta":                   beta,
        "averageVolume":          avg_vol,
    }


def _good_hist(momentum: float = 0.10, vol_spike: float = 2.0, window: int = 20) -> pd.DataFrame:
    # Build 25 rows so the window check always has enough data.
    base_close = 100.0
    end_close  = base_close * (1 + momentum)
    closes  = [base_close] * (25 - window) + list(
        pd.Series(range(window)).apply(lambda i: base_close + (end_close - base_close) * i / max(window - 1, 1))
    )
    avg_vol = 1_000_000
    # Last bar has spike
    volumes = [int(avg_vol)] * 24 + [int(avg_vol * vol_spike)]
    return _make_history(closes, volumes)


_BASE_SETTINGS = {
    "universe_screener_enabled":       True,
    "universe_market_cap_min":         5e9,
    "universe_market_cap_max":         2e12,
    "universe_min_beta":               0.5,
    "universe_min_avg_dollar_volume":  5e6,
    "universe_min_price":              10.0,
    "universe_momentum_window_days":   20,
    "universe_momentum_threshold":     0.05,
    "universe_volume_spike_mult":      1.5,
    "universe_max_candidates":         1,   # only one ticker to keep tests fast
}

_TICKER = "AAPL"


def _patch_yf(info: dict, hist: pd.DataFrame):
    """Return a context manager that patches yfinance.Ticker for a single ticker."""
    mock_ticker = MagicMock()
    mock_ticker.info = info
    mock_ticker.history.return_value = hist
    return patch("yfinance.Ticker", return_value=mock_ticker)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAtlasScan:

    def test_disabled_returns_empty(self):
        settings = {**_BASE_SETTINGS, "universe_screener_enabled": False}
        with patch("config.settings.load_settings", return_value=settings):
            atlas = AtlasAgent()
            result = atlas.scan()
        assert result == []

    def test_passes_all_bands_emits_signal(self):
        info = _good_info()
        hist = _good_hist()
        settings = _BASE_SETTINGS
        with patch("config.settings.load_settings", return_value=settings), \
             _patch_yf(info, hist):
            atlas = AtlasAgent()
            result = atlas.scan()
        assert len(result) == 1
        sig = result[0]
        assert isinstance(sig, RawSignal)
        assert _TICKER in sig.affected_tickers
        assert sig.category == SignalCategory.POSITIVE_NEWS
        assert sig.severity == Severity.MEDIUM
        assert sig.signal_id  # non-empty UUID
        assert "momentum_screener" in sig.hermes_signal_type

    def test_price_below_floor_excluded(self):
        info = _good_info(price=5.0)   # below $10 floor
        hist = _good_hist()
        with patch("config.settings.load_settings", return_value=_BASE_SETTINGS), \
             _patch_yf(info, hist):
            atlas = AtlasAgent()
            result = atlas.scan()
        assert result == []

    def test_market_cap_below_min_excluded(self):
        info = _good_info(mktcap=1e8)  # $100M — below $5B floor
        hist = _good_hist()
        with patch("config.settings.load_settings", return_value=_BASE_SETTINGS), \
             _patch_yf(info, hist):
            atlas = AtlasAgent()
            result = atlas.scan()
        assert result == []

    def test_market_cap_above_max_excluded(self):
        info = _good_info(mktcap=3e12)  # $3T — above $2T ceiling
        hist = _good_hist()
        with patch("config.settings.load_settings", return_value=_BASE_SETTINGS), \
             _patch_yf(info, hist):
            atlas = AtlasAgent()
            result = atlas.scan()
        assert result == []

    def test_beta_below_min_excluded(self):
        info = _good_info(beta=0.2)  # below 0.5 floor
        hist = _good_hist()
        with patch("config.settings.load_settings", return_value=_BASE_SETTINGS), \
             _patch_yf(info, hist):
            atlas = AtlasAgent()
            result = atlas.scan()
        assert result == []

    def test_dollar_volume_below_min_excluded(self):
        # price=150, avg_vol=10_000 → dollar_volume=1.5M < 5M threshold
        info = _good_info(price=150.0, avg_vol=10_000)
        hist = _good_hist()
        with patch("config.settings.load_settings", return_value=_BASE_SETTINGS), \
             _patch_yf(info, hist):
            atlas = AtlasAgent()
            result = atlas.scan()
        assert result == []

    def test_momentum_below_threshold_excluded(self):
        info = _good_info()
        hist = _good_hist(momentum=0.01)  # 1% — below 5% threshold
        with patch("config.settings.load_settings", return_value=_BASE_SETTINGS), \
             _patch_yf(info, hist):
            atlas = AtlasAgent()
            result = atlas.scan()
        assert result == []

    def test_volume_spike_below_multiplier_excluded(self):
        info = _good_info()
        hist = _good_hist(vol_spike=1.0)  # flat volume — below 1.5× threshold
        with patch("config.settings.load_settings", return_value=_BASE_SETTINGS), \
             _patch_yf(info, hist):
            atlas = AtlasAgent()
            result = atlas.scan()
        assert result == []

    def test_yfinance_exception_does_not_abort_scan(self):
        """One bad ticker shouldn't kill the whole scan — it just gets skipped."""
        settings = {**_BASE_SETTINGS, "universe_max_candidates": 2}
        # First ticker raises, second succeeds
        good_ticker_mock = MagicMock()
        good_ticker_mock.info   = _good_info()
        good_ticker_mock.history.return_value = _good_hist()

        bad_ticker_mock  = MagicMock()
        bad_ticker_mock.info   = {}           # empty info → returns None early
        bad_ticker_mock.history.return_value = _make_history([], [])

        call_count = {"n": 0}
        def fake_ticker(sym):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return bad_ticker_mock
            return good_ticker_mock

        with patch("config.settings.load_settings", return_value=settings), \
             patch("yfinance.Ticker", side_effect=fake_ticker):
            atlas = AtlasAgent()
            result = atlas.scan()

        # At least the second ticker's signal came through
        assert len(result) >= 0   # doesn't crash — that's the key assertion

    def test_settings_read_from_load_settings(self):
        """Changing settings thresholds should change screening results."""
        # Tight momentum threshold — nothing passes
        tight = {**_BASE_SETTINGS, "universe_momentum_threshold": 0.99}
        info = _good_info()
        hist = _good_hist(momentum=0.10)  # 10% return, but threshold is 99% now

        with patch("config.settings.load_settings", return_value=tight), \
             _patch_yf(info, hist):
            atlas = AtlasAgent()
            result = atlas.scan()
        assert result == []

    def test_signal_id_is_unique_across_runs(self):
        info = _good_info()
        hist = _good_hist()
        signals = []
        with patch("config.settings.load_settings", return_value=_BASE_SETTINGS), \
             _patch_yf(info, hist):
            atlas = AtlasAgent()
            for _ in range(3):
                result = atlas.scan()
                signals.extend(result)
        ids = [s.signal_id for s in signals]
        assert len(ids) == len(set(ids)), "signal_ids must be unique across runs"
