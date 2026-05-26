"""
core/watchdog.py — Agent health monitor + auto-recovery.

Runs as a background daemon thread inside the ZEUS process.
Every 60 seconds it polls every agent's health() method.
If an agent is FAILED, it attempts a restart via the registered
restart callback. Sends Telegram alert on state transitions.

Zero-outage design:
  - Watchdog failure never affects the trading pipeline
  - All watchdog errors are caught and logged, never re-raised
  - Restart callbacks are fire-and-forget — if restart fails, we retry next tick
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from core.types import AgentHealth, HealthReport

logger = logging.getLogger("watchdog")

POLL_INTERVAL   = 60    # seconds between health checks
MAX_RESTARTS    = 5     # max restart attempts before giving up and alerting
RESTART_COOLDOWN = 120  # seconds to wait between restart attempts


@dataclass
class AgentRegistration:
    name:            str
    health_fn:       Callable[[], AgentHealth]
    restart_fn:      Optional[Callable[[], None]] = None
    restart_count:   int                          = 0
    last_restart_at: Optional[datetime]           = None
    last_status:     AgentHealth                  = AgentHealth.HEALTHY


class Watchdog:
    """
    Monitors all registered agents. Runs in a background daemon thread.
    ZEUS calls `watchdog.register(...)` for each agent at startup.
    """

    def __init__(self, alert_fn: Optional[Callable[[str], None]] = None):
        self._agents:   dict[str, AgentRegistration] = {}
        self._alert_fn: Optional[Callable[[str], None]] = alert_fn
        self._thread:   Optional[threading.Thread]   = None
        self._stop_evt: threading.Event               = threading.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(
        self,
        name:       str,
        health_fn:  Callable[[], AgentHealth],
        restart_fn: Optional[Callable[[], None]] = None,
    ) -> None:
        self._agents[name] = AgentRegistration(
            name=name,
            health_fn=health_fn,
            restart_fn=restart_fn,
        )
        logger.info("[WATCHDOG] Registered agent: %s", name)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="watchdog",
            daemon=True,
        )
        self._thread.start()
        logger.info("[WATCHDOG] Started — polling every %ds.", POLL_INTERVAL)

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("[WATCHDOG] Stopped.")

    def poll_now(self) -> list[HealthReport]:
        """Force an immediate health check — useful for /status endpoint."""
        reports = []
        for reg in self._agents.values():
            reports.append(self._check(reg))
        return reports

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop_evt.is_set():
            try:
                for reg in list(self._agents.values()):
                    self._check(reg)
            except Exception as exc:
                logger.error("[WATCHDOG] Unexpected error in poll loop: %s", exc)
            self._stop_evt.wait(timeout=POLL_INTERVAL)

    def _check(self, reg: AgentRegistration) -> HealthReport:
        try:
            status = reg.health_fn()
        except Exception as exc:
            logger.error("[WATCHDOG] health_fn failed for %s: %s", reg.name, exc)
            status = AgentHealth.FAILED

        report = HealthReport(
            agent_name=reg.name,
            status=status,
            checked_at=datetime.now(timezone.utc),
        )

        self._persist_health(reg.name, status)

        if status != reg.last_status:
            self._on_transition(reg, status)
        reg.last_status = status

        if status == AgentHealth.FAILED:
            self._attempt_restart(reg)

        return report

    def _on_transition(self, reg: AgentRegistration, new_status: AgentHealth) -> None:
        msg = (
            f"[WATCHDOG] {reg.name}: {reg.last_status.value} → {new_status.value}"
        )
        logger.warning(msg)
        if new_status == AgentHealth.FAILED:
            self._send_alert(f"ZEUS ALERT: {reg.name} agent FAILED. Auto-restart initiated.")
        elif new_status == AgentHealth.HEALTHY and reg.last_status == AgentHealth.FAILED:
            self._send_alert(f"ZEUS: {reg.name} agent recovered — back to HEALTHY.")

    def _attempt_restart(self, reg: AgentRegistration) -> None:
        if reg.restart_fn is None:
            return
        if reg.restart_count >= MAX_RESTARTS:
            if reg.restart_count == MAX_RESTARTS:
                self._send_alert(
                    f"ZEUS CRITICAL: {reg.name} exceeded {MAX_RESTARTS} restart attempts. Manual intervention required."
                )
                reg.restart_count += 1  # increment past MAX so we don't spam
            return

        now = datetime.now(timezone.utc)
        if reg.last_restart_at:
            elapsed = (now - reg.last_restart_at).total_seconds()
            if elapsed < RESTART_COOLDOWN:
                return

        try:
            logger.warning("[WATCHDOG] Restarting %s (attempt %d)...", reg.name, reg.restart_count + 1)
            reg.restart_fn()
            reg.restart_count += 1
            reg.last_restart_at = now
        except Exception as exc:
            logger.error("[WATCHDOG] Restart of %s failed: %s", reg.name, exc)

    def _persist_health(self, agent_name: str, status: AgentHealth) -> None:
        try:
            import os
            if not (os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY")):
                return
            import core.supabase_client as supa
            supa.insert_agent_health(
                agent_name=agent_name,
                status=status.value,
                message="",
                error_count=0,
            )
        except Exception:
            pass  # Watchdog must never crash the pipeline

    def _send_alert(self, message: str) -> None:
        logger.warning(message)
        if self._alert_fn:
            try:
                self._alert_fn(message)
            except Exception as exc:
                logger.error("[WATCHDOG] Alert send failed: %s", exc)
