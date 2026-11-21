"""
Agent 5 — Ares: Trade Execution
God of decisive action — executes the strike.
Places bracket orders through a core.broker.BrokerAdapter:
  - IbkrAdapter (default) — Interactive Brokers via ib_async, clientId 1
  - CTraderAdapter        — cTrader Open API (IC Markets EU), injected by Zeus
Imports only from core — never from other agents.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from core.agent_knowledge import AgentKnowledgeBase
from core.broker import BrokerAdapter, BrokerConnectivityError, BrokerRejectedError
from core.brokers.ibkr import IbkrAdapter
from core.types import AgentHealth, SignalCategory, SizedSignal, TradeResult

# Market timezones — DST-correct via the IANA database (stdlib zoneinfo).
_ET  = ZoneInfo("America/New_York")
_CET = ZoneInfo("Europe/Berlin")

logger = logging.getLogger("ares")

# How long an approved trade stays retryable before it's expired as stale.
# 45 min: safely within a single NYSE session, safely before the gateway
# daily restart windows (11:45 PM ET / 06:00 ET).
PENDING_ORDER_MAX_AGE_MIN: int = 120

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
    if isinstance(exc, BrokerConnectivityError):
        return True
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
    if isinstance(exc, BrokerRejectedError):
        return True
    msg = str(exc).lower()
    return any(t.lower() in msg for t in _TERMINAL_IB_ERRORS)


def _same_session(t1: datetime, t2: datetime) -> bool:
    """Return True if both timestamps fall within the same trading session.

    Compares the ET date (NYSE) and the CET date (XETRA); same session if either
    market's local date matches. Uses zoneinfo for correct DST handling — the old
    fixed `timedelta(hours=4)` was EDT-only and wrong Nov–Mar, which could flip
    the ET date across midnight UTC in winter and expire pending orders early.
    """
    # Tolerate naive timestamps (assume UTC) so callers passing either work.
    if t1.tzinfo is None:
        t1 = t1.replace(tzinfo=timezone.utc)
    if t2.tzinfo is None:
        t2 = t2.replace(tzinfo=timezone.utc)
    d1_et  = t1.astimezone(_ET).date()
    d2_et  = t2.astimezone(_ET).date()
    d1_cet = t1.astimezone(_CET).date()
    d2_cet = t2.astimezone(_CET).date()
    return d1_et == d2_et or d1_cet == d2_cet


def _resolve_symbol_for_market(supplier: str, current_ticker: str) -> tuple[str, str]:
    """Return (ticker, exchange) to use based on which market is currently open.

    If NYSE is open (or no supplier name available), return the original ticker
    with exchange SMART.  If only XETRA is open and a XETRA ticker exists for
    the supplier, return the XETRA ticker with exchange XETRA.
    """
    from core.supabase_client import get_ticker_for_market
    from main import _is_nyse_open, _is_xetra_open

    if _is_nyse_open() or not supplier:
        return current_ticker, "SMART"

    if _is_xetra_open():
        xetra_ticker = get_ticker_for_market(supplier, preferred_exchange="XETRA")
        if xetra_ticker:
            return xetra_ticker, "XETRA"

    return current_ticker, "SMART"


class AresAgent:
    IB_PAPER_PORT = 4002
    IB_LIVE_PORT  = 4001
    IB_CLIENT_ID  = 1

    def __init__(
        self,
        paper: bool = True,
        host: str = "ibgateway",
        port: int | None = None,
        stop_loss_pct: float = 0.03,
        take_profit_pct: float = 0.06,
        default_account_equity: float = 100_000.0,
        broker: BrokerAdapter | None = None,
    ):
        self.paper                  = paper
        self.host                   = host
        self.port                   = port if port is not None else (self.IB_PAPER_PORT if paper else self.IB_LIVE_PORT)
        self.stop_loss_pct          = stop_loss_pct
        self.take_profit_pct        = take_profit_pct
        self.default_account_equity = default_account_equity
        # Default venue is IBKR (connect-per-call, clientId 1) — unchanged behaviour.
        self.broker: BrokerAdapter  = broker or IbkrAdapter(
            host=self.host, port=self.port, client_id=self.IB_CLIENT_ID, log_prefix="[ARES]",
        )
        self._pending: list[str]    = []   # placed order IDs (for cancel_all_pending)
        self.kb = AgentKnowledgeBase("ares")
        if self.broker.name == "ibkr":
            logger.info("[ARES] %s mode — IBKR port %d", "PAPER" if paper else "LIVE", self.port)
        else:
            logger.info("[ARES] %s mode — broker=%s", "PAPER" if paper else "LIVE", self.broker.name)

    @property
    def _ib(self):
        """Back-compat: the raw ib_async handle on the IBKR path (None otherwise)."""
        return getattr(self.broker, "_ib", None)

    def health(self) -> AgentHealth:
        try:
            return AgentHealth.HEALTHY if self.broker.health() else AgentHealth.DEGRADED
        except Exception:
            return AgentHealth.DEGRADED

    # ------------------------------------------------------------------
    # Venue-specific routing
    # ------------------------------------------------------------------

    def _resolve_symbol(self, sized: SizedSignal) -> tuple[str, str]:
        """(symbol, exchange) for the configured venue.

        IBKR: NYSE/XETRA routing via the ticker map (unchanged).
        Other venues: map the pipeline ticker onto the venue universe (e.g.
        cTrader FX/index CFDs). Unmapped tickers keep their raw name and are
        rejected by the adapter as a terminal error — never queued.
        """
        raw_ticker = sized.affected_tickers[0]
        if self.broker.name == "ibkr":
            return _resolve_symbol_for_market(sized.supplier, raw_ticker)
        return (self.broker.resolve_symbol(raw_ticker) or raw_ticker), self.broker.name.upper()

    def _resolve_side(self, sized: SizedSignal) -> str:
        explicit_side = getattr(sized, "side", None)
        if explicit_side in ("BUY", "SELL"):
            return explicit_side
        if not getattr(self.broker, "supports_short", False):
            # Long-only: always BUY on new signals — we never short stocks we don't hold.
            # Zeus LLM can set sized.side="SELL" explicitly to close an existing long.
            return "BUY"
        # Two-way venue (FX / index CFDs): a technical signal carries its own
        # direction (core/fx_signals.py → RawSignal.trade_side) …
        trade_side = getattr(sized, "trade_side", "") or ""
        if trade_side in ("BUY", "SELL"):
            return trade_side
        # … otherwise direction follows the signal category.
        return "SELL" if sized.category == SignalCategory.SUPPLIER_DISRUPTION else "BUY"

    # ------------------------------------------------------------------
    # Primary entry point — called from Zeus Stage 6
    # ------------------------------------------------------------------

    def place(self, sized: SizedSignal) -> TradeResult:
        if not sized.affected_tickers:
            logger.warning("[ARES] No tickers in signal %s", sized.signal_id)
            return self._null_result()
        symbol, exchange = self._resolve_symbol(sized)
        try:
            ib     = self._get_connection()
            result = self._place_bracket(ib, symbol, sized, exchange=exchange)
            self._pending.append(result.order_id)
            return result
        except Exception as exc:
            logger.error("[ARES] Order failed: %s", exc)
            if _is_connectivity_error(exc):
                return self._enqueue(sized, exc)
            # Terminal IB rejection or unknown error — do not queue.
            return self._error_result(symbol, exc)

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

        symbol, exchange = self._resolve_symbol(sized)
        side        = self._resolve_side(sized)
        approved_at = datetime.now(timezone.utc)
        expires_at  = approved_at + timedelta(minutes=PENDING_ORDER_MAX_AGE_MIN)

        payload = {
            "signal_id":         sized.signal_id,
            "symbol":            symbol,
            "exchange":          exchange,
            "side":              side,
            "position_size_pct": sized.position_size_pct,
            "confidence":        sized.confidence,
        }
        # Technical (cTrader) signals: keep the ATR stop so a retry sizes and
        # brackets exactly like the original attempt would have.
        if getattr(sized, "stop_distance", None):
            payload["stop_distance"] = sized.stop_distance
            payload["reward_risk"]   = sized.reward_risk

        db.enqueue_pending_order(
            signal_id=sized.signal_id,
            symbol=symbol,
            side=side,
            payload=payload,
            approved_at=approved_at,
            expires_at=expires_at,
        )
        logger.info(
            "[ARES] Trade queued (%s unreachable) — signal=%s symbol=%s expires=%s",
            self.broker.name, sized.signal_id, symbol, expires_at.isoformat(),
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
            stop_distance=payload.get("stop_distance"),
            reward_risk=payload.get("reward_risk"),
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
                stop_distance=payload.get("stop_distance"),
                reward_risk=payload.get("reward_risk"),
            )
        # Reconcile re-uses the exchange that was active when the order was queued.
        # The symbol in the payload is already the exchange-specific ticker (e.g. SIE.DE).
        exchange = payload.get("exchange", "SMART")
        return self._place_bracket(ib, symbol, sized, exchange=exchange)

    def cancel_all_pending(self) -> None:
        try:
            self._get_connection()
            n = self.broker.cancel_all_pending()
            logger.warning("[ARES] Cancelled %d open order(s) on %s.", n, self.broker.name)
        except Exception as exc:
            logger.error("[ARES] cancel_all_pending failed: %s", exc)

    def _place_bracket(
        self,
        ib,
        symbol: str,
        sized: SizedSignal,
        exchange: str = "SMART",
    ) -> TradeResult:
        """Price, size and place one bracket via the broker adapter.

        `ib` is the session handle returned by _get_connection(); kept in the
        signature for back-compat — the adapter owns the actual session.
        """
        mid = self.broker.get_quote(symbol, exchange=exchange)
        if mid is None or mid <= 0:
            raise ValueError(f"Could not price {symbol}")

        account_val = self._get_account_value(ib)
        if account_val <= 0:
            raise ValueError(f"Invalid account equity: {account_val}")

        side    = self._resolve_side(sized)
        is_long = side == "BUY"
        decimals = self.broker.price_decimals(symbol)

        signal_stop = getattr(sized, "stop_distance", None)
        if signal_stop and signal_stop > 0 and self.broker.name != "ibkr":
            # Market-appropriate (ATR) stop carried by the FX/index signal —
            # never the 3% stock default (≈300 pips on EURUSD).
            from core.fx_signals import bracket_from_distance
            rr = float(getattr(sized, "reward_risk", None) or self.take_profit_pct / self.stop_loss_pct)
            stop_price, limit_price = bracket_from_distance(mid, float(signal_stop), is_long, rr, decimals)
        else:
            stop_price, limit_price = self._compute_stops(symbol, mid, is_long, decimals=decimals)

        if getattr(self.broker, "sizing_mode", "notional") == "risk":
            # cTrader: size from the STOP — lose ≈ risk_pct of equity if it is hit.
            # Pythia / ZEUS / seniority sizes act as a ceiling on that fraction.
            if sized.position_size_pct <= 0:
                raise BrokerRejectedError(f"{symbol}: position_size_pct={sized.position_size_pct} — nothing to size")
            if not signal_stop:
                logger.warning(
                    "[ARES] %s has no signal stop — risk-sizing on the fallback %% stop (%s)",
                    symbol, stop_price,
                )
            risk_pct = min(float(self.broker.risk_per_trade_pct), float(sized.position_size_pct))
            sizing = self.broker.size_order_risk(symbol, entry=mid, stop_loss=stop_price, risk_pct=risk_pct)
            qty = sizing.units
            logger.info(
                "[ARES] Risk sizing %s: risk %.2f%% (budget %.2f, at stop %.2f) → %g units, "
                "notional %.2f (%.2fx equity)",
                symbol, risk_pct * 100, sizing.risk_budget, sizing.risk_at_stop, qty,
                sizing.notional, sizing.notional_fraction,
            )
        else:
            # IBKR (unchanged): unlevered notional from the configured budget —
            # the adapter converts it into shares / venue quantity.
            intended_amount = account_val * sized.position_size_pct
            qty = self.broker.size_order(symbol, intended_amount, mid)

        res = self.broker.place_bracket(
            symbol, side, qty, mid, stop_price, limit_price, exchange=exchange,
        )
        logger.info(
            "[ARES] %s %g %s @ %s | SL=%s TP=%s | venue=%s/%s | id=%s",
            side, res.qty, res.symbol, res.entry_price, res.stop_loss_price, res.take_profit_price,
            self.broker.name, exchange, res.order_id,
        )
        return TradeResult(
            order_id=res.order_id, symbol=res.symbol, side=side,
            fill_price=res.entry_price, qty=res.qty,
            stop_loss_price=res.stop_loss_price, take_profit_price=res.take_profit_price,
        )

    def _compute_stops(
        self,
        symbol: str,
        fill_price: float,
        is_long: bool,
        decimals: int = 2,
    ) -> tuple[float, float]:
        """
        Return (stop_price, take_profit_price), rounded to `decimals`
        (2 for IBKR equities; the symbol's digits on cTrader).

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

        stop_price  = round(fill_price * (1 - stop_pct if is_long else 1 + stop_pct), decimals)
        limit_price = round(fill_price * (1 + tp_pct   if is_long else 1 - tp_pct),   decimals)
        return stop_price, limit_price

    def _get_account_value(self, ib) -> float:
        # Always use the configured equity cap — never the raw broker paper/demo balance.
        # IBKR paper accounts start at EUR 1M+ which would massively over-size positions.
        # The configured default_account_equity (e.g. EUR 4,000) is the true risk budget.
        return self.default_account_equity

    def _get_connection(self):
        """Ensure a broker session and return its handle.

        IBKR: tears down the old handle and reconnects (3 attempts, 0/1/2s
        backoff, clientId 1) — exactly the previous behaviour.
        cTrader: no-op when the persistent session is already authorized.
        """
        return self.broker.connect()

    @staticmethod
    def _null_result() -> TradeResult:
        return TradeResult(order_id=str(uuid.uuid4())[:8], symbol="", side="",
                           fill_price=None, qty=0, status="skipped")

    @staticmethod
    def _error_result(symbol: str, exc: Exception) -> TradeResult:
        return TradeResult(order_id=str(uuid.uuid4())[:8], symbol=symbol, side="",
                           fill_price=None, qty=0, status=f"error: {exc}")
