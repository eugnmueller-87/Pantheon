"""
Risk-based sizing on the cTrader path (no network).

    risk_amount = equity × risk_pct
    units       = risk_amount / (|entry − stop| × quote_to_account)  → floored to step
    min lot     = used only if its loss at the stop <= risk × max_min_lot_risk_overshoot
    ESMA        = notional > equity × cap → shrink to cap; min lot over cap → skip

Also pins that the IBKR path keeps notional share sizing and its % stops.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from core.broker import BrokerRejectedError
from core.brokers.ctrader_instruments import (
    Instrument,
    SymbolSpec,
    compute_risk_volume,
    estimate_risk_notional_fraction,
)
from core.brokers.mock import MockBrokerAdapter
from core.types import (
    FilteredSignal,
    MacroContext,
    MarketRegime,
    RawSignal,
    Severity,
    SignalCategory,
    SizedSignal,
)

EURUSD = Instrument("EURUSD", "fx", "USD", ["EURUSD"])
DE40   = Instrument("DE40", "index", "EUR", ["DE40"])
EUR_PER_USD = 1 / 1.08


def _spec(min_v: int, step: int, max_v=None, digits=5) -> SymbolSpec:
    return SymbolSpec(symbol_id=1, broker_name="X", digits=digits, min_volume=min_v,
                      step_volume=step, max_volume=max_v)


# IC Markets-like FX spec: min 0.01 lot = 1000 units, step 1000 units (protocol ×100).
IC_FX = _spec(min_v=100_000, step=100_000)
FINE_FX = _spec(min_v=100, step=100)   # 1-unit min/step


def _size(**kw):
    base = dict(equity=500.0, risk_pct=0.01, entry=1.08000, stop=1.07700,
                quote_to_account=EUR_PER_USD, spec=FINE_FX, inst=EURUSD,
                max_min_lot_risk_overshoot=1.5)
    base.update(kw)
    return compute_risk_volume(**base)


class TestRiskFormula:
    def test_one_percent_of_500_eur_with_30_pip_stop(self):
        # risk €5; 30 pips = 0.003 USD = 0.0027778 EUR per unit → 1800 units
        r = _size()
        assert r.risk_budget == pytest.approx(5.0)
        assert r.loss_per_unit == pytest.approx(0.003 / 1.08)
        assert r.units == pytest.approx(1800.0)
        assert r.risk_at_stop == pytest.approx(5.0, abs=0.01)
        assert not r.used_min_lot and not r.capped_by_leverage

    def test_rounds_down_to_step(self):
        # Same trade on a 1000-unit step grid: 1800 → 1000 units (never rounds UP risk)
        r = _size(spec=IC_FX)
        assert r.units == 1000.0
        assert r.volume == 100_000
        assert r.risk_at_stop < r.risk_budget

    def test_short_side_uses_absolute_distance(self):
        r = _size(entry=1.08000, stop=1.08300)
        assert r.units == pytest.approx(1800.0)

    def test_risk_scales_with_stop_not_with_price(self):
        tight, wide = _size(stop=1.07850), _size(stop=1.07400)   # 15 vs 60 pips
        assert tight.units == pytest.approx(4 * wide.units, rel=1e-3)
        assert tight.risk_at_stop == pytest.approx(wide.risk_at_stop, abs=0.01)

    def test_notional_fraction_estimate_matches_sizer(self):
        r = _size()
        est = estimate_risk_notional_fraction(0.01, 1.08, 0.003)
        assert r.notional_fraction == pytest.approx(est, rel=1e-3)   # 3.6× equity


class TestMinLot:
    def test_min_lot_within_tolerance_is_used(self):
        # 60 pip stop: raw 900 units < 1000 min; min-lot risk €5.56 <= €5 × 1.5 → use min
        r = _size(spec=IC_FX, stop=1.07400)
        assert r.used_min_lot
        assert r.units == 1000.0
        assert r.risk_at_stop == pytest.approx(5.556, abs=0.01)

    def test_min_lot_beyond_tolerance_is_skipped_with_clear_reason(self):
        # 120 pip stop: min-lot risk €11.11 > €7.50 → skip, never silently over-risk
        with pytest.raises(BrokerRejectedError) as exc:
            _size(spec=IC_FX, stop=1.06800)
        msg = str(exc.value)
        assert "Intended risk 5.00" in msg
        assert "min-lot risk 11.11" in msg
        assert "1.5×" in msg and "not placed" in msg

    def test_tolerance_is_configurable(self):
        r = _size(spec=IC_FX, stop=1.06800, max_min_lot_risk_overshoot=2.5)
        assert r.used_min_lot and r.units == 1000.0


class TestEsmaCap:
    def test_notional_above_cap_is_shrunk_to_cap(self):
        # 1 pip stop → 54,000 units ≈ €54,000 notional > €500 × 30 = €15,000 → shrink
        r = _size(stop=1.07990)
        assert r.capped_by_leverage
        assert r.notional <= 500.0 * 30 + 1e-6
        assert r.units == pytest.approx(15_000.0, abs=1.0)
        assert r.risk_at_stop < r.risk_budget           # shrinking only lowers risk

    def test_min_lot_above_cap_is_skipped(self):
        # DE40 @ 20,000 EUR, 4-pt stop: risk-wise 1.25 units is fine, but the min
        # 1 unit = €20,000 notional > €500 × 20 = €10,000 → skip, never exceed ESMA
        with pytest.raises(BrokerRejectedError, match="ESMA"):
            compute_risk_volume(equity=500.0, risk_pct=0.01, entry=20_000.0, stop=19_996.0,
                                quote_to_account=1.0, spec=_spec(100, 100, digits=2), inst=DE40)

    def test_settings_cap_override_tightens(self):
        tight = Instrument("EURUSD", "fx", "USD", ["EURUSD"], cap_override=5.0)
        r = _size(stop=1.07990, inst=tight)
        assert r.notional <= 500.0 * 5 + 1e-6


class TestFailClosed:
    @pytest.mark.parametrize("q2a", [None, 0.0, -1.0])
    def test_missing_conversion_rate_refuses(self, q2a):
        with pytest.raises(BrokerRejectedError, match="conversion"):
            _size(quote_to_account=q2a)

    def test_stop_equal_entry_refuses(self):
        with pytest.raises(BrokerRejectedError, match="stop equals entry"):
            _size(stop=1.08000)

    def test_bad_equity_refuses(self):
        with pytest.raises(BrokerRejectedError):
            _size(equity=0.0)


# ── Adapter integration (fake transport, no network) ──────────────────────────

class TestAdapterRiskSizing:
    @pytest.fixture(autouse=True)
    def _need_sdk(self):
        pytest.importorskip("ctrader_open_api")

    def _adapter(self, **kw):
        from tests.test_ctrader_adapter import _adapter
        a, t = _adapter(**kw)
        a.connect()
        return a

    def test_500_eur_demo_equity(self):
        a = self._adapter(equity=500.0)
        r = a.size_order_risk("EURUSD", entry=1.08, stop_loss=1.077)
        assert r.risk_budget == pytest.approx(5.0)
        assert r.units == pytest.approx(1800.0)

    def test_risk_pct_is_capped_at_configured_value(self):
        a = self._adapter(equity=500.0, risk_per_trade_pct=0.01)
        r = a.size_order_risk("EURUSD", entry=1.08, stop_loss=1.077, risk_pct=0.05)
        assert r.risk_budget == pytest.approx(5.0)          # never above 1%
        r2 = a.size_order_risk("EURUSD", entry=1.08, stop_loss=1.077, risk_pct=0.005)
        assert r2.risk_budget == pytest.approx(2.5)         # upstream caps can lower it

    def test_usdjpy_converts_jpy_loss_via_eurjpy(self):
        a = self._adapter(equity=500.0)
        # 30 pip stop on USDJPY = 0.30 JPY/unit; EURJPY 160 → 0.001875 EUR/unit
        r = a.size_order_risk("USDJPY", entry=150.0, stop_loss=149.7)
        assert r.risk_at_stop == pytest.approx(5.0, abs=0.01)

    def test_missing_conversion_pair_fails_closed(self):
        a = self._adapter(equity=500.0)
        a._broker_symbols.pop("EURJPY")
        with pytest.raises(BrokerRejectedError, match="conversion"):
            a.size_order_risk("USDJPY", entry=150.0, stop_loss=149.7)

    def test_invalid_risk_config_refuses_to_start(self):
        from core.broker import BrokerConfigError
        with pytest.raises(BrokerConfigError):
            self._adapter(risk_per_trade_pct=0.5)
        with pytest.raises(BrokerConfigError):
            self._adapter(sizing_mode="yolo")


# ── Ares wiring ───────────────────────────────────────────────────────────────

def _sized(ticker="EURUSD", *, side="", stop=None, rr=None, ref=None, pct=0.02,
           category=SignalCategory.MACRO_SHIFT) -> SizedSignal:
    now = datetime.now(timezone.utc)
    raw = RawSignal(signal_id="rs-1", source_url="", headline="t", summary="", published_at=now,
                    category=category, severity=Severity.MEDIUM, affected_tickers=[ticker],
                    trade_side=side, stop_distance=stop, reward_risk=rr, reference_price=ref)
    return SizedSignal(original=FilteredSignal(original=raw, compliance_score=1.0),
                       macro=MacroContext(fetched_at=now, regime=MarketRegime.BULL, vix=15.0,
                                          sp500_1m_return=0.0),
                       confidence=0.6, position_size_pct=pct)


class TestAresRiskPath:
    def _ares(self, **kw):
        from agents.ares import AresAgent
        b = MockBrokerAdapter(prices={"EURUSD": 1.08}, decimals=5, sizing_mode="risk",
                              account_equity=500.0, **kw)
        return AresAgent(paper=True, broker=b, default_account_equity=500.0), b

    def test_uses_signal_atr_stop_and_explicit_side(self):
        ares, b = self._ares()
        res = ares.place(_sized(side="SELL", stop=0.0025, rr=2.0, ref=1.08))
        assert res.side == "SELL"                                   # trade_side, not category
        assert res.stop_loss_price == pytest.approx(1.0825)          # 25 pips, not 3%
        assert res.take_profit_price == pytest.approx(1.0750)        # 2R
        assert b.risk_calls[0]["risk_pct"] == pytest.approx(0.01)    # min(1%, Pythia 2%)
        assert res.qty == pytest.approx(500 * 0.01 / 0.0025)         # 2000 units

    def test_upstream_size_is_a_ceiling_on_risk(self):
        ares, b = self._ares()
        ares.place(_sized(side="BUY", stop=0.0025, pct=0.004))
        assert b.risk_calls[0]["risk_pct"] == pytest.approx(0.004)

    def test_zero_size_is_not_placed(self):
        ares, b = self._ares()
        res = ares.place(_sized(side="BUY", stop=0.0025, pct=0.0))
        assert res.status.startswith("error:") and not b.orders

    def test_queue_payload_keeps_stop(self):
        from agents.ares import AresAgent
        b = MockBrokerAdapter(prices={"EURUSD": 1.08}, reachable=False, sizing_mode="risk")
        ares = AresAgent(paper=True, broker=b, default_account_equity=500.0)
        with patch("core.supabase_client.enqueue_pending_order") as enq:
            res = ares.place(_sized(side="SELL", stop=0.0025, rr=2.0))
        assert res.status == "queued" and res.side == "SELL"
        payload = enq.call_args.kwargs["payload"]
        assert payload["stop_distance"] == 0.0025 and payload["side"] == "SELL"


class TestIbkrPathUnchanged:
    def test_ibkr_keeps_notional_shares_and_pct_stops(self):
        """Even if a signal carried a stop, IBKR keeps integer-share notional sizing
        (equity × position_size_pct / price) and the configured % stops."""
        from agents.ares import AresAgent
        from core.broker import BracketResult
        ares = AresAgent(paper=True, default_account_equity=4000.0)
        br = ares.broker
        assert br.name == "ibkr" and getattr(br, "sizing_mode", "notional") == "notional"
        br.connect = MagicMock(return_value=MagicMock())
        br.get_quote = MagicMock(return_value=10.0)
        br.place_bracket = MagicMock(side_effect=lambda sym, side, qty, entry, sl, tp, exchange=None:
                                     BracketResult("1", sym, side, qty, entry, sl, tp))
        with patch("agents.ares._resolve_symbol_for_market", return_value=("NVDA", "SMART")), \
             patch("config.settings.load_settings", return_value={"use_atr_stops": False}):
            res = ares.place(_sized("NVDA", side="SELL", stop=0.5, pct=0.02,
                                    category=SignalCategory.POSITIVE_NEWS))
        assert res.qty == 8                       # int(4000 × 2% / 10)
        assert res.side == "BUY"                  # long-only venue ignores trade_side
        assert res.stop_loss_price == 9.7 and res.take_profit_price == 10.6   # 3% / 6%
