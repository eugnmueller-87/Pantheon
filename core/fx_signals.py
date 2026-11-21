"""
FX / index-CFD signal generation for the cTrader path.

When the execution venue is cTrader (settings "broker" / env BROKER=ctrader)
the tradable universe is FX majors + index CFDs, so the EDGAR/Finnhub stock
signals are useless there. Icarus then calls FxSignalGenerator.generate()
INSTEAD of reading Supabase. broker=ibkr never reaches this module.

Strategies (simple, rule-based, unit-tested on synthetic bars, no hype):

  breakout        (index CFDs: US500, USTEC, DE40), daily bars by default
      LONG  when the last close > highest high of the prior `donchian_window` bars
            AND close > SMA(trend_ma_period)            (trend filter, optional)
      SHORT when the last close < lowest low of the prior window
            AND close < SMA(trend_ma_period)            (if allow_short)

  mean_reversion  (FX majors: EURUSD, GBPUSD, USDJPY), hourly bars by default
      z = (close − SMA(ma_period)) / stdev(close, ma_period)
      BUY  when −max_z <= z <= −entry_z     SELL when entry_z <= z <= max_z
      |z| beyond max_z is treated as a trend break / news move and NOT faded.

Filters (all config-driven, see settings "ctrader_signals"):
  - volatility: ATR% of price must sit in [min_atr_pct, max_atr_pct]; optional
    max_atr_expansion = ATR / mean true range over the prior lookback (skip blow-offs)
  - session:    "london_ny_overlap" | "london_or_ny" | "any" (weekends always skipped),
                evaluated at decision time in London / New York local time
  - staleness:  skip when the last bar is older than max_bar_age_hours

Stops: stop_distance = clamp(ATR × atr_stop_multiple, min_stop_pct × price,
max_stop_pct × price), an absolute PRICE distance carried on the RawSignal.
Take-profit = stop_distance × reward_risk. The cTrader risk sizer turns that
distance into units, so a tight FX stop no longer means a tiny position.

No valid setup → nothing is emitted. Nothing here places orders.

Imports only core.types (+ pandas / optional yfinance) — never other agents.
"""

from __future__ import annotations

import copy
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Any, Callable, Optional, Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from core.types import RawSignal, Severity, SignalCategory

logger = logging.getLogger("icarus.fx")

_LONDON = ZoneInfo("Europe/London")
_NEWYORK = ZoneInfo("America/New_York")

# Local-time trading windows used by the session filter.
_LONDON_OPEN, _LONDON_CLOSE = time(8, 0), time(16, 30)
_NY_OPEN, _NY_CLOSE = time(8, 0), time(17, 0)

SESSION_MODES = ("london_ny_overlap", "london_or_ny", "any")
STRATEGIES = ("breakout", "mean_reversion")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _deep_merge(base: dict, override: Optional[dict]) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_fx_signal_config(settings: Optional[dict] = None) -> dict:
    """Settings "ctrader_signals" deep-merged over the built-in defaults, so a
    partial override (e.g. only mean_reversion.entry_z) keeps every other key."""
    from config.settings import _DEFAULTS, load_settings
    s = settings if settings is not None else load_settings()
    return _deep_merge(_DEFAULTS["ctrader_signals"], s.get("ctrader_signals"))


def resolve_broker(settings: Optional[dict] = None, environ: Optional[dict] = None) -> str:
    """Execution venue name: env BROKER > settings "broker" > "ibkr" (same order as ZEUS)."""
    import os
    env = environ if environ is not None else os.environ
    s = settings if settings is not None else {}
    return (env.get("BROKER") or s.get("broker") or "ibkr").strip().lower()


def resolve_signal_universe(broker: str, settings: Optional[dict] = None) -> str:
    """'fx' when the venue is cTrader and ctrader_signals.enabled; else 'stock'."""
    if (broker or "").strip().lower() != "ctrader":
        return "stock"
    cfg = load_fx_signal_config(settings)
    return "fx" if cfg.get("enabled", True) else "stock"


# ---------------------------------------------------------------------------
# Indicators (pure)
# ---------------------------------------------------------------------------

def true_range(bars: pd.DataFrame) -> pd.Series:
    prev_close = bars["Close"].shift(1)
    return pd.concat(
        [bars["High"] - bars["Low"], (bars["High"] - prev_close).abs(), (bars["Low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def atr(bars: pd.DataFrame, period: int) -> pd.Series:
    """Simple-mean ATR (same definition Ares uses for its stock ATR stops)."""
    return true_range(bars).rolling(period, min_periods=period).mean()


def _bar_time(bars: pd.DataFrame) -> datetime:
    ts = pd.Timestamp(bars.index[-1])
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert("UTC").to_pydatetime()


# ---------------------------------------------------------------------------
# Filters (pure)
# ---------------------------------------------------------------------------

def in_session(now: datetime, mode: str) -> bool:
    """True if `now` is inside the configured trading session.

    london_ny_overlap: London 08:00–16:30 AND New York 08:00–17:00 local time
                       (≈ 12:00–15:30 UTC in winter; DST handled by zoneinfo).
    london_or_ny:      either centre open (≈ 07:00/08:00–21:00/22:00 UTC).
    any:               any weekday time.
    Saturdays/Sundays (UTC) are always outside the session.
    """
    if mode not in SESSION_MODES:
        raise ValueError(f"unknown session mode {mode!r} (expected one of {SESSION_MODES})")
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if now.astimezone(timezone.utc).weekday() >= 5:
        return False
    if mode == "any":
        return True
    ldn, ny = now.astimezone(_LONDON), now.astimezone(_NEWYORK)
    ldn_open = ldn.weekday() < 5 and _LONDON_OPEN <= ldn.time() < _LONDON_CLOSE
    ny_open  = ny.weekday() < 5 and _NY_OPEN <= ny.time() < _NY_CLOSE
    return (ldn_open and ny_open) if mode == "london_ny_overlap" else (ldn_open or ny_open)


def volatility_veto(bars: pd.DataFrame, params: dict) -> tuple[Optional[str], dict]:
    """Return (reason_or_None, metrics). Vetoes dead or disorderly markets."""
    period = int(params["atr_period"])
    a = atr(bars, period)
    cur_atr = float(a.iloc[-1]) if len(a) else float("nan")
    close = float(bars["Close"].iloc[-1])
    metrics: dict[str, float] = {"atr": cur_atr}
    if not math.isfinite(cur_atr) or close <= 0:
        return f"not enough bars for ATR({period})", metrics
    atr_pct = cur_atr / close
    metrics["atr_pct"] = atr_pct
    lo, hi = params.get("min_atr_pct"), params.get("max_atr_pct")
    if lo is not None and atr_pct < float(lo):
        return f"volatility too low: ATR {atr_pct:.4%} < {float(lo):.4%}", metrics
    if hi is not None and atr_pct > float(hi):
        return f"volatility too high: ATR {atr_pct:.4%} > {float(hi):.4%}", metrics
    max_exp = params.get("max_atr_expansion")
    if max_exp:
        lookback = int(params.get("atr_expansion_lookback", 100))
        tr = true_range(bars)
        base = tr.iloc[-(lookback + period):-period] if len(tr) > period else tr.iloc[:0]
        base = base.dropna()
        if len(base) >= period:
            expansion = cur_atr / float(base.mean()) if float(base.mean()) > 0 else float("inf")
            metrics["atr_expansion"] = expansion
            if expansion > float(max_exp):
                return f"volatility blow-off: ATR {expansion:.2f}× its recent average > {float(max_exp):g}×", metrics
    return None, metrics


def stop_distance_for(price: float, atr_value: float, params: dict) -> float:
    """ATR-multiple stop distance, clamped to [min_stop_pct, max_stop_pct] of price."""
    raw = atr_value * float(params["atr_stop_multiple"])
    lo = float(params.get("min_stop_pct") or 0.0) * price
    hi = float(params.get("max_stop_pct") or float("inf")) * price
    return max(lo, min(hi, raw))


def bracket_from_distance(
    entry: float, stop_distance: float, is_long: bool, reward_risk: float, decimals: int,
) -> tuple[float, float]:
    """(stop_price, take_profit_price) from an absolute stop distance."""
    tp_distance = stop_distance * reward_risk
    if is_long:
        return round(entry - stop_distance, decimals), round(entry + tp_distance, decimals)
    return round(entry + stop_distance, decimals), round(entry - tp_distance, decimals)


# ---------------------------------------------------------------------------
# Strategies (pure)
# ---------------------------------------------------------------------------

@dataclass
class Setup:
    logical:       str
    strategy:      str
    side:          str            # "BUY" | "SELL"
    entry:         float          # last close
    stop_distance: float          # absolute price distance
    reward_risk:   float
    bar_time:      datetime
    reason:        str
    metrics:       dict[str, float] = field(default_factory=dict)

    @property
    def stop_price(self) -> float:
        return self.entry - self.stop_distance if self.side == "BUY" else self.entry + self.stop_distance

    @property
    def take_profit_price(self) -> float:
        d = self.stop_distance * self.reward_risk
        return self.entry + d if self.side == "BUY" else self.entry - d


@dataclass
class Evaluation:
    logical:  str
    strategy: str
    setup:    Optional[Setup] = None
    skipped:  Optional[str]   = None   # why there is no setup (None when setup is set)


def _common_gates(logical: str, strategy: str, bars: Optional[pd.DataFrame], params: dict,
                  now: datetime, min_bars: int) -> Optional[Evaluation]:
    """Data sufficiency, staleness and session gates shared by both strategies."""
    if bars is None or len(bars) < min_bars:
        n = 0 if bars is None else len(bars)
        return Evaluation(logical, strategy, skipped=f"insufficient bars ({n} < {min_bars})")
    max_age = params.get("max_bar_age_hours")
    if max_age is not None:
        age = now - _bar_time(bars)
        if age > timedelta(hours=float(max_age)):
            return Evaluation(logical, strategy,
                              skipped=f"stale data: last bar {age.total_seconds() / 3600:.1f}h old > {max_age}h")
    mode = params.get("session", "any")
    if not in_session(now, mode):
        return Evaluation(logical, strategy, skipped=f"outside session ({mode})")
    return None


def evaluate_breakout(logical: str, bars: Optional[pd.DataFrame], params: dict, now: datetime) -> Evaluation:
    """Donchian breakout with an optional SMA trend filter and a volatility filter."""
    window = int(params["donchian_window"])
    trend = params.get("trend_ma_period")
    min_bars = max(window + 1, int(trend or 0), int(params["atr_period"]) + 1)
    gate = _common_gates(logical, "breakout", bars, params, now, min_bars)
    if gate:
        return gate

    veto, metrics = volatility_veto(bars, params)
    if veto:
        return Evaluation(logical, "breakout", skipped=veto)

    close = float(bars["Close"].iloc[-1])
    prior = bars.iloc[-(window + 1):-1]
    upper, lower = float(prior["High"].max()), float(prior["Low"].min())
    sma = float(bars["Close"].iloc[-int(trend):].mean()) if trend else None
    metrics.update({"donchian_high": upper, "donchian_low": lower})
    if sma is not None:
        metrics["sma"] = sma

    if close > upper:
        side = "BUY"
    elif close < lower:
        side = "SELL"
    else:
        return Evaluation(logical, "breakout",
                          skipped=f"no breakout (close {close:g} inside [{lower:g}, {upper:g}])")
    if sma is not None and ((side == "BUY" and close <= sma) or (side == "SELL" and close >= sma)):
        return Evaluation(logical, "breakout",
                          skipped=f"{side} breakout against trend (close {close:g} vs SMA{trend} {sma:g})")
    if side == "SELL" and not params.get("allow_short", True):
        return Evaluation(logical, "breakout", skipped="downside breakout but allow_short=False")

    dist = stop_distance_for(close, metrics["atr"], params)
    level = upper if side == "BUY" else lower
    reason = (f"{window}-bar Donchian breakout {'above' if side == 'BUY' else 'below'} {level:g} "
              f"(close {close:g}{f', SMA{trend} {sma:g}' if sma is not None else ''}, "
              f"ATR {metrics['atr_pct']:.2%})")
    return Evaluation(logical, "breakout", setup=Setup(
        logical=logical, strategy="breakout", side=side, entry=close, stop_distance=dist,
        reward_risk=float(params.get("reward_risk", 2.0)), bar_time=_bar_time(bars),
        reason=reason, metrics=metrics,
    ))


def evaluate_mean_reversion(logical: str, bars: Optional[pd.DataFrame], params: dict, now: datetime) -> Evaluation:
    """Z-score fade vs SMA, with session, volatility and max-stretch filters."""
    n = int(params["ma_period"])
    min_bars = max(n, int(params["atr_period"]) + 1)
    gate = _common_gates(logical, "mean_reversion", bars, params, now, min_bars)
    if gate:
        return gate

    closes = bars["Close"].iloc[-n:]
    mean, std = float(closes.mean()), float(closes.std(ddof=0))
    close = float(bars["Close"].iloc[-1])
    if std <= 0 or not math.isfinite(std):
        return Evaluation(logical, "mean_reversion", skipped="zero price dispersion")
    z = (close - mean) / std

    entry_z, max_z = float(params["entry_z"]), params.get("max_z")
    if abs(z) < entry_z:
        return Evaluation(logical, "mean_reversion", skipped=f"not stretched: |z|={abs(z):.2f} < {entry_z:g}")
    if max_z is not None and abs(z) > float(max_z):
        return Evaluation(logical, "mean_reversion",
                          skipped=f"over-stretched: |z|={abs(z):.2f} > {float(max_z):g} (not fading a trend break)")

    veto, metrics = volatility_veto(bars, params)
    if veto:
        return Evaluation(logical, "mean_reversion", skipped=veto)
    metrics.update({"z": z, "sma": mean, "std": std})

    side = "BUY" if z < 0 else "SELL"
    dist = stop_distance_for(close, metrics["atr"], params)
    reason = (f"mean reversion: close {close:g} is {z:+.2f}σ from SMA{n} {mean:g} "
              f"(ATR {metrics['atr_pct']:.3%}, session {params.get('session', 'any')})")
    return Evaluation(logical, "mean_reversion", setup=Setup(
        logical=logical, strategy="mean_reversion", side=side, entry=close, stop_distance=dist,
        reward_risk=float(params.get("reward_risk", 2.0)), bar_time=_bar_time(bars),
        reason=reason, metrics=metrics,
    ))


_EVALUATORS: dict[str, Callable[[str, Optional[pd.DataFrame], dict, datetime], Evaluation]] = {
    "breakout":       evaluate_breakout,
    "mean_reversion": evaluate_mean_reversion,
}


# ---------------------------------------------------------------------------
# RawSignal mapping
# ---------------------------------------------------------------------------

def setup_to_raw_signal(setup: Setup) -> RawSignal:
    """Pipeline contract for a technical setup.

    category is MACRO_SHIFT (existing enum value — the Supabase signal_category
    enum has no technical value); the DIRECTION is carried explicitly in
    trade_side, which Ares honours on two-way venues (cTrader). affected_tickers
    is the logical cTrader name so the adapter alias map resolves it.
    """
    direction = "LONG" if setup.side == "BUY" else "SHORT"
    sid = f"fx-{setup.logical}-{setup.strategy}-{setup.side}-{setup.bar_time:%Y%m%d%H%M}".lower()
    headline = f"{setup.logical} {setup.strategy.replace('_', ' ')} {direction}: {setup.reason}"
    return RawSignal(
        signal_id=sid,
        source_url="",
        headline=headline[:300],
        summary=(f"{setup.logical} {direction} @ {setup.entry:g}; stop {setup.stop_price:g} "
                 f"(distance {setup.stop_distance:g}), target {setup.take_profit_price:g} "
                 f"({setup.reward_risk:g}R)."),
        published_at=setup.bar_time,
        category=SignalCategory.MACRO_SHIFT,
        severity=Severity.MEDIUM,
        affected_tickers=[setup.logical],
        raw_text=headline,
        supplier=setup.logical,
        hermes_signal_type="TECHNICAL",
        trade_side=setup.side,
        stop_distance=setup.stop_distance,
        reward_risk=setup.reward_risk,
        reference_price=setup.entry,
        strategy=setup.strategy,
    )


# ---------------------------------------------------------------------------
# Bar sources
# ---------------------------------------------------------------------------

class BarSource(Protocol):
    name: str

    def bars(self, logical: str, interval: str, period: str) -> Optional[pd.DataFrame]: ...


class YFinanceBarSource:
    """Historical OHLC via yfinance (default; no credentials).

    `downloader(ticker, interval, period) -> DataFrame` is injectable so tests
    never touch the network.
    """
    name = "yfinance"

    def __init__(self, ticker_map: dict[str, str],
                 downloader: Optional[Callable[[str, str, str], pd.DataFrame]] = None):
        self.ticker_map = {k.upper(): v for k, v in ticker_map.items()}
        self._download = downloader or self._yf_download

    @staticmethod
    def _yf_download(ticker: str, interval: str, period: str) -> pd.DataFrame:
        import yfinance as yf
        return yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=False)

    def bars(self, logical: str, interval: str, period: str) -> Optional[pd.DataFrame]:
        yf_ticker = self.ticker_map.get(logical.upper())
        if not yf_ticker:
            logger.warning("[ICARUS] No yfinance ticker mapped for %s — skipping", logical)
            return None
        df = self._download(yf_ticker, interval, period)
        if df is None or df.empty:
            return None
        df = df[["Open", "High", "Low", "Close"]].dropna()
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        return df


class CTraderTrendbarSource:
    """Future hook: bars from the connected cTrader adapter.

    Only available when the adapter is healthy AND exposes
    get_trendbars(logical, interval, period) -> DataFrame (not implemented in
    CTraderAdapter yet — it needs ProtoOAGetTrendbarsReq). Until then the
    generator falls back to yfinance, so no credentials are ever required here.
    """
    name = "ctrader_trendbars"

    def __init__(self, adapter: Any):
        self.adapter = adapter

    def available(self) -> bool:
        fn = getattr(self.adapter, "get_trendbars", None)
        if not callable(fn):
            return False
        try:
            return bool(self.adapter.health())
        except Exception:
            return False

    def bars(self, logical: str, interval: str, period: str) -> Optional[pd.DataFrame]:
        return self.adapter.get_trendbars(logical, interval, period)


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

class FxSignalGenerator:
    """Scan the cTrader universe and emit RawSignals for valid setups only."""

    def __init__(
        self,
        config: Optional[dict] = None,
        instruments: Optional[dict[str, dict[str, Any]]] = None,
        source: Optional[BarSource] = None,
        adapter: Any = None,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        from config.settings import load_settings
        settings = load_settings() if (config is None or instruments is None) else {}
        self.config = _deep_merge(load_fx_signal_config({}), config) if config is not None \
            else load_fx_signal_config(settings)
        if instruments is None:
            instruments = (settings.get("ctrader") or {}).get("instruments") or {}
            if not instruments:
                from core.brokers.ctrader_instruments import DEFAULT_INSTRUMENTS
                instruments = DEFAULT_INSTRUMENTS
        wanted = self.config.get("instruments") or list(instruments)
        self.instruments = {k.upper(): instruments[k] for k in instruments if k.upper() in {w.upper() for w in wanted}}
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.source = source or self._default_source(adapter)
        self.last_evaluations: list[Evaluation] = []

    def _default_source(self, adapter: Any) -> BarSource:
        yf_source = YFinanceBarSource(self.config.get("yfinance_tickers") or {})
        if self.config.get("source") == "ctrader_trendbars":
            tb = CTraderTrendbarSource(adapter)
            if adapter is not None and tb.available():
                return tb
            logger.info("[ICARUS] ctrader_trendbars requested but adapter not connected/capable — using yfinance")
        return yf_source

    def strategy_for(self, logical: str) -> Optional[str]:
        asset_class = (self.instruments.get(logical) or {}).get("asset_class", "")
        strat = (self.config.get("strategy_by_asset_class") or {}).get(asset_class)
        return strat if strat in STRATEGIES else None

    def evaluate(self, logical: str, bars: Optional[pd.DataFrame], now: Optional[datetime] = None) -> Evaluation:
        strat = self.strategy_for(logical)
        if strat is None:
            return Evaluation(logical, "", skipped="no strategy configured for this asset class")
        return _EVALUATORS[strat](logical, bars, self.config[strat], now or self._clock())

    def generate(self) -> list[RawSignal]:
        now = self._clock()
        signals: list[RawSignal] = []
        self.last_evaluations = []
        for logical in self.instruments:
            strat = self.strategy_for(logical)
            if strat is None:
                continue
            params = self.config[strat]
            try:
                bars = self.source.bars(logical, params["interval"], params["period"])
            except Exception as exc:
                logger.warning("[ICARUS] %s bars unavailable from %s: %s", logical, self.source.name, exc)
                self.last_evaluations.append(Evaluation(logical, strat, skipped=f"data error: {exc}"))
                continue
            ev = self.evaluate(logical, bars, now)
            self.last_evaluations.append(ev)
            if ev.setup is None:
                logger.info("[ICARUS] %s %s: no setup — %s", logical, strat, ev.skipped)
                continue
            sig = setup_to_raw_signal(ev.setup)
            logger.info("[ICARUS] %s %s %s entry=%g stop_dist=%g — %s", logical, strat, ev.setup.side,
                        ev.setup.entry, ev.setup.stop_distance, ev.setup.reason)
            signals.append(sig)
        return signals
