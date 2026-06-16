"""
Tests for Pythia cold-start exploration (agents/pythia.py).

The deadlock it fixes: no closed trades → stats=None → confidence floors at 0.55
→ ZEUS vetoes → no trades close → forever. Exploration grants a bounded number
of PAPER-ONLY seeding sizings at a confidence above the floor so ZEUS lets them
through and Pythia gathers its first hit-rates.

Covers:
  - no history + paper + budget remaining → confidence bumped, is_exploration=True
  - real money (paper_trading=False) → NEVER bumps, stays floor 0.55
  - budget exhausted (closed >= _EXPLORATION_TRADES) → back to floor 0.55
  - history present (stats not None) → exploration irrelevant, uses real hit-rate
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import agents.pythia as pythia_mod
from agents.pythia import PythiaAgent
from core.types import (
    FilteredSignal,
    MacroContext,
    MarketRegime,
    RawSignal,
    Severity,
    SignalCategory,
)


def _filtered(ticker: str = "AAPL") -> FilteredSignal:
    raw = RawSignal(
        signal_id="explore-sig-1",
        source_url="https://example.com",
        headline=f"{ticker} earnings beat",
        summary="Beat by 10%",
        published_at=datetime.now(timezone.utc),
        category=SignalCategory.EARNINGS_SURPRISE,
        severity=Severity.HIGH,
        affected_tickers=[ticker],
        supplier=ticker,
    )
    return FilteredSignal(
        original=raw, compliance_score=0.9, esg_flag=False,
        ofac_flag=False, downgraded=False, notes=[],
    )


def _macro() -> MacroContext:
    return MacroContext(
        fetched_at=datetime.now(timezone.utc), regime=MarketRegime.BULL,
        vix=15.0, sp500_1m_return=0.03, suppress=False,
    )


@pytest.fixture
def pythia(tmp_path) -> PythiaAgent:
    # SQLite-backed (no Supabase env) so the test is hermetic.
    return PythiaAgent(db_path=tmp_path / "trade_log.db")


def _paper(value: bool):
    return patch("config.settings.load_settings", return_value={"paper_trading": value})


def test_paper_no_history_explores(pythia):
    with _paper(True):
        sized = pythia.size(_filtered(), _macro())
    assert sized.is_exploration is True
    assert sized.confidence == pytest.approx(pythia_mod._EXPLORATION_CONFIDENCE)
    assert sized.confidence > 0.55  # above the veto-prone floor
    assert sized.skip is False


def test_real_money_never_explores(pythia):
    with _paper(False):
        sized = pythia.size(_filtered(), _macro())
    assert sized.is_exploration is False
    assert sized.confidence == pytest.approx(0.55)  # untouched floor


def test_budget_exhausted_stops_exploring(pythia):
    with _paper(True), patch.object(
        pythia, "_total_closed_trades", return_value=pythia_mod._EXPLORATION_TRADES
    ):
        sized = pythia.size(_filtered(), _macro())
    assert sized.is_exploration is False
    assert sized.confidence == pytest.approx(0.55)


def test_history_overrides_exploration(pythia):
    # When real stats exist, exploration is irrelevant — use the observed hit-rate.
    with _paper(True), patch.object(
        pythia, "_lookup_stats", return_value={"n": 12, "hit_rate": 0.75}
    ):
        sized = pythia.size(_filtered(), _macro())
    assert sized.is_exploration is False
    assert sized.confidence == pytest.approx(0.75)
