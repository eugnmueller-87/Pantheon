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
                 account_equity: float = 4000.0, min_units: float = 0.0,
                 max_min_lot_risk_overshoot: float = 1.5, leverage_cap: Optional[float] = 30.0,
                 supports_modify: bool = True):
        self.prices          = dict(prices or {})
        # "notional" mirrors IBKR-style sizing; "risk" mirrors the cTrader path.
        self.sizing_mode        = sizing_mode
        self.risk_per_trade_pct = risk_per_trade_pct
        self.account_equity     = account_equity
        self.max_risk_per_trade_pct = risk_per_trade_pct
        # Optional venue minimum (units) with the cTrader min-lot risk rule.
        self.min_units          = min_units
        self.max_min_lot_risk_overshoot = max_min_lot_risk_overshoot
        self.leverage_cap       = leverage_cap
        self.risk_calls: list[dict] = []
        self.modifications: list[dict] = []
        self.events: list[tuple] = []          # ordered ("modify"|"place", ...) for call-order asserts
        if not supports_modify:
            self.modify_position_sltp = None   # type: ignore[assignment]  # feature-detected via getattr
        self.supports_short  = supports_short
        self.reachable       = reachable
        self.decimals        = decimals
        self.connect_calls   = 0
        self.orders: list[BracketResult] = []
        self.working: list[str] = []
        self._positions: list[BrokerPosition] = []
        self._fills: dict[str, list[dict]] = {}
        self.open_positions_on_fill = False

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
        """Simplified risk sizing (account ccy == quote ccy, 0.01-unit step).

        Optional min lot (min_units) follows the cTrader rule: use the min lot
        only if its loss <= budget × max_min_lot_risk_overshoot, else reject
        with the same wording as core.brokers.ctrader_instruments."""
        from types import SimpleNamespace
        pct = self.risk_per_trade_pct if risk_pct is None else min(risk_pct, self.max_risk_per_trade_pct)
        dist = abs(entry - stop_loss)
        if dist <= 0:
            raise BrokerRejectedError("stop equals entry")
        budget = self.account_equity * pct
        units = int(budget / dist * 100 + 1e-6) / 100
        self.risk_calls.append({"symbol": symbol, "entry": entry, "stop": stop_loss, "risk_pct": pct})
        if self.min_units and units < self.min_units:
            min_risk = self.min_units * dist
            if min_risk > budget * self.max_min_lot_risk_overshoot:
                raise BrokerRejectedError(
                    f"{symbol}: risk-sized {units:g} units is below the venue minimum ({self.min_units:g} units). "
                    f"Intended risk {budget:.2f}, min-lot risk {min_risk:.2f} > tolerance "
                    f"{self.max_min_lot_risk_overshoot:g}× — order not placed")
            units = self.min_units
        return SimpleNamespace(units=units, risk_budget=budget,
                               risk_at_stop=units * dist, notional=units * entry,
                               notional_fraction=units * entry / self.account_equity)

    def set_risk_ceiling(self, pct: float) -> None:
        if not self.risk_per_trade_pct <= pct <= 0.05:
            raise BrokerRejectedError(f"risk ceiling {pct} out of range")
        self.max_risk_per_trade_pct = pct

    def leverage_cap_for(self, symbol: str) -> Optional[float]:
        return self.leverage_cap

    def modify_position_sltp(self, position_id, stop_loss, take_profit=None, symbol=None) -> None:
        self.modifications.append({"position_id": position_id, "stop_loss": stop_loss,
                                   "take_profit": take_profit, "symbol": symbol})
        self.events.append(("modify", position_id, stop_loss, take_profit))
        for p in self._positions:
            if p.position_id == position_id:
                p.stop_loss, p.take_profit = stop_loss, take_profit

    def set_price(self, symbol: str, price: float) -> None:
        """Move the market: updates quotes and open positions' current price / P&L."""
        self.prices[symbol.upper()] = price
        for p in self._positions:
            if p.symbol.upper() == symbol.upper():
                p.current_price = price
                p.unrealized_pnl = (price - p.avg_cost) * p.qty * (1 if p.side == "LONG" else -1)

    def place_bracket(self, symbol, side, qty, entry, stop_loss, take_profit, exchange=None) -> BracketResult:
        """take_profit may be None (stop-loss only). With open_positions_on_fill
        the order also opens a BrokerPosition (position_id = order id)."""
        oid = uuid.uuid4().hex[:8]
        res = BracketResult(order_id=oid, symbol=symbol, side=side, qty=qty, entry_price=entry,
                            stop_loss_price=stop_loss, take_profit_price=take_profit,
                            raw={"position_id": oid})
        self.orders.append(res)
        self.working.append(oid)
        self.events.append(("place", symbol, side, qty, entry, stop_loss, take_profit))
        if self.open_positions_on_fill:
            self._positions.append(BrokerPosition(
                symbol=symbol, side="LONG" if side == "BUY" else "SHORT", qty=qty, avg_cost=entry,
                current_price=entry, unrealized_pnl=0.0, position_id=oid,
                stop_loss=stop_loss, take_profit=take_profit,
            ))
        return res

    def cancel_all_pending(self) -> int:
        n = len(self.working)
        self.working.clear()
        return n

    def add_position(self, pos: BrokerPosition) -> None:
        self._positions.append(pos)

    def close_all_positions(self, symbol: Optional[str] = None) -> None:
        self._positions = [p for p in self._positions if symbol and p.symbol.upper() != symbol.upper()]

    def add_fill(self, symbol: str, price: float, qty: float, time: str) -> None:
        self._fills.setdefault(symbol, []).append({"price": price, "qty": qty, "time": time})

    def positions(self) -> list[BrokerPosition]:
        return list(self._positions)

    def closing_fills(self) -> dict[str, list[dict]]:
        return {k: sorted(v, key=lambda r: r["time"]) for k, v in self._fills.items()}
