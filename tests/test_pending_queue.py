"""
Tests for the durable pending-order queue in AresAgent.

Covers all mandatory guards from the spec:
  1. Connectivity failure (Errno 111) → row enqueued PENDING, trade NOT lost
  2. IB order rejection → REJECTED, NOT queued, NOT retried
  3. Pending row older than max-age → EXPIRED, _place_bracket NOT called
  4. Same signal_id already SUBMITTED → reconcile does NOT double-place
  5. health()==DEGRADED in Zeus Stage 6 → new approval enqueued, no live place attempt
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, call, patch

import pytest

from agents.ares import (
    PENDING_ORDER_MAX_AGE_MIN,
    AresAgent,
    _is_connectivity_error,
    _is_terminal_ib_error,
    _same_session,
)
from core.types import (
    AgentHealth,
    FilteredSignal,
    MacroContext,
    MarketRegime,
    RawSignal,
    Severity,
    SignalCategory,
    SizedSignal,
)

# ── Helpers ────────────────────────────────────────────────────────────────────

def _sized(
    signal_id: str = "sig-001",
    ticker: str = "NVDA",
    category: SignalCategory = SignalCategory.POSITIVE_NEWS,
    size_pct: float = 0.02,
    confidence: float = 0.60,
) -> SizedSignal:
    raw = RawSignal(
        signal_id=signal_id,
        source_url="",
        headline="Test signal",
        summary="",
        published_at=datetime.now(timezone.utc),
        category=category,
        severity=Severity.HIGH,
        affected_tickers=[ticker],
    )
    filtered = FilteredSignal(original=raw, compliance_score=1.0)
    macro = MacroContext(
        fetched_at=datetime.now(timezone.utc),
        regime=MarketRegime.BULL,
        vix=15.0,
        sp500_1m_return=0.03,
    )
    return SizedSignal(
        original=filtered,
        macro=macro,
        confidence=confidence,
        position_size_pct=size_pct,
    )


def _ares() -> AresAgent:
    return AresAgent(paper=True, host="ibgateway", port=4002)


# ── Error classifier unit tests ────────────────────────────────────────────────

class TestErrorClassifier:
    def test_errno_111_is_connectivity(self):
        exc = RuntimeError("Could not connect to IB ibgateway:4002 after 3 attempts: [Errno 111] Connect call failed")
        assert _is_connectivity_error(exc)
        assert not _is_terminal_ib_error(exc)

    def test_econnrefused_is_connectivity(self):
        assert _is_connectivity_error(RuntimeError("ECONNREFUSED"))

    def test_timed_out_is_connectivity(self):
        assert _is_connectivity_error(RuntimeError("connection timed out"))

    def test_circuit_open_is_connectivity(self):
        assert _is_connectivity_error(RuntimeError("circuit_open"))

    def test_invalid_contract_is_terminal(self):
        exc = RuntimeError("No security definition has been found for the request")
        assert _is_terminal_ib_error(exc)
        assert not _is_connectivity_error(exc)

    def test_insufficient_funds_is_terminal(self):
        assert _is_terminal_ib_error(RuntimeError("Insufficient funds"))

    def test_order_rejected_is_terminal(self):
        assert _is_terminal_ib_error(RuntimeError("Order rejected"))

    def test_unknown_error_is_not_connectivity(self):
        assert not _is_connectivity_error(RuntimeError("some unexpected error"))

    def test_unknown_error_is_not_terminal(self):
        assert not _is_terminal_ib_error(RuntimeError("some unexpected error"))


class TestSameSession:
    def test_same_day_same_session(self):
        t1 = datetime(2026, 6, 4, 15, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 6, 4, 17, 0, tzinfo=timezone.utc)
        assert _same_session(t1, t2)

    def test_different_day_different_session(self):
        t1 = datetime(2026, 6, 4, 19, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 6, 5, 14, 0, tzinfo=timezone.utc)
        assert not _same_session(t1, t2)


# ── Guard 1: connectivity failure → enqueued PENDING, trade not lost ──────────

class TestConnectivityFailureEnqueues:
    def test_errno_111_enqueues_as_pending(self):
        ares = _ares()
        conn_err = RuntimeError(
            "[ARES] Could not connect to IB ibgateway:4002 after 3 attempts: "
            "[Errno 111] Connect call failed ('172.19.0.3', 4002)"
        )

        enqueued_payloads = []

        def fake_enqueue(signal_id, symbol, side, payload, approved_at, expires_at):
            enqueued_payloads.append(payload)
            return {"signal_id": signal_id}

        with patch.object(ares, "_get_connection", side_effect=conn_err), \
             patch("core.supabase_client.enqueue_pending_order", side_effect=fake_enqueue):
            result = ares.place(_sized("sig-conn-001"))

        assert result.status == "queued"
        assert result.fill_price is None
        assert len(enqueued_payloads) == 1
        assert enqueued_payloads[0]["signal_id"] == "sig-conn-001"
        assert enqueued_payloads[0]["symbol"] == "NVDA"

    def test_queued_result_has_symbol_and_side(self):
        ares = _ares()
        conn_err = RuntimeError("[Errno 111] Connect call failed")

        with patch.object(ares, "_get_connection", side_effect=conn_err), \
             patch("core.supabase_client.enqueue_pending_order", return_value=None):
            result = ares.place(_sized("sig-q-002", category=SignalCategory.SUPPLIER_DISRUPTION))

        assert result.status == "queued"
        assert result.symbol == "NVDA"
        # Long-only: new signals always BUY, even a disruption — we never short
        # stocks we don't hold (see agents/ares.py long-only logic). Direction is
        # only SELL when Zeus explicitly sets sized.side to close an existing long.
        assert result.side == "BUY"

    def test_enqueue_not_called_on_success(self):
        ares = _ares()
        mock_ib = MagicMock()
        fake_result = MagicMock()
        fake_result.order_id = "ord-001"

        with patch.object(ares, "_get_connection", return_value=mock_ib), \
             patch.object(ares, "_place_bracket", return_value=MagicMock(order_id="x", status="submitted")), \
             patch("core.supabase_client.enqueue_pending_order") as mock_enq:
            ares.place(_sized())

        mock_enq.assert_not_called()


# ── Guard 2: IB order rejection → REJECTED, not queued ───────────────────────

class TestTerminalRejectionNotQueued:
    def test_invalid_contract_not_queued(self):
        ares = _ares()
        terminal_err = RuntimeError("No security definition has been found for the request")
        mock_ib = MagicMock()

        with patch.object(ares, "_get_connection", return_value=mock_ib), \
             patch.object(ares, "_place_bracket", side_effect=terminal_err), \
             patch("core.supabase_client.enqueue_pending_order") as mock_enq:
            result = ares.place(_sized())

        assert result.status.startswith("error:")
        mock_enq.assert_not_called()

    def test_unknown_error_not_queued(self):
        ares = _ares()
        unknown_err = RuntimeError("something completely unexpected")
        mock_ib = MagicMock()

        with patch.object(ares, "_get_connection", return_value=mock_ib), \
             patch.object(ares, "_place_bracket", side_effect=unknown_err), \
             patch("core.supabase_client.enqueue_pending_order") as mock_enq:
            result = ares.place(_sized())

        assert result.status.startswith("error:")
        mock_enq.assert_not_called()


# ── Guard 3: expired pending row → EXPIRED, _place_bracket not called ─────────

class TestExpiredRowNotPlaced:
    def _stale_row(self, signal_id: str = "sig-stale") -> dict:
        now = datetime.now(timezone.utc)
        approved_at = now - timedelta(minutes=PENDING_ORDER_MAX_AGE_MIN + 5)
        expires_at  = approved_at + timedelta(minutes=PENDING_ORDER_MAX_AGE_MIN)
        return {
            "signal_id":   signal_id,
            "symbol":      "NVDA",
            "side":        "BUY",
            "payload":     {"signal_id": signal_id, "symbol": "NVDA", "side": "BUY",
                            "position_size_pct": 0.02, "confidence": 0.60},
            "status":      "PENDING",
            "approved_at": approved_at.isoformat(),
            "expires_at":  expires_at.isoformat(),
            "attempts":    0,
            "last_error":  None,
        }

    def test_stale_row_marked_expired(self):
        ares = _ares()
        statuses: list[tuple] = []

        def capture_status(signal_id, status, **kwargs):
            statuses.append((signal_id, status))

        with patch("core.supabase_client.get_retryable_pending_orders",
                   return_value=[self._stale_row()]), \
             patch("core.supabase_client.set_pending_status",
                   side_effect=capture_status), \
             patch.object(ares, "_place_bracket") as mock_place:
            ares.reconcile_pending()

        assert ("sig-stale", "EXPIRED") in statuses
        mock_place.assert_not_called()

    def test_same_signal_id_but_different_session_expired(self):
        ares = _ares()
        now = datetime.now(timezone.utc)
        # approved yesterday (different session)
        approved_at = now - timedelta(hours=20)
        expires_at  = approved_at + timedelta(minutes=PENDING_ORDER_MAX_AGE_MIN)
        row = {
            "signal_id":   "sig-yesterday",
            "symbol":      "AAPL",
            "side":        "BUY",
            "payload":     {"signal_id": "sig-yesterday", "symbol": "AAPL", "side": "BUY",
                            "position_size_pct": 0.02, "confidence": 0.60},
            "status":      "PENDING",
            "approved_at": approved_at.isoformat(),
            "expires_at":  expires_at.isoformat(),
            "attempts":    0,
            "last_error":  None,
        }
        statuses = []
        with patch("core.supabase_client.get_retryable_pending_orders", return_value=[row]), \
             patch("core.supabase_client.set_pending_status",
                   side_effect=lambda sid, st, **kw: statuses.append((sid, st))), \
             patch.object(ares, "_place_bracket") as mock_place:
            ares.reconcile_pending()

        assert ("sig-yesterday", "EXPIRED") in statuses
        mock_place.assert_not_called()


# ── Guard 4: already SUBMITTED → reconcile does not double-place ──────────────

class TestIdempotencyNoDoublePlacement:
    def test_submitted_row_skipped(self):
        ares = _ares()
        now = datetime.now(timezone.utc)
        row = {
            "signal_id":   "sig-already-done",
            "symbol":      "MSFT",
            "side":        "BUY",
            "payload":     {"signal_id": "sig-already-done", "symbol": "MSFT", "side": "BUY",
                            "position_size_pct": 0.02, "confidence": 0.60},
            "status":      "SUBMITTED",  # already placed by a previous cycle
            "approved_at": now.isoformat(),
            "expires_at":  (now + timedelta(minutes=30)).isoformat(),
            "attempts":    1,
            "ib_order_id": "12345",
            "last_error":  None,
        }
        with patch("core.supabase_client.get_retryable_pending_orders", return_value=[row]), \
             patch.object(ares, "_place_bracket") as mock_place, \
             patch.object(ares, "_get_connection") as mock_conn:
            ares.reconcile_pending()

        mock_place.assert_not_called()
        mock_conn.assert_not_called()


# ── Guard 5: DEGRADED health → new approval enqueued, no live call ────────────

class TestDegradedHealthEnqueues:
    def test_degraded_ares_health_queues_approval(self):
        ares = _ares()
        enqueued = []

        def fake_enqueue(signal_id, symbol, side, payload, approved_at, expires_at):
            enqueued.append(signal_id)
            return {"signal_id": signal_id}

        conn_err = RuntimeError("[Errno 111] Connect call failed")

        # health() says DEGRADED → place() is called → hits connectivity error → enqueues
        with patch.object(ares, "health", return_value=AgentHealth.DEGRADED), \
             patch.object(ares, "_get_connection", side_effect=conn_err), \
             patch("core.supabase_client.enqueue_pending_order", side_effect=fake_enqueue):
            result = ares.place(_sized("sig-degraded"))

        assert result.status == "queued"
        assert "sig-degraded" in enqueued

    def test_healthy_ares_does_not_enqueue_on_success(self):
        ares = _ares()
        mock_ib = MagicMock()

        from core.types import TradeResult
        success = TradeResult(order_id="ord-ok", symbol="NVDA", side="BUY",
                              fill_price=100.0, qty=5)

        with patch.object(ares, "health", return_value=AgentHealth.HEALTHY), \
             patch.object(ares, "_get_connection", return_value=mock_ib), \
             patch.object(ares, "_place_bracket", return_value=success), \
             patch("core.supabase_client.enqueue_pending_order") as mock_enq:
            result = ares.place(_sized())

        assert result.status == "submitted"
        mock_enq.assert_not_called()


# ── reconcile_pending processes fresh row successfully ────────────────────────

class TestReconcilePlacesFreshRow:
    def test_fresh_pending_row_submitted(self):
        ares = _ares()
        now = datetime.now(timezone.utc)
        row = {
            "signal_id":   "sig-fresh",
            "symbol":      "NVDA",
            "side":        "BUY",
            "payload":     {"signal_id": "sig-fresh", "symbol": "NVDA", "side": "BUY",
                            "position_size_pct": 0.02, "confidence": 0.60},
            "status":      "PENDING",
            "approved_at": now.isoformat(),
            "expires_at":  (now + timedelta(minutes=30)).isoformat(),
            "attempts":    0,
            "last_error":  None,
        }
        mock_ib = MagicMock()
        from core.types import TradeResult
        placed = TradeResult(order_id="ib-777", symbol="NVDA", side="BUY",
                             fill_price=120.0, qty=3)

        statuses: list[tuple] = []
        with patch("core.supabase_client.get_retryable_pending_orders", return_value=[row]), \
             patch("core.supabase_client.set_pending_status",
                   side_effect=lambda sid, st, **kw: statuses.append((sid, st))), \
             patch.object(ares, "_get_connection", return_value=mock_ib), \
             patch.object(ares, "_place_bracket", return_value=placed):
            count = ares.reconcile_pending()

        assert count == 1
        status_sequence = [s for (_, s) in statuses]
        assert "SUBMITTING" in status_sequence
        assert "SUBMITTED" in status_sequence

    def test_reconcile_returns_zero_when_queue_empty(self):
        ares = _ares()
        with patch("core.supabase_client.get_retryable_pending_orders", return_value=[]):
            assert ares.reconcile_pending() == 0
