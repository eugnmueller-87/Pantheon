"""
Signal frequency logging — fired vs skipped counters (core/signal_stats.py),
their wiring in the FX generator / Icarus / Ares / ZEUS, and the cTrader
adapter hooks trade management relies on (fake transport, no network).
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from core.brokers.mock import MockBrokerAdapter
from core.signal_stats import SIGNAL_STATS, SignalStats, classify_skip
from tests.test_fx_signals import (
    NOW_ASIA,
    _flat_noisy_range,
    _fx,
    _generator,
    _uptrend_breakout,
)


class TestClassifier:
    @pytest.mark.parametrize("reason,cat", [
        ("outside session (london_ny_overlap)", "outside session"),
        ("no breakout (close 5000 inside [4950, 5050])", "no breakout"),
        ("not stretched: |z|=0.10 < 2", "not stretched"),
        ("over-stretched: |z|=4.00 > 3.5 (not fading a trend break)", "over-stretched"),
        ("volatility too low: ATR 0.01% < 0.40%", "volatility veto"),
        ("volatility blow-off: ATR 3.00× its recent average > 2×", "volatility veto"),
        ("BUY breakout against trend (close 1 vs SMA50 2)", "against trend"),
        ("insufficient bars (3 < 51)", "insufficient bars"),
        ("stale data: last bar 9.0h old > 3h", "stale data"),
        ("data error: boom", "data error"),
        ("EURUSD: risk-sized 500 units is below the venue minimum (1000 units). Intended risk 5.00",
         "min-lot overshoot"),
        ("DE40: even the minimum lot (1 units ≈ 20000.00) exceeds the ESMA cap 1:20 on equity 500.00",
         "ESMA cap"),
        ("USDJPY: no JPY→account conversion rate — refusing to size (fail closed)", "missing conversion"),
        ("risk budget: open risk 3.00% (3 pos × 1.00%) + this trade 1.00% > cap 3.00%", "risk cap"),
        ("", "unknown"),
        ("something new: detail", "something new"),
    ])
    def test_existing_reason_strings_map_to_categories(self, reason, cat):
        assert classify_skip(reason) == cat


class TestCounters:
    def test_fired_and_skipped_cycle_and_lifetime(self):
        st = SignalStats()
        st.fired("icarus")
        st.skipped("icarus", "outside session (any)")
        st.skipped("icarus", "outside session (any)")
        st.skipped("icarus", "no breakout (close 1 inside [0, 2])")
        c = st.counts("icarus")
        assert c == {"fired": 1, "skipped": 3, "reasons": {"outside session": 2, "no breakout": 1}}
        assert st.summary("icarus") == "fired=1 skipped=3 (outside session=2, no breakout=1)"
        st.begin_cycle("icarus")
        assert st.counts("icarus") == {"fired": 0, "skipped": 0, "reasons": {}}
        assert st.counts("icarus", "lifetime")["skipped"] == 3        # lifetime kept
        st.skipped("icarus", "x", category="min-lot overshoot")
        assert st.counts("icarus", "lifetime")["reasons"]["min-lot overshoot"] == 1

    def test_sources_are_independent(self):
        st = SignalStats()
        st.fired("ares")
        st.skipped("icarus", "stale data: old")
        st.begin_cycle("ares")
        assert st.counts("ares")["fired"] == 0 and st.counts("icarus")["skipped"] == 1

    def test_reclassify_fired(self):
        st = SignalStats()
        st.fired("icarus")
        st.reclassify_fired("icarus", "already emitted this bar")
        assert st.counts("icarus") == {"fired": 0, "skipped": 1, "reasons": {"duplicate": 1}}

    def test_log_summary_line(self, caplog):
        st = SignalStats()
        st.fired("icarus")
        for r in ("outside session (x)", "outside session (x)", "no breakout (a)", "no breakout (b)"):
            st.skipped("icarus", r)
        st.skipped("icarus", "y", category="min-lot overshoot")
        lg = logging.getLogger("test.stats")
        with caplog.at_level(logging.INFO, logger="test.stats"):
            line = st.log_summary(lg, "icarus", "[ICARUS]")
        assert line.startswith(
            "[ICARUS] signals: fired=1 skipped=5 (no breakout=2, outside session=2, min-lot overshoot=1)")
        assert "lifetime fired=1 skipped=5" in line and line in caplog.messages

    def test_bad_scope(self):
        with pytest.raises(ValueError):
            SignalStats().counts("x", scope="week")


class TestGeneratorAndIcarus:
    def test_generator_counts_fired_and_skip_reasons(self):
        gen, _ = _generator({"US500": _uptrend_breakout(), "DE40": _flat_noisy_range(), "EURUSD": _fx(0.0)},
                            instruments=["US500", "DE40", "EURUSD"])
        gen.stats = SignalStats()
        sigs = gen.generate()
        assert len(sigs) == 1
        c = gen.stats.counts("icarus")
        assert c["fired"] == 1 and c["skipped"] == 2
        assert c["reasons"] == {"no breakout": 1, "not stretched": 1}
        gen.generate()                                     # new cycle resets cycle counts
        assert gen.stats.counts("icarus")["fired"] == 1
        assert gen.stats.counts("icarus", "lifetime")["fired"] == 2

    def test_session_skips_counted(self):
        gen, _ = _generator({"EURUSD": _fx(-0.0015)}, now=NOW_ASIA, instruments=["EURUSD"])
        gen.stats = SignalStats()
        assert gen.generate() == []
        assert gen.stats.counts("icarus")["reasons"] == {"outside session": 1}

    def test_icarus_logs_cycle_summary_and_counts_duplicates(self, caplog, monkeypatch):
        from agents.icarus import IcarusAgent
        monkeypatch.setenv("BROKER", "ctrader")
        gen, _ = _generator({"US500": _uptrend_breakout(), "DE40": _flat_noisy_range()},
                            instruments=["US500", "DE40"])
        gen.stats = SignalStats()
        with patch.object(IcarusAgent, "_init_redis", return_value=None):
            agent = IcarusAgent(api_key="k", fx_generator=gen)
        with caplog.at_level(logging.INFO, logger="icarus"):
            assert len(agent.fetch()) == 1
        assert any(m.startswith("[ICARUS] signals: fired=1 skipped=1 (no breakout=1)") for m in caplog.messages)
        assert agent.fetch() == []                          # same bar → de-duplicated
        assert gen.stats.counts("icarus") == {"fired": 0, "skipped": 2,
                                              "reasons": {"duplicate": 1, "no breakout": 1}}


class TestAresAndZeusCounters:
    def _sized(self, **kw):
        from tests.test_trade_management import _sized
        return _sized(**kw)

    def test_ares_counts_placed_and_min_lot_skip(self):
        from agents.ares import AresAgent
        st = SignalStats()
        b = MockBrokerAdapter(prices={"US500": 5000.0}, decimals=2, sizing_mode="risk", account_equity=10_000)
        ares = AresAgent(paper=True, broker=b, default_account_equity=10_000, stats=st)
        ares.place(self._sized())
        b.min_units = 10.0                                  # 10 × 50 = 500 > 1.5 × 100
        res = ares.place(self._sized())
        assert res.status.startswith("error")
        assert st.counts("ares") == {"fired": 1, "skipped": 1, "reasons": {"min-lot overshoot": 1}}

    def test_ares_counts_queued(self):
        from agents.ares import AresAgent
        st = SignalStats()
        b = MockBrokerAdapter(prices={"US500": 5000.0}, reachable=False, sizing_mode="risk")
        ares = AresAgent(paper=True, broker=b, default_account_equity=10_000, stats=st)
        with patch("core.supabase_client.enqueue_pending_order"):
            ares.place(self._sized())
        assert st.counts("ares")["reasons"] == {"queued (broker unreachable)": 1}

    def test_zeus_cycle_log_lines(self, caplog):
        from agents.zeus import ZeusOrchestrator
        z = ZeusOrchestrator.__new__(ZeusOrchestrator)
        SIGNAL_STATS.begin_cycle("ares")
        SIGNAL_STATS.begin_cycle("zeus")
        SIGNAL_STATS.begin_cycle("pyramid")
        SIGNAL_STATS.fired("ares")
        SIGNAL_STATS.skipped("zeus", "risk budget: open risk 3.00% ...", category="risk cap")
        with caplog.at_level(logging.INFO, logger="zeus"):
            z._log_cycle_stats()
        assert any(m.startswith("[ARES] execution: placed=1 skipped=0") for m in caplog.messages)
        assert any(m.startswith("[ZEUS] gates: passed=0 skipped=1 (risk cap=1)") for m in caplog.messages)
        assert not any("[ARES] pyramid:" in m for m in caplog.messages)   # no activity → no line
        for src in ("ares", "zeus"):
            SIGNAL_STATS.begin_cycle(src)

    def test_zeus_manage_positions_respects_halt_and_mock(self):
        from agents.zeus import ZeusConfig, ZeusOrchestrator
        from core.types import PipelineStatus
        z = ZeusOrchestrator.__new__(ZeusOrchestrator)
        z.config = ZeusConfig(broker="ctrader", mock_execution=False)
        z.ares = MagicMock()
        z.ares.managed_plans = {"US500": object()}
        z.ares.manage_open_positions.return_value = [{"action": "trail"}]
        z.cb = MagicMock()
        z.cb.call.side_effect = lambda name, fn, fallback: fn()
        z._seniority_report = None
        z.status = PipelineStatus.HALTED
        assert z._manage_positions() == [] and not z.ares.manage_open_positions.called
        z.status = PipelineStatus.RUNNING
        assert z._manage_positions() == [{"action": "trail"}]
        z.config.mock_execution = True
        z.ares.manage_open_positions.reset_mock()
        assert z._manage_positions() == [] and not z.ares.manage_open_positions.called


# ── cTrader adapter hooks (fake transport) ────────────────────────────────────

class TestCTraderHooks:
    @pytest.fixture(autouse=True)
    def _need_sdk(self):
        pytest.importorskip("ctrader_open_api")

    def _adapter(self, **kw):
        from tests.test_ctrader_adapter import _adapter
        a, t = _adapter(**kw)
        a.connect()
        return a, t

    def _last(self, t, name):
        return [m for m in t.sent if type(m).__name__ == name][-1]

    def test_place_without_take_profit_omits_tp(self):
        a, t = self._adapter()
        res = a.place_bracket("EURUSD", "BUY", 1000, 1.08, 1.077, None)
        req = self._last(t, "ProtoOANewOrderReq")
        assert req.HasField("relativeStopLoss") and not req.HasField("relativeTakeProfit")
        assert res.take_profit_price is None

    def test_place_with_take_profit_unchanged(self):
        a, t = self._adapter()
        a.place_bracket("EURUSD", "BUY", 1000, 1.08, 1.077, 1.086)
        req = self._last(t, "ProtoOANewOrderReq")
        assert req.relativeStopLoss == 300 and req.relativeTakeProfit == 600

    def test_no_tp_still_validates_stop_side(self):
        from core.broker import BrokerRejectedError
        a, _ = self._adapter()
        with pytest.raises(BrokerRejectedError):
            a.place_bracket("EURUSD", "BUY", 1000, 1.08, 1.09, None)
        with pytest.raises(BrokerRejectedError):
            a.place_bracket("EURUSD", "SELL", 1000, 1.08, 1.07, None)

    def test_modify_position_sltp_sends_amend(self):
        from tests.test_ctrader_adapter import ACC, MM, M
        a, t = self._adapter()
        t.overrides["ProtoOAAmendPositionSLTPReq"] = [
            M.ProtoOAExecutionEvent(ctidTraderAccountId=ACC, executionType=MM.ORDER_REPLACED)]
        a.modify_position_sltp("888", 1.081234567, 1.09, symbol="EURUSD")
        req = self._last(t, "ProtoOAAmendPositionSLTPReq")
        assert req.positionId == 888 and req.stopLoss == pytest.approx(1.08123) and req.takeProfit == 1.09

    def test_modify_without_tp_omits_it(self):
        from tests.test_ctrader_adapter import ACC, MM, M
        a, t = self._adapter()
        t.overrides["ProtoOAAmendPositionSLTPReq"] = [
            M.ProtoOAExecutionEvent(ctidTraderAccountId=ACC, executionType=MM.ORDER_REPLACED)]
        a.modify_position_sltp(9, 1.075)
        assert not self._last(t, "ProtoOAAmendPositionSLTPReq").HasField("takeProfit")

    def test_modify_rejection_raises(self):
        from core.broker import BrokerRejectedError
        from tests.test_ctrader_adapter import ACC, M
        a, t = self._adapter()
        t.overrides["ProtoOAAmendPositionSLTPReq"] = [
            M.ProtoOAOrderErrorEvent(ctidTraderAccountId=ACC, errorCode="TRADING_BAD_STOPS", description="bad")]
        with pytest.raises(BrokerRejectedError):
            a.modify_position_sltp(9, 1.075)

    def test_positions_carry_id_and_protection(self):
        from tests.test_ctrader_adapter import MM
        a, t = self._adapter()
        t.positions_on_account = [MM.ProtoOAPosition(
            positionId=9, positionStatus=1, swap=0, price=1.07, stopLoss=1.065, takeProfit=1.08,
            tradeData=MM.ProtoOATradeData(symbolId=1, volume=8000, tradeSide=MM.BUY))]
        [p] = a.positions()
        assert p.position_id == "9" and p.stop_loss == 1.065 and p.take_profit == 1.08

    def test_risk_ceiling_and_leverage_cap(self):
        from core.broker import BrokerConfigError
        a, _ = self._adapter(equity=500.0)
        assert a.size_order_risk("EURUSD", 1.08, 1.077, risk_pct=0.02).risk_budget == pytest.approx(5.0)
        a.set_risk_ceiling(0.02)
        assert a.size_order_risk("EURUSD", 1.08, 1.077, risk_pct=0.02).risk_budget == pytest.approx(10.0)
        with pytest.raises(BrokerConfigError):
            a.set_risk_ceiling(0.06)
        with pytest.raises(BrokerConfigError):
            a.set_risk_ceiling(0.005)
        assert a.leverage_cap_for("EURUSD") == 30.0 and a.leverage_cap_for("DE40") == 20.0
