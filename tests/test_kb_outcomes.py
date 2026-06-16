"""
query_outcomes_by_context must count only CLOSED directional outcomes:
- pnl_pct None (open) excluded
- pnl_pct 0.0 (flat/breakeven/placeholder) excluded — not a loss
- string-stored pnl_pct coerced; non-numeric skipped
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.knowledge_base import KnowledgeBase


def _kb(metas):
    kb = KnowledgeBase.__new__(KnowledgeBase)   # skip __init__ (no Chroma needed)
    kb._decisions_col = MagicMock()
    kb._decisions_col.get.return_value = {"metadatas": metas}
    return kb


def test_no_rows_returns_empty():
    kb = _kb([])
    assert kb.query_outcomes_by_context("positive_news", "bull") == {}


def test_all_open_trades_excluded():
    # 4 approved-but-open trades (pnl_pct None) → no closed sample, n=0.
    kb = _kb([{"pnl_pct": None} for _ in range(4)])
    out = kb.query_outcomes_by_context("positive_news", "bull")
    assert out["n"] == 0
    assert out["win_rate"] == 0.0


def test_flat_zero_pnl_not_counted_as_loss():
    # 2 wins + 2 flat (0.0). Flat must NOT drag win_rate to 50% — n=2, win_rate=1.0.
    kb = _kb([{"pnl_pct": 0.04}, {"pnl_pct": 0.02}, {"pnl_pct": 0.0}, {"pnl_pct": 0.0}])
    out = kb.query_outcomes_by_context("earnings_surprise", "bull")
    assert out["n"] == 2
    assert out["win_rate"] == pytest.approx(1.0)


def test_mixed_closed_outcomes():
    # 2 wins, 2 losses, 3 open, 1 flat → n=4 closed directional, win_rate=0.5.
    metas = (
        [{"pnl_pct": 0.05}, {"pnl_pct": 0.03}]
        + [{"pnl_pct": -0.02}, {"pnl_pct": -0.04}]
        + [{"pnl_pct": None}, {"pnl_pct": None}, {"pnl_pct": None}]
        + [{"pnl_pct": 0.0}]
    )
    out = _kb(metas).query_outcomes_by_context("positive_news", "bull")
    assert out["n"] == 4
    assert out["win_rate"] == pytest.approx(0.5)


def test_string_pnl_coerced_and_nonnumeric_skipped():
    kb = _kb([{"pnl_pct": "0.04"}, {"pnl_pct": "-0.02"}, {"pnl_pct": "n/a"}])
    out = kb.query_outcomes_by_context("positive_news", "bull")
    assert out["n"] == 2  # "n/a" skipped
    assert out["win_rate"] == pytest.approx(0.5)
