"""
Trade management for the cTrader FX / index path: trailing stops, pyramiding
into winners, and an optional risk ramp. EVERYTHING HERE IS DISABLED BY DEFAULT;
with the defaults the system behaves exactly as before (fixed reward_risk
target + static ATR initial stop, flat risk_per_trade_pct, no adds).

Pure logic only — no broker, no network. Ares owns the live wiring (it is the
only agent that touches orders); this module just computes.

Config (config/settings.py)
---------------------------
  ctrader_signals.<strategy>.trailing_stop   (strategy = "breakout" | "mean_reversion")
      enabled, style ("atr" | "chandelier"), trail_activation_R,
      trail_atr_multiple, use_fixed_target
  ctrader_signals.<strategy>.pyramiding
      enabled, pyramid_trigger_R, pyramid_max_adds, breakeven_buffer_R
  ctrader.risk_ramp
      enabled, risk_ramp_high_pct, risk_ramp_min_trades, risk_ramp_drawdown_revert_pct
  plus the existing ctrader.risk_per_trade_pct / ctrader.max_open_risk_pct.
  Env on/off overrides (non-empty wins over settings):
      CTRADER_TRAILING_BREAKOUT, CTRADER_TRAILING_MEAN_REVERSION,
      CTRADER_PYRAMID_BREAKOUT,  CTRADER_PYRAMID_MEAN_REVERSION, CTRADER_RISK_RAMP

Definitions
-----------
  R              = the INITIAL stop distance |entry − initial stop| of the first leg.
  r_multiple     = (price − entry) / R for a long, (entry − price) / R for a short.

Trailing stop (per strategy)
  - The initial stop is unchanged (ATR multiple with floor/ceiling, fx_signals).
  - Activation: once the best price since entry (highest high for a long,
    lowest low for a short) reaches +trail_activation_R.
  - Candidate (long; mirror for short):
        "atr":        last close        − trail_atr_multiple × ATR
        "chandelier": highest high since entry − trail_atr_multiple × ATR
  - stop = max(stop, candidate) for a long / min(...) for a short: the stop only
    ever TIGHTENS, it is never loosened and never moved to add risk.
  - use_fixed_target=True keeps the reward_risk TP alongside the trail;
    False (only while trailing is enabled) places NO take-profit.

Pyramiding (per strategy)
  - Never on a position at 0R or below (hard rule, checked twice).
  - The next add needs price >= pyramid_trigger_R × R beyond the MOST RECENT
    entry (first add: beyond the original entry). At most pyramid_max_adds.
  - Before the add, every leg's stop moves to break-even: original entry
    ± breakeven_buffer_R × R in the trade's favour (never loosened).
  - The add's stop is that break-even stop; it is sized with the normal risk
    sizer at most ctrader.risk_per_trade_pct (the BASE risk, never the ramp).
  - Symbol AND account open risk (to the current stops) + the add must stay
    <= ctrader.max_open_risk_pct × equity; combined symbol notional must stay
    <= equity × ESMA cap; the min-lot overshoot rule applies unchanged.
    Any breach skips the add — rules are never relaxed to force one.
  - No martingale: an add only ever follows a winner with BE locked; a loss
    never increases size.

Risk ramp (account-wide)
  - expectancy = arithmetic MEAN of the outcome values of the most recent
    risk_ramp_min_trades closed trades (ZEUS feeds Pythia's pnl_pct). It is a
    simple sign check, not a validated edge metric.
  - base → high only when >= risk_ramp_min_trades closed trades AND
    expectancy > 0 AND drawdown from peak < risk_ramp_drawdown_revert_pct.
  - high → base on a drawdown >= risk_ramp_drawdown_revert_pct from peak (and
    re-ramping then waits until equity is back at that peak), or when the
    expectancy is no longer positive.
  - Every step change is logged at INFO with the reason.

Imports stdlib + config only (pandas optional for trailing_stop_path).
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

logger = logging.getLogger("trade_management")

TRAIL_STYLES = ("atr", "chandelier")
STRATEGY_ENV_KEYS = {"breakout": "BREAKOUT", "mean_reversion": "MEAN_REVERSION"}

_EPS = 1e-12


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TrailingStopConfig:
    enabled:            bool  = False
    style:              str   = "atr"
    trail_activation_R: float = 1.0
    trail_atr_multiple: float = 2.0
    use_fixed_target:   bool  = True

    def __post_init__(self):
        if self.style not in TRAIL_STYLES:
            raise ValueError(f"trailing_stop.style must be one of {TRAIL_STYLES}, got {self.style!r}")
        if not self.trail_atr_multiple > 0:
            raise ValueError(f"trailing_stop.trail_atr_multiple must be > 0, got {self.trail_atr_multiple}")
        if self.trail_activation_R < 0:
            raise ValueError(f"trailing_stop.trail_activation_R must be >= 0, got {self.trail_activation_R}")

    @property
    def omit_take_profit(self) -> bool:
        """True only when trailing is ON and the fixed target is switched off."""
        return self.enabled and not self.use_fixed_target


@dataclass(frozen=True)
class PyramidConfig:
    enabled:            bool  = False
    pyramid_trigger_R:  float = 1.0
    pyramid_max_adds:   int   = 2
    breakeven_buffer_R: float = 0.0

    def __post_init__(self):
        if not self.pyramid_trigger_R > 0:
            raise ValueError(f"pyramiding.pyramid_trigger_R must be > 0, got {self.pyramid_trigger_R}")
        if self.pyramid_max_adds < 0:
            raise ValueError(f"pyramiding.pyramid_max_adds must be >= 0, got {self.pyramid_max_adds}")
        if not 0 <= self.breakeven_buffer_R < self.pyramid_trigger_R:
            raise ValueError("pyramiding.breakeven_buffer_R must be in [0, pyramid_trigger_R)")


@dataclass(frozen=True)
class RiskRampConfig:
    enabled:                       bool  = False
    base_pct:                      float = 0.01
    risk_ramp_high_pct:            float = 0.02
    risk_ramp_min_trades:          int   = 50
    risk_ramp_drawdown_revert_pct: float = 0.10

    def __post_init__(self):
        if not 0 < self.base_pct <= 0.05:
            raise ValueError(f"risk ramp base_pct {self.base_pct} outside (0, 0.05]")
        if not self.base_pct <= self.risk_ramp_high_pct <= 0.05:
            raise ValueError(
                f"risk_ramp_high_pct {self.risk_ramp_high_pct} must be in [base {self.base_pct}, 0.05]")
        if self.risk_ramp_min_trades < 1:
            raise ValueError("risk_ramp_min_trades must be >= 1")
        if not 0 < self.risk_ramp_drawdown_revert_pct < 1:
            raise ValueError("risk_ramp_drawdown_revert_pct must be in (0, 1)")


@dataclass(frozen=True)
class TradeManagementConfig:
    trailing:           Mapping[str, TrailingStopConfig] = field(default_factory=dict)
    pyramiding:         Mapping[str, PyramidConfig]      = field(default_factory=dict)
    risk_ramp:          RiskRampConfig                    = field(default_factory=RiskRampConfig)
    risk_per_trade_pct: float = 0.01
    max_open_risk_pct:  float = 0.03
    atr_stop_multiple:  Mapping[str, float] = field(default_factory=dict)

    def trailing_for(self, strategy: Optional[str]) -> TrailingStopConfig:
        return self.trailing.get(strategy or "", TrailingStopConfig())

    def pyramid_for(self, strategy: Optional[str]) -> PyramidConfig:
        return self.pyramiding.get(strategy or "", PyramidConfig())

    def manages(self, strategy: Optional[str]) -> bool:
        """True when a placed trade of this strategy needs a live PositionPlan."""
        return self.trailing_for(strategy).enabled or self.pyramid_for(strategy).enabled

    @property
    def any_enabled(self) -> bool:
        return (any(c.enabled for c in self.trailing.values())
                or any(c.enabled for c in self.pyramiding.values())
                or self.risk_ramp.enabled)


def _env_bool(environ: Mapping[str, str], key: str) -> Optional[bool]:
    raw = (environ.get(key) or "").strip().lower()
    if not raw:
        return None
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{key}={environ.get(key)!r} is not a boolean (true/false)")


def load_trade_management_config(settings: Optional[dict] = None,
                                 environ: Optional[Mapping[str, str]] = None) -> TradeManagementConfig:
    """Build the config from settings (deep-merged over the defaults) + env."""
    from config.settings import _DEFAULTS, load_settings
    from core.fx_signals import _deep_merge, load_fx_signal_config

    s = settings if settings is not None else load_settings()
    env = environ if environ is not None else os.environ
    sig = load_fx_signal_config(s)
    ct = _deep_merge(_DEFAULTS["ctrader"], s.get("ctrader") or {})

    trailing: dict[str, TrailingStopConfig] = {}
    pyramiding: dict[str, PyramidConfig] = {}
    atr_mult: dict[str, float] = {}
    for strat, env_key in STRATEGY_ENV_KEYS.items():
        params = sig.get(strat) or {}
        atr_mult[strat] = float(params.get("atr_stop_multiple") or 2.0)
        t = dict(params.get("trailing_stop") or {})
        t_env = _env_bool(env, f"CTRADER_TRAILING_{env_key}")
        if t_env is not None:
            t["enabled"] = t_env
        trailing[strat] = TrailingStopConfig(
            enabled=bool(t.get("enabled", False)),
            style=str(t.get("style", "atr")).lower(),
            trail_activation_R=float(t.get("trail_activation_R", 1.0)),
            trail_atr_multiple=float(t.get("trail_atr_multiple", 2.0)),
            use_fixed_target=bool(t.get("use_fixed_target", True)),
        )
        p = dict(params.get("pyramiding") or {})
        p_env = _env_bool(env, f"CTRADER_PYRAMID_{env_key}")
        if p_env is not None:
            p["enabled"] = p_env
        pyramiding[strat] = PyramidConfig(
            enabled=bool(p.get("enabled", False)),
            pyramid_trigger_R=float(p.get("pyramid_trigger_R", 1.0)),
            pyramid_max_adds=int(p.get("pyramid_max_adds", 2)),
            breakeven_buffer_R=float(p.get("breakeven_buffer_R", 0.0)),
        )

    base = float(ct.get("risk_per_trade_pct", 0.01))
    rr = dict(ct.get("risk_ramp") or {})
    rr_env = _env_bool(env, "CTRADER_RISK_RAMP")
    if rr_env is not None:
        rr["enabled"] = rr_env
    ramp = RiskRampConfig(
        enabled=bool(rr.get("enabled", False)),
        base_pct=base,
        risk_ramp_high_pct=float(rr.get("risk_ramp_high_pct", 0.02)),
        risk_ramp_min_trades=int(rr.get("risk_ramp_min_trades", 50)),
        risk_ramp_drawdown_revert_pct=float(rr.get("risk_ramp_drawdown_revert_pct", 0.10)),
    )
    return TradeManagementConfig(
        trailing=trailing, pyramiding=pyramiding, risk_ramp=ramp,
        risk_per_trade_pct=base,
        max_open_risk_pct=float(ct.get("max_open_risk_pct", 0.03)),
        atr_stop_multiple=atr_mult,
    )


# ---------------------------------------------------------------------------
# Trailing stop (pure)
# ---------------------------------------------------------------------------

def _is_long(side: str) -> bool:
    s = (side or "").upper()
    if s in ("BUY", "LONG"):
        return True
    if s in ("SELL", "SHORT"):
        return False
    raise ValueError(f"side must be BUY/SELL/LONG/SHORT, got {side!r}")


def r_multiple(side: str, entry: float, risk_distance: float, price: float) -> float:
    """Open profit in R. risk_distance is the INITIAL |entry − stop| (> 0)."""
    if not risk_distance or risk_distance <= 0:
        raise ValueError(f"risk_distance must be > 0, got {risk_distance}")
    move = price - entry if _is_long(side) else entry - price
    return move / risk_distance


def tighter(side: str, current_stop: float, candidate: float) -> float:
    """The tighter of two stops for this side — never loosens."""
    return max(current_stop, candidate) if _is_long(side) else min(current_stop, candidate)


def is_tighter(side: str, new_stop: float, old_stop: float) -> bool:
    return new_stop > old_stop + _EPS if _is_long(side) else new_stop < old_stop - _EPS


def stop_valid_for_price(side: str, stop: float, price: float) -> bool:
    """A working stop must sit on the losing side of the market."""
    return stop < price if _is_long(side) else stop > price


def trail_candidate(side: str, cfg: TrailingStopConfig, atr_value: float,
                    close: float, highest: float, lowest: float) -> float:
    """Raw trailing level before the ratchet (see module docstring)."""
    k = cfg.trail_atr_multiple * float(atr_value)
    long_ = _is_long(side)
    if cfg.style == "atr":
        ref = close
    else:  # chandelier
        ref = highest if long_ else lowest
    return ref - k if long_ else ref + k


@dataclass
class TrailState:
    """Mutable trailing state of one position (pure — no broker)."""
    side:          str
    entry:         float
    risk_distance: float          # initial R (price distance)
    stop:          float          # current stop (starts at the initial stop)
    atr:           float
    highest:       float = float("-inf")
    lowest:        float = float("inf")
    active:        bool  = False

    def __post_init__(self):
        if self.highest == float("-inf"):
            self.highest = self.entry
        if self.lowest == float("inf"):
            self.lowest = self.entry


def update_trailing_stop(state: TrailState, cfg: TrailingStopConfig, high: float, low: float,
                         close: float, atr_value: Optional[float] = None) -> float:
    """Advance the trail by one bar / observation and return the new stop.

    Disabled config → the stop is returned unchanged (static initial stop).
    """
    state.highest = max(state.highest, float(high))
    state.lowest = min(state.lowest, float(low))
    if atr_value is not None and math.isfinite(float(atr_value)) and atr_value > 0:
        state.atr = float(atr_value)
    if not cfg.enabled:
        return state.stop
    best = state.highest if _is_long(state.side) else state.lowest
    if not state.active and r_multiple(state.side, state.entry, state.risk_distance, best) \
            >= cfg.trail_activation_R - _EPS:
        state.active = True
    if state.active:
        cand = trail_candidate(state.side, cfg, state.atr, float(close), state.highest, state.lowest)
        state.stop = tighter(state.side, state.stop, cand)
    return state.stop


def trailing_stop_path(side: str, entry: float, initial_stop: float, atr_value: float,
                       bars: Any, cfg: TrailingStopConfig,
                       atr_series: Optional[Sequence[float]] = None) -> list[float]:
    """Stop level after each bar of `bars` (rows with High/Low/Close, a
    DataFrame or an iterable of (high, low, close) tuples), starting from the
    unchanged initial stop. Stop-outs are NOT simulated — callers compare the
    path to prices themselves. atr_series optionally refreshes ATR per bar."""
    state = TrailState(side=side, entry=float(entry), risk_distance=abs(float(entry) - float(initial_stop)),
                       stop=float(initial_stop), atr=float(atr_value))
    rows: Iterable[tuple[float, float, float]]
    if hasattr(bars, "itertuples") and hasattr(bars, "columns"):
        rows = zip(bars["High"].tolist(), bars["Low"].tolist(), bars["Close"].tolist())
    else:
        rows = bars
    out: list[float] = []
    for i, (h, lo, c) in enumerate(rows):
        a = atr_series[i] if atr_series is not None else None
        out.append(update_trailing_stop(state, cfg, h, lo, c, a))
    return out


# ---------------------------------------------------------------------------
# Live position plan (state Ares keeps per managed symbol)
# ---------------------------------------------------------------------------

@dataclass
class Leg:
    """One fill of a managed position (the original entry or an add)."""
    entry:          float
    units:          float
    position_id:    Optional[str]   = None
    # Account-currency P&L per 1.0 PRICE move for the whole leg
    # (= RiskSizing.risk_at_stop / stop distance). None when unknown.
    value_per_point: Optional[float] = None
    notional_at_entry: Optional[float] = None   # account ccy


@dataclass
class PositionPlan:
    symbol:        str
    strategy:      str
    side:          str               # "BUY" | "SELL"
    entry:         float             # original entry
    risk_distance: float             # initial R (price distance)
    stop:          float             # current working stop of every leg
    take_profit:   Optional[float]   # None → no TP (trailing with use_fixed_target=False)
    atr:           float
    trailing:      TrailingStopConfig = field(default_factory=TrailingStopConfig)
    pyramiding:    PyramidConfig      = field(default_factory=PyramidConfig)
    legs:          list[Leg]          = field(default_factory=list)
    adds:          int                = 0
    decimals:      int                = 5
    trail:         Optional[TrailState] = None

    def __post_init__(self):
        if self.trail is None:
            self.trail = TrailState(side=self.side, entry=self.entry, risk_distance=self.risk_distance,
                                    stop=self.stop, atr=self.atr)

    @property
    def is_long(self) -> bool:
        return _is_long(self.side)

    @property
    def last_entry(self) -> float:
        return self.legs[-1].entry if self.legs else self.entry

    def r_multiple(self, price: float) -> float:
        return r_multiple(self.side, self.entry, self.risk_distance, price)

    def risk_to_stop(self, stop: Optional[float] = None) -> Optional[float]:
        """Account-ccy loss if every leg is stopped at `stop` (default: current).
        Legs whose stop is at/through their entry contribute 0 (locked).
        None when any leg's value_per_point is unknown."""
        s = self.stop if stop is None else stop
        total = 0.0
        for leg in self.legs:
            if leg.value_per_point is None:
                return None
            loss_pts = (leg.entry - s) if self.is_long else (s - leg.entry)
            total += max(0.0, loss_pts) * leg.value_per_point
        return total

    def notional(self, price: float) -> Optional[float]:
        total = 0.0
        for leg in self.legs:
            if leg.notional_at_entry is None or leg.entry <= 0:
                return None
            total += leg.notional_at_entry * (price / leg.entry)
        return total


def breakeven_stop(plan: PositionPlan, buffer_R: float = 0.0) -> float:
    """Original entry ± buffer in the trade's favour, never looser than now."""
    be = plan.entry + buffer_R * plan.risk_distance if plan.is_long else plan.entry - buffer_R * plan.risk_distance
    be = round(be, plan.decimals)
    return tighter(plan.side, plan.stop, be)


# ---------------------------------------------------------------------------
# Pyramiding gates (pure)
# ---------------------------------------------------------------------------

@dataclass
class PyramidGate:
    allowed:        bool
    reason:         str
    category:       str               = ""
    r:              Optional[float]   = None
    breakeven_stop: Optional[float]   = None


def pyramid_gate(plan: PositionPlan, price: float, cfg: Optional[PyramidConfig] = None,
                 existing_legs: Optional[int] = None) -> PyramidGate:
    """Pre-sizing rules for one add. `existing_legs` = open broker positions on
    the symbol (restart guard: adds done >= existing_legs − 1)."""
    cfg = cfg or plan.pyramiding
    if not cfg.enabled:
        return PyramidGate(False, "pyramiding disabled", "pyramid: disabled")
    r = plan.r_multiple(price)
    if not r > _EPS:
        # HARD RULE: never add to a position at 0R or in loss.
        return PyramidGate(False, f"position at {r:+.2f}R — never add to a loser", "pyramid: losing position", r)
    adds_done = max(plan.adds, (existing_legs - 1) if existing_legs else 0)
    if adds_done >= cfg.pyramid_max_adds:
        return PyramidGate(False, f"max adds reached ({adds_done}/{cfg.pyramid_max_adds})",
                           "pyramid: max adds", r)
    r_from_last = r_multiple(plan.side, plan.last_entry, plan.risk_distance, price)
    if r_from_last < cfg.pyramid_trigger_R - _EPS:
        return PyramidGate(False, f"{r_from_last:+.2f}R beyond last entry < trigger {cfg.pyramid_trigger_R:g}R",
                           "pyramid: below trigger", r)
    be = breakeven_stop(plan, cfg.breakeven_buffer_R)
    if not stop_valid_for_price(plan.side, be, price):
        return PyramidGate(False, f"break-even stop {be:g} not valid vs price {price:g}",
                           "pyramid: invalid stop", r)
    return PyramidGate(True, f"+{r:.2f}R, {r_from_last:.2f}R beyond last entry", "", r, be)


def pyramid_cap_violation(plan: PositionPlan, *, add_risk: float, add_notional: Optional[float],
                          price: float, be_stop: float, equity: float, max_open_risk_pct: float,
                          other_open_risk: float, leverage_cap: Optional[float]) -> Optional[tuple[str, str]]:
    """(category, reason) when the add would breach a cap, else None.

    other_open_risk: account-ccy open risk of every OTHER position (ZEUS-style
    conservative estimate for untracked ones)."""
    cap = equity * max_open_risk_pct
    sym_now = plan.risk_to_stop(be_stop)
    if sym_now is None:
        return "pyramid: unknown risk", "leg risk unknown (not risk-sized) — refusing to add"
    sym_after = sym_now + add_risk
    if sym_after > cap + 1e-9:
        return ("pyramid: risk cap",
                f"symbol open risk {sym_after:.2f} > cap {cap:.2f} ({max_open_risk_pct:.2%} of {equity:.2f})")
    acct_after = other_open_risk + sym_after
    if acct_after > cap + 1e-9:
        return ("pyramid: risk cap",
                f"account open risk {acct_after:.2f} > cap {cap:.2f} ({max_open_risk_pct:.2%} of {equity:.2f})")
    if leverage_cap is None or add_notional is None:
        return "pyramid: ESMA cap", "ESMA leverage cap unknown for symbol — refusing to add (fail closed)"
    cur = plan.notional(price)
    if cur is None:
        return "pyramid: ESMA cap", "existing notional unknown — refusing to add (fail closed)"
    legal = equity * leverage_cap
    if cur + add_notional > legal + 1e-9:
        return ("pyramid: ESMA cap",
                f"combined notional {cur + add_notional:.2f} > ESMA cap 1:{leverage_cap:g} on {equity:.2f}")
    return None


# ---------------------------------------------------------------------------
# Risk ramp
# ---------------------------------------------------------------------------

def expectancy(outcomes: Sequence[float], window: int) -> Optional[float]:
    """Mean of the last `window` outcomes, or None when fewer than `window`."""
    vals = [float(x) for x in outcomes if x is not None and math.isfinite(float(x))]
    if len(vals) < window:
        return None
    recent = vals[-window:]
    return sum(recent) / len(recent)


class RiskRamp:
    """Steps risk between base_pct and risk_ramp_high_pct (see module docstring)."""

    def __init__(self, cfg: RiskRampConfig, unit: str = "pnl_pct"):
        self.cfg = cfg
        self.unit = unit
        self.current_pct = cfg.base_pct
        self._peak: Optional[float] = None
        self._rearm_at: Optional[float] = None   # equity to regain before re-ramping
        self.last_reason = "base risk"

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    @property
    def is_high(self) -> bool:
        return self.current_pct > self.cfg.base_pct + _EPS

    def _step(self, new_pct: float, reason: str) -> None:
        old = self.current_pct
        self.current_pct = new_pct
        self.last_reason = reason
        logger.info("[ARES] risk ramp %s → %s %s", _fmt_pct(old), _fmt_pct(new_pct), reason)

    def update(self, outcomes: Sequence[float], equity: Optional[float] = None,
               peak_equity: Optional[float] = None) -> float:
        """Recompute the risk level from closed-trade outcomes and equity."""
        cfg = self.cfg
        if not cfg.enabled:
            self.current_pct = cfg.base_pct
            return self.current_pct
        if equity is not None and equity > 0:
            self._peak = max(x for x in (self._peak, equity, peak_equity) if x is not None)
        dd = 0.0
        if self._peak and equity is not None and equity > 0:
            dd = max(0.0, (self._peak - equity) / self._peak)
        if self._rearm_at is not None and equity is not None and equity >= self._rearm_at - 1e-9:
            self._rearm_at = None
        n = len([x for x in outcomes if x is not None])
        exp = expectancy(outcomes, cfg.risk_ramp_min_trades)

        if self.is_high:
            if dd >= cfg.risk_ramp_drawdown_revert_pct - _EPS:
                self._rearm_at = self._peak
                self._step(cfg.base_pct, f"after {dd:.0%} drawdown from peak")
            elif exp is None or exp <= 0:
                self._step(cfg.base_pct, f"expectancy no longer positive ({_fmt_exp(exp, self.unit)} "
                                         f"over last {cfg.risk_ramp_min_trades} closed trades)")
        else:
            if (exp is not None and exp > 0 and dd < cfg.risk_ramp_drawdown_revert_pct - _EPS
                    and self._rearm_at is None):
                self._step(cfg.risk_ramp_high_pct,
                           f"after {n} closed trades, expectancy {_fmt_exp(exp, self.unit)} "
                           f"(mean of last {cfg.risk_ramp_min_trades})")
        return self.current_pct


def _fmt_pct(x: float) -> str:
    return f"{x * 100:g}%"


def _fmt_exp(exp: Optional[float], unit: str) -> str:
    if exp is None:
        return "n/a"
    return f"{exp:+.4g} {unit}"
