"""
Agent 5 (Mock) — Ares Mock: Trade Execution (no IB required)
Drop-in replacement for AresAgent during testing.
Imports only from core.types — never from other agents.
"""

from __future__ import annotations

import logging
import random
import uuid
from typing import Optional

import yfinance as yf

from core.agent_knowledge import AgentKnowledgeBase
from core.types import AgentHealth, SignalCategory, SizedSignal, TradeResult

logger = logging.getLogger("ares.mock")

class AresMockAgent:
    def __init__(
        self,
        slippage_bps: int = 5,
        account_equity: float = 100_000.0,
        stop_loss_pct: float = 0.03,
        take_profit_pct: float = 0.06,
    ):
        self.slippage_bps   = slippage_bps
        self.account_equity = account_equity
        self.stop_loss_pct  = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self._pending: list[str] = []
        self.kb = AgentKnowledgeBase("ares")   # shares Ares skills with live agent
        logger.info("[ARES-MOCK] Initialised — no IB connection required.")

    def health(self) -> AgentHealth:
        return AgentHealth.HEALTHY

    def place(self, sized: SizedSignal) -> TradeResult:
        if not sized.affected_tickers:
            return self._null_result()

        symbol = sized.affected_tickers[0]
        mid    = self._get_price(symbol)
        if mid is None:
            return self._null_result()

        slippage    = mid * (self.slippage_bps / 10_000) * random.choice([-1, 1])
        fill_price  = round(mid + slippage, 4)
        qty         = max(1, int(self.account_equity * sized.position_size_pct / fill_price))
        is_long     = sized.category != SignalCategory.SUPPLIER_DISRUPTION
        side        = "BUY" if is_long else "SELL"
        sl  = self.stop_loss_pct
        tp  = self.take_profit_pct
        stop_price  = round(fill_price * (1 - sl if is_long else 1 + sl), 4)
        limit_price = round(fill_price * (1 + tp if is_long else 1 - tp), 4)
        order_id    = str(uuid.uuid4())[:8]

        self._pending.append(order_id)
        logger.info("[ARES-MOCK] SIMULATED %s %d %s @ %.4f | SL=%.4f TP=%.4f | id=%s",
                    side, qty, symbol, fill_price, stop_price, limit_price, order_id)

        return TradeResult(
            order_id=order_id, symbol=symbol, side=side,
            fill_price=fill_price, qty=qty,
            stop_loss_price=stop_price, take_profit_price=limit_price,
            status="simulated",
        )

    def cancel_all_pending(self) -> None:
        logger.info("[ARES-MOCK] Cancelled %d pending orders.", len(self._pending))
        self._pending.clear()

    def _get_price(self, symbol: str) -> Optional[float]:
        try:
            hist = yf.Ticker(symbol).history(period="1d")
            if not hist.empty:
                return float(hist["Close"].iloc[-1])
        except Exception as exc:
            logger.warning("[ARES-MOCK] Price fetch failed for %s: %s", symbol, exc)
        return None

    @staticmethod
    def _null_result() -> TradeResult:
        return TradeResult(order_id=str(uuid.uuid4())[:8], symbol="", side="",
                           fill_price=None, qty=0, status="skipped")
