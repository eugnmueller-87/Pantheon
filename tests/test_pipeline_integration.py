"""
Quality gate — Full pipeline integration.

End-to-end tests that run mock signals through the complete
Hades → Trend → Pattern → Execution chain and verify:
  - OFAC/ESG signals are killed before reaching execution
  - Suppressed signals never reach execution
  - Clean signals with valid tickers produce TradeResults
  - Trade outcomes are recorded in Pattern's SQLite log

These run without any network calls (mocked yfinance) and
without IBKR connection. All logic is exercised in isolation.
"""

import pytest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
import pandas as pd
import uuid

from core.types import RawSignal, SignalCategory, Severity, MarketRegime
from agents.hades import HadesAgent
from agents.artemis import ArtemisAgent as TrendAgent
from agents.pythia import PythiaAgent as PatternAgent
from agents.ares_mock import AresMockAgent as MockExecutionAgent


def _raw(
    text: str,
    category: SignalCategory = SignalCategory.POSITIVE_NEWS,
    tickers: list = None,
    sid: str = None,
) -> RawSignal:
    return RawSignal(
        signal_id=sid or str(uuid.uuid4()),
        source_url="https://mock",
        headline=text,
        summary=text,
        published_at=datetime.now(timezone.utc),
        category=category,
        severity=Severity.HIGH,
        affected_tickers=tickers or ["AAPL"],
        raw_text=text,
        supplier="TestCorp",
    )


def _mock_price(price=150.0):
    hist = pd.DataFrame({"Close": [price]})
    return patch('yfinance.Ticker', return_value=type('T', (), {
        'history': lambda *a, **kw: hist
    })())


@pytest.fixture
def pipeline(tmp_path):
    """Full pipeline stack with isolated SQLite DB."""
    trend = TrendAgent()
    # Pre-warm the macro cache now, while the conftest yfinance mock is active
    # (VIX=18, SPY bullish). Without this, the first analyze() call can happen
    # inside _mock_price which patches yfinance.Ticker to a single flat price,
    # making VIX=150 and triggering spurious suppression.
    trend._get_context()
    return (
        HadesAgent(),
        trend,
        PatternAgent(db_path=tmp_path / "trades.db"),
        MockExecutionAgent(slippage_bps=0),
    )


def run(hades, trend, pattern, execution, raw):
    """Run one signal through the pipeline. Returns (stage_killed, result)."""
    filtered = hades.filter(raw)
    if filtered is None:
        return "hades", None

    macro = trend.analyze(filtered)
    if macro.suppress:
        return "trend", None

    sized = pattern.size(filtered, macro)
    if sized.skip:
        return "pattern", None

    result = execution.place(sized)
    pattern.record_trade(sized, result)
    return None, result


# ── OFAC signals never reach execution ────────────────────────────────────────

class TestOfacNeverReachesExecution:
    def test_rusal_killed_at_hades(self, pipeline):
        h, t, p, e = pipeline
        with _mock_price():
            stage, result = run(h, t, p, e, _raw("RUSAL supply chain update"))
        assert stage == "hades"
        assert result is None

    def test_sberbank_killed_at_hades(self, pipeline):
        h, t, p, e = pipeline
        with _mock_price():
            stage, result = run(h, t, p, e, _raw("Sberbank credit expansion"))
        assert stage == "hades"
        assert result is None


# ── Extreme VIX suppresses all signals ────────────────────────────────────────

class TestVixSuppression:
    def test_all_categories_suppressed_at_extreme_vix(self, pipeline, tmp_path):
        h = HadesAgent()
        p = PatternAgent(db_path=tmp_path / "trades2.db")
        e = MockExecutionAgent()

        # Mock VIX at 40 (extreme)
        extreme_vix_hist = pd.DataFrame({"Close": [40.0]})
        normal_hist      = pd.DataFrame({"Close": [150.0]})

        def ticker_factory(symbol):
            if symbol == "^VIX":
                return type('T', (), {'history': lambda *a, **kw: extreme_vix_hist})()
            return type('T', (), {'history': lambda *a, **kw: normal_hist})()

        with patch('yfinance.Ticker', side_effect=ticker_factory), \
             patch('agents.artemis.yf.Ticker', side_effect=ticker_factory):
            t = TrendAgent(cache_ttl_seconds=0)
            for cat in [SignalCategory.POSITIVE_NEWS, SignalCategory.EARNINGS_SURPRISE,
                        SignalCategory.REGULATORY_ACTION]:
                stage, result = run(h, t, p, e, _raw("Big news", cat))
                assert stage == "trend", f"{cat} should be suppressed at VIX=40"
                assert result is None


# ── Clean signals reach execution ──────────────────────────────────────────────

class TestCleanSignalsExecute:
    def test_clean_positive_signal_produces_buy(self, pipeline):
        h, t, p, e = pipeline
        with _mock_price(150.0):
            stage, result = run(h, t, p, e, _raw("SAP record revenue quarter"))
        assert stage is None
        assert result is not None
        assert result.side == "BUY"
        assert result.symbol == "AAPL"
        assert result.fill_price == pytest.approx(150.0, rel=0.001)

    def test_disruption_signal_produces_sell(self, pipeline):
        h, t, p, e = pipeline
        raw = _raw("TSMC fab fire — production halted", SignalCategory.SUPPLIER_DISRUPTION)
        with _mock_price(100.0):
            stage, result = run(h, t, p, e, raw)
        assert stage is None
        assert result is not None
        assert result.side == "SELL"

    def test_trade_recorded_in_sqlite(self, pipeline, tmp_path):
        import sqlite3
        h, t, p, e = pipeline
        with _mock_price(100.0):
            run(h, t, p, e, _raw("NVIDIA beats estimates", SignalCategory.EARNINGS_SURPRISE))
        with sqlite3.connect(tmp_path / "trades.db") as conn:
            rows = conn.execute("SELECT COUNT(*) FROM trades").fetchone()
        assert rows[0] == 1


# ── R/R ratio maintained end-to-end ───────────────────────────────────────────

class TestRRRatio:
    def test_rr_ratio_is_2to1_through_full_pipeline(self, pipeline):
        h, t, p, e = pipeline
        with _mock_price(200.0):
            _, result = run(h, t, p, e, _raw("Apple partnership announced"))
        assert result is not None
        risk   = abs(result.fill_price - result.stop_loss_price)
        reward = abs(result.take_profit_price - result.fill_price)
        assert reward / risk == pytest.approx(2.0, rel=0.05)


# ── Position size sanity ───────────────────────────────────────────────────────

class TestPositionSizeSanity:
    def test_position_size_never_exceeds_5pct(self, pipeline):
        h, t, p, e = pipeline
        with _mock_price(100.0):
            _, result = run(h, t, p, e, _raw("Big tech news"))
        if result:
            # qty × price / account_equity ≤ 5%
            exposure = (result.qty * result.fill_price) / 100_000.0
            assert exposure <= 0.051  # tiny tolerance for rounding

    def test_cold_start_uses_2pct_default(self, pipeline):
        h, t, p, e = pipeline
        with _mock_price(100.0):
            _, result = run(h, t, p, e, _raw("Tech news"))
        if result:
            exposure = (result.qty * result.fill_price) / 100_000.0
            assert exposure == pytest.approx(0.02, abs=0.005)


# ── ESG downgrade does not kill — just reduces compliance score ────────────────

class TestEsgDowngradeNotKill:
    def test_esg_signal_reaches_execution_with_lower_score(self, pipeline):
        h, t, p, e = pipeline
        raw = _raw("Big tobacco company reports earnings", SignalCategory.EARNINGS_SURPRISE)
        with _mock_price(50.0):
            stage, result = run(h, t, p, e, raw)
        # ESG = downgrade, not kill → should reach execution
        assert result is not None
        assert stage is None
