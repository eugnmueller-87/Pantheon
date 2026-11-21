"""
FX / index signal generation for the cTrader path — synthetic OHLC, no network.

Covers:
  - index breakout: BUY / SELL on clear breakouts, quiet on a flat/noisy range
  - FX mean reversion: fades a stretched z-score, quiet at the mean / over-stretched
  - volatility, ATR-expansion, session and staleness filters suppress
  - ATR stops are market-sized (pips, not 3%) and TP = reward_risk × stop
  - emitted tickers are logical cTrader names the adapter alias map resolves
  - broker=ibkr never calls the FX path; broker=ctrader replaces stock signals
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from core.brokers.ctrader_instruments import DEFAULT_INSTRUMENTS, load_instruments, resolve_logical
from core.fx_signals import (
    FxSignalGenerator,
    YFinanceBarSource,
    evaluate_breakout,
    evaluate_mean_reversion,
    in_session,
    load_fx_signal_config,
    resolve_signal_universe,
)
from core.types import SignalCategory

CFG = load_fx_signal_config({})
BO = CFG["breakout"]
MR = CFG["mean_reversion"]

# Wednesday 2026-11-18. 14:00 UTC = 14:00 London (GMT) / 09:00 New York (EST) → overlap.
NOW_OVERLAP = datetime(2026, 11, 18, 14, 0, tzinfo=timezone.utc)
NOW_ASIA    = datetime(2026, 11, 18, 3, 0, tzinfo=timezone.utc)
SATURDAY    = datetime(2026, 11, 21, 14, 0, tzinfo=timezone.utc)


def _bars(closes: list[float], spread, end: datetime, freq: str) -> pd.DataFrame:
    """OHLC with High/Low = close ± spread (scalar or per-bar list), Open = prior close."""
    spreads = spread if isinstance(spread, list) else [spread] * len(closes)
    idx = pd.date_range(end=end, periods=len(closes), freq=freq, tz="UTC")
    opens = [closes[0]] + closes[:-1]
    return pd.DataFrame({
        "Open":  opens,
        "High":  [max(o, c) + s for o, c, s in zip(opens, closes, spreads)],
        "Low":   [min(o, c) - s for o, c, s in zip(opens, closes, spreads)],
        "Close": closes,
    }, index=idx)


# ── Index fixtures (daily) ────────────────────────────────────────────────────

_DAY_END = datetime(2026, 11, 18, 0, 0, tzinfo=timezone.utc)


def _uptrend_breakout() -> pd.DataFrame:
    closes = [5000 + i * 3 + 15 * math.sin(i) for i in range(80)]
    closes[-1] = max(closes[-21:-1]) + 60          # close well above the prior 20-bar high
    return _bars(closes, 25.0, _DAY_END, "D")


def _downtrend_breakout() -> pd.DataFrame:
    closes = [5300 - i * 3 + 15 * math.sin(i) for i in range(80)]
    closes[-1] = min(closes[-21:-1]) - 60
    return _bars(closes, 25.0, _DAY_END, "D")


def _flat_noisy_range() -> pd.DataFrame:
    closes = [5000 + 30 * math.sin(i * 0.9) + 10 * math.cos(i * 2.3) for i in range(80)]
    closes[-1] = 5000.0
    return _bars(closes, 25.0, _DAY_END, "D")


class TestIndexBreakout:
    def test_upside_breakout_buys(self):
        ev = evaluate_breakout("US500", _uptrend_breakout(), BO, NOW_OVERLAP)
        assert ev.setup is not None, ev.skipped
        assert ev.setup.side == "BUY" and ev.setup.strategy == "breakout"

    def test_downside_breakout_sells(self):
        ev = evaluate_breakout("DE40", _downtrend_breakout(), BO, NOW_OVERLAP)
        assert ev.setup is not None, ev.skipped
        assert ev.setup.side == "SELL"

    def test_downside_breakout_quiet_when_shorts_disabled(self):
        ev = evaluate_breakout("DE40", _downtrend_breakout(), {**BO, "allow_short": False}, NOW_OVERLAP)
        assert ev.setup is None and "allow_short" in ev.skipped

    def test_flat_noisy_range_is_quiet(self):
        ev = evaluate_breakout("US500", _flat_noisy_range(), BO, NOW_OVERLAP)
        assert ev.setup is None and "no breakout" in ev.skipped

    def test_breakout_against_trend_filter_is_quiet(self):
        # Steep downtrend, then a close above the 20-bar high that is still below SMA50.
        closes = [5600 - i * 25 for i in range(80)]
        closes[-1] = max(closes[-21:-1]) + 70        # clears the prior highs (incl. spread)
        ev = evaluate_breakout("US500", _bars(closes, 25.0, _DAY_END, "D"), BO, NOW_OVERLAP)
        assert ev.setup is None and "against trend" in ev.skipped
        # Same bars with the trend filter off → it IS a breakout
        ev2 = evaluate_breakout("US500", _bars(closes, 25.0, _DAY_END, "D"),
                                {**BO, "trend_ma_period": None}, NOW_OVERLAP)
        assert ev2.setup is not None and ev2.setup.side == "BUY"

    def test_atr_stop_and_reward_risk(self):
        ev = evaluate_breakout("US500", _uptrend_breakout(), BO, NOW_OVERLAP)
        s = ev.setup
        expected = ev.setup.metrics["atr"] * BO["atr_stop_multiple"]
        assert s.stop_distance == pytest.approx(
            max(BO["min_stop_pct"] * s.entry, min(BO["max_stop_pct"] * s.entry, expected)))
        assert s.take_profit_price - s.entry == pytest.approx(2.0 * s.stop_distance)
        assert s.stop_price < s.entry < s.take_profit_price

    def test_low_volatility_suppresses(self):
        closes = [5000 + i * 0.5 for i in range(80)]
        closes[-1] = max(closes[-21:-1]) + 3
        ev = evaluate_breakout("US500", _bars(closes, 0.5, _DAY_END, "D"), BO, NOW_OVERLAP)
        assert ev.setup is None and "volatility too low" in ev.skipped

    def test_high_volatility_suppresses(self):
        closes = [5000 + i * 3 + 15 * math.sin(i) for i in range(80)]
        closes[-1] = max(closes[-21:-1]) + 400
        ev = evaluate_breakout("US500", _bars(closes, 250.0, _DAY_END, "D"), BO, NOW_OVERLAP)
        assert ev.setup is None and "volatility too high" in ev.skipped

    def test_stale_bars_suppress(self):
        ev = evaluate_breakout("US500", _uptrend_breakout(), BO, NOW_OVERLAP + timedelta(days=6))
        assert ev.setup is None and "stale" in ev.skipped

    def test_too_few_bars(self):
        ev = evaluate_breakout("US500", _uptrend_breakout().iloc[-10:], BO, NOW_OVERLAP)
        assert ev.setup is None and "insufficient bars" in ev.skipped


# ── FX fixtures (hourly) ──────────────────────────────────────────────────────

_HOUR_END = datetime(2026, 11, 18, 13, 0, tzinfo=timezone.utc)


def _fx(last_offset: float, spread=0.0004, n=150, last_spreads=None) -> pd.DataFrame:
    closes = [1.08 + 0.0006 * math.sin(i * 0.5) for i in range(n)]
    closes[-1] = 1.08 + last_offset
    spreads = [spread] * n
    if last_spreads:
        spreads[-len(last_spreads):] = last_spreads
    return _bars(closes, spreads, _HOUR_END, "h")


class TestFxMeanReversion:
    def test_stretched_below_mean_buys(self):
        ev = evaluate_mean_reversion("EURUSD", _fx(-0.0015), MR, NOW_OVERLAP)
        assert ev.setup is not None, ev.skipped
        assert ev.setup.side == "BUY"
        assert MR["entry_z"] <= -ev.setup.metrics["z"] <= MR["max_z"]

    def test_stretched_above_mean_sells(self):
        ev = evaluate_mean_reversion("GBPUSD", _fx(+0.0015), MR, NOW_OVERLAP)
        assert ev.setup is not None, ev.skipped
        assert ev.setup.side == "SELL"
        assert ev.setup.take_profit_price < ev.setup.entry < ev.setup.stop_price

    def test_at_the_mean_is_quiet(self):
        ev = evaluate_mean_reversion("EURUSD", _fx(0.0), MR, NOW_OVERLAP)
        assert ev.setup is None and "not stretched" in ev.skipped

    def test_over_stretched_is_not_faded(self):
        ev = evaluate_mean_reversion("EURUSD", _fx(-0.02), MR, NOW_OVERLAP)
        assert ev.setup is None and "over-stretched" in ev.skipped

    def test_stop_is_pips_not_three_percent(self):
        s = evaluate_mean_reversion("EURUSD", _fx(-0.0015), MR, NOW_OVERLAP).setup
        assert s.stop_distance < 0.01 * s.entry           # far tighter than 3% (~320 pips)
        assert s.stop_distance >= MR["min_stop_pct"] * s.entry
        assert s.stop_distance == pytest.approx(
            max(MR["min_stop_pct"] * s.entry, s.metrics["atr"] * MR["atr_stop_multiple"]))

    def test_session_filter_suppresses_asian_hours(self):
        ev = evaluate_mean_reversion("EURUSD", _fx(-0.0015), MR, NOW_ASIA)
        assert ev.setup is None and "outside session" in ev.skipped

    def test_weekend_suppressed(self):
        ev = evaluate_mean_reversion("EURUSD", _fx(-0.0015), {**MR, "max_bar_age_hours": None}, SATURDAY)
        assert ev.setup is None and "outside session" in ev.skipped

    def test_atr_blow_off_suppresses(self):
        # Same stretch, but the last 14 bars have 6× the usual range → don't fade it.
        ev = evaluate_mean_reversion("EURUSD", _fx(-0.0015, last_spreads=[0.0025] * 14), MR, NOW_OVERLAP)
        assert ev.setup is None and "blow-off" in ev.skipped

    def test_max_atr_pct_suppresses(self):
        ev = evaluate_mean_reversion("EURUSD", _fx(-0.0015, spread=0.004),
                                     {**MR, "max_atr_expansion": None}, NOW_OVERLAP)
        assert ev.setup is None and "volatility too high" in ev.skipped

    def test_stale_hourly_bars_suppress(self):
        ev = evaluate_mean_reversion("EURUSD", _fx(-0.0015), MR, NOW_OVERLAP + timedelta(hours=5))
        assert ev.setup is None and "stale" in ev.skipped


class TestSession:
    @pytest.mark.parametrize("now,mode,expected", [
        (datetime(2026, 11, 18, 14, 0, tzinfo=timezone.utc), "london_ny_overlap", True),
        (datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc), "london_ny_overlap", False),  # NY not open
        (datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc), "london_or_ny", True),
        (datetime(2026, 11, 18, 3, 0, tzinfo=timezone.utc), "london_or_ny", False),
        (datetime(2026, 11, 18, 3, 0, tzinfo=timezone.utc), "any", True),
        (datetime(2026, 11, 22, 3, 0, tzinfo=timezone.utc), "any", False),                # Sunday
        # Summer (BST/EDT): 12:00 UTC = 13:00 London / 08:00 NY → overlap open
        (datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc), "london_ny_overlap", True),
        (datetime(2026, 7, 15, 15, 45, tzinfo=timezone.utc), "london_ny_overlap", False), # London closed 16:30 BST
    ])
    def test_windows(self, now, mode, expected):
        assert in_session(now, mode) is expected

    def test_unknown_mode_rejected(self):
        with pytest.raises(ValueError):
            in_session(NOW_OVERLAP, "tokyo")


# ── Generator / RawSignal contract ────────────────────────────────────────────

class _FixtureSource:
    name = "fixture"

    def __init__(self, bars: dict):
        self._bars = bars
        self.calls: list[tuple] = []

    def bars(self, logical, interval, period):
        self.calls.append((logical, interval, period))
        return self._bars.get(logical)


def _generator(bars: dict, now=NOW_OVERLAP, **cfg) -> tuple[FxSignalGenerator, _FixtureSource]:
    src = _FixtureSource(bars)
    gen = FxSignalGenerator(config=cfg, instruments=DEFAULT_INSTRUMENTS, source=src, clock=lambda: now)
    return gen, src


class TestGenerator:
    def test_emits_logical_cTrader_names_with_direction_and_stop(self):
        gen, src = _generator({
            "US500": _uptrend_breakout(), "DE40": _downtrend_breakout(), "USTEC": _flat_noisy_range(),
            "EURUSD": _fx(-0.0015), "GBPUSD": _fx(0.0), "USDJPY": None,
        })
        sigs = gen.generate()
        by_ticker = {s.affected_tickers[0]: s for s in sigs}
        assert set(by_ticker) == {"US500", "DE40", "EURUSD"}
        assert by_ticker["US500"].trade_side == "BUY"
        assert by_ticker["DE40"].trade_side == "SELL"
        assert by_ticker["EURUSD"].trade_side == "BUY"
        instruments = load_instruments()
        for t, s in by_ticker.items():
            assert resolve_logical(t, instruments) == t         # adapter alias map resolves it
            assert s.category == SignalCategory.MACRO_SHIFT    # valid Supabase enum value
            assert s.stop_distance and s.stop_distance > 0 and s.reward_risk == 2.0
            assert s.reference_price and s.strategy in ("breakout", "mean_reversion")
            assert s.signal_id.startswith("fx-")
        # Each asset class uses its own bar interval
        intervals = {logical: interval for logical, interval, _ in src.calls}
        assert intervals["US500"] == "1d" and intervals["EURUSD"] == "1h"

    def test_no_setups_emits_nothing(self):
        gen, _ = _generator({k: _flat_noisy_range() for k in ("US500", "USTEC", "DE40")}
                            | {k: _fx(0.0) for k in ("EURUSD", "GBPUSD", "USDJPY")})
        assert gen.generate() == []
        assert all(ev.skipped for ev in gen.last_evaluations)

    def test_data_error_is_skipped_not_raised(self):
        class Boom:
            name = "boom"

            def bars(self, *a):
                raise RuntimeError("yahoo down")
        gen = FxSignalGenerator(config={}, instruments=DEFAULT_INSTRUMENTS, source=Boom(),
                                clock=lambda: NOW_OVERLAP)
        assert gen.generate() == []

    def test_signal_id_is_stable_per_bar(self):
        gen, _ = _generator({"US500": _uptrend_breakout()}, instruments=["US500"])
        assert gen.generate()[0].signal_id == gen.generate()[0].signal_id

    def test_instrument_subset_and_partial_config_override(self):
        gen, src = _generator({"EURUSD": _fx(-0.0015)}, instruments=["EURUSD"],
                              mean_reversion={"entry_z": 9.0})
        assert gen.generate() == []                         # z never reaches 9
        assert gen.config["mean_reversion"]["ma_period"] == MR["ma_period"]   # deep-merged
        assert [c[0] for c in src.calls] == ["EURUSD"]

    def test_yfinance_source_uses_documented_tickers_and_injected_downloader(self):
        seen = []

        def fake_download(ticker, interval, period):
            seen.append(ticker)
            return _fx(0.0)

        src = YFinanceBarSource(CFG["yfinance_tickers"], downloader=fake_download)
        for logical in ("EURUSD", "GBPUSD", "USDJPY", "US500", "USTEC", "DE40"):
            assert src.bars(logical, "1h", "60d") is not None
        assert seen == ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "^GSPC", "^NDX", "^GDAXI"]

    def test_trendbar_hook_falls_back_to_yfinance_without_adapter(self):
        gen = FxSignalGenerator(config={"source": "ctrader_trendbars"}, instruments=DEFAULT_INSTRUMENTS,
                                adapter=None)
        assert gen.source.name == "yfinance"

    def test_trendbar_hook_used_when_adapter_capable(self):
        adapter = MagicMock()
        adapter.health.return_value = True
        gen = FxSignalGenerator(config={"source": "ctrader_trendbars"}, instruments=DEFAULT_INSTRUMENTS,
                                adapter=adapter)
        assert gen.source.name == "ctrader_trendbars"


# ── Icarus wiring ─────────────────────────────────────────────────────────────

class TestUniverseSelection:
    def test_resolution(self):
        assert resolve_signal_universe("ibkr", {}) == "stock"
        assert resolve_signal_universe("ctrader", {}) == "fx"
        assert resolve_signal_universe("ctrader", {"ctrader_signals": {"enabled": False}}) == "stock"

    @pytest.fixture
    def no_redis(self):
        from agents.icarus import IcarusAgent
        with patch.object(IcarusAgent, "_init_redis", return_value=None):
            yield

    def test_ibkr_never_calls_fx_path(self, no_redis, monkeypatch):
        from agents.icarus import IcarusAgent
        monkeypatch.setenv("BROKER", "ibkr")
        fx = MagicMock()
        agent = IcarusAgent(api_key="k", fx_generator=fx)
        assert agent.signal_universe == "stock"
        with patch.object(agent, "_fetch_from_supabase", return_value=[]) as supa, \
             patch("core.fx_signals.FxSignalGenerator") as ctor:
            assert agent.fetch() == []
        supa.assert_called_once()
        fx.generate.assert_not_called()
        ctor.assert_not_called()

    def test_default_without_env_is_stock(self, no_redis, monkeypatch):
        from agents.icarus import IcarusAgent
        monkeypatch.delenv("BROKER", raising=False)
        assert IcarusAgent(api_key="k").signal_universe == "stock"

    def test_ctrader_replaces_stock_sources(self, no_redis, monkeypatch):
        from agents.icarus import IcarusAgent
        monkeypatch.setenv("BROKER", "ctrader")
        gen, _ = _generator({"US500": _uptrend_breakout()}, instruments=["US500"])
        agent = IcarusAgent(api_key="k", fx_generator=gen)
        assert agent.signal_universe == "fx"
        with patch.object(agent, "_fetch_from_supabase") as supa:
            first = agent.fetch()
            second = agent.fetch()                     # same bar → de-duplicated
        supa.assert_not_called()
        assert [s.affected_tickers for s in first] == [["US500"]]
        assert second == []
        from core.types import AgentHealth
        assert agent.health() == AgentHealth.HEALTHY   # FX path needs no Supabase


# ── ZEUS risk-budget gate (cTrader) ───────────────────────────────────────────

class TestZeusRiskBudget:
    def _zeus(self, open_positions: int, cfg=None):
        from agents.zeus import ZeusConfig, ZeusOrchestrator
        z = ZeusOrchestrator.__new__(ZeusOrchestrator)
        z.config = ZeusConfig(broker="ctrader", default_account_equity=500.0)
        z._ctrader_cfg = {"risk_per_trade_pct": 0.01, "max_open_risk_pct": 0.03, **(cfg or {})}
        z.argus = MagicMock()
        z.argus.open_position_count.return_value = open_positions
        return z

    def _sized(self, pct=0.02):
        from tests.test_risk_sizing import _sized
        return _sized(side="BUY", stop=0.0025, ref=1.08, pct=pct)

    def test_risk_sizing_active_only_on_ctrader(self):
        from agents.zeus import ZeusConfig, ZeusOrchestrator
        z = self._zeus(0)
        assert z._uses_risk_sizing
        z.config = ZeusConfig(broker="ibkr")
        assert not z._uses_risk_sizing
        z.config = ZeusConfig(broker="ctrader")
        z._ctrader_cfg = {"sizing_mode": "notional"}
        assert not z._uses_risk_sizing
        bare = ZeusOrchestrator.__new__(ZeusOrchestrator)   # no __init__ (as in test_debate)
        bare.config = ZeusConfig()
        assert not bare._uses_risk_sizing

    def test_room_for_trade_passes_even_though_notional_exceeds_equity(self):
        # 1% risk on a 25-pip stop ≈ 4.3× equity notional — the notional
        # max_deployed_pct (90%) check would wrongly block it; the risk gate passes.
        assert self._zeus(open_positions=2)._check_risk_budget(self._sized(), 500.0) is None

    def test_full_risk_budget_blocks(self):
        reason = self._zeus(open_positions=3)._check_risk_budget(self._sized(), 500.0)
        assert reason and reason.startswith("risk budget")

    def test_smaller_upstream_size_uses_less_budget(self):
        z = self._zeus(open_positions=2, cfg={"max_open_risk_pct": 0.025})
        assert z._check_risk_budget(self._sized(pct=0.02), 500.0) is not None   # 2% + 1% > 2.5%
        assert z._check_risk_budget(self._sized(pct=0.005), 500.0) is None      # 2% + 0.5%
