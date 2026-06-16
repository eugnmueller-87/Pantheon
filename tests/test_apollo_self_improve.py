"""
Apollo._analyse_traces writes win-rate lines into zeus_skills.md (ZEUS reads it
every cycle). Open trades (pnl_pct None) and flat 0.0 must NOT count as
non-winners — mirrors the KB query fix (Section 1).
"""

from __future__ import annotations

from agents.apollo import ApolloAgent


def _meta(pnl, cat="positive_news", regime="bull", approved=True):
    return {"pnl_pct": pnl, "category": cat, "regime": regime, "approved": approved}


def test_all_open_trades_emit_no_win_rate():
    # 5 approved but all open (pnl_pct None) → no closed sample for the category,
    # so no "0% win rate over N" line (n=0). approved_count gate still met.
    metas = [_meta(None) for _ in range(5)]
    out = ApolloAgent._analyse_traces(metas)
    assert out is not None              # 5 approved → insight block emitted
    assert "0% win rate" not in out     # but NOT a poisoned 0% line
    assert "(n=0)" not in out           # category had no closed trades to list


def test_closed_wins_reported_without_open_drag():
    # 5 closed wins + 5 open. Win rate must be 100% over the 5 closed, not 50%.
    metas = [_meta(0.04) for _ in range(5)] + [_meta(None) for _ in range(5)]
    out = ApolloAgent._analyse_traces(metas)
    assert out is not None
    assert "100% win rate" in out
    assert "(n=5)" in out


def test_flat_zero_pnl_not_counted_as_loss():
    # 3 wins + 2 flat (0.0) + enough approvals. Flat must not drag win rate.
    metas = [_meta(0.03) for _ in range(3)] + [_meta(0.0) for _ in range(2)]
    out = ApolloAgent._analyse_traces(metas)
    assert out is not None
    assert "100% win rate" in out       # 3/3 closed, flats excluded
    assert "(n=3)" in out


def test_below_threshold_returns_none():
    metas = [_meta(0.04) for _ in range(4)]   # < 5 approved
    assert ApolloAgent._analyse_traces(metas) is None
