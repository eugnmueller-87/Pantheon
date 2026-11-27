"""
Broker abstraction — Ares/Argus through the BrokerAdapter seam, plus Zeus
broker selection (ibkr / ctrader / mock_execution).
"""

from __future__ import annotations

import os
from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from agents.ares import AresAgent
from agents.ares_mock import AresMockAgent
from agents.argus import ArgusAgent
from agents.zeus import ZeusConfig, ZeusOrchestrator
from core.broker import BrokerAdapter, BrokerConfigError, BrokerPosition
from core.brokers.ibkr import IbkrAdapter
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

_CT_ENV = {
    "CTRADER_CLIENT_ID":     "cid-test",
    "CTRADER_CLIENT_SECRET": "secret-test",
    "CTRADER_ACCESS_TOKEN":  "access-test",
    "CTRADER_REFRESH_TOKEN": "refresh-test",
    "CTRADER_ACCOUNT_ID":    "123456",
}


def _sized(ticker="EURUSD", category=SignalCategory.POSITIVE_NEWS, pct=0.02) -> SizedSignal:
    now = datetime.now(timezone.utc)
    raw = RawSignal(signal_id="b-1", source_url="", headline="t", summary="", published_at=now,
                    category=category, severity=Severity.HIGH, affected_tickers=[ticker])
    return SizedSignal(original=FilteredSignal(original=raw, compliance_score=1.0),
                       macro=MacroContext(fetched_at=now, regime=MarketRegime.BULL, vix=15.0, sp500_1m_return=0.0),
                       confidence=0.7, position_size_pct=pct)


# ── protocol conformance ─────────────────────────────────────────────────────

class TestProtocol:
    def test_adapters_satisfy_protocol(self):
        assert isinstance(IbkrAdapter("h", 4002, 1), BrokerAdapter)
        assert isinstance(MockBrokerAdapter(), BrokerAdapter)
        pytest.importorskip("ctrader_open_api")
        from core.brokers.ctrader import CTraderAdapter, CTraderCredentials
        ct = CTraderAdapter(CTraderCredentials.from_env(_CT_ENV), account_equity=4000, transport=MagicMock())
        assert isinstance(ct, BrokerAdapter)

    def test_default_agents_use_ibkr_with_stable_client_ids(self):
        ares, argus = AresAgent(paper=True), ArgusAgent()
        assert isinstance(ares.broker, IbkrAdapter) and ares.broker.client_id == 1
        assert isinstance(argus.broker, IbkrAdapter) and argus.broker.client_id == 2
        assert ares.broker.port == 4002 and argus.broker.port == 4002
        assert AresAgent(paper=False).broker.port == 4001


# ── Ares / Argus via the mock adapter ────────────────────────────────────────

class TestAresViaMockBroker:
    def test_sizes_from_configured_equity_not_broker(self):
        b = MockBrokerAdapter(prices={"EURUSD": 1.0})
        ares = AresAgent(paper=True, broker=b, default_account_equity=4000.0)
        res = ares.place(_sized(pct=0.02))
        assert res.qty == pytest.approx(80.0)          # 2% of €4000, unlevered
        assert b.orders[0].entry_price == 1.0

    def test_two_way_venue_follows_category(self):
        b = MockBrokerAdapter(prices={"EURUSD": 1.0})
        ares = AresAgent(paper=True, broker=b, default_account_equity=4000.0)
        assert ares.place(_sized(category=SignalCategory.SUPPLIER_DISRUPTION)).side == "SELL"

    def test_long_only_venue_stays_long(self):
        b = MockBrokerAdapter(prices={"EURUSD": 1.0}, supports_short=False)
        ares = AresAgent(paper=True, broker=b, default_account_equity=4000.0)
        assert ares.place(_sized(category=SignalCategory.SUPPLIER_DISRUPTION)).side == "BUY"

    def test_unreachable_broker_queues(self):
        b = MockBrokerAdapter(prices={"EURUSD": 1.0}, reachable=False)
        ares = AresAgent(paper=True, broker=b, default_account_equity=4000.0)
        with patch("core.supabase_client.enqueue_pending_order"):
            assert ares.place(_sized()).status == "queued"

    def test_cancel_all_pending(self):
        b = MockBrokerAdapter(prices={"EURUSD": 1.0})
        ares = AresAgent(paper=True, broker=b, default_account_equity=4000.0)
        ares.place(_sized())
        ares.cancel_all_pending()
        assert b.working == []

    def test_stops_rounded_to_broker_decimals(self):
        b = MockBrokerAdapter(prices={"EURUSD": 1.08123}, decimals=5)
        ares = AresAgent(paper=True, broker=b, default_account_equity=4000.0)
        res = ares.place(_sized())
        assert res.stop_loss_price == round(1.08123 * 0.97, 5)


class TestArgusViaMockBroker:
    def test_refresh_builds_snapshots_from_broker(self):
        b = MockBrokerAdapter()
        b.add_position(BrokerPosition("EURUSD", "SHORT", 80.0, 1.10, 1.08, 1.6))
        argus = ArgusAgent(default_account_equity=4000.0, broker=b)
        state = argus.refresh()
        snap = state.snapshots[0]
        assert snap.side == "SHORT" and snap.stop_loss_price > snap.avg_cost
        assert state.total_equity == 4000.0

    def test_closing_fills_delegated(self):
        b = MockBrokerAdapter()
        b.add_fill("EURUSD", 1.08, 80.0, "2026-11-20T12:00:00+00:00")
        argus = ArgusAgent(broker=b)
        assert argus._closing_fills_by_symbol()["EURUSD"][0]["price"] == 1.08

    def test_broker_failure_falls_back(self):
        b = MockBrokerAdapter(reachable=False)
        argus = ArgusAgent(default_account_equity=4000.0, broker=b)
        state = argus.refresh()                      # must not raise
        assert state.total_equity == 4000.0


# ── IBKR adapter keeps the old IB behaviour ──────────────────────────────────

class TestIbkrAdapterBehaviour:
    def _ib(self):
        ib = MagicMock()
        ticker = MagicMock()
        ticker.midpoint.return_value = 100.0
        ib.reqMktData.return_value = ticker
        order = MagicMock(orderId=42)
        ib.bracketOrder.return_value = [order, MagicMock(), MagicMock()]
        return ib

    def test_place_bracket_and_quote(self):
        import sys
        import types as _t
        fake = _t.ModuleType("ib_async")
        fake.Stock = MagicMock()
        with patch.dict(sys.modules, {"ib_async": fake}):
            ad = IbkrAdapter("h", 4002, 1)
            ad._ib = self._ib()
            assert ad.get_quote("AAPL", exchange="SMART") == 100.0
            assert ad.size_order("AAPL", 80.0, 100.0) == 1        # integer shares, min 1
            res = ad.place_bracket("AAPL", "BUY", 5, 100.0, 97.0, 106.0, exchange="SMART")
            ad._ib.bracketOrder.assert_called_once_with("BUY", 5, 100.0, 106.0, 97.0)
            assert ad._ib.placeOrder.call_count == 3 and res.order_id == "42"
            fake.Stock.assert_called_with("AAPL", "SMART", "USD")

    def test_cancel_and_positions(self):
        ad = IbkrAdapter("h", 4002, 2)
        ib = MagicMock()
        ib.openTrades.return_value = [MagicMock(), MagicMock()]
        pos = MagicMock(position=3, averageCost=10.0, marketPrice=11.0, unrealizedPNL=3.0)
        pos.contract.symbol = "SAP"
        flat = MagicMock(position=0)
        ib.portfolio.return_value = [pos, flat]
        ad._ib = ib
        assert ad.cancel_all_pending() == 2
        [p] = ad.positions()
        assert (p.symbol, p.side, p.qty) == ("SAP", "LONG", 3)

    def test_closing_fills_only_sells(self):
        ad = IbkrAdapter("h", 4002, 2)
        ib = MagicMock()
        sell = MagicMock()
        sell.execution.side, sell.execution.avgPrice, sell.execution.shares, sell.execution.time = "SLD", 9.0, 3, "t2"
        sell.contract.symbol = "SAP"
        buy = MagicMock()
        buy.execution.side = "BOT"
        ib.fills.return_value = [sell, buy]
        ad._ib = ib
        assert ad.closing_fills() == {"SAP": [{"price": 9.0, "qty": 3, "time": "t2"}]}


# ── Zeus broker selection ────────────────────────────────────────────────────

def _zeus(env: dict, mock_execution: bool) -> ZeusOrchestrator:
    cfg = ZeusConfig(paper_trading=True, mock_execution=mock_execution, default_account_equity=4000.0)
    clean = {k: "" for k in list(_CT_ENV) + ["CTRADER_ENV", "BROKER", "IB_PORT"]}
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key-not-real", **clean, **env}, clear=False):
        for k, v in list(os.environ.items()):
            if k in clean and not env.get(k):
                os.environ.pop(k, None)
        with ExitStack() as stack:
            stack.enter_context(patch("anthropic.Anthropic"))
            stack.enter_context(patch("core.redis_bridge.RedisBridge"))
            stack.enter_context(patch("core.knowledge_base.KnowledgeBase"))
            stack.enter_context(patch.object(ZeusOrchestrator, "_warm_broker"))
            return ZeusOrchestrator(config=cfg)


class TestZeusBrokerSelection:
    def test_mock_execution_needs_no_ctrader_credentials(self):
        zeus = _zeus({"BROKER": "ctrader"}, mock_execution=True)
        assert isinstance(zeus.ares, AresMockAgent)
        assert zeus.broker is None and zeus.argus._mock is True

    def test_ibkr_default_wiring_unchanged(self):
        zeus = _zeus({}, mock_execution=False)
        assert zeus.config.broker == "ibkr" and zeus.broker is None
        assert isinstance(zeus.ares, AresAgent)
        assert zeus.ares.broker.client_id == 1 and zeus.argus.broker.client_id == 2
        assert zeus.ares.port == 4002 and zeus.argus._ib_port == 4002
        assert zeus.ares.default_account_equity == 4000.0   # risk budget wired through

    def test_ctrader_missing_env_fails_closed(self):
        with pytest.raises(BrokerConfigError, match="CTRADER_CLIENT_ID"):
            _zeus({"BROKER": "ctrader"}, mock_execution=False)

    def test_ctrader_shared_session_and_demo_forced_in_paper(self):
        pytest.importorskip("ctrader_open_api")
        zeus = _zeus({"BROKER": "ctrader", "CTRADER_ENV": "live", **_CT_ENV}, mock_execution=False)
        assert zeus.broker is not None and zeus.broker.name == "ctrader"
        assert zeus.ares.broker is zeus.broker and zeus.argus.broker is zeus.broker
        assert zeus.broker.creds.env == "demo"          # paper ⇒ never the live endpoint
        assert zeus.broker.sizing_leverage == 1.0       # unlevered by default
        assert zeus.broker.account_equity == 4000.0

    def test_invalid_broker_name_rejected(self):
        with pytest.raises(ValueError, match="BROKER"):
            _zeus({"BROKER": "alpaca"}, mock_execution=False)


class TestEnvBlocklist:
    """conftest's autouse block must keep a developer's .env CTRADER_* values
    (e.g. CTRADER_ACCOUNT_EQUITY=500 for a small demo) out of every test."""

    def test_every_ctrader_var_is_blocked_and_restored(self, monkeypatch):
        from tests.conftest import _blocked_env

        # As if load_dotenv() had injected them before the test started.
        monkeypatch.setenv("CTRADER_ACCOUNT_EQUITY", "500")
        monkeypatch.setenv("CTRADER_ENV", "live")
        monkeypatch.setenv("CTRADER_SOME_FUTURE_SWITCH", "1")
        with _blocked_env():
            assert not [k for k in os.environ if k.startswith("CTRADER_")]
            os.environ["CTRADER_RISK_RAMP"] = "true"          # injected mid-test
        assert os.environ["CTRADER_ACCOUNT_EQUITY"] == "500"  # pre-test state restored
        assert os.environ["CTRADER_ENV"] == "live"
        assert "CTRADER_RISK_RAMP" not in os.environ          # mid-test injection removed

    def test_unrelated_vars_untouched(self, monkeypatch):
        from tests.conftest import _blocked_env

        monkeypatch.setenv("BROKER", "ctrader")
        monkeypatch.setenv("XCTRADER_ACCOUNT_EQUITY", "1")   # prefix match only
        with _blocked_env():
            assert os.environ["BROKER"] == "ctrader"
            assert os.environ["XCTRADER_ACCOUNT_EQUITY"] == "1"
