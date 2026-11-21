"""
IbkrAdapter — Interactive Brokers via ib_async (moved out of agents/ares.py and
agents/argus.py unchanged in behaviour).

Session model: connect-per-call. Every connect() tears down the previous IB
handle and retries 3 times (0/1/2s backoff) with a STABLE clientId (Ares=1,
Argus=2) — two independent adapters, one per agent, exactly as before.
"""

from __future__ import annotations

import logging
import math
import socket
import time
from typing import Any, Optional

from core.broker import BracketResult, BrokerPosition

logger = logging.getLogger("broker.ibkr")


def _bad(px: Any) -> bool:
    return px is None or (isinstance(px, float) and math.isnan(px)) or px <= 0


class IbkrAdapter:
    name = "ibkr"
    supports_short = False   # cash equities: Ares stays long-only on IBKR

    def __init__(self, host: str, port: int, client_id: int, log_prefix: str = "[IBKR]"):
        self.host       = host
        self.port       = port
        self.client_id  = client_id
        self._prefix    = log_prefix
        self._ib        = None
        self._contracts: dict[tuple[str, str], Any] = {}

    # ── session ─────────────────────────────────────────────────────────────

    def connect(self):
        import asyncio

        from ib_async import IB

        # Close the previous loop cleanly before creating a new one.
        try:
            old_loop = asyncio.get_event_loop()
            if not old_loop.is_running():
                old_loop.close()
        except Exception:
            pass
        asyncio.set_event_loop(asyncio.new_event_loop())

        last_exc: Exception | None = None
        for attempt, backoff in enumerate([0, 1, 2]):
            # Clean up any previous handle before each attempt (including first).
            self.disconnect()
            if backoff:
                time.sleep(backoff)
            try:
                self._ib = IB()
                self._ib.connect(self.host, self.port, clientId=self.client_id)
                logger.info("%s Connected to IB %s:%d (attempt %d)", self._prefix, self.host, self.port, attempt + 1)
                return self._ib
            except Exception as exc:
                last_exc = exc
                logger.warning("%s Connect attempt %d failed: %s", self._prefix, attempt + 1, exc)

        raise RuntimeError(
            f"{self._prefix} Could not connect to IB {self.host}:{self.port} after 3 attempts: {last_exc}"
        )

    def disconnect(self) -> None:
        if self._ib is not None:
            try:
                self._ib.disconnect()
            except Exception:
                pass
            self._ib = None

    def health(self) -> bool:
        try:
            with socket.create_connection((self.host, self.port), timeout=3):
                pass
            return True
        except OSError:
            return False

    def _handle(self):
        return self._ib if self._ib is not None else self.connect()

    # ── instruments ─────────────────────────────────────────────────────────

    def resolve_symbol(self, ticker: str) -> Optional[str]:
        return ticker or None

    def price_decimals(self, symbol: str) -> int:
        return 2

    def _contract(self, symbol: str, exchange: Optional[str]):
        from ib_async import Stock

        exchange = exchange or "SMART"
        key = (symbol, exchange)
        if key not in self._contracts:
            currency = "EUR" if exchange == "XETRA" else "USD"
            contract = Stock(symbol, exchange, currency)
            self._handle().qualifyContracts(contract)
            self._contracts[key] = contract
        return self._contracts[key]

    def get_quote(self, symbol: str, exchange: Optional[str] = None) -> float:
        ib       = self._handle()
        contract = self._contract(symbol, exchange)
        # Request delayed data (no live subscription on paper account)
        ib.reqMarketDataType(3)  # 3 = delayed, 4 = delayed-frozen
        ticker = ib.reqMktData(contract, "", False, False)
        ib.sleep(2)
        mid = ticker.midpoint()
        if _bad(mid):
            mid = ticker.last
        if _bad(mid):
            mid = ticker.close
        ib.cancelMktData(contract)
        if _bad(mid):
            raise ValueError(f"Could not price {symbol}")
        return float(mid)

    def size_order(self, symbol: str, notional: float, price: float) -> float:
        """Integer shares, minimum 1 (unchanged IBKR behaviour)."""
        qty = max(1, int(notional / price))
        actual = qty * price
        if qty == 1 and actual > notional * 1.5:
            logger.warning(
                "%s Position oversized — %s: intended %.0f → actual %.0f (1 share). "
                "Signal quality justified the trade.",
                self._prefix, symbol, notional, actual,
            )
        return qty

    # ── orders ──────────────────────────────────────────────────────────────

    def place_bracket(
        self,
        symbol: str,
        side: str,
        qty: float,
        entry: float,
        stop_loss: float,
        take_profit: float,
        exchange: Optional[str] = None,
    ) -> BracketResult:
        ib       = self._handle()
        contract = self._contract(symbol, exchange)
        bracket  = ib.bracketOrder(side, int(qty), entry, take_profit, stop_loss)
        for o in bracket:
            ib.placeOrder(contract, o)
        return BracketResult(
            order_id=str(bracket[0].orderId), symbol=symbol, side=side, qty=int(qty),
            entry_price=entry, stop_loss_price=stop_loss, take_profit_price=take_profit,
        )

    def cancel_all_pending(self) -> int:
        ib = self._handle()
        n = 0
        for trade in ib.openTrades():
            ib.cancelOrder(trade.order)
            n += 1
        return n

    # ── portfolio ───────────────────────────────────────────────────────────

    def positions(self) -> list[BrokerPosition]:
        out: list[BrokerPosition] = []
        for pos in self._handle().portfolio():
            if pos.position == 0:
                continue
            out.append(BrokerPosition(
                symbol         = pos.contract.symbol,
                side           = "LONG" if pos.position > 0 else "SHORT",
                qty            = abs(pos.position),
                avg_cost       = pos.averageCost or 1.0,
                current_price  = pos.marketPrice,
                unrealized_pnl = pos.unrealizedPNL,
            ))
        return out

    def closing_fills(self) -> dict[str, list[dict]]:
        """Long-only system: a closing execution is a SELL (side 'SLD')."""
        out: dict[str, list] = {}
        try:
            for f in self._handle().fills():
                ex = getattr(f, "execution", None)
                if ex is None or getattr(ex, "side", "") != "SLD":
                    continue
                sym = getattr(getattr(f, "contract", None), "symbol", None)
                if not sym:
                    continue
                price = getattr(ex, "avgPrice", None) or getattr(ex, "price", None)
                out.setdefault(sym, []).append({
                    "price": price,
                    "qty":   getattr(ex, "shares", None),
                    "time":  getattr(ex, "time", None),
                })
        except Exception as exc:
            logger.debug("%s could not read IB fills: %s", self._prefix, exc)
            return {}
        for sym in out:
            out[sym].sort(key=lambda d: d.get("time") or "")
        return out

