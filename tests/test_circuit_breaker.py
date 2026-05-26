"""
Quality gate — Circuit Breaker.

Circuit breakers are the zero-outage mechanism. If they fail to open,
one crashing agent takes down the whole pipeline. If they fail to close,
ZEUS stops trading permanently after one transient network error.
Both failure modes are tested exhaustively.
"""

import time

import pytest

from core.circuit_breaker import CircuitBreaker


@pytest.fixture
def cb():
    return CircuitBreaker(failure_threshold=3, window_seconds=10, reset_timeout=1)


# ── Normal operation ───────────────────────────────────────────────────────────

class TestNormalOperation:
    def test_successful_call_returns_value(self, cb):
        result = cb.call("agent", fn=lambda: 42, fallback=-1)
        assert result == 42

    def test_fallback_not_used_on_success(self, cb):
        result = cb.call("agent", fn=lambda: "real", fallback="fallback")
        assert result == "real"

    def test_multiple_successes_stay_closed(self, cb):
        for _ in range(10):
            cb.call("agent", fn=lambda: True, fallback=False)
        assert cb.status()["agent"]["state"] == "CLOSED"


# ── Opening the circuit ────────────────────────────────────────────────────────

class TestCircuitOpening:
    def test_opens_after_threshold_failures(self, cb):
        for _ in range(3):
            cb.call("agent", fn=lambda: (_ for _ in ()).throw(RuntimeError("fail")), fallback=None)
        assert cb.status()["agent"]["state"] == "OPEN"

    def test_open_circuit_returns_fallback(self, cb):
        for _ in range(3):
            cb.call("agent", fn=lambda: (_ for _ in ()).throw(RuntimeError("fail")), fallback=None)
        result = cb.call("agent", fn=lambda: "should not run", fallback="safe_fallback")
        assert result == "safe_fallback"

    def test_open_circuit_does_not_call_fn(self, cb):
        calls = []
        def fn():
            calls.append(1)
            raise RuntimeError("fail")

        for _ in range(3):
            cb.call("agent", fn=fn, fallback=None)
        calls.clear()
        cb.call("agent", fn=lambda: calls.append("called"), fallback=None)
        assert "called" not in calls

    def test_two_below_threshold_stays_closed(self, cb):
        for _ in range(2):
            cb.call("agent", fn=lambda: (_ for _ in ()).throw(RuntimeError("fail")), fallback=None)
        assert cb.status()["agent"]["state"] == "CLOSED"


# ── Isolation between agents ───────────────────────────────────────────────────

class TestAgentIsolation:
    def test_one_agent_failure_does_not_affect_another(self, cb):
        for _ in range(3):
            cb.call("bad_agent", fn=lambda: (_ for _ in ()).throw(RuntimeError("fail")), fallback=None)
        result = cb.call("good_agent", fn=lambda: "ok", fallback="fallback")
        assert result == "ok"

    def test_each_agent_has_independent_state(self, cb):
        for _ in range(3):
            cb.call("agent_a", fn=lambda: (_ for _ in ()).throw(RuntimeError("fail")), fallback=None)
        assert cb.status()["agent_a"]["state"] == "OPEN"
        assert cb.status().get("agent_b", {}).get("state", "CLOSED") == "CLOSED"


# ── Half-open / recovery ───────────────────────────────────────────────────────

class TestRecovery:
    def test_circuit_moves_to_half_open_after_timeout(self, cb):
        for _ in range(3):
            cb.call("agent", fn=lambda: (_ for _ in ()).throw(RuntimeError("fail")), fallback=None)
        assert cb.status()["agent"]["state"] == "OPEN"
        time.sleep(1.1)  # reset_timeout=1s
        # Probe call — should transition to HALF_OPEN then CLOSED on success
        result = cb.call("agent", fn=lambda: "recovered", fallback="fb")
        assert result == "recovered"

    def test_successful_call_after_half_open_closes_circuit(self, cb):
        for _ in range(3):
            cb.call("agent", fn=lambda: (_ for _ in ()).throw(RuntimeError("fail")), fallback=None)
        time.sleep(1.1)
        cb.call("agent", fn=lambda: "ok", fallback=None)
        assert cb.status()["agent"]["state"] == "CLOSED"

    def test_failed_call_in_half_open_reopens_circuit(self, cb):
        for _ in range(3):
            cb.call("agent", fn=lambda: (_ for _ in ()).throw(RuntimeError("fail")), fallback=None)
        time.sleep(1.1)
        cb.call("agent", fn=lambda: (_ for _ in ()).throw(RuntimeError("still broken")), fallback=None)
        assert cb.status()["agent"]["state"] == "OPEN"


# ── Failure window expiry ──────────────────────────────────────────────────────

class TestWindowExpiry:
    def test_failures_outside_window_dont_count(self):
        """Failures outside the time window should not contribute to threshold."""
        cb = CircuitBreaker(failure_threshold=3, window_seconds=1, reset_timeout=60)
        # Two failures
        for _ in range(2):
            cb.call("agent", fn=lambda: (_ for _ in ()).throw(RuntimeError("fail")), fallback=None)
        time.sleep(1.1)  # window expires
        # One more — should not open (window reset)
        cb.call("agent", fn=lambda: (_ for _ in ()).throw(RuntimeError("fail")), fallback=None)
        assert cb.status()["agent"]["state"] == "CLOSED"


# ── Status dict ────────────────────────────────────────────────────────────────

class TestStatusDict:
    def test_status_returns_dict(self, cb):
        cb.call("a1", fn=lambda: 1, fallback=None)
        status = cb.status()
        assert isinstance(status, dict)
        assert "a1" in status

    def test_status_entry_has_required_keys(self, cb):
        cb.call("a1", fn=lambda: 1, fallback=None)
        entry = cb.status()["a1"]
        assert "state" in entry
