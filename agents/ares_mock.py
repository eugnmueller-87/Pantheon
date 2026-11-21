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
        risk_per_trade_pct: Optional[float] = None,
    ):
        self.slippage_bps    = slippage_bps
        # Set by ZEUS only when broker == "ctrader": technical signals that carry
        # a stop are then risk-sized (equity × risk / stop distance) like the
        # real cTrader path. None → stock behaviour, exactly as before.
        self.risk_per_trade_pct = risk_per_trade_pct
        self.account_equity  = account_equity
        self.stop_loss_pct   = stop_loss_pct
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
        signal_stop = getattr(sized, "stop_distance", None)
        ref_price   = getattr(sized, "reference_price", None)
        # Technical (FX/index) signals carry the close they were computed on —
        # logical names like EURUSD are not yfinance tickers.
        mid = float(ref_price) if (signal_stop and ref_price) else self._get_price(symbol)
        if mid is None:
            return self._null_result()

        slippage   = mid * (self.slippage_bps / 10_000) * random.choice([-1, 1])
        trade_side = getattr(sized, "trade_side", "") or ""
        if trade_side in ("BUY", "SELL"):
            is_long = trade_side == "BUY"
        else:
            is_long = sized.category != SignalCategory.SUPPLIER_DISRUPTION
        side       = "BUY" if is_long else "SELL"

        if signal_stop:
            from core.fx_signals import bracket_from_distance
            fill_price = round(mid + slippage, 5)
            rr = float(getattr(sized, "reward_risk", None) or self.take_profit_pct / self.stop_loss_pct)
            stop_price, limit_price = bracket_from_distance(fill_price, float(signal_stop), is_long, rr, 5)
        else:
            fill_price = round(mid + slippage, 4)
            stop_price, limit_price = self._compute_stops(symbol, fill_price, is_long)

        if signal_stop and self.risk_per_trade_pct:
            # Simulated risk sizing (ignores venue min lot and FX conversion).
            risk_pct = min(self.risk_per_trade_pct, sized.position_size_pct)
            qty = int(self.account_equity * risk_pct / float(signal_stop) * 100) / 100
        else:
            qty = max(1, int(self.account_equity * sized.position_size_pct / fill_price))
        order_id = str(uuid.uuid4())[:8]

        self._pending.append(order_id)
        logger.info("[ARES-MOCK] SIMULATED %s %g %s @ %.5g | SL=%.5g TP=%.5g | id=%s",
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

    # ------------------------------------------------------------------
    # ATR-aware stop/target calculation
    # ------------------------------------------------------------------

    def _compute_stops(
        self,
        symbol: str,
        fill_price: float,
        is_long: bool,
    ) -> tuple[float, float]:
        """
        Return (stop_price, take_profit_price).

        If use_atr_stops is True in settings, derive stop distance from the
        ATR of the last `atr_period` days, clamped to [min_stop_pct, max_stop_pct].
        The take-profit is stop_distance × reward_risk_ratio.

        Falls back to the fixed percentage stops when ATR is unavailable or
        use_atr_stops is False.
        """
        from config.settings import load_settings
        s = load_settings()

        use_atr = s.get("use_atr_stops", False)
        atr_val: Optional[float] = None

        if use_atr:
            atr_period = int(s.get("atr_period", 14))
            try:
                atr_val = self._fetch_atr(symbol, atr_period)
            except Exception as exc:
                logger.debug("[ARES-MOCK] ATR fetch failed for %s: %s — using fixed stops", symbol, exc)

        if use_atr and atr_val is not None and fill_price > 0:
            min_stop = float(s.get("min_stop_pct", 0.03))
            max_stop = float(s.get("max_stop_pct", 0.12))
            rr       = float(s.get("reward_risk_ratio", 2.0))

            raw_stop_pct = atr_val / fill_price * float(s.get("atr_stop_multiple", 2.0))
            stop_pct     = max(min_stop, min(max_stop, raw_stop_pct))
            tp_pct       = stop_pct * rr

            logger.info(
                "[ARES-MOCK] ATR stop — symbol=%s ATR=%.4f fill=%.4f stop_pct=%.3f tp_pct=%.3f",
                symbol, atr_val, fill_price, stop_pct, tp_pct,
            )
        else:
            stop_pct = self.stop_loss_pct
            tp_pct   = self.take_profit_pct

        stop_price  = round(fill_price * (1 - stop_pct if is_long else 1 + stop_pct), 4)
        limit_price = round(fill_price * (1 + tp_pct   if is_long else 1 - tp_pct),   4)
        return stop_price, limit_price

    def _fetch_atr(self, symbol: str, period: int) -> float:
        """Compute Average True Range over `period` days from yfinance history."""
        hist = yf.Ticker(symbol).history(period=f"{period + 5}d")
        if hist is None or len(hist) < period:
            raise ValueError(f"Insufficient history for ATR({period}) on {symbol}")
        high   = hist["High"]
        low    = hist["Low"]
        close  = hist["Close"]
        prev_c = close.shift(1)
        tr = (
            (high - low)
            .combine(abs(high - prev_c), max)
            .combine(abs(low  - prev_c), max)
        )
        return float(tr.iloc[-period:].mean())

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
