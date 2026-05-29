"""
Agent 5 — Ares: Trade Execution (Interactive Brokers)
God of decisive action — executes the strike.
Places bracket orders via ib_insync.
Imports only from core.types — never from other agents.
"""

from __future__ import annotations

import logging
import uuid

from core.agent_knowledge import AgentKnowledgeBase
from core.types import AgentHealth, SignalCategory, SizedSignal, TradeResult

logger = logging.getLogger("ares")


class AresAgent:
    IB_PAPER_PORT = 4004  # socat bridge inside ibgateway container (4004→127.0.0.1:4002)
    IB_LIVE_PORT  = 4001

    def __init__(
        self,
        paper: bool = True,
        host: str = "ibgateway",
        stop_loss_pct: float = 0.03,
        take_profit_pct: float = 0.06,
        default_account_equity: float = 100_000.0,
    ):
        self.paper                  = paper
        self.host                   = host
        self.port                   = self.IB_PAPER_PORT if paper else self.IB_LIVE_PORT
        self.stop_loss_pct          = stop_loss_pct
        self.take_profit_pct        = take_profit_pct
        self.default_account_equity = default_account_equity
        self._ib                    = None
        self._pending: list[str]    = []
        self.kb = AgentKnowledgeBase("ares")
        logger.info("[ARES] %s mode — port %d", "PAPER" if paper else "LIVE", self.port)

    def health(self) -> AgentHealth:
        if self._ib is None:
            return AgentHealth.HEALTHY  # not yet connected — report healthy until first trade
        return AgentHealth.HEALTHY if self._ib.isConnected() else AgentHealth.DEGRADED

    def place(self, sized: SizedSignal) -> TradeResult:
        if not sized.affected_tickers:
            logger.warning("[ARES] No tickers in signal %s", sized.signal_id)
            return self._null_result()
        try:
            ib     = self._get_connection()
            result = self._place_bracket(ib, sized.affected_tickers[0], sized)
            self._pending.append(result.order_id)
            return result
        except Exception as exc:
            logger.error("[ARES] Order failed: %s", exc)
            return self._error_result(sized.affected_tickers[0], exc)

    def cancel_all_pending(self) -> None:
        try:
            ib = self._get_connection()
            for trade in ib.openTrades():
                ib.cancelOrder(trade.order)
            logger.warning("[ARES] Cancelled open orders.")
        except Exception as exc:
            logger.error("[ARES] cancel_all_pending failed: %s", exc)

    def _place_bracket(self, ib, symbol: str, sized: SizedSignal) -> TradeResult:
        from ib_insync import Stock
        contract = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(contract)

        import math
        # Request delayed data (no live subscription on paper account)
        ib.reqMarketDataType(3)  # 3 = delayed, 4 = delayed-frozen
        ticker = ib.reqMktData(contract, "", False, False)
        ib.sleep(2)
        mid = ticker.midpoint()
        if mid is None or (isinstance(mid, float) and math.isnan(mid)) or mid == 0:
            mid = ticker.last
        if mid is None or (isinstance(mid, float) and math.isnan(mid)) or mid == 0:
            mid = ticker.close
        ib.cancelMktData(contract)
        if mid is None or (isinstance(mid, float) and math.isnan(mid)) or mid <= 0:
            raise ValueError(f"Could not price {symbol}")

        account_val = self._get_account_value(ib)
        if account_val <= 0:
            raise ValueError(f"Invalid account equity: {account_val}")
        qty         = max(1, int(account_val * sized.position_size_pct / mid))
        is_long     = sized.category != SignalCategory.SUPPLIER_DISRUPTION
        side        = "BUY" if is_long else "SELL"
        sl  = self.stop_loss_pct
        tp  = self.take_profit_pct
        stop_price  = round(mid * (1 - sl if is_long else 1 + sl), 2)
        limit_price = round(mid * (1 + tp if is_long else 1 - tp), 2)

        bracket = ib.bracketOrder(side, qty, mid, limit_price, stop_price)
        for o in bracket:
            ib.placeOrder(contract, o)

        order_id = str(bracket[0].orderId)
        logger.info("[ARES] %s %d %s @ %.2f | SL=%.2f TP=%.2f | id=%s",
                    side, qty, symbol, mid, stop_price, limit_price, order_id)
        return TradeResult(
            order_id=order_id, symbol=symbol, side=side,
            fill_price=mid, qty=qty,
            stop_loss_price=stop_price, take_profit_price=limit_price,
        )

    def _get_account_value(self, ib) -> float:
        # Always use the configured equity cap — never the raw IBKR paper balance.
        # IBKR paper accounts start at EUR 1M+ which would massively over-size positions.
        # The configured default_account_equity (e.g. EUR 4,000) is the true risk budget.
        return self.default_account_equity

    def _get_connection(self):
        import asyncio
        asyncio.set_event_loop(asyncio.new_event_loop())
        from ib_insync import IB  # eventkit reads event loop at import time
        if self._ib is not None:
            try:
                self._ib.disconnect()
            except Exception:
                pass
        self._ib = IB()
        self._ib.connect(self.host, self.port, clientId=1)
        logger.info("[ARES] Connected to IB %s:%d", self.host, self.port)
        return self._ib

    @staticmethod
    def _null_result() -> TradeResult:
        return TradeResult(order_id=str(uuid.uuid4())[:8], symbol="", side="",
                           fill_price=None, qty=0, status="skipped")

    @staticmethod
    def _error_result(symbol: str, exc: Exception) -> TradeResult:
        return TradeResult(order_id=str(uuid.uuid4())[:8], symbol=symbol, side="",
                           fill_price=None, qty=0, status=f"error: {exc}")
