"""
MockBrokerAdapter — in-memory BrokerAdapter for unit tests.

Not wired into Zeus: mock_execution=True still uses agents/ares_mock.AresMockAgent
(unchanged behaviour). This adapter exists so Ares/Argus can be exercised
through the BrokerAdapter seam without any SDK or network.
"""

from __future__ import annotations

import uuid
from typing import Optional

from core.broker import BracketResult, BrokerConnectivityError, BrokerPosition, BrokerRejectedError


class MockBrokerAdapter:
    name = "mock"

    def __init__(self, prices: Optional[dict[str, float]] = None, supports_short: bool = True,
                 reachable: bool = True, decimals: int = 4,
                 sizing_mode: str = "notional", risk_per_trade_pct: float = 0.01,
                 account_equity: float = 4000.0):
        self.prices          = dict(prices or {})
        # "notional" mirrors IBKR-style sizing; "risk" mirrors the cTrader path.
        self.sizing_mode        = sizing_mode
        self.risk_per_trade_pct = risk_per_trade_pct
        self.account_equity     = account_equity
        self.risk_calls: list[dict] = []
        self.supports_short  = supports_short
        self.reachable       = reachable
        self.decimals        = decimals
        self.connect_calls   = 0
        self.orders: list[BracketResult] = []
        self.working: list[str] = []
        self._positions: list[BrokerPosition] = []
        self._fills: dict[str, list[dict]] = {}

    def connect(self):
        self.connect_calls += 1
        if not self.reachable:
            raise BrokerConnectivityError("mock broker unreachable")
        return self

    def disconnect(self) -> None:
        pass

    def health(self) -> bool:
        return self.reachable

    def resolve_symbol(self, ticker: str) -> Optional[str]:
        t = (ticker or "").upper()
        return t if t in self.prices else None

    def price_decimals(self, symbol: str) -> int:
        return self.decimals

    def get_quote(self, symbol: str, exchange: Optional[str] = None) -> float:
        return self.prices[symbol.upper()]

    def size_order(self, symbol: str, notional: float, price: float) -> float:
        return round(notional / price, 2)

    def size_order_risk(self, symbol: str, entry: float, stop_loss: float,
                        risk_pct: Optional[float] = None):
        """Simplified risk sizing (account ccy == quote ccy, 0.01-unit step, no min lot)."""
        from types import SimpleNamespace
        pct = self.risk_per_trade_pct if risk_pct is None else min(risk_pct, self.risk_per_trade_pct)
        dist = abs(entry - stop_loss)
        if dist <= 0:
            raise BrokerRejectedError("stop equals entry")
        units = int(self.account_equity * pct / dist * 100) / 100
        self.risk_calls.append({"symbol": symbol, "entry": entry, "stop": stop_loss, "risk_pct": pct})
        return SimpleNamespace(units=units, risk_budget=self.account_equity * pct,
                               risk_at_stop=units * dist, notional=units * entry,
                               notional_fraction=units * entry / self.account_equity)

    def place_bracket(self, symbol, side, qty, entry, stop_loss, take_profit, exchange=None) -> BracketResult:
        oid = uuid.uuid4().hex[:8]
        res = BracketResult(order_id=oid, symbol=symbol, side=side, qty=qty, entry_price=entry,
                            stop_loss_price=stop_loss, take_profit_price=take_profit)
        self.orders.append(res)
        self.working.append(oid)
        return res

    def cancel_all_pending(self) -> int:
        n = len(self.working)
        self.working.clear()
        return n

    def add_position(self, pos: BrokerPosition) -> None:
        self._positions.append(pos)

    def add_fill(self, symbol: str, price: float, qty: float, time: str) -> None:
        self._fills.setdefault(symbol, []).append({"price": price, "qty": qty, "time": time})

    def positions(self) -> list[BrokerPosition]:
        return list(self._positions)

    def closing_fills(self) -> dict[str, list[dict]]:
        return {k: sorted(v, key=lambda r: r["time"]) for k, v in self._fills.items()}
