"""
Load ZEUS runtime settings from config/settings.json.
Provides typed defaults so the system runs out of the box.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_SETTINGS_FILE = Path(__file__).parent / "settings.json"

_DEFAULTS: dict[str, Any] = {
    # Pipeline
    "paper_trading": True,
    "mock_execution": True,
    "max_drawdown_pct": 0.08,
    "max_open_positions": 3,        # €4k account — max 3 concurrent positions
    "max_open_positions_per_ticker": 1,   # never hold more than 1 open position per ticker
    "max_deployed_pct": 0.90,       # never deploy >90% of equity — keep a cash buffer, grow not over-commit
    "ticker_cooldown_hours": 48,          # hours to wait before re-trading the same ticker
    "poll_interval_seconds": 900,
    "webhook_port": 8080,
    # Icarus reads signals from Supabase (written by hermes_local EDGAR/Finnhub fetcher)
    "signal_source": "supabase",
    # IBKR
    "ib_host": "127.0.0.1",
    "ib_paper_port": 4002,
    "ib_live_port": 4001,
    # Single source of truth for account equity — feeds BOTH the milestone
    # stage/tier gate and € position sizing (was split into default_account_equity
    # + starting_equity, which could drift and misclassify the stage).
    "default_account_equity": 4_000.0,
    # Risk parameters — Ares bracket order
    "stop_loss_pct": 0.03,        # 3% stop
    "take_profit_pct": 0.06,      # 6% target (2:1 R/R)
    # Outcome resolution — Argus backfills pnl_pct/hit when the IB bracket closes
    # a position. allow_approximate_exit OFF means a close with no trusted IB fill
    # is left open rather than backfilled with a guessed price — protects Pythia's
    # hit-rate data. Flip ON briefly to clear historical stuck trades.
    "outcome_resolution": {
        "allow_approximate_exit": False,
    },
    # ZEUS bull/bear debate — adversarial sub-step before the Director verdict.
    # Bull argues to take the trade, Bear rebuts (sees the bull's case), then
    # ZEUS adjudicates with both arguments in context. Strictly additive: if a
    # debate call fails, ZEUS falls back to single-shot reasoning.
    "use_debate": True,            # master switch — False reverts to single-shot
    "debate_rounds": 1,            # bull→bear pairs per signal (1 to start)
    "debate_max_tokens": 400,      # output cap per debate call
    # Anthropic model IDs — single source of truth (override per account/upgrade).
    "anthropic_model_director": "claude-sonnet-4-6",
    "anthropic_model_debate":   "claude-sonnet-4-6",
    # Confidence thresholds
    "zeus_min_confidence": 0.55,
    "pythia_default_confidence": 0.55,
    "pythia_min_confidence": 0.45,
    "pythia_tier1_confidence": 0.70,
    # Macro thresholds — Artemis
    "vix_medium": 15.0,
    "vix_high": 25.0,
    "vix_extreme": 35.0,
    "bull_threshold": 0.02,       # SPY 1m return above this = bull
    "bear_threshold": -0.03,      # SPY 1m return below this = bear
    # Atlas — self-sourced universe screener (Phase 2)
    "universe_screener_enabled":       False,   # dark by default — flip to True to activate
    "universe_market_cap_min":         5e9,     # $5B floor
    "universe_market_cap_max":         2e12,    # $2T ceiling (exclude hyper-mega-caps)
    "universe_min_beta":               0.5,     # exclude sleepy low-beta names
    "universe_min_avg_dollar_volume":  5e6,     # $5M/day minimum liquidity
    "universe_min_price":              10.0,    # $10 floor — no penny stocks
    "universe_momentum_window_days":   20,      # trading days for momentum measurement
    "universe_momentum_threshold":     0.05,    # 5% return over window to qualify
    "universe_volume_spike_mult":      1.5,     # current volume must be > 1.5× 20-day avg
    "universe_max_candidates":         50,      # cap screener input size
    # Ares — ATR-based volatility-aware stops (Phase 3)
    "use_atr_stops":       False,   # dark by default — flip to True to activate
    "atr_period":          14,      # ATR lookback window (trading days)
    "atr_stop_multiple":   2.0,     # stop = entry ± (ATR × multiple)
    "min_stop_pct":        0.03,    # floor: stop never tighter than 3%
    "max_stop_pct":        0.12,    # ceiling: stop never wider than 12%
    "reward_risk_ratio":   2.0,     # take-profit = entry ± (stop_distance × ratio)
}


def load_settings() -> dict[str, Any]:
    if _SETTINGS_FILE.exists():
        with open(_SETTINGS_FILE, encoding="utf-8") as f:
            user = json.load(f)
        return {**_DEFAULTS, **user}
    return dict(_DEFAULTS)
