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
        # Equity used for sizing + the milestone gate when broker == "ctrader".
        # None → falls back to the global default_account_equity (4000, which the
        # IBKR path keeps). For a small demo set e.g. 500.0 here, or env
        # CTRADER_ACCOUNT_EQUITY=500 (env wins). Never the broker's raw balance.
        "account_equity":        None,
        # Sizing model on the cTrader path:
        #   "risk"     (default) — units = equity × risk_per_trade_pct / stop loss per unit
        #   "notional" (legacy)  — units = equity × position_size_pct × sizing_leverage / price
        "sizing_mode":           "risk",
        # Fraction of equity LOST if the stop is hit (1% default). Pythia/ZEUS/
        # seniority/milestone sizes act as a CEILING on this fraction on the
        # cTrader path — they can lower the risk, never raise it above this value.
        "risk_per_trade_pct":    0.01,
        # If the risk-sized volume is below the venue min lot, use the min lot only
        # when the min lot's stop-out LOSS is <= this multiple of the risk budget;
        # otherwise skip the trade and log intended vs min-lot risk.
        "max_min_lot_risk_overshoot": 1.5,
        # ZEUS budget gate on the cTrader path (replaces the notional
        # max_deployed_pct check, which is meaningless for risk-sized CFDs):
        # sum of open-trade risk + this trade's risk must stay <= this × equity.
        "max_open_risk_pct":     0.03,
        # Optional risk ramp (core/trade_management.RiskRamp). DISABLED by default:
        # risk stays at risk_per_trade_pct. When enabled, risk steps up to
        # risk_ramp_high_pct only after risk_ramp_min_trades closed trades whose
        # MEAN pnl_pct (the last risk_ramp_min_trades of them) is > 0, and drops
        # back to risk_per_trade_pct after a risk_ramp_drawdown_revert_pct
        # drawdown from peak equity (or if that expectancy turns <= 0). Upstream
        # caps (Pythia / ZEUS / seniority) can still only LOWER the risk.
        # Env CTRADER_RISK_RAMP=true|false overrides "enabled".
        "risk_ramp": {
            "enabled":                       False,
            "risk_ramp_high_pct":            0.02,
            "risk_ramp_min_trades":          50,
            "risk_ramp_drawdown_revert_pct": 0.10,
        },
        # LEGACY notional mode only: leverage applied to notional sizing. 1.0 =
        # unlevered. Clamped to the ESMA caps below.
        "sizing_leverage":       1.0,
        # LEGACY notional mode only: min-lot bump tolerance in NOTIONAL terms.
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
    # Signal universe for the cTrader path (core/fx_signals.py). Only consulted
    # when broker == "ctrader": the FX/index generator then REPLACES the
    # Supabase/EDGAR/Finnhub stock signals in Icarus. broker == "ibkr" never
    # touches this block. Bars come from yfinance by default (no credentials).
    "ctrader_signals": {
        "enabled": True,
        # "yfinance" (default) | "ctrader_trendbars" (hook only: used when the
        # connected adapter exposes get_trendbars(); otherwise falls back to yfinance)
        "source": "yfinance",
        # Logical cTrader names to scan. None → every key of ctrader.instruments.
        "instruments": None,
        # Logical name → yfinance ticker. US500 may use "ES=F" (futures, ~23h
        # session) instead of the cash index "^GSPC"; same for "NQ=F" / USTEC.
        "yfinance_tickers": {
            "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X",
            "US500": "^GSPC", "USTEC": "^NDX", "DE40": "^GDAXI",
        },
        # Which strategy runs for which asset class.
        "strategy_by_asset_class": {"index": "breakout", "fx": "mean_reversion"},
        # Index CFDs — Donchian breakout with trend + volatility filters, daily bars.
        "breakout": {
            "interval": "1d", "period": "1y",
            "donchian_window":   20,      # close beyond the prior 20-bar high/low
            "trend_ma_period":   50,      # long only above SMA50, short only below (None = off)
            "atr_period":        14,
            "atr_stop_multiple": 2.0,     # stop = entry ∓ 2 × ATR
            "min_stop_pct":      0.004,   # stop distance floor/ceiling as fraction of price
            "max_stop_pct":      0.03,
            "min_atr_pct":       0.004,   # skip dead markets: ATR < 0.4% of price
            "max_atr_pct":       0.03,    # skip disorderly markets: ATR > 3% of price
            "max_atr_expansion": None,    # breakouts tolerate expansion; set e.g. 3.0 to cap
            "atr_expansion_lookback": 100,
            "reward_risk":       2.0,
            "allow_short":       True,
            "session":           "any",   # weekdays only
            "max_bar_age_hours": 96,      # ignore stale data (covers a weekend)
            # Trade management (core/trade_management.py) — both DISABLED by default.
            # trailing_stop: after +trail_activation_R, trail the stop at
            #   trail_atr_multiple × ATR — style "atr" (from the latest price) or
            #   "chandelier" (from the best price since entry). Only ever tightens.
            #   use_fixed_target=False drops the fixed reward_risk TP so winners run
            #   (only honoured while enabled=True). Env CTRADER_TRAILING_BREAKOUT=true|false.
            "trailing_stop": {
                "enabled":            False,
                "style":              "atr",     # "atr" | "chandelier"
                "trail_activation_R": 1.0,
                "trail_atr_multiple": 3.0,
                "use_fixed_target":   True,
            },
            # pyramiding: add to a WINNER only — at >= pyramid_trigger_R beyond the
            #   last entry, first move the stop to break-even (+ breakeven_buffer_R × R
            #   in the trade's favour), then add one leg risking at most
            #   ctrader.risk_per_trade_pct. Never on 0R / losers. Account + symbol open
            #   risk <= ctrader.max_open_risk_pct, ESMA and min-lot rules unchanged.
            #   Env CTRADER_PYRAMID_BREAKOUT=true|false.
            "pyramiding": {
                "enabled":            False,
                "pyramid_trigger_R":  1.0,
                "pyramid_max_adds":   2,
                "breakeven_buffer_R": 0.0,
            },
        },
        # FX majors — z-score mean reversion vs SMA with session + volatility filters, hourly bars.
        "mean_reversion": {
            "interval": "1h", "period": "60d",
            "ma_period":         50,
            "entry_z":           2.0,     # fade when |z| >= 2.0 …
            "max_z":             3.5,     # … but not beyond 3.5 (likely news / trend break)
            "atr_period":        14,
            "atr_stop_multiple": 2.0,
            "min_stop_pct":      0.001,   # ≈ 10 pips on EURUSD
            "max_stop_pct":      0.01,
            "min_atr_pct":       0.0003,
            "max_atr_pct":       0.005,
            "max_atr_expansion": 2.0,     # skip when ATR > 2× its recent average (blow-off)
            "atr_expansion_lookback": 100,
            "reward_risk":       2.0,
            # "london_ny_overlap" (default) | "london_or_ny" | "any" — evaluated in
            # Europe/London and America/New_York local time (DST-correct).
            "session":           "london_ny_overlap",
            "max_bar_age_hours": 3,
            # Trade management (core/trade_management.py) — both DISABLED by default.
            # trailing_stop: after +trail_activation_R, trail the stop at
            #   trail_atr_multiple × ATR — style "atr" (from the latest price) or
            #   "chandelier" (from the best price since entry). Only ever tightens.
            #   use_fixed_target=False drops the fixed reward_risk TP so winners run
            #   (only honoured while enabled=True). Env CTRADER_TRAILING_MEAN_REVERSION=true|false.
            "trailing_stop": {
                "enabled":            False,
                "style":              "atr",     # "atr" | "chandelier"
                "trail_activation_R": 1.0,
                "trail_atr_multiple": 2.0,
                "use_fixed_target":   True,
            },
            # pyramiding: add to a WINNER only — at >= pyramid_trigger_R beyond the
            #   last entry, first move the stop to break-even (+ breakeven_buffer_R × R
            #   in the trade's favour), then add one leg risking at most
            #   ctrader.risk_per_trade_pct. Never on 0R / losers. Account + symbol open
            #   risk <= ctrader.max_open_risk_pct, ESMA and min-lot rules unchanged.
            #   Env CTRADER_PYRAMID_MEAN_REVERSION=true|false.
            "pyramiding": {
                "enabled":            False,
                "pyramid_trigger_R":  1.0,
                "pyramid_max_adds":   2,
                "breakeven_buffer_R": 0.0,
            },
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
