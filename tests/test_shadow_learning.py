"""
Quality gate — Shadow Learning Layer.

Tests cover all 4 components:
  1. OutcomeResolver  — P&L calculation, Supabase backfill (mocked), KB update
  2. PromotionGate    — Bayesian shrinkage, trust thresholds, cold start
  3. Backtester       — KB replay through Hades→Pythia, outcome inference
  4. ReplayEngine     — trace reconstruction, agreement rate calculation

No network calls — all external dependencies mocked.
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from pathlib import Path

from core.shadow_learning import (
    OutcomeResolver, PromotionGate, Backtester, ReplayEngine,
    BacktestResult, ReplayResult, _MIN_SAMPLES, _PRIOR_WEIGHT, _PRIOR_WIN_RATE,
)
from core.types import (
    DecisionTrace, FilteredSignal, MacroContext, MarketRegime,
    RawSignal, Severity, SignalCategory, SizedSignal,
)


# ── Fixtures ───────────────────────────────────────────────────────────────────

def _make_trace(approved=True, pnl=0.04, regime="bull", vix=15.0) -> DecisionTrace:
    return DecisionTrace(
        trace_id           = "t-001",
        signal_id          = "s-001",
        timestamp          = datetime.now(timezone.utc),
        headline           = "NVDA earnings beat — record revenue quarter",
        supplier           = "TestCorp",
        category           = SignalCategory.EARNINGS_SURPRISE.value,
        severity           = Severity.HIGH.value,
        hades_passed       = True,
        hades_notes        = [],
        trend_suppressed   = False,
        trend_regime       = regime,
        trend_vix          = vix,
        pattern_confidence = 0.65,
        pattern_size_pct   = 0.02,
        zeus_reasoning     = "Strong earnings beat in bull regime",
        zeus_approved      = approved,
        zeus_override      = False,
        zeus_override_reason = None,
        trade_placed       = approved,
        symbol             = "NVDA",
        side               = "BUY",
        fill_price         = 200.0,
        pnl_pct            = pnl,
    )


def _make_sized(category=SignalCategory.EARNINGS_SURPRISE) -> SizedSignal:
    raw = RawSignal(
        signal_id="s-001", source_url="", headline="NVDA beat",
        summary="", published_at=datetime.now(timezone.utc),
        category=category, severity=Severity.HIGH,
        affected_tickers=["NVDA"], raw_text="test", supplier="TestCorp",
    )
    filtered = FilteredSignal(original=raw, compliance_score=1.0)
    macro = MacroContext(
        fetched_at=datetime.now(timezone.utc),
        regime=MarketRegime.BULL, vix=15.0, sp500_1m_return=0.03,
    )
    return SizedSignal(original=filtered, macro=macro,
                       confidence=0.65, position_size_pct=0.02)


# ── 1. OutcomeResolver ────────────────────────────────────────────────────────

class TestOutcomeResolver:
    def test_long_positive_pnl_calculated_correctly(self):
        resolver = OutcomeResolver()
        resolver.track_open("ord-1", fill_price=100.0, symbol="NVDA")
        pnl = resolver.resolve_closed("ord-1", exit_price=104.0, side="BUY")
        assert pnl == pytest.approx(0.04, rel=0.01)

    def test_long_negative_pnl_calculated_correctly(self):
        resolver = OutcomeResolver()
        resolver.track_open("ord-1", fill_price=100.0, symbol="NVDA")
        pnl = resolver.resolve_closed("ord-1", exit_price=97.0, side="BUY")
        assert pnl == pytest.approx(-0.03, rel=0.01)

    def test_short_pnl_inverted(self):
        resolver = OutcomeResolver()
        resolver.track_open("ord-1", fill_price=100.0, symbol="NVDA")
        # Short: price went down = profit
        pnl = resolver.resolve_closed("ord-1", exit_price=97.0, side="SELL")
        assert pnl == pytest.approx(0.03, rel=0.01)

    def test_unknown_order_id_returns_none(self):
        resolver = OutcomeResolver()
        result = resolver.resolve_closed("unknown-ord", exit_price=100.0, side="BUY")
        assert result is None

    def test_resolved_order_removed_from_tracking(self):
        resolver = OutcomeResolver()
        resolver.track_open("ord-1", fill_price=100.0, symbol="NVDA")
        assert resolver.open_count == 1
        resolver.resolve_closed("ord-1", exit_price=104.0, side="BUY")
        assert resolver.open_count == 0

    def test_kb_update_outcome_called_on_resolve(self):
        mock_kb = MagicMock()
        resolver = OutcomeResolver(knowledge_base=mock_kb)
        resolver.track_open("ord-1", fill_price=100.0, symbol="NVDA")
        with patch("core.shadow_learning.OutcomeResolver._backfill_supabase"):
            resolver.resolve_closed("ord-1", exit_price=104.0, side="BUY")
        mock_kb.update_outcome.assert_called_once_with("ord-1", pytest.approx(0.04, rel=0.01))

    def test_supabase_backfill_skipped_without_env(self):
        """No SUPABASE_URL → backfill silently skips, no exception."""
        import os
        os.environ.pop("SUPABASE_URL", None)
        resolver = OutcomeResolver()
        resolver.track_open("ord-1", fill_price=100.0, symbol="NVDA")
        # Should not raise
        resolver.resolve_closed("ord-1", exit_price=110.0, side="BUY")

    def test_zero_fill_price_returns_none(self):
        resolver = OutcomeResolver()
        resolver.track_open("ord-1", fill_price=0.0, symbol="NVDA")
        result = resolver.resolve_closed("ord-1", exit_price=100.0, side="BUY")
        assert result is None


# ── 2. PromotionGate ──────────────────────────────────────────────────────────

class TestPromotionGate:
    def test_none_stats_returns_cold_start(self):
        result = PromotionGate.evaluate(None)
        assert result["status"] == "cold_start"
        assert result["trusted"] is False
        assert result["confidence"] == _PRIOR_WIN_RATE

    def test_zero_n_returns_cold_start(self):
        result = PromotionGate.evaluate({"n": 0, "hit_rate": 0.8})
        assert result["status"] == "cold_start"
        assert result["trusted"] is False

    def test_below_min_samples_is_learning(self):
        result = PromotionGate.evaluate({"n": 5, "hit_rate": 0.8})
        assert result["status"] == "learning"
        assert result["trusted"] is False

    def test_at_min_samples_is_trusted(self):
        result = PromotionGate.evaluate({"n": _MIN_SAMPLES, "hit_rate": 0.7})
        assert result["status"] == "trusted"
        assert result["trusted"] is True

    def test_bayesian_shrinkage_reduces_extreme_rates(self):
        """100% win rate on 3 trades should be shrunk toward 50%."""
        result = PromotionGate.evaluate({"n": 3, "hit_rate": 1.0})
        assert result["shrunk_rate"] < 1.0
        assert result["shrunk_rate"] > 0.5

    def test_bayesian_shrinkage_formula(self):
        """Verify the exact formula: (n*obs + prior_weight*prior) / (n + prior_weight)."""
        n, obs = 20, 0.70
        expected = (n * obs + _PRIOR_WEIGHT * _PRIOR_WIN_RATE) / (n + _PRIOR_WEIGHT)
        result = PromotionGate.evaluate({"n": n, "hit_rate": obs})
        assert result["shrunk_rate"] == pytest.approx(expected, rel=0.001)

    def test_large_n_shrinkage_minimal(self):
        """With 1000 trades, shrinkage toward prior is tiny."""
        result = PromotionGate.evaluate({"n": 1000, "hit_rate": 0.70})
        assert result["shrunk_rate"] > 0.69   # barely moved from 0.70

    def test_small_n_shrinkage_significant(self):
        """With 5 trades, shrinkage toward 50% is significant."""
        result_small = PromotionGate.evaluate({"n": 5,   "hit_rate": 0.80})
        result_large = PromotionGate.evaluate({"n": 100, "hit_rate": 0.80})
        assert result_small["shrunk_rate"] < result_large["shrunk_rate"]

    def test_confidence_equals_shrunk_rate(self):
        result = PromotionGate.evaluate({"n": 15, "hit_rate": 0.65})
        assert result["confidence"] == result["shrunk_rate"]


# ── 3. Backtester ─────────────────────────────────────────────────────────────

class TestBacktester:
    def _make_agents(self, tmp_path):
        from agents.hades import HadesAgent
        from agents.pythia import PythiaAgent
        return HadesAgent(), PythiaAgent(db_path=tmp_path / "bt.db")

    def _make_macro(self):
        return MacroContext(
            fetched_at=datetime.now(timezone.utc),
            regime=MarketRegime.BULL, vix=16.0, sp500_1m_return=0.02,
        )

    def test_run_with_no_kb_returns_empty(self, tmp_path):
        hades, pythia = self._make_agents(tmp_path)
        bt = Backtester(hades, pythia, self._make_macro())
        result = bt.run(knowledge_base=None)
        assert result.total_signals == 0
        assert "No knowledge_base provided" in result.errors

    def test_run_with_mock_kb_processes_entries(self, tmp_path):
        hades, pythia = self._make_agents(tmp_path)
        mock_kb = MagicMock()
        mock_kb.query_knowledge.return_value = [
            "Earnings history: NVDA — 2024-01-15\nResult: BEAT | Reported EPS: 2.50 | Estimated: 2.30\n5-day price reaction: +4.20%\nPattern: NVDA earnings BEAT historically causes positive price movement.",
            "Earnings history: AAPL — 2024-04-15\nResult: MISS | Reported EPS: 1.20 | Estimated: 1.50\n5-day price reaction: -3.10%\nPattern: AAPL earnings MISS historically causes negative price movement.",
        ]
        bt = Backtester(hades, pythia, self._make_macro())
        result = bt.run(mock_kb)
        assert result.total_signals > 0
        assert isinstance(result.summary(), dict)

    def test_extract_ticker_from_earnings_text(self):
        text = "Earnings history: NVDA — 2024-01-15\nResult: BEAT"
        ticker = Backtester._extract_ticker(text)
        assert ticker == "NVDA"

    def test_extract_ticker_returns_none_for_unknown_format(self):
        ticker = Backtester._extract_ticker("some random text without ticker pattern")
        assert ticker is None

    def test_infer_outcome_earnings_beat_positive(self):
        text = "earnings beat positive price reaction"
        outcome = Backtester._infer_outcome(text, SignalCategory.EARNINGS_SURPRISE)
        assert outcome > 0

    def test_infer_outcome_earnings_miss_negative(self):
        text = "earnings miss negative price reaction"
        outcome = Backtester._infer_outcome(text, SignalCategory.EARNINGS_SURPRISE)
        assert outcome < 0

    def test_infer_outcome_supply_disruption_negative(self):
        text = "supply chain disruption component shortage"
        outcome = Backtester._infer_outcome(text, SignalCategory.SUPPLIER_DISRUPTION)
        assert outcome < 0

    def test_infer_outcome_insider_buy_positive(self):
        text = "insider open-market purchase buy shares"
        outcome = Backtester._infer_outcome(text, SignalCategory.POSITIVE_NEWS)
        assert outcome > 0

    def test_summary_dict_has_required_keys(self, tmp_path):
        hades, pythia = self._make_agents(tmp_path)
        bt = Backtester(hades, pythia, self._make_macro())
        result = bt.run(knowledge_base=None)
        summary = result.summary()
        for key in ("total_signals", "hades_killed", "pythia_sized",
                    "context_keys_seeded", "errors", "started_at", "finished_at"):
            assert key in summary


# ── 4. ReplayEngine ───────────────────────────────────────────────────────────

class TestReplayEngine:
    def _make_zeus_mock(self, approved=True):
        zeus = MagicMock()
        zeus.decide.return_value = {"approved": approved, "reasoning": "test reasoning"}
        return zeus

    def test_replay_trace_approved_to_rejected(self):
        zeus = self._make_zeus_mock(approved=False)
        engine = ReplayEngine(zeus)
        trace = _make_trace(approved=True)
        result = engine.replay_trace(trace)
        assert result is not None
        assert result.original_approved is True
        assert result.replay_approved is False
        assert result.changed_mind() is True
        assert result.agreement is False

    def test_replay_trace_agreement(self):
        zeus = self._make_zeus_mock(approved=True)
        engine = ReplayEngine(zeus)
        trace = _make_trace(approved=True)
        result = engine.replay_trace(trace)
        assert result.agreement is True
        assert result.changed_mind() is False

    def test_replay_trace_empty_headline_returns_none(self):
        zeus = self._make_zeus_mock()
        engine = ReplayEngine(zeus)
        trace = _make_trace()
        trace.headline = ""
        result = engine.replay_trace(trace)
        assert result is None

    def test_agreement_rate_all_agree(self):
        engine = ReplayEngine(MagicMock())
        results = [
            ReplayResult("t1", True, True, "", "", True),
            ReplayResult("t2", False, False, "", "", True),
        ]
        assert engine.agreement_rate(results) == 1.0

    def test_agreement_rate_none_agree(self):
        engine = ReplayEngine(MagicMock())
        results = [
            ReplayResult("t1", True, False, "", "", False),
            ReplayResult("t2", False, True, "", "", False),
        ]
        assert engine.agreement_rate(results) == 0.0

    def test_agreement_rate_empty_list(self):
        engine = ReplayEngine(MagicMock())
        assert engine.agreement_rate([]) == 0.0

    def test_replay_recent_with_empty_kb(self):
        zeus = self._make_zeus_mock()
        engine = ReplayEngine(zeus)
        mock_kb = MagicMock()
        mock_kb.get_recent_decisions.return_value = {"metadatas": [], "documents": []}
        results = engine.replay_recent(mock_kb, limit=10)
        assert results == []

    def test_replay_recent_processes_traces(self):
        zeus = self._make_zeus_mock(approved=True)
        engine = ReplayEngine(zeus)
        mock_kb = MagicMock()
        mock_kb.get_recent_decisions.return_value = {
            "metadatas": [{
                "trace_id":    "t-001",
                "signal_id":   "s-001",
                "timestamp":   datetime.now(timezone.utc).isoformat(),
                "category":    "earnings_surprise",
                "regime":      "bull",
                "vix":         15.0,
                "approved":    "True",
                "pnl_pct":     0.04,
            }],
            "documents": ["Signal: NVDA earnings beat\nSupplier: TestCorp"],
        }
        results = engine.replay_recent(mock_kb, limit=10)
        assert len(results) == 1
        assert results[0].trace_id == "t-001"

    def test_trace_to_macro_context_unknown_regime(self):
        trace = _make_trace()
        trace.trend_regime = "unknown_regime"
        macro = ReplayEngine._trace_to_macro_context(trace)
        assert macro.regime == MarketRegime.SIDEWAYS   # safe default

    def test_trace_to_filtered_signal_unknown_category(self):
        trace = _make_trace()
        trace.category = "nonexistent_category"
        signal = ReplayEngine._trace_to_filtered_signal(trace)
        assert signal.original.category == SignalCategory.POSITIVE_NEWS   # safe default


# ── Argus wiring ──────────────────────────────────────────────────────────────

class TestArgusOutcomeResolverWiring:
    def test_argus_has_outcome_resolver(self):
        from agents.argus import ArgusAgent
        argus = ArgusAgent()
        assert hasattr(argus, "outcome_resolver")
        assert isinstance(argus.outcome_resolver, OutcomeResolver)

    def test_argus_set_knowledge_base_wires_resolver(self):
        from agents.argus import ArgusAgent
        argus = ArgusAgent()
        mock_kb = MagicMock()
        argus.set_knowledge_base(mock_kb)
        assert argus.outcome_resolver._kb is mock_kb

    def test_argus_outcome_resolver_tracks_and_resolves(self):
        from agents.argus import ArgusAgent
        argus = ArgusAgent()
        argus.outcome_resolver.track_open("ord-99", fill_price=150.0, symbol="AAPL")
        with patch("core.shadow_learning.OutcomeResolver._backfill_supabase"):
            pnl = argus.outcome_resolver.resolve_closed("ord-99", exit_price=159.0, side="BUY")
        assert pnl == pytest.approx(0.06, rel=0.01)
