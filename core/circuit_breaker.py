"""
core/circuit_breaker.py — Per-agent circuit breaker.

If an agent fails N times within a window, its circuit opens:
  - ZEUS degrades gracefully (uses defaults, skips that stage)
  - Watchdog keeps trying to restart the agent
  - Circuit resets automatically after the reset timeout

States: CLOSED (normal) → OPEN (failing) → HALF_OPEN (testing recovery)
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Callable, Optional, TypeVar

from core.types import AgentHealth

logger = logging.getLogger("circuit_breaker")

T = TypeVar("T")


class CircuitOpenError(Exception):
    """Raised when a call is attempted on an open circuit."""


class CircuitBreaker:
    """
    Thread-safe per-agent circuit breaker.

    Usage:
        cb = CircuitBreaker()
        with cb.guard("hades"):
            result = hades.filter(signal)   # if this raises, failure is counted
    """

    def __init__(
        self,
        failure_threshold: int   = 3,     # failures within window → OPEN
        window_seconds:    int   = 300,   # 5-minute rolling window
        reset_timeout:     int   = 120,   # seconds before HALF_OPEN attempt
    ):
        self._threshold  = failure_threshold
        self._window     = timedelta(seconds=window_seconds)
        self._reset_to   = timedelta(seconds=reset_timeout)
        self._lock       = threading.RLock()
        self._failures:  dict[str, deque]   = defaultdict(deque)  # agent → timestamps
        self._opened_at: dict[str, Optional[datetime]] = defaultdict(lambda: None)
        self._state:     dict[str, str]     = defaultdict(lambda: "CLOSED")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def call(self, agent_name: str, fn: Callable[[], T], fallback: T) -> T:
        """
        Execute fn() with circuit protection.
        Returns fallback value if circuit is OPEN.
        """
        self._prune(agent_name)

        state = self._get_state(agent_name)
        if state == "OPEN":
            logger.warning("[CB] Circuit OPEN for %s — using fallback.", agent_name)
            return fallback

        try:
            result = fn()
            self._on_success(agent_name)
            return result
        except Exception as exc:
            self._on_failure(agent_name)
            new_state = self._get_state(agent_name)
            if new_state == "OPEN":
                logger.error(
                    "[CB] Circuit OPENED for %s after %d failures — %s",
                    agent_name, self._threshold, exc,
                )
            return fallback

    def health(self, agent_name: str) -> AgentHealth:
        state = self._get_state(agent_name)
        if state == "CLOSED":
            return AgentHealth.HEALTHY
        if state == "HALF_OPEN":
            return AgentHealth.DEGRADED
        return AgentHealth.FAILED

    def reset(self, agent_name: str) -> None:
        with self._lock:
            self._failures[agent_name].clear()
            self._opened_at[agent_name] = None
            self._state[agent_name] = "CLOSED"
        logger.info("[CB] Circuit manually reset for %s.", agent_name)

    def status(self) -> dict[str, dict[str, str]]:
        with self._lock:
            return {name: {"state": self._get_state(name)} for name in self._state}

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_state(self, agent: str) -> str:
        with self._lock:
            if self._state[agent] == "OPEN":
                opened = self._opened_at[agent]
                if opened and datetime.utcnow() - opened >= self._reset_to:
                    self._state[agent] = "HALF_OPEN"
                    logger.info("[CB] Circuit HALF_OPEN for %s — testing recovery.", agent)
            return self._state[agent]

    def _on_success(self, agent: str) -> None:
        with self._lock:
            if self._state[agent] in ("HALF_OPEN", "OPEN"):
                logger.info("[CB] Circuit CLOSED for %s — recovery confirmed.", agent)
            self._state[agent] = "CLOSED"
            self._failures[agent].clear()
            self._opened_at[agent] = None

    def _on_failure(self, agent: str) -> None:
        now = datetime.utcnow()
        with self._lock:
            self._failures[agent].append(now)
            self._prune_locked(agent, now)
            if len(self._failures[agent]) >= self._threshold:
                self._state[agent] = "OPEN"
                self._opened_at[agent] = now

    def _prune(self, agent: str) -> None:
        with self._lock:
            self._prune_locked(agent, datetime.utcnow())

    def _prune_locked(self, agent: str, now: datetime) -> None:
        cutoff = now - self._window
        q = self._failures[agent]
        while q and q[0] < cutoff:
            q.popleft()
