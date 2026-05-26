"""
Agent 6 — Argus: Portfolio Monitor & Kill Switch
The hundred-eyed giant — watches everything, never sleeps.
Drawdown tracking, kill switch, Telegram alerts, outcome backfill.
Imports only from core.types — never from other agents.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

import requests

from core.types import AgentHealth
from core.agent_knowledge import AgentKnowledgeBase
from core.shadow_learning import OutcomeResolver

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


@dataclass
class PortfolioState:
    snapshots:           list[PositionSnapshot] = field(default_factory=list)
    total_equity:        float                  = 0.0
    peak_equity:         float                  = 0.0
    current_drawdown_pct: float                 = 0.0
    refreshed_at:        Optional[datetime]     = None


class ArgusAgent:
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
        ib_port:                int                            = 4004,  # socat bridge (4004→127.0.0.1:4002)
    ):
        self.max_drawdown_pct  = max_drawdown_pct
        self._on_kill          = on_kill
        self._alert_fn         = alert_fn
        self._telegram_token   = telegram_token   or os.getenv("TELEGRAM_BOT_TOKEN")
        self._telegram_chat_id = telegram_chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self._state            = PortfolioState(total_equity=default_account_equity,
                                               peak_equity=default_account_equity)
        self._ib               = None
        self._ib_host          = ib_host
        self._ib_port          = ib_port
        self._milestone        = milestone_manager
        self._default_equity   = default_account_equity
        self.kb                = AgentKnowledgeBase("argus")
        self.outcome_resolver  = OutcomeResolver()

    def set_knowledge_base(self, knowledge_base) -> None:
        """Wire the shared KB into the OutcomeResolver after construction."""
        self.outcome_resolver = OutcomeResolver(knowledge_base=knowledge_base)

    def health(self) -> AgentHealth:
        return AgentHealth.HEALTHY

    def refresh(self) -> PortfolioState:
        try:
            ib           = self._get_connection()
            self._state  = self._build_state(ib)
        except Exception as exc:
            logger.warning("[ARGUS] Refresh failed (no IB?): %s", exc)
            return self._state

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

    def open_position_count(self) -> int:
        return len(self._state.snapshots)

    def portfolio_state(self) -> PortfolioState:
        return self._state

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

    def _build_state(self, ib) -> PortfolioState:
        equity = self._default_equity
        for av in ib.accountValues():
            if av.tag == "NetLiquidationByCurrency" and av.currency == "BASE":
                equity = float(av.value)
                break

        snapshots: list[PositionSnapshot] = []
        for pos in ib.portfolio():
            if pos.position == 0:
                continue
            cost = pos.averageCost or 1.0
            qty = abs(pos.position)
            denom = qty * cost
            unrealized_pnl_pct = pos.unrealizedPNL / denom if denom != 0 else 0.0
            snapshots.append(PositionSnapshot(
                symbol             = pos.contract.symbol,
                side               = "LONG" if pos.position > 0 else "SHORT",
                qty                = qty,
                avg_cost           = cost,
                current_price      = pos.marketPrice,
                unrealized_pnl     = pos.unrealizedPNL,
                unrealized_pnl_pct = unrealized_pnl_pct,
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

    def _persist_to_supabase(self) -> None:
        try:
            import core.supabase_client as supa
            supa.upsert_portfolio_state({
                "total_equity":         self._state.total_equity,
                "peak_equity":          self._state.peak_equity,
                "current_drawdown_pct": self._state.current_drawdown_pct,
                "open_positions":       len(self._state.snapshots),
                "paper_trading":        True,
                "refreshed_at":         self._state.refreshed_at.isoformat(),
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
                        "refreshed_at":       self._state.refreshed_at.isoformat(),
                    }
                    for s in self._state.snapshots
                ]
                supa.upsert_portfolio_positions(positions)
        except Exception as exc:
            logger.warning("[ARGUS] Supabase persist failed: %s", exc)

    def _get_connection(self):
        import asyncio
        asyncio.set_event_loop(asyncio.new_event_loop())
        from ib_insync import IB  # eventkit imports event loop at module level
        connected = False
        if self._ib is not None:
            try:
                connected = self._ib.isConnected()
            except Exception:
                connected = False
        if not connected:
            self._ib = IB()
            self._ib.connect(self._ib_host, self._ib_port, clientId=2)
        return self._ib
