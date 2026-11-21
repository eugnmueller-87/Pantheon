"""
Trade management on the cTrader path — trailing stops, pyramiding, risk ramp.
No network, no live broker: pure functions + MockBrokerAdapter.

Units used throughout (unless stated): long US500 entry 5000, initial stop 4950
→ R = 50 points, ATR = 25 (stop = 2 × ATR). Equity 10,000, base risk 1% = 100
→ first leg 2.00 units, notional 10,000 (1× equity).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from core.broker import BrokerPosition
from core.brokers.mock import MockBrokerAdapter
from core.signal_stats import SignalStats
from core.trade_management import (
    Leg,
    PositionPlan,
    PyramidConfig,
    RiskRamp,
    RiskRampConfig,
    TradeManagementConfig,
    TrailingStopConfig,
    TrailState,
    breakeven_stop,
    expectancy,
    load_trade_management_config,
    pyramid_cap_violation,
    pyramid_gate,
    r_multiple,
    trailing_stop_path,
    update_trailing_stop,
)
from core.types import (
    FilteredSignal,
    MacroContext,
    MarketRegime,
    RawSignal,
    Severity,
    SignalCategory,
    SizedSignal,
)

ATR_TRAIL = TrailingStopConfig(enabled=True, style="atr", trail_activation_R=1.0, trail_atr_multiple=2.0)
CHANDELIER = TrailingStopConfig(enabled=True, style="chandelier", trail_activation_R=1.0, trail_atr_multiple=2.0)
OFF = TrailingStopConfig()


def _sized(ticker="US500", *, side="BUY", stop=50.0, rr=2.0, ref=5000.0, pct=0.02,
           strategy="breakout", atr=25.0) -> SizedSignal:
    now = datetime.now(timezone.utc)
    raw = RawSignal(signal_id="rs-tm", source_url="", headline="t", summary="", published_at=now,
                    category=SignalCategory.MACRO_SHIFT, severity=Severity.MEDIUM, affected_tickers=[ticker],
                    trade_side=side, stop_distance=stop, reward_risk=rr, reference_price=ref,
                    strategy=strategy, atr=atr)
    return SizedSignal(original=FilteredSignal(original=raw, compliance_score=1.0),
                       macro=MacroContext(fetched_at=now, regime=MarketRegime.BULL, vix=15.0, sp500_1m_return=0.0),
                       confidence=0.6, position_size_pct=pct)


def _tm(*, trail: dict | None = None, pyr: dict | None = None, ramp: RiskRampConfig | None = None,
        max_open_risk_pct: float = 0.03) -> TradeManagementConfig:
    return TradeManagementConfig(
        trailing={k: v for k, v in (trail or {}).items()},
        pyramiding={k: v for k, v in (pyr or {}).items()},
        risk_ramp=ramp or RiskRampConfig(),
        risk_per_trade_pct=0.01, max_open_risk_pct=max_open_risk_pct,
        atr_stop_multiple={"breakout": 2.0, "mean_reversion": 2.0},
    )


def _ares(tm: TradeManagementConfig | None = None, *, price=5000.0, equity=10_000.0, stats=None, **bkw):
    from agents.ares import AresAgent
    b = MockBrokerAdapter(prices={"US500": price, "EURUSD": 1.08}, decimals=2, sizing_mode="risk",
                          account_equity=equity, **bkw)
    b.open_positions_on_fill = True
    ares = AresAgent(paper=True, broker=b, default_account_equity=equity,
                     trade_management=tm if tm is not None else _tm(), stats=stats or SignalStats())
    return ares, b


def _places(b: MockBrokerAdapter) -> list[tuple]:
    return [e for e in b.events if e[0] == "place"]


# ═════════════════════════════════════════════════════════════════════════════
# Trailing stop — pure
# ═════════════════════════════════════════════════════════════════════════════

class TestTrailingPure:
    def test_off_keeps_static_initial_stop(self):
        path = trailing_stop_path("BUY", 5000, 4950, 25, [(5200, 5100, 5190), (5400, 5300, 5390)], OFF)
        assert path == [4950, 4950]

    def test_atr_trail_stays_put_before_activation(self):
        # best price 5049 → +0.98R < 1R: initial stop untouched
        path = trailing_stop_path("BUY", 5000, 4950, 25, [(5020, 4990, 5010), (5049, 5000, 5040)], ATR_TRAIL)
        assert path == [4950, 4950]

    def test_atr_trail_ratchets_after_1R_and_never_loosens(self):
        bars = [
            (5050, 5000, 5050),   # +1R → active, close − 2×ATR = 5000 (break-even)
            (5100, 5050, 5100),   # 5050
            (5110, 5060, 5070),   # pullback: 5070 − 50 = 5020 < 5050 → stays 5050
            (5160, 5100, 5160),   # 5110
            (5100, 4990, 5000),   # deep pullback: never loosens
        ]
        path = trailing_stop_path("BUY", 5000, 4950, 25, bars, ATR_TRAIL)
        assert path == [5000, 5050, 5050, 5110, 5110]
        assert all(b >= a for a, b in zip(path, path[1:]))          # monotone, long side

    def test_short_mirror(self):
        bars = [(5000, 4950, 4950), (4950, 4900, 4900), (4960, 4930, 4960)]
        path = trailing_stop_path("SELL", 5000, 5050, 25, bars, ATR_TRAIL)
        assert path == [5000, 4950, 4950]                            # only ever moves DOWN

    def test_atr_multiple_is_configurable(self):
        cfg3 = TrailingStopConfig(enabled=True, trail_atr_multiple=3.0)
        assert trailing_stop_path("BUY", 5000, 4950, 25, [(5100, 5050, 5100)], cfg3) == [5025]

    def test_activation_threshold_is_configurable(self):
        cfg = TrailingStopConfig(enabled=True, trail_activation_R=2.0, trail_atr_multiple=2.0)
        path = trailing_stop_path("BUY", 5000, 4950, 25, [(5090, 5050, 5090), (5100, 5050, 5100)], cfg)
        assert path == [4950, 5050]                                  # 1.8R: no; 2R: yes

    def test_chandelier_trails_from_highest_high_since_entry(self):
        bars = [
            (5080, 5040, 5050),   # active (high +1.6R); HH 5080 − 50 = 5030 (ATR style would be 5000)
            (5120, 5050, 5060),   # HH 5120 → 5070   (ATR style: 5060 − 50 = 5010)
            (5090, 5040, 5045),   # HH still 5120 → 5070
        ]
        assert trailing_stop_path("BUY", 5000, 4950, 25, bars, CHANDELIER) == [5030, 5070, 5070]
        assert trailing_stop_path("BUY", 5000, 4950, 25, bars, ATR_TRAIL) == [5000, 5010, 5010]

    def test_chandelier_short_uses_lowest_low(self):
        bars = [(4960, 4920, 4950), (4950, 4880, 4940)]
        assert trailing_stop_path("SELL", 5000, 5050, 25, bars, CHANDELIER) == [4970, 4930]

    def test_atr_series_refresh(self):
        path = trailing_stop_path("BUY", 5000, 4950, 25, [(5100, 5050, 5100), (5110, 5060, 5110)],
                                  ATR_TRAIL, atr_series=[25, 10])
        assert path == [5050, 5090]

    def test_dataframe_input(self):
        import pandas as pd
        df = pd.DataFrame({"High": [5050, 5100], "Low": [5000, 5050], "Close": [5050, 5100]})
        assert trailing_stop_path("BUY", 5000, 4950, 25, df, ATR_TRAIL) == [5000, 5050]

    def test_invalid_style_rejected(self):
        with pytest.raises(ValueError):
            TrailingStopConfig(enabled=True, style="parabolic")

    def test_r_multiple(self):
        assert r_multiple("BUY", 100, 2, 103) == pytest.approx(1.5)
        assert r_multiple("SELL", 100, 2, 103) == pytest.approx(-1.5)
        with pytest.raises(ValueError):
            r_multiple("BUY", 100, 0, 101)

    def test_update_never_adds_risk_even_with_atr_spike(self):
        st = TrailState(side="BUY", entry=5000, risk_distance=50, stop=4950, atr=25)
        update_trailing_stop(st, ATR_TRAIL, 5100, 5050, 5100)            # → 5050
        update_trailing_stop(st, ATR_TRAIL, 5100, 5050, 5100, atr_value=200)   # 5100 − 400 = 4700
        assert st.stop == 5050

    def test_omit_take_profit_only_when_enabled(self):
        assert TrailingStopConfig(enabled=True, use_fixed_target=False).omit_take_profit
        assert not TrailingStopConfig(enabled=False, use_fixed_target=False).omit_take_profit
        assert not TrailingStopConfig(enabled=True, use_fixed_target=True).omit_take_profit


# ═════════════════════════════════════════════════════════════════════════════
# Trailing stop — config + Ares wiring (mock broker)
# ═════════════════════════════════════════════════════════════════════════════

class TestTrailingConfig:
    def test_defaults_all_off(self):
        tm = load_trade_management_config({}, environ={})
        for strat in ("breakout", "mean_reversion"):
            assert not tm.trailing_for(strat).enabled
            assert tm.trailing_for(strat).use_fixed_target is True
            assert not tm.pyramid_for(strat).enabled
            assert not tm.manages(strat)
        assert not tm.risk_ramp.enabled and not tm.any_enabled
        assert tm.pyramid_for("breakout").pyramid_max_adds == 2
        assert tm.pyramid_for("breakout").pyramid_trigger_R == 1.0
        assert tm.risk_ramp.risk_ramp_high_pct == 0.02 and tm.risk_ramp.risk_ramp_min_trades == 50
        assert tm.risk_ramp.risk_ramp_drawdown_revert_pct == 0.10 and tm.max_open_risk_pct == 0.03

    def test_per_strategy_settings_toggle(self):
        s = {"ctrader_signals": {"breakout": {"trailing_stop": {"enabled": True, "style": "chandelier"}}}}
        tm = load_trade_management_config(s, environ={})
        assert tm.trailing_for("breakout").enabled and tm.trailing_for("breakout").style == "chandelier"
        assert not tm.trailing_for("mean_reversion").enabled          # FX untouched
        tm2 = load_trade_management_config(
            {"ctrader_signals": {"mean_reversion": {"trailing_stop": {"enabled": True}}}}, environ={})
        assert tm2.trailing_for("mean_reversion").enabled and not tm2.trailing_for("breakout").enabled

    def test_partial_override_keeps_other_defaults(self):
        tm = load_trade_management_config(
            {"ctrader_signals": {"breakout": {"trailing_stop": {"enabled": True}}}}, environ={})
        t = tm.trailing_for("breakout")
        assert t.trail_activation_R == 1.0 and t.trail_atr_multiple == 3.0 and t.use_fixed_target

    def test_env_overrides(self):
        env = {"CTRADER_TRAILING_BREAKOUT": "true", "CTRADER_PYRAMID_MEAN_REVERSION": "1",
               "CTRADER_RISK_RAMP": "yes"}
        tm = load_trade_management_config({}, environ=env)
        assert tm.trailing_for("breakout").enabled and not tm.trailing_for("mean_reversion").enabled
        assert tm.pyramid_for("mean_reversion").enabled and not tm.pyramid_for("breakout").enabled
        assert tm.risk_ramp.enabled
        off = load_trade_management_config(
            {"ctrader": {"risk_ramp": {"enabled": True}}}, environ={"CTRADER_RISK_RAMP": "false"})
        assert not off.risk_ramp.enabled
        with pytest.raises(ValueError):
            load_trade_management_config({}, environ={"CTRADER_RISK_RAMP": "maybe"})

    def test_partial_risk_ramp_block_keeps_defaults(self):
        tm = load_trade_management_config({"ctrader": {"risk_ramp": {"enabled": True}}}, environ={})
        assert tm.risk_ramp.enabled and tm.risk_ramp.risk_ramp_high_pct == 0.02


class TestTrailingAres:
    def test_off_unchanged_fixed_2R_target(self):
        ares, b = _ares(_tm())
        res = ares.place(_sized())
        assert res.stop_loss_price == 4950 and res.take_profit_price == 5100     # 2R
        assert ares.managed_plans == {}                                           # nothing managed
        b.set_price("US500", 5200)
        assert ares.manage_open_positions() == [] and b.modifications == []

    def test_lazy_default_config_is_off(self):
        """No config injected → loaded from (default) settings → features off."""
        from agents.ares import AresAgent
        b = MockBrokerAdapter(prices={"US500": 5000.0}, decimals=2, sizing_mode="risk", account_equity=10_000)
        ares = AresAgent(paper=True, broker=b, default_account_equity=10_000)
        res = ares.place(_sized())
        assert res.take_profit_price == 5100 and ares.managed_plans == {}

    def test_use_fixed_target_false_omits_tp(self):
        ares, b = _ares(_tm(trail={"breakout": TrailingStopConfig(enabled=True, use_fixed_target=False)}))
        res = ares.place(_sized())
        assert res.take_profit_price is None and b.orders[-1].take_profit_price is None
        assert res.stop_loss_price == 4950
        assert ares.managed_plans["US500"].take_profit is None

    def test_use_fixed_target_true_keeps_tp_alongside_trail(self):
        ares, b = _ares(_tm(trail={"breakout": ATR_TRAIL}))
        res = ares.place(_sized())
        assert res.take_profit_price == 5100
        b.set_price("US500", 5050)                                 # +1R
        ares.manage_open_positions()
        assert b.modifications[-1]["stop_loss"] == 5000 and b.modifications[-1]["take_profit"] == 5100

    def test_live_trail_ratchets_and_never_loosens(self):
        ares, b = _ares(_tm(trail={"breakout": TrailingStopConfig(enabled=True, trail_atr_multiple=2.0)}))
        ares.place(_sized())
        b.set_price("US500", 5040)                                  # 0.8R — before activation
        assert ares.manage_open_positions() == [] and b.modifications == []
        b.set_price("US500", 5100)                                  # 2R → 5050
        acts = ares.manage_open_positions()
        assert acts == [{"symbol": "US500", "action": "trail", "stop": 5050.0}]
        b.set_price("US500", 5060)                                  # pullback
        assert ares.manage_open_positions() == []
        assert [m["stop_loss"] for m in b.modifications] == [5050.0]
        assert ares.managed_plans["US500"].stop == 5050.0

    def test_trail_short(self):
        ares, b = _ares(_tm(trail={"breakout": ATR_TRAIL}))
        res = ares.place(_sized(side="SELL"))
        assert res.stop_loss_price == 5050 and res.take_profit_price == 4900
        b.set_price("US500", 4900)
        ares.manage_open_positions()
        assert b.modifications[-1]["stop_loss"] == 4950

    def test_per_strategy_isolation(self):
        ares, b = _ares(_tm(trail={"breakout": TrailingStopConfig(enabled=True, use_fixed_target=False)}))
        fx = ares.place(_sized("EURUSD", stop=0.03, ref=1.08, strategy="mean_reversion", atr=0.015))
        assert fx.take_profit_price is not None and "EURUSD" not in ares.managed_plans
        idx = ares.place(_sized())
        assert idx.take_profit_price is None and "US500" in ares.managed_plans

    def test_fx_only_toggle_does_not_touch_breakout(self):
        ares, b = _ares(_tm(trail={"mean_reversion": ATR_TRAIL}))
        ares.place(_sized())
        assert "US500" not in ares.managed_plans

    def test_closed_position_drops_plan(self):
        ares, b = _ares(_tm(trail={"breakout": ATR_TRAIL}))
        ares.place(_sized())
        b.close_all_positions("US500")
        assert ares.manage_open_positions() == [{"symbol": "US500", "action": "closed"}]
        assert ares.managed_plans == {}

    def test_broker_without_modify_hook_keeps_stop(self):
        ares, b = _ares(_tm(trail={"breakout": ATR_TRAIL}), supports_modify=False)
        ares.place(_sized())
        b.set_price("US500", 5100)
        assert ares.manage_open_positions() == []
        assert ares.managed_plans["US500"].stop == 4950

    def test_ibkr_never_loads_trade_management(self):
        from agents.ares import AresAgent
        ares = AresAgent(paper=True, default_account_equity=4000.0)
        assert ares.trade_management is None and ares.risk_ramp is None


# ═════════════════════════════════════════════════════════════════════════════
# Pyramiding
# ═════════════════════════════════════════════════════════════════════════════

PYR = PyramidConfig(enabled=True, pyramid_trigger_R=1.0, pyramid_max_adds=2)


def _plan(**kw) -> PositionPlan:
    base = dict(symbol="US500", strategy="breakout", side="BUY", entry=5000.0, risk_distance=50.0,
                stop=4950.0, take_profit=5100.0, atr=25.0, pyramiding=PYR, decimals=2,
                legs=[Leg(entry=5000.0, units=2.0, position_id="p1", value_per_point=2.0,
                          notional_at_entry=10_000.0)])
    base.update(kw)
    return PositionPlan(**base)


class TestPyramidGatePure:
    @pytest.mark.parametrize("price", [5000.0, 4995.0, 4950.0, 4800.0, 4999.99])
    def test_never_adds_to_losers_or_flat(self, price):
        g = pyramid_gate(_plan(), price)
        assert not g.allowed and g.category == "pyramid: losing position"
        assert g.r is not None and g.r <= 0

    @pytest.mark.parametrize("price", [5050.0, 4900.0])
    def test_short_loser_never_added(self, price):
        g = pyramid_gate(_plan(side="SELL", stop=5050.0, take_profit=4900.0), price)
        if price > 5000:
            assert not g.allowed and g.category == "pyramid: losing position"
        else:
            assert g.allowed and g.breakeven_stop == 5000.0

    def test_disabled(self):
        g = pyramid_gate(_plan(pyramiding=PyramidConfig()), 5200.0)
        assert not g.allowed and g.category == "pyramid: disabled"

    def test_below_trigger(self):
        g = pyramid_gate(_plan(), 5049.0)
        assert not g.allowed and g.category == "pyramid: below trigger"

    def test_at_trigger_allowed_with_break_even(self):
        g = pyramid_gate(_plan(), 5050.0)
        assert g.allowed and g.breakeven_stop == 5000.0 and g.r == pytest.approx(1.0)

    def test_breakeven_buffer(self):
        p = _plan(pyramiding=PyramidConfig(enabled=True, breakeven_buffer_R=0.1))
        assert breakeven_stop(p, 0.1) == 5005.0
        assert breakeven_stop(_plan(stop=5020.0), 0.0) == 5020.0        # never loosened

    def test_max_adds_and_restart_guard(self):
        assert pyramid_gate(_plan(adds=2), 5500.0).category == "pyramid: max adds"
        # restart: in-memory adds=0 but the broker shows 3 legs on the symbol
        assert pyramid_gate(_plan(adds=0), 5500.0, existing_legs=3).category == "pyramid: max adds"

    def test_next_add_measured_from_last_entry(self):
        p = _plan(adds=1)
        p.legs.append(Leg(entry=5050.0, units=2.0, position_id="p2", value_per_point=2.0, notional_at_entry=10_100))
        assert not pyramid_gate(p, 5090.0).allowed           # +0.8R beyond last entry
        assert pyramid_gate(p, 5100.0).allowed

    def test_cap_violation_helpers(self):
        p = _plan()
        kw = dict(add_risk=100.0, add_notional=10_100.0, price=5050.0, be_stop=5000.0, equity=10_000.0,
                  max_open_risk_pct=0.03, other_open_risk=0.0, leverage_cap=20.0)
        assert pyramid_cap_violation(p, **kw) is None
        assert pyramid_cap_violation(p, **{**kw, "max_open_risk_pct": 0.005})[0] == "pyramid: risk cap"
        assert pyramid_cap_violation(p, **{**kw, "other_open_risk": 250.0})[0] == "pyramid: risk cap"
        assert pyramid_cap_violation(p, **{**kw, "leverage_cap": 2.0})[0] == "pyramid: ESMA cap"
        assert pyramid_cap_violation(p, **{**kw, "leverage_cap": None})[0] == "pyramid: ESMA cap"
        # symbol risk counts the existing legs at the BE stop: 0 for leg 1 at 5000
        assert p.risk_to_stop(5000.0) == 0.0 and p.risk_to_stop(4950.0) == pytest.approx(100.0)


def _pyr_ares(pyr: PyramidConfig = PYR, *, max_open_risk_pct=0.03, stats=None, **bkw):
    return _ares(_tm(pyr={"breakout": pyr}, max_open_risk_pct=max_open_risk_pct), stats=stats, **bkw)


class TestPyramidAres:
    def test_no_add_when_disabled(self):
        ares, b = _ares(_tm())
        ares.place(_sized())
        b.set_price("US500", 5200)
        ares.manage_open_positions()
        assert len(_places(b)) == 1 and b.modifications == []

    def test_disabled_for_breakout_when_only_fx_enabled(self):
        ares, b = _ares(_tm(pyr={"mean_reversion": PYR}))
        ares.place(_sized())
        b.set_price("US500", 5200)
        ares.manage_open_positions()
        assert len(_places(b)) == 1

    @pytest.mark.parametrize("price", [5000.0, 4995.0, 4975.0, 4955.0])
    def test_hard_no_add_on_loser(self, price):
        stats = SignalStats()
        ares, b = _pyr_ares(stats=stats)
        ares.place(_sized())
        b.set_price("US500", price)
        assert ares.manage_open_positions() == []
        assert len(_places(b)) == 1, "a losing / flat position must NEVER receive an add"
        assert b.modifications == [] and len(b.risk_calls) == 1
        assert stats.counts("pyramid")["reasons"] == {"pyramid: losing position": 1}

    def test_no_add_before_trigger(self):
        ares, b = _pyr_ares()
        ares.place(_sized())
        b.set_price("US500", 5040)
        ares.manage_open_positions()
        assert len(_places(b)) == 1 and b.modifications == []

    def test_at_trigger_moves_stop_to_be_first_then_adds_base_risk(self):
        ares, b = _pyr_ares()
        first = ares.place(_sized())
        assert first.qty == 2.0
        b.set_price("US500", 5050)
        acts = ares.manage_open_positions()
        assert acts[-1]["action"] == "pyramid" and acts[-1]["add"] == 1
        # order: BE modify of the original leg strictly BEFORE the add order
        kinds = [e[0] for e in b.events]
        assert kinds == ["place", "modify", "place"]
        assert b.events[1][2] == 5000.0                         # original stop → entry (BE)
        add = b.events[2]
        assert add[5] == 5000.0 and add[6] == 5100.0            # add: BE stop, same target
        # sized with the base 1% risk on the post-BE stop distance (50 points)
        assert b.risk_calls[-1] == {"symbol": "US500", "entry": 5050.0, "stop": 5000.0, "risk_pct": 0.01}
        assert acts[-1]["risk"] <= 10_000 * 0.01 + 1e-9
        plan = ares.managed_plans["US500"]
        assert plan.adds == 1 and plan.stop == 5000.0 and len(plan.legs) == 2

    def test_third_add_rejected_with_max_adds_2(self):
        stats = SignalStats()
        ares, b = _pyr_ares(stats=stats)
        ares.place(_sized())
        for px in (5050, 5100):                                  # add 1, add 2 (+1R beyond each entry)
            b.set_price("US500", px)
            ares.manage_open_positions()
        assert ares.managed_plans["US500"].adds == 2 and len(_places(b)) == 3
        b.set_price("US500", 5200)
        ares.manage_open_positions()
        assert len(_places(b)) == 3                              # no third add
        assert stats.counts("pyramid")["fired"] == 2
        assert stats.counts("pyramid")["reasons"].get("pyramid: max adds") == 1

    def test_second_add_risk_counts_open_legs(self):
        """Add 2: leg 2 still risks 100 to the BE stop; with a 1.5% cap the add is refused."""
        ares, b = _pyr_ares(max_open_risk_pct=0.015)
        ares.place(_sized())
        b.set_price("US500", 5050)
        ares.manage_open_positions()                             # add 1: symbol risk 0 + 100 ≤ 150
        b.set_price("US500", 5100)
        ares.manage_open_positions()                             # add 2: 100 + 100 > 150
        assert ares.managed_plans["US500"].adds == 1

    def test_rejected_on_account_risk_cap(self):
        stats = SignalStats()
        ares, b = _pyr_ares(stats=stats)
        ares.place(_sized())
        for i in range(3):                                       # 3 untracked positions × 1% = 3%
            b.add_position(BrokerPosition(f"X{i}", "LONG", 1, 1.0, 1.0, 0.0, position_id=f"u{i}"))
        b.set_price("US500", 5050)
        ares.manage_open_positions()
        assert len(_places(b)) == 1 and b.modifications == []    # BE not moved either
        assert stats.counts("pyramid")["reasons"] == {"pyramid: risk cap": 1}

    def test_rejected_on_symbol_risk_cap(self):
        ares, b = _pyr_ares(max_open_risk_pct=0.005)
        ares.place(_sized())
        b.set_price("US500", 5050)
        ares.manage_open_positions()
        assert len(_places(b)) == 1

    def test_rejected_on_esma_cap(self):
        stats = SignalStats()
        ares, b = _pyr_ares(stats=stats, leverage_cap=1.5)       # 10k + 10.1k > 15k
        ares.place(_sized())
        b.set_price("US500", 5050)
        ares.manage_open_positions()
        assert len(_places(b)) == 1
        assert stats.counts("pyramid")["reasons"] == {"pyramid: ESMA cap": 1}

    def test_unknown_esma_cap_fails_closed(self):
        ares, b = _pyr_ares(leverage_cap=None)
        ares.place(_sized())
        b.set_price("US500", 5050)
        ares.manage_open_positions()
        assert len(_places(b)) == 1

    def test_rejected_on_min_lot_overshoot(self):
        """The existing min-lot rule (min-lot loss <= 1.5 × budget) also gates adds —
        it is never relaxed to force one."""
        stats = SignalStats()
        ares, b = _pyr_ares(stats=stats, min_units=2.0)          # first leg: exactly 2 units, OK
        ares.place(_sized())
        b.min_units = 3.5                                        # add: 3.5 × 50 = 175 > 1.5 × 100
        b.set_price("US500", 5050)
        ares.manage_open_positions()
        assert len(_places(b)) == 1 and b.modifications == []    # no add, BE not moved
        assert stats.counts("pyramid")["reasons"] == {"pyramid: min-lot overshoot": 1}

    def test_min_lot_within_tolerance_still_adds(self):
        ares, b = _pyr_ares(min_units=2.0)
        ares.place(_sized())
        b.min_units = 2.5                                        # 2.5 × 50 = 125 <= 150 → min lot
        b.set_price("US500", 5050)
        ares.manage_open_positions()
        assert len(_places(b)) == 2 and _places(b)[-1][3] == 2.5

    def test_be_move_failure_aborts_add(self):
        ares, b = _pyr_ares(supports_modify=False)
        ares.place(_sized())
        b.set_price("US500", 5050)
        ares.manage_open_positions()
        assert len(_places(b)) == 1

    def test_no_martingale_loss_never_triggers_larger_order(self):
        ares, b = _pyr_ares()
        first = ares.place(_sized())
        for px in (4980, 4960, 4951, 5000):                      # losing / flat path
            b.set_price("US500", px)
            ares.manage_open_positions()
        assert len(_places(b)) == 1 and len(b.risk_calls) == 1
        b.set_price("US500", 5050)                               # recovers to +1R → add allowed
        ares.manage_open_positions()
        add = _places(b)[-1]
        assert add[3] <= first.qty                               # never larger than the first fill
        assert all(c["risk_pct"] <= 0.01 for c in b.risk_calls)  # never above base risk

    def test_add_uses_base_risk_even_when_ramped(self):
        tm = _tm(pyr={"breakout": PYR}, ramp=RiskRampConfig(enabled=True, risk_ramp_min_trades=1))
        ares, b = _ares(tm)
        ares.update_risk_ramp([0.05], equity=10_000)
        assert ares.current_risk_pct() == 0.02
        ares.place(_sized())
        assert b.risk_calls[0]["risk_pct"] == 0.02
        b.set_price("US500", 5050)
        ares.manage_open_positions()
        assert b.risk_calls[-1]["risk_pct"] == 0.01

    def test_seniority_ceiling_lowers_add_risk(self):
        ares, b = _pyr_ares()
        ares.place(_sized())
        b.set_price("US500", 5050)
        ares.manage_open_positions(risk_ceiling_pct=0.005)
        assert b.risk_calls[-1]["risk_pct"] == 0.005

    def test_short_pyramid(self):
        ares, b = _pyr_ares()
        ares.place(_sized(side="SELL"))
        b.set_price("US500", 4950)
        ares.manage_open_positions()
        kinds = [e[0] for e in b.events]
        assert kinds == ["place", "modify", "place"] and b.events[1][2] == 5000.0
        assert _places(b)[-1][2] == "SELL"

    def test_restart_without_plan_never_adds(self):
        """A fresh Ares (restart) has no plan for a broker position → no adds at all."""
        ares, b = _pyr_ares()
        b.add_position(BrokerPosition("US500", "LONG", 2, 5000, 5200, 400, position_id="old"))
        assert ares.manage_open_positions() == [] and _places(b) == []

    def test_trailing_and_pyramid_together(self):
        tm = _tm(trail={"breakout": ATR_TRAIL}, pyr={"breakout": PYR})
        ares, b = _ares(tm)
        ares.place(_sized())
        b.set_price("US500", 5100)                               # trail → 5050, then add with stop 5050
        acts = ares.manage_open_positions()
        assert [a["action"] for a in acts] == ["trail", "pyramid"]
        add = _places(b)[-1]
        assert add[5] == 5050.0                                  # add uses the (already tighter) stop
        assert all(m["stop_loss"] >= 5000 for m in b.modifications)


# ═════════════════════════════════════════════════════════════════════════════
# Risk ramp
# ═════════════════════════════════════════════════════════════════════════════

RAMP = RiskRampConfig(enabled=True, base_pct=0.01, risk_ramp_high_pct=0.02, risk_ramp_min_trades=50,
                      risk_ramp_drawdown_revert_pct=0.10)


class TestRiskRamp:
    def test_disabled_always_base(self):
        r = RiskRamp(RiskRampConfig())
        assert r.update([1.0] * 500, equity=10_000) == 0.01
        assert r.update([1.0] * 500, equity=20_000) == 0.01

    def test_fewer_than_n_trades_stays_base(self):
        r = RiskRamp(RAMP)
        assert r.update([0.5] * 49, equity=10_000) == 0.01

    @pytest.mark.parametrize("outcomes", [[-0.01] * 50, [0.0] * 50, [0.02, -0.02] * 25])
    def test_non_positive_expectancy_stays_base(self, outcomes):
        assert RiskRamp(RAMP).update(outcomes, equity=10_000) == 0.01

    def test_n_trades_positive_expectancy_steps_up_and_logs(self, caplog):
        r = RiskRamp(RAMP)
        with caplog.at_level(logging.INFO, logger="trade_management"):
            assert r.update([0.01] * 50, equity=10_000) == 0.02
        msgs = [m for m in caplog.messages if "risk ramp" in m]
        assert len(msgs) == 1
        assert "1% → 2%" in msgs[0] and "after 50 closed trades" in msgs[0] and "expectancy +0.01" in msgs[0]

    def test_expectancy_uses_last_n(self):
        assert expectancy([-1.0] * 50 + [0.1] * 50, 50) == pytest.approx(0.1)
        assert expectancy([0.1] * 10, 50) is None
        assert RiskRamp(RAMP).update([-1.0] * 50 + [0.1] * 50, equity=10_000) == 0.02

    def test_drawdown_reverts_and_logs(self, caplog):
        r = RiskRamp(RAMP)
        r.update([0.01] * 50, equity=10_000)
        assert r.update([0.01] * 50, equity=9_500) == 0.02           # 5% DD: stays
        with caplog.at_level(logging.INFO, logger="trade_management"):
            assert r.update([0.01] * 50, equity=9_000) == 0.01       # 10% DD: back to base
        assert any("2% → 1%" in m and "after 10% drawdown from peak" in m for m in caplog.messages)

    def test_rearm_only_after_recovering_peak(self):
        r = RiskRamp(RAMP)
        r.update([0.01] * 50, equity=10_000)
        r.update([0.01] * 50, equity=8_900)
        assert r.update([0.01] * 50, equity=9_800) == 0.01           # still below the old peak
        assert r.update([0.01] * 50, equity=10_000) == 0.02          # recovered → may ramp again

    def test_uses_argus_peak(self):
        r = RiskRamp(RAMP)
        assert r.update([0.01] * 50, equity=8_900, peak_equity=10_000) == 0.01   # already 11% below peak

    def test_expectancy_turning_negative_drops_back(self, caplog):
        r = RiskRamp(RAMP)
        r.update([0.01] * 50, equity=10_000)
        with caplog.at_level(logging.INFO, logger="trade_management"):
            assert r.update([0.01] * 50 + [-0.05] * 50, equity=10_000) == 0.01
        assert any("expectancy no longer positive" in m for m in caplog.messages)

    def test_no_log_without_step_change(self, caplog):
        r = RiskRamp(RAMP)
        with caplog.at_level(logging.INFO, logger="trade_management"):
            r.update([0.01] * 10, equity=10_000)
            r.update([0.01] * 20, equity=10_000)
        assert not [m for m in caplog.messages if "risk ramp" in m]

    def test_config_validation(self):
        with pytest.raises(ValueError):
            RiskRampConfig(enabled=True, base_pct=0.01, risk_ramp_high_pct=0.005)
        with pytest.raises(ValueError):
            RiskRampConfig(enabled=True, risk_ramp_high_pct=0.10)    # > 5%


class TestRiskRampWiring:
    def test_disabled_ares_risk_is_base(self):
        ares, b = _ares(_tm())
        assert ares.risk_ramp is None and ares.current_risk_pct() == 0.01
        ares.update_risk_ramp([1.0] * 100, equity=10_000)
        ares.place(_sized())
        assert b.risk_calls[-1]["risk_pct"] == 0.01

    def test_enabled_ares_sizes_at_2pct_after_ramp(self):
        ares, b = _ares(_tm(ramp=RAMP))
        assert b.max_risk_per_trade_pct == 0.02                     # broker ceiling raised
        ares.place(_sized())
        assert b.risk_calls[-1]["risk_pct"] == 0.01                 # not ramped yet
        ares.update_risk_ramp([0.01] * 50, equity=10_000)
        ares.place(_sized())
        assert b.risk_calls[-1]["risk_pct"] == 0.02
        ares.place(_sized(pct=0.015))                               # upstream ceiling still lowers it
        assert b.risk_calls[-1]["risk_pct"] == 0.015

    def test_zeus_risk_budget_uses_ramp_level(self):
        from unittest.mock import MagicMock

        from agents.zeus import ZeusConfig, ZeusOrchestrator
        z = ZeusOrchestrator.__new__(ZeusOrchestrator)
        z.config = ZeusConfig(broker="ctrader", default_account_equity=10_000.0)
        z._ctrader_cfg = {"risk_per_trade_pct": 0.01, "max_open_risk_pct": 0.03}
        z.argus = MagicMock()
        z.argus.open_position_count.return_value = 1
        z.ares = MagicMock()
        z.ares.risk_ramp = RiskRamp(RAMP)
        assert z._current_risk_pct() == 0.01
        assert z._check_risk_budget(_sized(pct=0.05), 10_000) is None          # 1% + 1%
        z.ares.risk_ramp.update([0.01] * 50, equity=10_000)
        assert z._current_risk_pct() == 0.02
        assert z._check_risk_budget(_sized(pct=0.05), 10_000) is not None      # 2% + 2% > 3%

    def test_pythia_closed_trade_outcomes_sqlite(self, tmp_path):
        import sqlite3

        from agents.pythia import PythiaAgent
        p = PythiaAgent(db_path=tmp_path / "t.db")
        with sqlite3.connect(p.db_path) as c:
            for i, (pnl, hit) in enumerate([(0.02, 1), (None, None), (-0.01, 0), (0.03, 1)]):
                c.execute("INSERT INTO trades (trade_id, pnl_pct, hit, recorded_at) VALUES (?,?,?,?)",
                          (f"t{i}", pnl, hit, f"2026-11-0{i + 1}T00:00:00"))
        assert p.closed_trade_outcomes() == [0.02, -0.01, 0.03]
