"""
Quality gate — core/types.py contracts.

The data contracts are the backbone of the entire pipeline.
If a field is missing, mistyped, or a property breaks, every
downstream agent silently misbehaves. These tests are the spec.
"""

from dataclasses import fields
from datetime import datetime, timezone

import pytest

from core.types import (
    AgentHealth,
    DecisionTrace,
    FilteredSignal,
    MacroContext,
    MarketRegime,
    PipelineStatus,
    RawSignal,
    Severity,
    SignalCategory,
    SizedSignal,
    TradeResult,
)


def _raw(category=SignalCategory.POSITIVE_NEWS, severity=Severity.MEDIUM) -> RawSignal:
    return RawSignal(
        signal_id="s-001",
        source_url="https://mock",
        headline="Test headline",
        summary="Test summary",
        published_at=datetime.now(timezone.utc),
        category=category,
        severity=severity,
        affected_tickers=["AAPL"],
        raw_text="Test raw text",
        supplier="TestCorp",
    )


def _filtered(raw=None) -> FilteredSignal:
    return FilteredSignal(
        original=raw or _raw(),
        compliance_score=0.9,
    )


def _macro(regime=MarketRegime.BULL, vix=15.0) -> MacroContext:
    return MacroContext(
        fetched_at=datetime.now(timezone.utc),
        regime=regime,
        vix=vix,
        sp500_1m_return=0.03,
        sector_momentum={"tech": 0.05},
    )


# ── RawSignal ──────────────────────────────────────────────────────────────────

class TestRawSignal:
    def test_required_fields_present(self):
        sig = _raw()
        assert sig.signal_id == "s-001"
        assert sig.category == SignalCategory.POSITIVE_NEWS
        assert sig.severity == Severity.MEDIUM

    def test_default_tickers_is_list(self):
        sig = RawSignal(
            signal_id="x", source_url="", headline="", summary="",
            published_at=datetime.now(timezone.utc),
            category=SignalCategory.NEUTRAL,
            severity=Severity.LOW,
        )
        assert isinstance(sig.affected_tickers, list)

    def test_two_instances_dont_share_ticker_list(self):
        a = _raw()
        b = _raw()
        a.affected_tickers.append("EXTRA")
        assert "EXTRA" not in b.affected_tickers


# ── FilteredSignal pass-throughs ───────────────────────────────────────────────

class TestFilteredSignalProperties:
    def test_signal_id_passthrough(self):
        f = _filtered(_raw())
        assert f.signal_id == "s-001"

    def test_headline_passthrough(self):
        raw = _raw()
        raw.headline = "Breaking news"
        f = _filtered(raw)
        assert f.headline == "Breaking news"

    def test_tickers_passthrough(self):
        raw = _raw()
        raw.affected_tickers = ["NVDA", "TSM"]
        f = _filtered(raw)
        assert f.affected_tickers == ["NVDA", "TSM"]

    def test_supplier_passthrough(self):
        raw = _raw()
        raw.supplier = "ASML"
        f = _filtered(raw)
        assert f.supplier == "ASML"

    def test_category_passthrough(self):
        raw = _raw(category=SignalCategory.REGULATORY_ACTION)
        f = _filtered(raw)
        assert f.category == SignalCategory.REGULATORY_ACTION

    def test_severity_passthrough(self):
        raw = _raw(severity=Severity.CRITICAL)
        f = _filtered(raw)
        assert f.severity == Severity.CRITICAL

    def test_default_flags_are_false(self):
        f = _filtered()
        assert f.esg_flag is False
        assert f.ofac_flag is False
        assert f.downgraded is False
        assert f.notes == []


# ── MacroContext ───────────────────────────────────────────────────────────────

class TestMacroContext:
    def test_bull_properties(self):
        m = _macro(regime=MarketRegime.BULL, vix=12.0)
        assert m.is_bull is True
        assert m.is_bear is False
        assert m.is_high_volatility is False

    def test_bear_properties(self):
        m = _macro(regime=MarketRegime.BEAR, vix=30.0)
        assert m.is_bear is True
        assert m.is_bull is False
        assert m.is_high_volatility is True

    def test_high_volatility_threshold(self):
        assert _macro(vix=24.9).is_high_volatility is False
        assert _macro(vix=25.0).is_high_volatility is False  # boundary: > 25, not >=
        assert _macro(vix=25.1).is_high_volatility is True

    def test_suppress_defaults_false(self):
        m = _macro()
        assert m.suppress is False
        assert m.suppress_reason is None

    def test_regime_is_str_enum(self):
        assert MarketRegime.BULL.value == "bull"
        assert MarketRegime.BEAR.value == "bear"
        assert MarketRegime.SIDEWAYS.value == "sideways"
        assert MarketRegime.UNKNOWN.value == "unknown"


# ── SizedSignal ────────────────────────────────────────────────────────────────

class TestSizedSignal:
    def _sized(self, confidence=0.65, size=0.02, skip=False) -> SizedSignal:
        return SizedSignal(
            original=_filtered(),
            macro=_macro(),
            confidence=confidence,
            position_size_pct=size,
            skip=skip,
        )

    def test_passthrough_properties(self):
        s = self._sized()
        assert s.signal_id == "s-001"
        assert s.supplier == "TestCorp"
        assert s.category == SignalCategory.POSITIVE_NEWS
        assert s.severity == Severity.MEDIUM
        assert s.headline == "Test headline"
        assert s.affected_tickers == ["AAPL"]

    def test_skip_defaults_false(self):
        s = self._sized()
        assert s.skip is False
        assert s.skip_reason is None


# ── TradeResult ────────────────────────────────────────────────────────────────

class TestTradeResult:
    def test_default_status(self):
        r = TradeResult(order_id="x", symbol="AAPL", side="BUY",
                        fill_price=150.0, qty=10)
        assert r.status == "submitted"

    def test_optional_fields_default_none(self):
        r = TradeResult(order_id="x", symbol="AAPL", side="BUY",
                        fill_price=150.0, qty=10)
        assert r.stop_loss_price is None
        assert r.take_profit_price is None
        assert r.pnl_pct is None


# ── DecisionTrace ──────────────────────────────────────────────────────────────

class TestDecisionTrace:
    def _trace(self) -> DecisionTrace:
        return DecisionTrace(
            trace_id="tr-001",
            signal_id="s-001",
            timestamp=datetime.now(timezone.utc),
            headline="Test",
            supplier="TestCorp",
            category="positive_news",
            severity="MEDIUM",
            hades_passed=True,
        )

    def test_defaults(self):
        t = self._trace()
        assert t.zeus_approved is False
        assert t.trade_placed is False
        assert t.pnl_pct is None
        assert t.killed_at_stage is None
        assert t.zeus_override is False
        assert t.hades_notes == []

    def test_all_fields_serialisable(self):
        import json
        t = self._trace()
        # Should not raise
        json.dumps({
            "trace_id": t.trace_id,
            "approved": t.zeus_approved,
            "pnl": t.pnl_pct,
            "ts": t.timestamp.isoformat(),
        })


# ── Enumerations ───────────────────────────────────────────────────────────────

class TestEnumerations:
    def test_signal_category_values(self):
        expected = {"supplier_disruption", "positive_news", "earnings_surprise",
                    "regulatory_action", "macro_shift", "neutral"}
        assert {c.value for c in SignalCategory} == expected

    def test_severity_ordering(self):
        assert Severity.LOW.value < Severity.MEDIUM.value < Severity.HIGH.value < Severity.CRITICAL.value

    def test_agent_health_has_three_states(self):
        assert len(AgentHealth) == 3

    def test_pipeline_status_values(self):
        values = {s.value for s in PipelineStatus}
        assert "running" in values
        assert "halted" in values
