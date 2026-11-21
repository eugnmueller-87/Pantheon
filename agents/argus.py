"""
Agent 6 — Argus: Portfolio Monitor & Kill Switch
The hundred-eyed giant — watches everything, never sleeps.
Drawdown tracking, kill switch, Telegram alerts, outcome backfill.
Reads positions / closing fills through a core.broker.BrokerAdapter
(IbkrAdapter clientId 2 by default; Zeus injects the shared CTraderAdapter).
Imports only from core — never from other agents.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

import requests

from core.agent_knowledge import AgentKnowledgeBase
from core.broker import BrokerAdapter
from core.brokers.ibkr import IbkrAdapter
from core.shadow_learning import OutcomeResolver
from core.types import AgentHealth

logger = logging.getLogger("argus")

_USE_SUPABASE = bool(
    os.getenv("SUPABASE_URL") and
    os.getenv("SUPABASE_SERVICE_ROLE_KEY")
)


@dataclass
class PositionSnapshot:
    symbol:              str
    side:                str
    qty:                 float
    avg_cost:            float
    current_price:       float
    unrealized_pnl:      float
    unrealized_pnl_pct:  float
    stop_loss_price:     Optional[float] = None
    take_profit_price:   Optional[float] = None


@dataclass
class PortfolioState:
    snapshots:           list[PositionSnapshot] = field(default_factory=list)
    total_equity:        float                  = 0.0
    peak_equity:         float                  = 0.0
    current_drawdown_pct: float                 = 0.0
    refreshed_at:        Optional[datetime]     = None


class ArgusAgent:
    IB_CLIENT_ID = 2

    def __init__(
        self,
        max_drawdown_pct:       float                          = 0.08,
        on_kill:                Optional[Callable[[str], None]] = None,
        alert_fn:               Optional[Callable[[str], None]] = None,
        telegram_token:         Optional[str]                  = None,
        telegram_chat_id:       Optional[str]                  = None,
        milestone_manager = None,   # MilestoneManager injected by ZEUS
        default_account_equity: float                          = 100_000.0,
        ib_host:                str                            = "ibgateway",
        ib_port:                int                            = 4002,
        mock:                   bool                           = False,  # skip IB, track positions in-memory only
        broker:                 Optional[BrokerAdapter]        = None,   # default: IbkrAdapter(clientId=2)
    ):
        self.max_drawdown_pct  = max_drawdown_pct
        self._on_kill          = on_kill
        self._alert_fn         = alert_fn
        self._telegram_token   = telegram_token   or os.getenv("TELEGRAM_BOT_TOKEN")
        self._telegram_chat_id = telegram_chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self._state            = PortfolioState(total_equity=default_account_equity,
                                               peak_equity=default_account_equity)
        self._ib_host          = ib_host
        self._ib_port          = ib_port
        self._mock             = mock
        self.broker: BrokerAdapter = broker or IbkrAdapter(
            host=ib_host, port=ib_port, client_id=self.IB_CLIENT_ID, log_prefix="[ARGUS]",
        )
        self._milestone        = milestone_manager
        self._default_equity   = default_account_equity
        self.kb                = AgentKnowledgeBase("argus")
        self.outcome_resolver  = OutcomeResolver()

    def set_knowledge_base(self, knowledge_base) -> None:
        """Wire the shared KB into the OutcomeResolver after construction."""
        self.outcome_resolver = OutcomeResolver(knowledge_base=knowledge_base)

    @property
    def _ib(self):
        """Back-compat: the raw ib_async handle on the IBKR path (None otherwise)."""
        return getattr(self.broker, "_ib", None)

    def health(self) -> AgentHealth:
        if self._mock:
            return AgentHealth.HEALTHY
        try:
            return AgentHealth.HEALTHY if self.broker.health() else AgentHealth.DEGRADED
        except Exception:
            return AgentHealth.DEGRADED

    def refresh(self) -> PortfolioState:
        if self._mock:
            # Mock mode — no IB connection, keep in-memory state as-is
            logger.debug("[ARGUS] Mock mode — skipping IB refresh, using in-memory state")
        else:
            try:
                ib           = self._get_connection()
                self._state  = self._build_state(ib)
                # Backfill outcomes for trades the broker's bracket has closed.
                # DB-driven + best-effort: never let it break the portfolio tick.
                self._resolve_closed_outcomes(ib)
            except Exception as exc:
                logger.warning("[ARGUS] %s refresh failed (%s) — falling back to Supabase equity",
                               self.broker.name, exc)
                # Recompute equity from Supabase positions so the dashboard
                # reflects real cash (starting_equity − cost_basis + market_value)
                # rather than always showing the raw starting_equity figure.
                fallback_equity = self._supabase_equity_fallback()
                peak = max(self._state.peak_equity, fallback_equity)
                drawdown = max(0.0, (peak - fallback_equity) / peak) if peak > 0 else 0.0
                self._state.total_equity        = fallback_equity
                self._state.peak_equity         = peak
                self._state.current_drawdown_pct = drawdown

        # Update milestone — adjusts risk params + fires vault alert if crossed
        if self._milestone:
            crossed = self._milestone.update(self._state.total_equity)
            if crossed:
                logger.info("[ARGUS] Milestone crossed → %s", crossed.value)
            # Use milestone's current kill switch threshold (overrides default)
            self.max_drawdown_pct = self._milestone.config.drawdown_kill_pct

        self._check_drawdown()
        self._state.refreshed_at = datetime.now(timezone.utc)

        if _USE_SUPABASE:
            self._persist_to_supabase()

        logger.info("[ARGUS] equity=%.2f drawdown=%.2f%% positions=%d",
                    self._state.total_equity,
                    self._state.current_drawdown_pct * 100,
                    len(self._state.snapshots))
        return self._state

    def _resolve_closed_outcomes(self, ib=None) -> None:
        """Detect trades the IB bracket has closed and backfill pnl_pct/hit.

        Restart-safe: reconstructs the open set from the DB (not in-memory
        tracking), so it resolves outcomes even after a container restart.
        Best-effort — any failure is logged and never breaks the portfolio tick.
        """
        if self._mock or not _USE_SUPABASE:
            return
        try:
            import core.supabase_client as supa
            from config.settings import load_settings
            open_db = supa.fetch_open_trades()
            if not open_db:
                return
            open_symbols  = {s.symbol for s in self._state.snapshots}
            closing_fills = self._closing_fills_by_symbol(ib)
            allow_approx  = bool(
                load_settings().get("outcome_resolution", {}).get("allow_approximate_exit", False)
            )
            resolved = self.outcome_resolver.resolve_from_db_and_portfolio(
                open_db, open_symbols, closing_fills, None, allow_approx,
            )
            if resolved:
                logger.info("[ARGUS] Backfilled %d closed trade outcome(s)", len(resolved))
        except Exception as exc:
            logger.warning("[ARGUS] outcome resolution failed: %s", exc)

    def _closing_fills_by_symbol(self, ib=None) -> dict[str, list]:
        """Map symbol -> [{price, qty, time}] of broker closing executions, oldest-first.

        IBKR (long-only): SELL executions ('SLD'). cTrader: deals that closed a
        position. Best-effort — returns {} on any error so a fills hiccup never
        breaks the tick.
        """
        try:
            return self.broker.closing_fills()
        except Exception as exc:
            logger.debug("[ARGUS] could not read %s fills: %s", self.broker.name, exc)
            return {}

    def open_position_count(self) -> int:
        return len(self._state.snapshots)

    def portfolio_state(self) -> PortfolioState:
        return self._state

    def add_mock_position(
        self,
        symbol: str,
        side: str,
        qty: float,
        avg_cost: float,
        current_price: float,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
    ) -> None:
        """Register a simulated trade into in-memory state (mock mode only)."""
        if not self._mock:
            return
        unrealized_pnl = qty * (current_price - avg_cost) * (1 if side == "LONG" else -1)
        unrealized_pnl_pct = unrealized_pnl / (qty * avg_cost) if avg_cost else 0.0
        # Remove existing position for same symbol if present
        self._state.snapshots = [s for s in self._state.snapshots if s.symbol != symbol]
        self._state.snapshots.append(PositionSnapshot(
            symbol=symbol, side=side, qty=qty,
            avg_cost=avg_cost, current_price=current_price,
            unrealized_pnl=unrealized_pnl, unrealized_pnl_pct=unrealized_pnl_pct,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
        ))
        logger.info("[ARGUS] Mock position added: %s %s x%.0f @ %.4f", side, symbol, qty, avg_cost)
        if _USE_SUPABASE:
            self._persist_to_supabase()

    def remove_mock_position(self, symbol: str) -> None:
        """Remove a closed simulated position (mock mode only)."""
        if not self._mock:
            return
        self._state.snapshots = [s for s in self._state.snapshots if s.symbol != symbol]
        logger.info("[ARGUS] Mock position removed: %s", symbol)
        if _USE_SUPABASE:
            self._persist_to_supabase()

    def send_alert(self, message: str) -> None:
        # External alert_fn first (used by Watchdog)
        if self._alert_fn:
            try:
                self._alert_fn(message)
            except Exception:
                pass
        # Telegram
        if self._telegram_token and self._telegram_chat_id:
            try:
                requests.post(
                    f"https://api.telegram.org/bot{self._telegram_token}/sendMessage",
                    json={"chat_id": self._telegram_chat_id, "text": message},
                    timeout=5,
                )
            except Exception as exc:
                logger.warning("[ARGUS] Telegram failed: %s", exc)
        else:
            logger.info("[ARGUS] Alert (no Telegram): %s", message)

    def committed_capital(self) -> float:
        """Cost basis of currently OPEN positions, read from the persisted DB
        (portfolio_positions) — broker-independent, so it stays correct even when
        IB Gateway is down. This is the money already deployed in the market."""
        try:
            import core.supabase_client as supa
            rows = supa.get_client().table("portfolio_positions") \
                .select("qty,avg_cost") \
                .is_("closed_at", "null") \
                .execute()
            return sum(float(r["qty"]) * float(r["avg_cost"]) for r in (rows.data or []))
        except Exception as exc:
            logger.warning("[ARGUS] committed_capital query failed: %s", exc)
            # Fall back to in-memory live snapshots if the DB is unreachable.
            return sum(s.qty * s.avg_cost for s in self._state.snapshots)

    def available_cash(self) -> float:
        """Equity not yet deployed = starting equity − committed capital.
        The wallet ZEUS must check before approving a new trade."""
        return max(0.0, self._default_equity - self.committed_capital())

    def _supabase_equity_fallback(self) -> float:
        """
        When IB is unreachable, compute equity from Supabase:
          available_cash = starting_equity − sum(qty × avg_cost) for open positions
          total_equity   = available_cash + sum(qty × current_price)
        Falls back to self._default_equity if Supabase is also unavailable.
        """
        try:
            import core.supabase_client as supa
            rows = supa.get_client().table("portfolio_positions") \
                .select("qty,avg_cost,current_price,side") \
                .is_("closed_at", "null") \
                .execute()
            positions = rows.data or []
            cost_basis   = sum(float(r["qty"]) * float(r["avg_cost"])    for r in positions)
            market_value = sum(float(r["qty"]) * float(r["current_price"]) for r in positions)
            available_cash = self._default_equity - cost_basis
            total_equity   = available_cash + market_value
            logger.debug(
                "[ARGUS] Supabase equity fallback: cash=%.2f mktval=%.2f total=%.2f",
                available_cash, market_value, total_equity,
            )
            return total_equity
        except Exception as exc:
            logger.warning("[ARGUS] Supabase equity fallback failed: %s", exc)
            return self._default_equity

    def _build_state(self, ib=None) -> PortfolioState:
        from config.settings import load_settings
        cfg = load_settings()
        sl_pct = cfg.get("stop_loss_pct", 0.03)
        tp_pct = cfg.get("take_profit_pct", 0.06)

        # Always use the configured equity cap — never the raw IBKR paper balance.
        # IBKR paper accounts start at USD 100k+ which would misrepresent our actual
        # risk budget (default_account_equity, e.g. EUR 4,000).
        # We track our own P&L from the configured starting capital, not IB's notional.
        equity = self._default_equity

        snapshots: list[PositionSnapshot] = []
        for pos in self.broker.positions():
            if pos.qty == 0:
                continue
            cost = pos.avg_cost or 1.0
            qty = abs(pos.qty)
            denom = qty * cost
            unrealized_pnl_pct = pos.unrealized_pnl / denom if denom != 0 else 0.0
            is_long = pos.side == "LONG"
            sl_price = round(cost * (1 - sl_pct if is_long else 1 + sl_pct), 4)
            tp_price = round(cost * (1 + tp_pct if is_long else 1 - tp_pct), 4)
            snapshots.append(PositionSnapshot(
                symbol             = pos.symbol,
                side               = pos.side,
                qty                = qty,
                avg_cost           = cost,
                current_price      = pos.current_price,
                unrealized_pnl     = pos.unrealized_pnl,
                unrealized_pnl_pct = unrealized_pnl_pct,
                stop_loss_price    = sl_price,
                take_profit_price  = tp_price,
            ))

        peak = max(self._state.peak_equity, equity)
        drawdown = max(0.0, (peak - equity) / peak) if peak > 0 else 0.0
        return PortfolioState(
            snapshots=snapshots, total_equity=equity,
            peak_equity=peak, current_drawdown_pct=drawdown,
        )

    def _check_drawdown(self) -> None:
        dd = self._state.current_drawdown_pct
        if dd >= self.max_drawdown_pct:
            msg = (f"ZEUS EMERGENCY HALT\n"
                   f"Drawdown {dd*100:.1f}% >= limit {self.max_drawdown_pct*100:.1f}%\n"
                   f"All trading suspended.")
            logger.critical("[ARGUS] %s", msg)
            self.send_alert(msg)
            if self._on_kill:
                self._on_kill(f"drawdown {dd*100:.1f}%")

    def _open_position_count(self, supa) -> int:
        """Authoritative open-position count from portfolio_positions.

        len(self._state.snapshots) comes from ib.portfolio(), which can briefly
        return empty right after the gateway (re)starts or on a transient hiccup.
        Writing that 0 into portfolio_state desyncs it from the real open rows
        (the bug: state said 0 while NVDA/LIN were open). The DB's open rows are
        the source of truth, so prefer them and fall back to the live snapshot.
        """
        try:
            rows = supa.get_client().table("portfolio_positions") \
                .select("symbol", count="exact") \
                .is_("closed_at", "null") \
                .execute()
            db_count = rows.count if rows.count is not None else len(rows.data or [])
            # Trust the DB unless the live snapshot legitimately shows more
            # (e.g. a fill that hasn't been persisted yet this cycle).
            return max(db_count, len(self._state.snapshots))
        except Exception:
            return len(self._state.snapshots)

    def _persist_to_supabase(self) -> None:
        try:
            import core.supabase_client as supa
            open_positions = self._open_position_count(supa)
            supa.upsert_portfolio_state({
                "total_equity":         self._state.total_equity,
                "peak_equity":          self._state.peak_equity,
                "current_drawdown_pct": self._state.current_drawdown_pct,
                "open_positions":       open_positions,
                "paper_trading":        True,
                "refreshed_at":         self._state.refreshed_at.isoformat(),
            })
            # Append to the equity time-series (008) — portfolio_state is a
            # singleton (latest only), so the equity curve needs its own table.
            supa.insert_equity_snapshot({
                "total_equity":         self._state.total_equity,
                "peak_equity":          self._state.peak_equity,
                "current_drawdown_pct": self._state.current_drawdown_pct,
                "open_positions":       open_positions,
                "paper_trading":        True,
                "recorded_at":          self._state.refreshed_at.isoformat(),
            })
            if self._state.snapshots:
                positions = [
                    {
                        "symbol":             s.symbol,
                        "side":               s.side,
                        "qty":                s.qty,
                        "avg_cost":           s.avg_cost,
                        "current_price":      s.current_price,
                        "unrealized_pnl":     s.unrealized_pnl,
                        "unrealized_pnl_pct": s.unrealized_pnl_pct,
                        "stop_loss":          s.stop_loss_price,
                        "take_profit":        s.take_profit_price,
                        "refreshed_at":       self._state.refreshed_at.isoformat(),
                    }
                    for s in self._state.snapshots
                ]
                supa.upsert_portfolio_positions(positions)
        except Exception as exc:
            logger.warning("[ARGUS] Supabase persist failed: %s", exc)

    def _get_connection(self):
        """Ensure a broker session (IBKR: reconnect w/ clientId 2, 3 attempts)."""
        return self.broker.connect()
