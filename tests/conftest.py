"""
Shared pytest fixtures and configuration for ZEUS quality gate.

Isolation rules:
  - No network calls in any test (mock yfinance, mock Redis)
  - No real filesystem paths (use tmp_path fixture)
  - No ANTHROPIC_API_KEY, HERMES_API_KEY, or Upstash credentials needed
  - Tests must pass cold (no data/chroma or data/trade_log.db on disk)
"""

import os
import pytest
import pandas as pd
from unittest.mock import patch, MagicMock

# Block any accidental real API calls during tests
os.environ.setdefault("ANTHROPIC_API_KEY",        "test-key-not-real")
os.environ.setdefault("HERMES_API_KEY",            "test-key-not-real")
os.environ.setdefault("UPSTASH_REDIS_REST_URL",    "https://mock-redis.upstash.io")
os.environ.setdefault("UPSTASH_REDIS_REST_TOKEN",  "mock-token")


def _make_ticker(close_values):
    hist = pd.DataFrame({"Close": close_values})
    ticker = MagicMock()
    ticker.history.return_value = hist
    return ticker


def _yfinance_ticker_factory(symbol):
    """Return realistic-enough fake data for any ticker."""
    if symbol == "^VIX":
        return _make_ticker([18.0])
    if symbol == "SPY":
        return _make_ticker([490.0, 495.0])
    # Sector ETFs and everything else — two data points for return calc
    return _make_ticker([100.0, 102.0])


@pytest.fixture(autouse=True)
def mock_yfinance():
    """Block all yfinance network calls globally across every test."""
    with patch("yfinance.Ticker", side_effect=_yfinance_ticker_factory):
        yield


@pytest.fixture(autouse=True)
def mock_upstash_redis():
    """Block all Upstash Redis network calls globally across every test."""
    mock_redis = MagicMock()
    mock_redis.set.return_value = True
    mock_redis.setex.return_value = True
    mock_redis.lpush.return_value = 1
    mock_redis.ltrim.return_value = True
    with patch("upstash_redis.Redis", return_value=mock_redis):
        yield mock_redis
