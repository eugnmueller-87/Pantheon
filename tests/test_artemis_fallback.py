"""
Artemis must FAIL CLOSED when the VIX fetch fails — return a suppressed UNKNOWN
macro context with the -1.0 sentinel, not a fabricated benign VIX of 20.0.
Alerts are rate-limited to once per hour.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from agents.artemis import ArtemisAgent
from core.types import MarketRegime


def _raise(*a, **k):
    raise RuntimeError("yfinance throttled")


def test_vix_failure_returns_suppressed_unknown():
    art = ArtemisAgent()
    with patch("agents.artemis.yf.Ticker", side_effect=_raise):
        ctx = art._fetch_macro()
    assert ctx.regime == MarketRegime.UNKNOWN
    assert ctx.vix == -1.0
    assert ctx.suppress is True
    assert ctx.suppress_reason == "macro data unavailable"


def test_alert_rate_limited_to_once_per_hour():
    art = ArtemisAgent()
    with patch("agents.artemis.yf.Ticker", side_effect=_raise), \
         patch.object(art, "_alert_macro_unavailable", wraps=art._alert_macro_unavailable) as spy, \
         patch("os.getenv", return_value=None):  # no telegram → log path
        art._fetch_macro()   # 1st failure → alert fires (sets _last_macro_alert)
        first_alert_time = art._last_macro_alert
        art._fetch_macro()   # 2nd failure within the hour → no new alert
    # _alert_macro_unavailable is called each failure, but the timestamp only
    # advances on the first (the hour gate suppresses the second send).
    assert spy.call_count == 2
    assert art._last_macro_alert == first_alert_time


def test_alert_fires_again_after_an_hour():
    art = ArtemisAgent()
    art._last_macro_alert = datetime.now(timezone.utc) - timedelta(hours=2)
    with patch("os.getenv", return_value=None):
        art._alert_macro_unavailable("stale")
    # Timestamp advanced (alert allowed again after the hour window).
    assert (datetime.now(timezone.utc) - art._last_macro_alert).total_seconds() < 60


def test_vix_empty_data_also_fails_closed():
    class _EmptyHist:
        empty = True
    class _Ticker:
        def history(self, *a, **k):
            return _EmptyHist()
    art = ArtemisAgent()
    with patch("agents.artemis.yf.Ticker", return_value=_Ticker()):
        ctx = art._fetch_macro()
    assert ctx.vix == -1.0 and ctx.suppress is True
