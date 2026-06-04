"""
Agent 5 — Ares: Trade Execution (Interactive Brokers)
God of decisive action — executes the strike.
Places bracket orders via ib_async (community successor to ib_insync).
Imports only from core.types — never from other agents.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from core.agent_knowledge import AgentKnowledgeBase
from core.types import AgentHealth, SignalCategory, SizedSignal, TradeResult

logger = logging.getLogger("ares")

# How long an approved trade stays retryable before it's expired as stale.
# 45 min: safely within a single NYSE session, safely before the gateway
# daily restart windows (11:45 PM ET / 06:00 ET).
PENDING_ORDER_MAX_AGE_MIN: int = 45

# Max retryable rows drained per reconcile pass (prevents long stalls).
MAX_RECONCILE_PER_CYCLE: int = 10

# IB error messages that indicate a terminal rejection — never retry these.
_TERMINAL_IB_ERRORS = (
    "No security definition",
    "Invalid contract",
    "Order rejected",
    "Insufficient funds",
    "Buying power",
    "margin",
    "not found",
    "unknown contract",
)


def _is_connectivity_error(exc: Exception) -> bool:
    """True for transient IB-unreachable errors that are safe to queue and retry."""
    msg = str(exc).lower()
    connectivity_markers = (
        "errno 111",
        "connection refused",
        "econnrefused",
        "could not connect",
        "not connected",
        "timed out",
        "timeout",
        "circuit_open",
        "connect call failed",
    )
    return any(m in msg for m in connectivity_markers)


def _is_terminal_ib_error(exc: Exception) -> bool:
    """True for IB order rejections that must NOT be retried."""
    msg = str(exc).lower()
    return any(t.lower() in msg for t in _TERMINAL_IB_ERRORS)


def _same_session(t1: datetime, t2: datetime) -> bool:
    """Return True if both timestamps fall within the same NYSE calendar session."""
    # NYSE session: 09:30–16:00 ET = 13:30–20:00 UTC
    # We compare calendar date in ET (UTC-4 / UTC-5; using UTC-4 as approximation).
    ET_OFFSET = timedelta(hours=4)
    d1 = (t1 - ET_OFFSET).date()
    d2 = (t2 - ET_OFFSET).date()
    return d1 == d2


class AresAgent:
    IB_PAPER_PORT = 4002
    IB_LIVE_PORT  = 4001

    def __init__(
        self,
        paper: bool = True,
        host: str = "ibgateway",
        port: int | None = None,
        stop_loss_pct: float = 0.03,
        take_profit_pct: float = 0.06,
        default_account_equity: float = 100_000.0,
    ):
        self.paper                  = paper
        self.host                   = host
        self.port                   = port if port is not None else (self.IB_PAPER_PORT if paper else self.IB_LIVE_PORT)
        self.stop_loss_pct          = stop_loss_pct
        self.take_profit_pct        = take_profit_pct
        self.default_account_equity = default_account_equity
        self._ib                    = None
        self._pending: list[str]    = []   # placed order IDs (for cancel_all_pending)
        self.kb = AgentKnowledgeBase("ares")
        logger.info("[ARES] %s mode — port %d", "PAPER" if paper else "LIVE", self.port)

    def health(self) -> AgentHealth:
        import socket
        try:
            with socket.create_connection((self.host, self.port), timeout=3):
                pass
            return AgentHealth.HEALTHY
        except OSError:
            return AgentHealth.DEGRADED

    # ------------------------------------------------------------------
    # Primary entry point — called from Zeus Stage 6
    # ------------------------------------------------------------------

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
            if _is_connectivity_error(exc):
                return self._enqueue(sized, exc)
            # Terminal IB rejection or unknown error — do not queue.
            return self._error_result(sized.affected_tickers[0], exc)

    # ------------------------------------------------------------------
    # Retry pass — called by Zeus Stage 6 when IB is healthy
    # ------------------------------------------------------------------

    def reconcile_pending(self) -> int:
        """
        Drain the pending_orders queue.  Returns the number of orders processed.
        Called at the start of Stage 6 when Ares health() == HEALTHY.
        """
        from core import supabase_client as db

        now = datetime.now(timezone.utc)
        rows = db.get_retryable_pending_orders(now, limit=MAX_RECONCILE_PER_CYCLE)
        if not rows:
            return 0

        processed = 0
        for row in rows:
            sid = row["signal_id"]

            # Expiry guards — age and session boundary
            approved_at = datetime.fromisoformat(row["approved_at"])
            expires_at  = datetime.fromisoformat(row["expires_at"])
            if now >= expires_at:
                logger.info("[ARES] Pending order %s expired (age limit)", sid)
                db.set_pending_status(sid, "EXPIRED")
                processed += 1
                continue
            if not _same_session(approved_at, now):
                logger.info("[ARES] Pending order %s expired (session boundary)", sid)
                db.set_pending_status(sid, "EXPIRED")
                processed += 1
                continue

            # Idempotency — skip if already submitted by a parallel cycle
            if row["status"] == "SUBMITTED":
                processed += 1
                continue

            # Atomically claim the row so a concurrent cycle won't double-place
            db.set_pending_status(
                sid, "SUBMITTING",
                last_attempt_at=now.isoformat(),
            )

            payload = row["payload"]
            symbol  = row["symbol"]
            try:
                ib     = self._get_connection()
                result = self._place_bracket_from_payload(ib, symbol, payload)
                self._pending.append(result.order_id)
                db.set_pending_status(
                    sid, "SUBMITTED",
                    ib_order_id=result.order_id,
                    last_error=None,
                )
                logger.info("[ARES] Reconciled pending order %s → SUBMITTED ib_order=%s", sid, result.order_id)
            except Exception as exc:
                err_msg = str(exc)
                if _is_terminal_ib_error(exc):
                    logger.warning("[ARES] Pending order %s terminal rejection: %s", sid, exc)
                    db.set_pending_status(sid, "REJECTED", last_error=err_msg)
                elif now >= expires_at:
                    db.set_pending_status(sid, "EXPIRED", last_error=err_msg)
                else:
                    # Transient — put back to PENDING for the next cycle
                    attempts = row.get("attempts", 0) + 1
                    db.set_pending_status(
                        sid, "PENDING",
                        attempts=attempts,
                        last_error=err_msg,
                    )
                    logger.warning("[ARES] Pending order %s retry %d failed: %s", sid, attempts, exc)
            processed += 1

        return processed

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _enqueue(self, sized: SizedSignal, exc: Exception) -> TradeResult:
        """Write the approved trade to pending_orders instead of dropping it."""
        from core import supabase_client as db

        symbol      = sized.affected_tickers[0]
        is_long     = sized.category != SignalCategory.SUPPLIER_DISRUPTION
        side        = "BUY" if is_long else "SELL"
        approved_at = datetime.now(timezone.utc)
        expires_at  = approved_at + timedelta(minutes=PENDING_ORDER_MAX_AGE_MIN)

        payload = {
            "signal_id":        sized.signal_id,
            "symbol":           symbol,
            "side":             side,
            "position_size_pct": sized.position_size_pct,
            "confidence":       sized.confidence,
        }

        db.enqueue_pending_order(
            signal_id=sized.signal_id,
            symbol=symbol,
            side=side,
            payload=payload,
            approved_at=approved_at,
            expires_at=expires_at,
        )
        logger.info(
            "[ARES] Trade queued (IB unreachable) — signal=%s symbol=%s expires=%s",
            sized.signal_id, symbol, expires_at.isoformat(),
        )
        return TradeResult(
            order_id=str(uuid.uuid4())[:8],
            symbol=symbol,
            side=side,
            fill_price=None,
            qty=0,
            status="queued",
        )

    def _place_bracket_from_payload(self, ib, symbol: str, payload: dict) -> TradeResult:
        """Re-place a queued order from its stored payload dict."""
        from datetime import datetime, timezone

        from core.types import (
            FilteredSignal,
            MacroContext,
            MarketRegime,
            RawSignal,
            Severity,
        )

        # Reconstruct the minimal SizedSignal needed by _place_bracket
        raw = RawSignal(
            signal_id=payload["signal_id"],
            source_url="",
            headline="(queued retry)",
            summary="",
            published_at=datetime.now(timezone.utc),
            category=SignalCategory.POSITIVE_NEWS,  # direction already encoded in side
            severity=Severity.HIGH,
            affected_tickers=[symbol],
        )
        filtered = FilteredSignal(original=raw, compliance_score=1.0)
        macro = MacroContext(
            fetched_at=datetime.now(timezone.utc),
            regime=MarketRegime.BULL,
            vix=15.0,
            sp500_1m_return=0.0,
        )
        sized = SizedSignal(
            original=filtered,
            macro=macro,
            confidence=payload.get("confidence", 0.55),
            position_size_pct=payload.get("position_size_pct", 0.02),
        )
        # Respect the stored side: override category-based direction
        side = payload.get("side", "BUY")
        if side == "SELL":
            # Temporarily override so _place_bracket routes to SELL
            sized.original.original = RawSignal(
                signal_id=payload["signal_id"],
                source_url="",
                headline="(queued retry)",
                summary="",
                published_at=datetime.now(timezone.utc),
                category=SignalCategory.SUPPLIER_DISRUPTION,
                severity=Severity.HIGH,
                affected_tickers=[symbol],
            )
        return self._place_bracket(ib, symbol, sized)

    def cancel_all_pending(self) -> None:
        try:
            ib = self._get_connection()
            for trade in ib.openTrades():
                ib.cancelOrder(trade.order)
            logger.warning("[ARES] Cancelled open orders.")
        except Exception as exc:
            logger.error("[ARES] cancel_all_pending failed: %s", exc)

    def _place_bracket(self, ib, symbol: str, sized: SizedSignal) -> TradeResult:
        from ib_async import Stock
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
        qty     = max(1, int(account_val * sized.position_size_pct / mid))
        is_long = sized.category != SignalCategory.SUPPLIER_DISRUPTION
        side    = "BUY" if is_long else "SELL"

        stop_price, limit_price = self._compute_stops(symbol, mid, is_long)

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

    def _compute_stops(
        self,
        symbol: str,
        fill_price: float,
        is_long: bool,
    ) -> tuple[float, float]:
        """
        Return (stop_price, take_profit_price).

        If use_atr_stops is True in settings, derive stop distance from ATR,
        clamped to [min_stop_pct, max_stop_pct].
        Falls back to fixed percentage stops when ATR is unavailable.
        """
        from typing import Optional as _Opt

        from config.settings import load_settings
        s = load_settings()

        use_atr = s.get("use_atr_stops", False)
        atr_val: "_Opt[float]" = None

        if use_atr:
            atr_period = int(s.get("atr_period", 14))
            try:
                import yfinance as yf
                hist = yf.Ticker(symbol).history(period=f"{atr_period + 5}d")
                if hist is not None and len(hist) >= atr_period:
                    high   = hist["High"]
                    low    = hist["Low"]
                    close  = hist["Close"]
                    prev_c = close.shift(1)
                    tr = (
                        (high - low)
                        .combine(abs(high - prev_c), max)
                        .combine(abs(low  - prev_c), max)
                    )
                    atr_val = float(tr.iloc[-atr_period:].mean())
            except Exception as exc:
                logger.debug("[ARES] ATR fetch failed for %s: %s — using fixed stops", symbol, exc)

        if use_atr and atr_val is not None and fill_price > 0:
            min_stop = float(s.get("min_stop_pct", 0.03))
            max_stop = float(s.get("max_stop_pct", 0.12))
            rr       = float(s.get("reward_risk_ratio", 2.0))

            raw_stop_pct = atr_val / fill_price * float(s.get("atr_stop_multiple", 2.0))
            stop_pct     = max(min_stop, min(max_stop, raw_stop_pct))
            tp_pct       = stop_pct * rr

            logger.info(
                "[ARES] ATR stop — symbol=%s ATR=%.4f fill=%.4f stop_pct=%.3f tp_pct=%.3f",
                symbol, atr_val, fill_price, stop_pct, tp_pct,
            )
        else:
            stop_pct = self.stop_loss_pct
            tp_pct   = self.take_profit_pct

        stop_price  = round(fill_price * (1 - stop_pct if is_long else 1 + stop_pct), 2)
        limit_price = round(fill_price * (1 + tp_pct   if is_long else 1 - tp_pct),   2)
        return stop_price, limit_price

    def _get_account_value(self, ib) -> float:
        # Always use the configured equity cap — never the raw IBKR paper balance.
        # IBKR paper accounts start at EUR 1M+ which would massively over-size positions.
        # The configured default_account_equity (e.g. EUR 4,000) is the true risk budget.
        return self.default_account_equity

    def _get_connection(self):
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
            # Clean up any previous handle before each attempt (including first,
            # in case self._ib was left over from a prior call).
            if self._ib is not None:
                try:
                    self._ib.disconnect()
                except Exception:
                    pass
                self._ib = None
            if backoff:
                time.sleep(backoff)
            try:
                self._ib = IB()
                self._ib.connect(self.host, self.port, clientId=1)
                logger.info("[ARES] Connected to IB %s:%d (attempt %d)", self.host, self.port, attempt + 1)
                return self._ib
            except Exception as exc:
                last_exc = exc
                logger.warning("[ARES] Connect attempt %d failed: %s", attempt + 1, exc)

        raise RuntimeError(f"[ARES] Could not connect to IB {self.host}:{self.port} after 3 attempts: {last_exc}")

    @staticmethod
    def _null_result() -> TradeResult:
        return TradeResult(order_id=str(uuid.uuid4())[:8], symbol="", side="",
                           fill_price=None, qty=0, status="skipped")

    @staticmethod
    def _error_result(symbol: str, exc: Exception) -> TradeResult:
        return TradeResult(order_id=str(uuid.uuid4())[:8], symbol=symbol, side="",
                           fill_price=None, qty=0, status=f"error: {exc}")
