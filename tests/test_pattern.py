"""
Quality gate — Pattern Learner (position sizing + Kelly logic).

Position sizing directly determines how much real money is at risk.
A bug here that doubles the position size on every trade is catastrophic.
These tests verify the Kelly math and SQLite round-trip are correct.
"""

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agents.pythia import PythiaAgent
from agents.pythia import PythiaAgent as PatternAgent
from core.types import (
    FilteredSignal,
    MacroContext,
    MarketRegime,
    RawSignal,
    Severity,
    SignalCategory,
    SizedSignal,
    TradeResult,
)


@pytest.fixture
def tmp_db(tmp_path) -> Path:
    return tmp_path / "test_trade_log.db"


@pytest.fixture
def pattern(tmp_db) -> PatternAgent:
    return PatternAgent(db_path=tmp_db)


def _raw(category=SignalCategory.POSITIVE_NEWS) -> RawSignal:
    return RawSignal(
        signal_id=str(uuid.uuid4()),
        source_url="",
        headline="Test signal",
        summary="",
        published_at=datetime.now(timezone.utc),
        category=category,
        severity=Severity.MEDIUM,
        affected_tickers=["AAPL"],
        raw_text="test",
        supplier="TestCorp",
    )


def _filtered(category=SignalCategory.POSITIVE_NEWS) -> FilteredSignal:
    return FilteredSignal(original=_raw(category), compliance_score=1.0)


def _macro(regime=MarketRegime.BULL, vix=15.0) -> MacroContext:
    return MacroContext(
        fetched_at=datetime.now(timezone.utc),
        regime=regime, vix=vix, sp500_1m_return=0.03,
    )


def _result(order_id: str, symbol: str = "AAPL", pnl: float = None) -> TradeResult:
    return TradeResult(
        order_id=order_id, symbol=symbol, side="BUY",
        fill_price=150.0, qty=10, pnl_pct=pnl, status="simulated",
    )


# ── Default sizing (no history) ────────────────────────────────────────────────

class TestDefaultSizing:
    def test_cold_start_uses_default_size(self, pattern):
        sized = pattern.size(_filtered(), _macro())
        assert sized.position_size_pct == pytest.approx(0.02)

    def test_cold_start_uses_prior_confidence(self, pattern):
        sized = pattern.size(_filtered(), _macro())
        assert sized.confidence == pytest.approx(0.55)

    def test_cold_start_not_skipped(self, pattern):
        sized = pattern.size(_filtered(), _macro())
        assert sized.skip is False

    def test_low_confidence_signal_skipped(self, pattern):
        """Confidence below MIN_CONFIDENCE (0.45) must be skipped."""
        # Seed exactly 10 trades all losing to drive win rate to 0.0
        f = _filtered()
        m = _macro()
        for i in range(10):
            r = _result(f"lose-{i}", pnl=-0.03)
            pattern.record_trade(
                SizedSignal(original=f, macro=m, confidence=0.55, position_size_pct=0.02),
                r,
            )
        sized = pattern.size(f, m)
        assert sized.skip is True
        assert sized.skip_reason is not None


# ── Kelly sizing math ──────────────────────────────────────────────────────────

class TestKellySizing:
    def _seed_win_rate(self, pattern, win_rate: float, n: int = 10):
        f = _filtered()
        m = _macro()
        wins  = int(n * win_rate)
        for i in range(n):
            pnl = 0.06 if i < wins else -0.03
            r = _result(f"trade-{i}", pnl=pnl)
            pattern.record_trade(
                SizedSignal(original=f, macro=m, confidence=0.55, position_size_pct=0.02),
                r,
            )
        return f, m

    def test_50pct_win_rate_gives_default_size(self, pattern):
        f, m = self._seed_win_rate(pattern, win_rate=0.5)
        sized = pattern.size(f, m)
        assert sized.position_size_pct == pytest.approx(0.02, abs=0.001)

    def test_higher_win_rate_gives_larger_size(self, pattern):
        f, m = self._seed_win_rate(pattern, win_rate=0.7)
        sized = pattern.size(f, m)
        assert sized.position_size_pct > 0.02

    def test_size_never_exceeds_5pct_cap(self, pattern):
        """Even at 100% win rate, size must be capped at 5%."""
        f, m = self._seed_win_rate(pattern, win_rate=1.0, n=20)
        sized = pattern.size(f, m)
        assert sized.position_size_pct <= 0.05

    def test_size_never_below_2pct_floor(self, pattern):
        """Size should not drop below 2% just because win rate is mediocre."""
        f, m = self._seed_win_rate(pattern, win_rate=0.5)
        sized = pattern.size(f, m)
        assert sized.position_size_pct >= 0.02

    def test_kelly_formula_at_60pct(self, pattern):
        """At 60% win rate: edge=0.2, size=0.02 + 0.2*0.03 = 0.026"""
        f, m = self._seed_win_rate(pattern, win_rate=0.6, n=20)
        sized = pattern.size(f, m)
        assert sized.position_size_pct == pytest.approx(0.026, abs=0.005)


# ── Context key isolation ──────────────────────────────────────────────────────

class TestContextKeyIsolation:
    def test_different_regimes_use_different_stats(self, pattern):
        f_bull = _filtered()
        f_bear = _filtered()
        m_bull = _macro(regime=MarketRegime.BULL, vix=12.0)
        m_bear = _macro(regime=MarketRegime.BEAR, vix=30.0)

        # Seed bull with 10 wins
        for i in range(10):
            pattern.record_trade(
                SizedSignal(original=f_bull, macro=m_bull, confidence=0.55, position_size_pct=0.02),
                _result(f"bull-{i}", pnl=0.06),
            )
        # Bear has no history → default
        sized_bull = pattern.size(f_bull, m_bull)
        sized_bear = pattern.size(f_bear, m_bear)
        assert sized_bull.confidence > sized_bear.confidence

    def test_different_categories_use_different_stats(self, pattern):
        m = _macro()
        f_pos = _filtered(SignalCategory.POSITIVE_NEWS)
        f_dis = _filtered(SignalCategory.SUPPLIER_DISRUPTION)

        for i in range(10):
            pattern.record_trade(
                SizedSignal(original=f_pos, macro=m, confidence=0.55, position_size_pct=0.02),
                _result(f"pos-{i}", pnl=0.06),
            )
        sized_pos = pattern.size(f_pos, m)
        sized_dis = pattern.size(f_dis, m)
        assert sized_pos.confidence != sized_dis.confidence


# ── SQLite round-trip ──────────────────────────────────────────────────────────

class TestSQLiteRoundTrip:
    def test_trade_recorded_and_retrievable(self, pattern, tmp_db):
        f = _filtered()
        m = _macro()
        sized = SizedSignal(original=f, macro=m, confidence=0.55, position_size_pct=0.02)
        r = _result("order-abc", pnl=0.03)
        pattern.record_trade(sized, r)

        with sqlite3.connect(tmp_db) as conn:
            rows = conn.execute("SELECT * FROM trades WHERE trade_id = 'order-abc'").fetchall()
        assert len(rows) == 1

    def test_open_trade_pnl_null_not_counted(self, pattern):
        """Trades with pnl_pct=None (still open) must not count in win rate stats."""
        f = _filtered()
        m = _macro()
        for i in range(15):
            pattern.record_trade(
                SizedSignal(original=f, macro=m, confidence=0.55, position_size_pct=0.02),
                _result(f"open-{i}", pnl=None),  # still open
            )
        # Should fall back to default (no closed trades counted)
        sized = pattern.size(f, m)
        assert sized.confidence == pytest.approx(0.55)

    def test_db_schema_has_required_columns(self, pattern, tmp_db):
        with sqlite3.connect(tmp_db) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(trades)")}
        required = {"trade_id", "signal_id", "context_key", "category",
                    "regime", "vix_band", "confidence", "position_pct",
                    "symbol", "side", "fill_price", "pnl_pct", "hit", "recorded_at"}
        assert required.issubset(cols)


# ── VIX band classification ────────────────────────────────────────────────────

class TestVixBand:
    def test_bands(self):
        assert PythiaAgent._vix_band(10.0) == "low"
        assert PythiaAgent._vix_band(14.9) == "low"
        assert PythiaAgent._vix_band(15.0) == "medium"
        assert PythiaAgent._vix_band(24.9) == "medium"
        assert PythiaAgent._vix_band(25.0) == "high"
        assert PythiaAgent._vix_band(34.9) == "high"
        assert PythiaAgent._vix_band(35.0) == "extreme"
        assert PythiaAgent._vix_band(50.0) == "extreme"


# ── Health check ──────────────────────────────────────────────────────────────

class TestPatternHealth:
    def test_healthy_with_valid_db(self, pattern):
        from core.types import AgentHealth
        assert pattern.health() == AgentHealth.HEALTHY
