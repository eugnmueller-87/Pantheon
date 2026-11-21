"""
cTrader adapter — unit tests with a fake transport (no network, no reactor).

Covers: env validation, auth sequence, token refresh, heartbeat scheduling,
reconnect + re-auth + spot resubscribe, symbol mapping, volume conversion,
unlevered sizing vs ESMA caps, place_bracket, cancel_all_pending, positions /
closing fills, and Ares/Argus driving the adapter.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

pytest.importorskip("ctrader_open_api")

from ctrader_open_api.messages import OpenApiMessages_pb2 as M  # noqa: E402
from ctrader_open_api.messages import OpenApiModelMessages_pb2 as MM  # noqa: E402

from core.broker import (  # noqa: E402
    BrokerConfigError,
    BrokerConnectivityError,
    BrokerRejectedError,
)
from core.brokers.ctrader import (  # noqa: E402
    REQUIRED_ENV,
    CTraderAdapter,
    CTraderCredentials,
    TwistedTransport,
)
from core.brokers.ctrader_instruments import (  # noqa: E402
    Instrument,
    SymbolSpec,
    compute_volume,
    effective_leverage,
    load_instruments,
    match_broker_symbols,
    relative_distance,
    resolve_logical,
    round_volume,
    units_to_volume,
    volume_to_units,
)

ACC = 4242001
_FAKE_ENV = {
    "CTRADER_CLIENT_ID":     "cid-test",
    "CTRADER_CLIENT_SECRET": "secret-test-value",
    "CTRADER_ACCESS_TOKEN":  "access-test-value",
    "CTRADER_REFRESH_TOKEN": "refresh-test-value",
    "CTRADER_ACCOUNT_ID":    str(ACC),
}

# symbolId → (broker name, price, digits, minVolume, stepVolume)
_SYMBOLS = {
    1:  ("EURUSD", 1.08000, 5, 100, 100),
    2:  ("GBPUSD", 1.27000, 5, 100, 100),
    3:  ("USDJPY", 150.000, 3, 100, 100),
    4:  ("EURJPY", 160.000, 3, 100, 100),
    10: ("US500",  5000.0,  2, 1, 1),
    11: ("NAS100", 18000.0, 2, 1, 1),   # USTEC resolves via alias
    12: ("GER40",  20000.0, 2, 1, 1),   # DE40 resolves via alias
}


def _price_int(px: float) -> int:
    return int(round(px * 100_000))


class FakeTransport:
    """Records every request; answers with real protobuf response objects."""

    def __init__(self, connected: bool = True):
        self._connected_flag = connected
        self.sent: list = []
        self.cb = None
        self.orders_on_account: list = []
        self.positions_on_account: list = []
        self.deals: list = []
        self.overrides: dict = {}     # payload name -> list of responses (consumed FIFO)
        self.push_spots = True
        self.heartbeat_interval = 9.0

    # transport API -----------------------------------------------------
    @property
    def is_connected(self):
        return self._connected_flag

    def wait_connected(self, timeout):
        return self._connected_flag

    def start(self, on_connected, on_disconnected, on_message):
        self.cb = (on_connected, on_disconnected, on_message)

    def stop(self):
        self._connected_flag = False

    def request(self, message, timeout):
        name = type(message).__name__
        self.sent.append(message)
        if self.overrides.get(name):
            return self.overrides[name].pop(0)
        return getattr(self, "_r_" + name)(message)

    # helpers -------------------------------------------------------------
    def names(self):
        return [type(m).__name__ for m in self.sent]

    def simulate_disconnect(self):
        self._connected_flag = False
        self.cb[1]("connection lost")

    def simulate_reconnect(self):
        self._connected_flag = True
        self.cb[0]()

    def push_spot(self, sid, px):
        ev = M.ProtoOASpotEvent(ctidTraderAccountId=ACC, symbolId=sid,
                                bid=_price_int(px) - 1, ask=_price_int(px) + 1)
        self.cb[2](ev)

    # responders ------------------------------------------------------------
    def _r_ProtoOAApplicationAuthReq(self, m):
        return M.ProtoOAApplicationAuthRes()

    def _r_ProtoOAGetAccountListByAccessTokenReq(self, m):
        return M.ProtoOAGetAccountListByAccessTokenRes(
            accessToken=m.accessToken, permissionScope=1,
            ctidTraderAccount=[MM.ProtoOACtidTraderAccount(ctidTraderAccountId=ACC, isLive=False)],
        )

    def _r_ProtoOAAccountAuthReq(self, m):
        return M.ProtoOAAccountAuthRes(ctidTraderAccountId=m.ctidTraderAccountId)

    def _r_ProtoOASymbolsListReq(self, m):
        return M.ProtoOASymbolsListRes(ctidTraderAccountId=ACC, symbol=[
            MM.ProtoOALightSymbol(symbolId=sid, symbolName=v[0], enabled=True) for sid, v in _SYMBOLS.items()
        ])

    def _r_ProtoOASymbolByIdReq(self, m):
        return M.ProtoOASymbolByIdRes(ctidTraderAccountId=ACC, symbol=[
            MM.ProtoOASymbol(symbolId=sid, digits=_SYMBOLS[sid][2], pipPosition=4,
                             minVolume=_SYMBOLS[sid][3], stepVolume=_SYMBOLS[sid][4],
                             maxVolume=10_000_000_000)
            for sid in m.symbolId
        ])

    def _r_ProtoOAReconcileReq(self, m):
        return M.ProtoOAReconcileRes(ctidTraderAccountId=ACC,
                                     position=self.positions_on_account, order=self.orders_on_account)

    def _r_ProtoOASubscribeSpotsReq(self, m):
        if self.push_spots:
            for sid in m.symbolId:
                self.push_spot(sid, _SYMBOLS[sid][1])
        return M.ProtoOASubscribeSpotsRes(ctidTraderAccountId=ACC)

    def _r_ProtoOANewOrderReq(self, m):
        order = MM.ProtoOAOrder(
            orderId=777, orderType=m.orderType, orderStatus=MM.ORDER_STATUS_ACCEPTED, positionId=888,
            tradeData=MM.ProtoOATradeData(symbolId=m.symbolId, volume=m.volume, tradeSide=m.tradeSide),
        )
        return M.ProtoOAExecutionEvent(ctidTraderAccountId=ACC, executionType=MM.ORDER_ACCEPTED, order=order)

    def _r_ProtoOACancelOrderReq(self, m):
        return M.ProtoOAExecutionEvent(ctidTraderAccountId=ACC, executionType=MM.ORDER_CANCELLED,
                                       order=MM.ProtoOAOrder(orderId=m.orderId))

    def _r_ProtoOARefreshTokenReq(self, m):
        return M.ProtoOARefreshTokenRes(accessToken="new-access-value", tokenType="bearer",
                                        expiresIn=2_628_000, refreshToken="new-refresh-value")

    def _r_ProtoOADealListReq(self, m):
        return M.ProtoOADealListRes(ctidTraderAccountId=ACC, deal=self.deals, hasMore=False)


def _adapter(transport=None, equity=4000.0, **kw) -> tuple[CTraderAdapter, FakeTransport]:
    t = transport or FakeTransport()
    creds = CTraderCredentials.from_env(_FAKE_ENV)
    kw.setdefault("quote_timeout", 0.2)
    return CTraderAdapter(creds, account_equity=equity, transport=t, **kw), t


# ── env / credentials ────────────────────────────────────────────────────────

class TestCredentials:
    def test_missing_all_env_vars_lists_every_name(self):
        with pytest.raises(BrokerConfigError) as ei:
            CTraderCredentials.from_env({})
        msg = str(ei.value)
        for k in REQUIRED_ENV:
            assert k in msg

    def test_missing_one_names_only_that_one_and_no_values(self):
        env = dict(_FAKE_ENV)
        env.pop("CTRADER_REFRESH_TOKEN")
        with pytest.raises(BrokerConfigError) as ei:
            CTraderCredentials.from_env(env)
        msg = str(ei.value)
        assert "CTRADER_REFRESH_TOKEN" in msg and "CTRADER_CLIENT_ID" not in msg
        assert "secret-test-value" not in msg and "access-test-value" not in msg

    def test_blank_values_count_as_missing(self):
        env = {**_FAKE_ENV, "CTRADER_ACCESS_TOKEN": "   "}
        with pytest.raises(BrokerConfigError, match="CTRADER_ACCESS_TOKEN"):
            CTraderCredentials.from_env(env)

    def test_default_env_is_demo(self):
        c = CTraderCredentials.from_env(_FAKE_ENV)
        assert c.env == "demo" and c.host == "demo.ctraderapi.com"

    def test_live_only_when_explicit(self):
        c = CTraderCredentials.from_env({**_FAKE_ENV, "CTRADER_ENV": "live"})
        assert c.host == "live.ctraderapi.com"

    def test_invalid_env_rejected(self):
        with pytest.raises(BrokerConfigError):
            CTraderCredentials.from_env({**_FAKE_ENV, "CTRADER_ENV": "prod"})

    def test_non_numeric_account_rejected(self):
        with pytest.raises(BrokerConfigError, match="numeric"):
            CTraderCredentials.from_env({**_FAKE_ENV, "CTRADER_ACCOUNT_ID": "abc"})

    def test_repr_hides_secrets(self):
        r = repr(CTraderCredentials.from_env(_FAKE_ENV))
        assert "secret-test-value" not in r and "access-test-value" not in r and "refresh-test-value" not in r


# ── auth sequence / persistent session ───────────────────────────────────────

class TestAuthSequence:
    def test_connect_runs_app_then_account_auth(self):
        a, t = _adapter()
        a.connect()
        names = t.names()
        assert names[:3] == [
            "ProtoOAApplicationAuthReq",
            "ProtoOAGetAccountListByAccessTokenReq",
            "ProtoOAAccountAuthReq",
        ]
        assert "ProtoOASymbolsListReq" in names and names[-1] == "ProtoOAReconcileReq"
        app, acct = t.sent[0], t.sent[2]
        assert app.clientId == "cid-test" and app.clientSecret == "secret-test-value"
        assert acct.ctidTraderAccountId == ACC and acct.accessToken == "access-test-value"
        assert a.health() and a.auth_count == 1

    def test_connect_is_persistent_not_per_order(self):
        a, t = _adapter()
        a.connect()
        n = len(t.sent)
        a.connect()
        a.connect()
        assert len(t.sent) == n   # no re-auth while the session is up

    def test_account_not_linked_to_token_fails_closed(self):
        a, t = _adapter()
        t.overrides["ProtoOAGetAccountListByAccessTokenReq"] = [
            M.ProtoOAGetAccountListByAccessTokenRes(accessToken="x", ctidTraderAccount=[
                MM.ProtoOACtidTraderAccount(ctidTraderAccountId=1, isLive=False)])
        ]
        with pytest.raises(BrokerConfigError, match="not linked"):
            a.connect()
        assert "ProtoOAAccountAuthReq" not in t.names()
        assert not a.health()

    def test_app_auth_failure_is_config_error(self):
        a, t = _adapter()
        t.overrides["ProtoOAApplicationAuthReq"] = [
            M.ProtoOAErrorRes(errorCode="CH_CLIENT_AUTH_FAILURE", description="bad app")]
        with pytest.raises(BrokerConfigError, match="CH_CLIENT_AUTH_FAILURE"):
            a.connect()

    def test_already_logged_in_is_tolerated(self):
        a, t = _adapter()
        t.overrides["ProtoOAAccountAuthReq"] = [M.ProtoOAErrorRes(errorCode="ALREADY_LOGGED_IN")]
        a.connect()
        assert a.health()

    def test_transport_never_connects_raises_connectivity(self):
        a, _ = _adapter(FakeTransport(connected=False))
        with pytest.raises(BrokerConnectivityError):
            a.connect()

    def test_expired_token_refreshes_and_retries_without_logging_tokens(self, caplog):
        a, t = _adapter()
        t.overrides["ProtoOAAccountAuthReq"] = [
            M.ProtoOAErrorRes(errorCode="CH_ACCESS_TOKEN_INVALID", description="expired"),
        ]
        with caplog.at_level(logging.DEBUG):
            a.connect()
        acct_reqs = [m for m in t.sent if type(m).__name__ == "ProtoOAAccountAuthReq"]
        assert len(acct_reqs) == 2
        assert acct_reqs[1].accessToken == "new-access-value"
        assert a.creds.refresh_token == "new-refresh-value"
        assert "ProtoOARefreshTokenReq" in t.names()
        for secret in ("new-access-value", "new-refresh-value", "access-test-value", "secret-test-value"):
            assert secret not in caplog.text
        assert "CTRADER_ACCESS_TOKEN" in caplog.text   # operator told to update env

    def test_proactive_refresh_before_expiry(self):
        env = {**_FAKE_ENV, "CTRADER_TOKEN_EXPIRES_AT": str(int(datetime.now(timezone.utc).timestamp()) + 3600)}
        t = FakeTransport()
        a = CTraderAdapter(CTraderCredentials.from_env(env), account_equity=4000, transport=t)
        a.connect()
        assert "ProtoOARefreshTokenReq" in t.names()
        assert a.creds.token_expires_at > datetime.now(timezone.utc)


# ── heartbeat + reconnect ────────────────────────────────────────────────────

class TestHeartbeatAndReconnect:
    def test_heartbeat_loop_fires_at_least_every_10s(self):
        pytest.importorskip("twisted")
        from twisted.internet import task

        from core.brokers.ctrader import HeartbeatLoop

        clock = task.Clock()
        sent = []
        hb = HeartbeatLoop(lambda: sent.append(clock.seconds()), interval=9.0, clock=clock)
        hb.start()
        clock.advance(8.9)
        assert sent == []
        clock.advance(0.2)
        assert len(sent) == 1
        for _ in range(10):
            clock.advance(9.0)
        assert len(sent) == 11
        gaps = [b - a for a, b in zip(sent, sent[1:])]
        assert max(gaps) < 10.0
        hb.stop()
        assert not hb.running

    def test_heartbeat_failure_does_not_stop_loop(self):
        pytest.importorskip("twisted")
        from twisted.internet import task

        from core.brokers.ctrader import HeartbeatLoop

        clock = task.Clock()
        calls = []

        def boom():
            calls.append(1)
            raise RuntimeError("socket gone")

        hb = HeartbeatLoop(boom, interval=9.0, clock=clock)
        hb.start()
        clock.advance(9.0)
        clock.advance(9.0)
        assert len(calls) == 2 and hb.running

    def test_default_transport_heartbeat_under_10s(self):
        assert TwistedTransport("demo.ctraderapi.com").heartbeat_interval < 10
        a = CTraderAdapter(CTraderCredentials.from_env(_FAKE_ENV), account_equity=4000)
        assert a.transport.heartbeat_interval < 10

    def test_request_before_start_is_connectivity_error(self):
        with pytest.raises(BrokerConnectivityError):
            TwistedTransport("demo.ctraderapi.com").request(M.ProtoOAVersionReq(), 1)

    def test_reconnect_reauthorizes_reconciles_and_resubscribes(self):
        a, t = _adapter()
        a.connect()
        a.get_quote("EURUSD")          # subscribes symbolId 1
        t.sent.clear()

        t.simulate_disconnect()
        assert not a.health()

        t.simulate_reconnect()
        names = t.names()
        assert names[0] == "ProtoOAApplicationAuthReq"
        assert "ProtoOAAccountAuthReq" in names
        assert "ProtoOAReconcileReq" in names
        resub = [m for m in t.sent if type(m).__name__ == "ProtoOASubscribeSpotsReq"]
        assert resub and list(resub[0].symbolId) == [1]
        assert "ProtoOASymbolsListReq" not in names   # symbol map cached across reconnects
        assert a.health() and a.auth_count == 2

    def test_account_disconnect_event_triggers_reauth(self):
        a, t = _adapter()
        a.connect()
        t.sent.clear()
        t.cb[2](M.ProtoOAAccountDisconnectEvent(ctidTraderAccountId=ACC))
        assert "ProtoOAAccountAuthReq" in t.names() and a.health()

    def test_connectivity_error_code_maps_to_connectivity(self):
        a, t = _adapter()
        a.connect()
        t.overrides["ProtoOAReconcileReq"] = [M.ProtoOAErrorRes(errorCode="ACCOUNT_NOT_AUTHORIZED")]
        with pytest.raises(BrokerConnectivityError):
            a.reconcile()
        assert not a.health()   # next call re-authorizes


# ── symbol mapping ───────────────────────────────────────────────────────────

class TestSymbolMapping:
    def test_aliases_resolve_to_broker_names(self):
        inst = load_instruments()
        broker = {v[0]: sid for sid, v in _SYMBOLS.items()}
        m = match_broker_symbols(inst, broker)
        assert m["USTEC"] == ("NAS100", 11)
        assert m["DE40"] == ("GER40", 12)
        assert m["EURUSD"] == ("EURUSD", 1)

    def test_unlisted_instrument_is_absent(self):
        inst = load_instruments()
        m = match_broker_symbols(inst, {"EURUSD": 1})
        assert set(m) == {"EURUSD"}

    def test_pipeline_ticker_aliases(self):
        inst = load_instruments()
        assert resolve_logical("EURUSD=X", inst) == "EURUSD"
        assert resolve_logical("^GDAXI", inst) == "DE40"
        assert resolve_logical("nas100", inst) == "USTEC"
        assert resolve_logical("NVDA", inst) is None       # no stocks on this venue
        assert resolve_logical("", inst) is None

    def test_adapter_symbol_map_after_auth(self):
        a, _ = _adapter()
        a.connect()
        assert a.symbol_map["DE40"] == ("GER40", 12)
        assert a.resolve_symbol("GER40") == "DE40"

    def test_unknown_asset_class_rejected(self):
        with pytest.raises(BrokerConfigError):
            load_instruments({"XAUUSD": {"asset_class": "metals"}})

    def test_settings_cap_can_only_tighten(self):
        inst = load_instruments(None, {"fx": 50.0, "index": 5.0})
        assert inst["EURUSD"].leverage_cap == 30.0   # cannot exceed ESMA
        assert inst["US500"].leverage_cap == 5.0     # can tighten


# ── volume conversion ────────────────────────────────────────────────────────

def _spec(min_v=100, step=100, max_v=None, digits=5):
    return SymbolSpec(symbol_id=1, broker_name="X", digits=digits, min_volume=min_v,
                      step_volume=step, max_volume=max_v)


class TestVolumeConversion:
    def test_protocol_scale(self):
        assert units_to_volume(10.0) == 1000          # 1000 in protocol == 10.00 units
        assert volume_to_units(1000) == 10.0
        assert units_to_volume(0.009) == 0             # floors, never rounds up

    def test_round_to_step_floor(self):
        s = _spec(min_v=1000, step=500)
        assert round_volume(1000, s) == 1000
        assert round_volume(1499, s) == 1000
        assert round_volume(1500, s) == 1500
        assert round_volume(999, s) == 0

    def test_step_anchored_at_min(self):
        s = _spec(min_v=150, step=100)
        assert round_volume(320, s) == 250

    def test_max_volume_clamp(self):
        s = _spec(min_v=100, step=100, max_v=1050)
        assert round_volume(99_999, s) == 1000

    def test_relative_distance_snaps_to_tick(self):
        assert relative_distance(1.08, 1.0476, 5) == 3240
        assert relative_distance(150.0, 145.5, 3) == 450_000
        assert relative_distance(5000.0, 4850.0, 2) % 1000 == 0
        assert relative_distance(1.0, 1.0, 5) >= 1     # never zero


# ── unlevered sizing vs ESMA caps ────────────────────────────────────────────

_FX  = Instrument("EURUSD", "fx", "USD", ["EURUSD"])
_IDX = Instrument("DE40", "index", "EUR", ["DE40"])


class TestUnleveredSizing:
    def test_two_percent_of_4000_is_80_eur_not_2400(self):
        equity, pct = 4000.0, 0.02
        notional = equity * pct * effective_leverage(1.0, _FX)
        vol = compute_volume(notional_acct=notional, price=1.08, quote_to_account=1 / 1.08,
                             spec=_spec(min_v=100, step=100), inst=_FX, equity=equity)
        units = volume_to_units(vol)
        deployed_eur = units * 1.08 * (1 / 1.08)
        assert deployed_eur == pytest.approx(80.0, abs=1.0)
        assert deployed_eur < 80.0 * 30 / 10     # nowhere near 1:30 notional

    def test_sizing_leverage_clamped_to_esma_cap(self):
        assert effective_leverage(1.0, _FX) == 1.0
        assert effective_leverage(0.3, _FX) == 1.0     # never below unlevered
        assert effective_leverage(100.0, _FX) == 30.0
        assert effective_leverage(100.0, _IDX) == 20.0

    def test_min_lot_bump_within_tolerance(self):
        # intent €80, min lot = 1 unit of DE40 @ 100 → €100 (1.25×) → allowed
        vol = compute_volume(notional_acct=80.0, price=100.0, quote_to_account=1.0,
                             spec=_spec(min_v=100, step=100), inst=_IDX, equity=4000)
        assert vol == 100

    def test_min_lot_far_above_intent_is_rejected_not_oversized(self):
        # IC Markets-like FX min 0.01 lot = 1000 units ≈ €1000 vs €80 intent → skip
        with pytest.raises(BrokerRejectedError, match="below the venue minimum"):
            compute_volume(notional_acct=80.0, price=1.08, quote_to_account=1 / 1.08,
                           spec=_spec(min_v=100_000, step=100_000), inst=_FX, equity=4000)

    def test_legal_cap_never_exceeded(self):
        with pytest.raises(BrokerRejectedError, match="ESMA"):
            compute_volume(notional_acct=4000 * 40, price=1.0, quote_to_account=1.0,
                           spec=_spec(), inst=_FX, equity=4000)

    def test_adapter_size_order_eurusd_unlevered(self):
        a, _ = _adapter()
        a.connect()
        units = a.size_order("EURUSD", 80.0, 1.08)
        assert units == pytest.approx(80.0, abs=1.0)    # 1 EUR per unit

    def test_adapter_size_order_usdjpy_converts_via_eurjpy(self):
        a, _ = _adapter()
        a.connect()
        units = a.size_order("USDJPY", 80.0, 150.0)     # 1 USD = 150/160 EUR
        assert units * 150.0 / 160.0 == pytest.approx(80.0, abs=1.0)

    def test_adapter_ignores_high_sizing_leverage_above_cap(self):
        a, _ = _adapter(sizing_leverage=1000.0)
        a.connect()
        units = a.size_order("EURUSD", 80.0, 1.08)
        assert units <= 80.0 * 30 + 1

    def test_missing_conversion_pair_fails_closed(self):
        a, t = _adapter()
        a.connect()
        a._broker_symbols.pop("EURJPY")
        with pytest.raises(BrokerRejectedError, match="conversion"):
            a.size_order("USDJPY", 80.0, 150.0)


# ── quotes / orders ──────────────────────────────────────────────────────────

class TestOrders:
    def test_get_quote_from_spot_subscription(self):
        a, t = _adapter()
        a.connect()
        assert a.get_quote("EURUSD") == pytest.approx(1.08, abs=1e-5)
        assert a.get_quote("DE40") == pytest.approx(20000.0)
        subs = [m for m in t.sent if type(m).__name__ == "ProtoOASubscribeSpotsReq"]
        a.get_quote("EURUSD")
        assert len([m for m in t.sent if type(m).__name__ == "ProtoOASubscribeSpotsReq"]) == len(subs)

    def test_no_spot_raises_broker_error(self):
        a, t = _adapter()
        t.push_spots = False
        a.connect()
        with pytest.raises(Exception, match="Could not price"):
            a.get_quote("EURUSD")

    def test_market_bracket_buy_uses_relative_sl_tp(self):
        a, t = _adapter()
        a.connect()
        res = a.place_bracket("EURUSD", "BUY", 80.0, 1.08, 1.0476, 1.1448)
        req = [m for m in t.sent if type(m).__name__ == "ProtoOANewOrderReq"][-1]
        assert req.orderType == MM.MARKET and req.tradeSide == MM.BUY
        assert req.volume == 8000 and req.symbolId == 1 and req.ctidTraderAccountId == ACC
        assert req.relativeStopLoss == 3240 and req.relativeTakeProfit == 6480
        assert not req.HasField("stopLoss") and not req.HasField("takeProfit")
        assert res.order_id == "777" and res.side == "BUY" and res.qty == 80.0
        assert res.raw["position_id"] == "888"

    def test_market_bracket_sell_supported(self):
        a, t = _adapter()
        a.connect()
        res = a.place_bracket("DE40", "SELL", 0.01, 20000.0, 20600.0, 18800.0)
        req = [m for m in t.sent if type(m).__name__ == "ProtoOANewOrderReq"][-1]
        assert req.tradeSide == MM.SELL and req.symbolId == 12 and req.volume == 1
        assert res.side == "SELL" and res.symbol == "DE40"

    def test_limit_bracket_uses_absolute_sl_tp(self):
        a, t = _adapter()
        a.connect()
        a.place_bracket("EURUSD", "BUY", 1.0, 1.0, 0.97, 1.06, order_type="LIMIT")
        req = [m for m in t.sent if type(m).__name__ == "ProtoOANewOrderReq"][-1]
        assert req.orderType == MM.LIMIT and req.limitPrice == pytest.approx(1.0)
        assert req.stopLoss == pytest.approx(0.97) and req.takeProfit == pytest.approx(1.06)
        assert req.timeInForce == MM.GOOD_TILL_CANCEL

    def test_bad_bracket_geometry_rejected_before_sending(self):
        a, t = _adapter()
        a.connect()
        n = len(t.sent)
        with pytest.raises(BrokerRejectedError):
            a.place_bracket("EURUSD", "BUY", 10.0, 1.08, 1.10, 1.12)
        with pytest.raises(BrokerRejectedError):
            a.place_bracket("EURUSD", "SELL", 10.0, 1.08, 1.05, 1.02)
        assert len(t.sent) == n

    def test_below_min_volume_rejected(self):
        a, _ = _adapter()
        a.connect()
        with pytest.raises(BrokerRejectedError, match="minimum"):
            a.place_bracket("EURUSD", "BUY", 0.5, 1.08, 1.05, 1.14)

    def test_order_error_event_is_rejection(self):
        a, t = _adapter()
        a.connect()
        t.overrides["ProtoOANewOrderReq"] = [M.ProtoOAOrderErrorEvent(
            ctidTraderAccountId=ACC, errorCode="NOT_ENOUGH_MONEY", description="no")]
        with pytest.raises(BrokerRejectedError, match="NOT_ENOUGH_MONEY"):
            a.place_bracket("EURUSD", "BUY", 10.0, 1.08, 1.05, 1.14)

    def test_unknown_symbol_rejected(self):
        a, _ = _adapter()
        a.connect()
        with pytest.raises(BrokerRejectedError):
            a.get_quote("NVDA")

    def test_cancel_all_pending_cancels_working_orders_only(self):
        a, t = _adapter()
        td = MM.ProtoOATradeData(symbolId=1, volume=100, tradeSide=MM.BUY)
        t.orders_on_account = [
            MM.ProtoOAOrder(orderId=1, tradeData=td, orderType=MM.LIMIT, orderStatus=MM.ORDER_STATUS_ACCEPTED),
            MM.ProtoOAOrder(orderId=2, tradeData=td, orderType=MM.STOP_LOSS_TAKE_PROFIT,
                            orderStatus=MM.ORDER_STATUS_ACCEPTED),
            MM.ProtoOAOrder(orderId=3, tradeData=td, orderType=MM.STOP, orderStatus=MM.ORDER_STATUS_ACCEPTED),
        ]
        a.connect()
        assert a.cancel_all_pending() == 2
        cancelled = [m.orderId for m in t.sent if type(m).__name__ == "ProtoOACancelOrderReq"]
        assert cancelled == [1, 3]     # protective SL/TP (2) left on the position


# ── portfolio ────────────────────────────────────────────────────────────────

class TestPortfolio:
    def test_positions_map_to_snapshots(self):
        a, t = _adapter()
        t.positions_on_account = [
            MM.ProtoOAPosition(positionId=9, positionStatus=1, swap=0, price=1.07,
                               tradeData=MM.ProtoOATradeData(symbolId=1, volume=8000, tradeSide=MM.BUY)),
            MM.ProtoOAPosition(positionId=10, positionStatus=1, swap=0, price=20100.0,
                               tradeData=MM.ProtoOATradeData(symbolId=12, volume=1, tradeSide=MM.SELL)),
        ]
        a.connect()
        pos = {p.symbol: p for p in a.positions()}
        assert pos["EURUSD"].side == "LONG" and pos["EURUSD"].qty == 80.0
        assert pos["EURUSD"].unrealized_pnl > 0           # 1.07 → 1.08
        assert pos["DE40"].side == "SHORT" and pos["DE40"].unrealized_pnl > 0   # 20100 → 20000

    def test_closing_fills_from_deals(self):
        a, t = _adapter()
        ts = int(datetime(2026, 11, 20, 12, tzinfo=timezone.utc).timestamp() * 1000)
        cpd = MM.ProtoOAClosePositionDetail(entryPrice=1.07, grossProfit=80, swap=0, commission=0, balance=1)
        t.deals = [
            MM.ProtoOADeal(dealId=1, orderId=1, positionId=9, volume=8000, filledVolume=8000, symbolId=1,
                           createTimestamp=ts, executionTimestamp=ts, tradeSide=MM.SELL,
                           dealStatus=MM.FILLED, executionPrice=1.08, closePositionDetail=cpd),
            MM.ProtoOADeal(dealId=2, orderId=2, positionId=11, volume=100, filledVolume=100, symbolId=1,
                           createTimestamp=ts, executionTimestamp=ts, tradeSide=MM.BUY,
                           dealStatus=MM.FILLED, executionPrice=1.09),   # opening deal — ignored
        ]
        a.connect()
        fills = a.closing_fills()
        assert list(fills) == ["EURUSD"]
        assert fills["EURUSD"][0]["price"] == pytest.approx(1.08)
        assert fills["EURUSD"][0]["qty"] == 80.0


# ── Ares / Argus through the cTrader adapter ─────────────────────────────────

def _sized(ticker="EURUSD", category=None, pct=0.02):
    from core.types import (
        FilteredSignal,
        MacroContext,
        MarketRegime,
        RawSignal,
        Severity,
        SignalCategory,
        SizedSignal,
    )
    now = datetime.now(timezone.utc)
    raw = RawSignal(signal_id="ct-1", source_url="", headline="t", summary="", published_at=now,
                    category=category or SignalCategory.POSITIVE_NEWS, severity=Severity.HIGH,
                    affected_tickers=[ticker])
    return SizedSignal(original=FilteredSignal(original=raw, compliance_score=1.0),
                       macro=MacroContext(fetched_at=now, regime=MarketRegime.BULL, vix=15.0, sp500_1m_return=0.0),
                       confidence=0.7, position_size_pct=pct)


class TestAresArgusOnCTrader:
    def test_ares_places_unlevered_buy(self):
        from agents.ares import AresAgent
        a, t = _adapter()
        ares = AresAgent(paper=True, broker=a, default_account_equity=4000.0)
        res = ares.place(_sized("EURUSD=X"))
        assert res.status == "submitted" and res.side == "BUY" and res.symbol == "EURUSD"
        assert res.qty * res.fill_price * (1 / 1.08) == pytest.approx(80.0, abs=1.0)
        assert res.stop_loss_price < res.fill_price < res.take_profit_price
        assert res.stop_loss_price == round(res.stop_loss_price, 5)

    def test_ares_disruption_goes_short_on_two_way_venue(self):
        from agents.ares import AresAgent
        from core.types import SignalCategory
        a, t = _adapter(max_min_lot_overshoot=100)
        ares = AresAgent(paper=True, broker=a, default_account_equity=4000.0)
        res = ares.place(_sized("DE40", SignalCategory.SUPPLIER_DISRUPTION))
        assert res.side == "SELL" and res.take_profit_price < res.fill_price < res.stop_loss_price

    def test_ares_stock_ticker_is_error_not_queued(self):
        from agents.ares import AresAgent
        a, _ = _adapter()
        ares = AresAgent(paper=True, broker=a, default_account_equity=4000.0)
        with patch("core.supabase_client.enqueue_pending_order") as enq:
            res = ares.place(_sized("NVDA"))
        assert res.status.startswith("error:")
        enq.assert_not_called()

    def test_ares_queues_when_ctrader_unreachable(self):
        from agents.ares import AresAgent
        from core.types import AgentHealth
        a, _ = _adapter(FakeTransport(connected=False))
        ares = AresAgent(paper=True, broker=a, default_account_equity=4000.0)
        assert ares.health() == AgentHealth.DEGRADED
        with patch("core.supabase_client.enqueue_pending_order") as enq:
            res = ares.place(_sized("EURUSD"))
        assert res.status == "queued"
        assert enq.call_args.kwargs["payload"]["exchange"] == "CTRADER"

    def test_ares_cancel_all_pending_delegates(self):
        from agents.ares import AresAgent
        a, t = _adapter()
        td = MM.ProtoOATradeData(symbolId=1, volume=100, tradeSide=MM.BUY)
        t.orders_on_account = [MM.ProtoOAOrder(orderId=5, tradeData=td, orderType=MM.LIMIT,
                                               orderStatus=MM.ORDER_STATUS_ACCEPTED)]
        AresAgent(paper=True, broker=a, default_account_equity=4000.0).cancel_all_pending()
        assert [m.orderId for m in t.sent if type(m).__name__ == "ProtoOACancelOrderReq"] == [5]

    def test_argus_refresh_reads_ctrader_positions(self):
        from agents.argus import ArgusAgent
        a, t = _adapter()
        t.positions_on_account = [
            MM.ProtoOAPosition(positionId=9, positionStatus=1, swap=0, price=1.07,
                               tradeData=MM.ProtoOATradeData(symbolId=1, volume=8000, tradeSide=MM.BUY)),
        ]
        argus = ArgusAgent(default_account_equity=4000.0, broker=a)
        state = argus.refresh()
        assert [s.symbol for s in state.snapshots] == ["EURUSD"]
        assert state.total_equity == 4000.0      # risk budget, not the demo balance


# ── smoke test script guards ─────────────────────────────────────────────────

class TestSmokeScript:
    def _run(self, env, argv=None, transport=None):
        import os

        from scripts import ctrader_smoke_test as smoke
        clean = {k: "" for k in list(REQUIRED_ENV) + ["CTRADER_ENV", "CTRADER_SMOKE_ALLOW_LIVE"]}
        with patch.dict(os.environ, {**clean, **env}), patch("dotenv.load_dotenv"), \
             patch("core.brokers.ctrader.TwistedTransport", return_value=transport or FakeTransport()):
            return smoke.main(argv or [])

    def test_missing_env_refuses_with_setup_message(self, capsys):
        assert self._run({}) == 2
        err = capsys.readouterr().err
        assert "CTRADER_CLIENT_ID" in err and "openapi.ctrader.com" in err

    def test_live_refused_without_second_opt_in(self):
        t = FakeTransport()
        assert self._run({**_FAKE_ENV, "CTRADER_ENV": "live"}, transport=t) == 3
        assert t.sent == []

    def test_auth_failure_places_no_order(self):
        t = FakeTransport()
        t.overrides["ProtoOAApplicationAuthReq"] = [M.ProtoOAErrorRes(errorCode="CH_CLIENT_AUTH_FAILURE")]
        assert self._run(_FAKE_ENV, transport=t) == 1
        assert "ProtoOANewOrderReq" not in t.names()

    def test_unresolved_symbol_places_no_order(self):
        t = FakeTransport()
        assert self._run(_FAKE_ENV, ["--symbol", "NVDA"], transport=t) == 1
        assert "ProtoOANewOrderReq" not in t.names()

    def test_happy_path_places_min_volume_limit_and_cancels(self, capsys):
        t = FakeTransport()
        assert self._run(_FAKE_ENV, transport=t) == 0
        [order] = [m for m in t.sent if type(m).__name__ == "ProtoOANewOrderReq"]
        assert order.orderType == MM.LIMIT and order.volume == _SYMBOLS[1][3]   # venue minimum
        assert order.limitPrice < 1.08 * 0.85                                   # far from market
        assert [m.orderId for m in t.sent if type(m).__name__ == "ProtoOACancelOrderReq"] == [777]
        out = capsys.readouterr()
        assert "secret-test-value" not in out.out + out.err
        assert "access-test-value" not in out.out + out.err
