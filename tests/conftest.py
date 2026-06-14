"""
Shared pytest fixtures and configuration for ZEUS quality gate.

Isolation rules:
  - No network calls in any test (mock yfinance, mock Redis)
  - No real filesystem paths (use tmp_path fixture)
  - No ANTHROPIC_API_KEY, HERMES_API_KEY, or Upstash credentials needed
  - Tests must pass cold (no data/chroma or data/trade_log.db on disk)
"""

import os
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

# Block any accidental real API calls during tests
os.environ.setdefault("ANTHROPIC_API_KEY",        "test-key-not-real")
os.environ.setdefault("HERMES_API_KEY",            "test-key-not-real")
os.environ.setdefault("UPSTASH_REDIS_REST_URL",    "https://mock-redis.upstash.io")
os.environ.setdefault("UPSTASH_REDIS_REST_TOKEN",  "mock-token")
# Explicitly unset Supabase so PythiaAgent always uses SQLite in tests
os.environ.pop("SUPABASE_URL", None)
os.environ.pop("SUPABASE_SERVICE_ROLE_KEY", None)

# List of env vars that must never be set during tests — importing any module
# that calls load_dotenv() (e.g. main.py) can inject these from a local .env
# file and cause PythiaAgent to switch from SQLite to Supabase mid-suite.
_BLOCKED_ENV_VARS = (
    "SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
)


@pytest.fixture(autouse=True)
def _block_supabase_env():
    """
    Re-enforce the Supabase env block before every test and restore the env
    to its pre-test state afterwards. This prevents any module imported during
    a test (e.g. main.py via load_dotenv()) from leaking SUPABASE_URL into
    later tests and causing PythiaAgent to silently switch to Supabase mode.
    """
    saved = {k: os.environ.pop(k, None) for k in _BLOCKED_ENV_VARS}
    yield
    # Restore exactly: re-inject only vars that existed before the test ran
    # (normally None — they were absent), and remove any that were injected.
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


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
        # +5% return → clearly BULL, no suppression triggered
        return _make_ticker([470.0, 494.0])
    # Sector ETFs and everything else — two data points for return calc
    return _make_ticker([100.0, 102.0])


@pytest.fixture(autouse=True)
def mock_yfinance():
    """Block all yfinance network calls globally across every test."""
    with patch("yfinance.Ticker", side_effect=_yfinance_ticker_factory), \
         patch("agents.artemis.yf.Ticker", side_effect=_yfinance_ticker_factory):
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
