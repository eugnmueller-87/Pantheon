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
    # Execution venue: "ibkr" (default, IB Gateway) | "ctrader" (IC Markets EU via
    # cTrader Open API). Env BROKER overrides. Ignored when mock_execution=True.
    "broker": "ibkr",
    # cTrader — only read when broker == "ctrader". Credentials are env-only
    # (CTRADER_* in .env), never here.
    "ctrader": {
        "account_currency":      "EUR",
        # Risk leverage used for SIZING. 1.0 = unlevered notional
        # (equity × position_size_pct). The Argus 8% drawdown kill assumes this —
        # do not raise it to the broker's 1:30. Clamped to the caps below.
        "sizing_leverage":       1.0,
        # If the floored size is below the venue min lot, bump to the min lot only
        # when that is <= this multiple of the intended notional; otherwise skip.
        "max_min_lot_overshoot": 1.5,
        "entry_order_type":      "MARKET",   # MARKET (relative SL/TP) | LIMIT at mid (absolute SL/TP)
        "heartbeat_interval_s":  9.0,        # cTrader requires a heartbeat at least every 10s
        "request_timeout_s":     10.0,
        "deal_lookback_days":    7,          # closing-deal window for Argus outcome backfill
        # IC Markets EU retail (ESMA) maximum leverage per asset class. Recorded so
        # we never request more than the legal maximum; values here can only
        # TIGHTEN the built-in ESMA caps (core/brokers/ctrader_instruments.py).
        "leverage_caps": {"fx": 30.0, "index": 20.0, "commodity": 10.0, "stock": 5.0, "crypto": 2.0},
        # Tradable universe on the cTrader path: FX majors + index CFDs (no stocks —
        # IC Markets EU offers stock CFDs on MT5 only). These are typical IC Markets /
        # cTrader names and MUST be confirmed against the demo/live symbol list in
        # scripts/ctrader_smoke_test.py; brokers sometimes use US30, NAS100, GER40, etc.
        # Each logical name resolves to the FIRST alias the broker lists.
        "instruments": {
            "EURUSD": {"asset_class": "fx",    "quote_ccy": "USD", "aliases": ["EURUSD"]},
            "GBPUSD": {"asset_class": "fx",    "quote_ccy": "USD", "aliases": ["GBPUSD"]},
            "USDJPY": {"asset_class": "fx",    "quote_ccy": "JPY", "aliases": ["USDJPY"]},
            "US500":  {"asset_class": "index", "quote_ccy": "USD", "aliases": ["US500", "SPX500", "US 500", "USA500"]},
            "USTEC":  {"asset_class": "index", "quote_ccy": "USD", "aliases": ["USTEC", "NAS100", "US100", "USTECH"]},
            "DE40":   {"asset_class": "index", "quote_ccy": "EUR", "aliases": ["DE40", "GER40", "DAX40", "GER30", "DE30"]},
        },
    },
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
        merged = {**_DEFAULTS, **user}
        # One-level merge for nested sections (e.g. a partial "ctrader" block in
        # settings.json keeps the remaining defaults).
        for key, default in _DEFAULTS.items():
            if isinstance(default, dict) and isinstance(user.get(key), dict):
                merged[key] = {**default, **user[key]}
        return merged
    return dict(_DEFAULTS)
