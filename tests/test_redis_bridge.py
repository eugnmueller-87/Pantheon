"""
Quality gate — RedisBridge (ZEUS → SpendLens intelligence feed).

The bridge writes to production Redis. These tests use a mock Redis
client to verify payload structure, key naming, and TTL settings
without touching the real Upstash instance.

Key invariants:
  - zeus:* keys never collide with hermes:* keys
  - DecisionTrace payload contains every field SpendLens expects
  - All writes are fire-and-forget (failures must not raise)
"""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch

import pytest

from core.redis_bridge import _DECISION_TTL, _DECISIONS_LIST_KEY, _MACRO_KEY, _SUPPLIER_TTL, RedisBridge
from core.types import DecisionTrace, FilteredSignal, MacroContext, MarketRegime, RawSignal, Severity, SignalCategory


def _mock_bridge() -> tuple[RedisBridge, MagicMock]:
    """Return a RedisBridge with a mocked Redis client."""
    bridge = RedisBridge.__new__(RedisBridge)
    mock_r = MagicMock()
    bridge._r = mock_r
    bridge._enabled = True
    bridge._url   = "https://mock"
    bridge._token = "mock-token"
    return bridge, mock_r


def _macro(regime=MarketRegime.BULL, vix=15.0) -> MacroContext:
    return MacroContext(
        fetched_at=datetime.now(timezone.utc),
        regime=regime, vix=vix, sp500_1m_return=0.03,
        sector_momentum={"tech": 0.05},
    )


def _trace(approved=True, placed=True) -> DecisionTrace:
    return DecisionTrace(
        trace_id="tr-abc123",
        signal_id="sig-001",
        timestamp=datetime.now(timezone.utc),
        headline="NVIDIA supply chain disruption",
        supplier="NVIDIA",
        category="supplier_disruption",
        severity="HIGH",
        hades_passed=True,
        hades_notes=["clean"],
        trend_regime="bull",
        trend_vix=15.0,
        pattern_confidence=0.72,
        pattern_size_pct=0.025,
        zeus_reasoning="Strong signal in bull market.",
        zeus_approved=approved,
        trade_placed=placed,
        symbol="NVDA",
        side="SELL",
        fill_price=450.0,
        pnl_pct=None,
    )


def _filtered_signal(supplier="NVIDIA", score=0.9) -> FilteredSignal:
    raw = RawSignal(
        signal_id="s-001", source_url="", headline="Test",
        summary="", published_at=datetime.now(timezone.utc),
        category=SignalCategory.SUPPLIER_DISRUPTION,
        severity=Severity.HIGH,
        affected_tickers=["NVDA"],
        raw_text="test", supplier=supplier,
    )
    return FilteredSignal(original=raw, compliance_score=score)


# ── Key namespace ──────────────────────────────────────────────────────────────

class TestKeyNamespace:
    def test_macro_key_starts_with_zeus(self):
        assert _MACRO_KEY.startswith("zeus:")

    def test_decisions_list_key_starts_with_zeus(self):
        assert _DECISIONS_LIST_KEY.startswith("zeus:")

    def test_decision_key_uses_trace_id(self):
        bridge, mock_r = _mock_bridge()
        bridge.push_decision(_trace())
        calls = [str(c) for c in mock_r.setex.call_args_list]
        assert any("tr-abc123" in c for c in calls)

    def test_supplier_risk_key_uses_slug(self):
        bridge, mock_r = _mock_bridge()
        bridge.push_supplier_risk(_filtered_signal("NVIDIA Corporation"))
        calls = [str(c) for c in mock_r.setex.call_args_list]
        assert any("nvidia_corporation" in c for c in calls)

    def test_supplier_slug_has_no_spaces(self):
        bridge, mock_r = _mock_bridge()
        bridge.push_supplier_risk(_filtered_signal("Deutsche Telekom"))
        key_arg = mock_r.setex.call_args[0][0]
        assert " " not in key_arg

    def test_no_hermes_prefix_in_any_key(self):
        bridge, mock_r = _mock_bridge()
        bridge.push_macro(_macro())
        bridge.push_decision(_trace())
        bridge.push_supplier_risk(_filtered_signal())
        all_calls = mock_r.set.call_args_list + mock_r.setex.call_args_list
        for c in all_calls:
            key = c[0][0]
            assert not key.startswith("hermes:"), f"Key {key!r} uses hermes: namespace"


# ── Macro payload ──────────────────────────────────────────────────────────────

class TestMacroPayload:
    def test_macro_written_to_correct_key(self):
        bridge, mock_r = _mock_bridge()
        bridge.push_macro(_macro())
        key = mock_r.set.call_args[0][0]
        assert key == _MACRO_KEY

    def test_macro_payload_has_required_fields(self):
        bridge, mock_r = _mock_bridge()
        bridge.push_macro(_macro(regime=MarketRegime.BULL, vix=17.5))
        payload = json.loads(mock_r.set.call_args[0][1])
        assert payload["regime"] == "bull"
        assert payload["vix"] == pytest.approx(17.5)
        assert "sp500_1m_pct" in payload
        assert "sector_momentum" in payload
        assert "updated_at" in payload
        assert payload["source"] == "zeus"


# ── Decision trace payload ─────────────────────────────────────────────────────

class TestDecisionPayload:
    def test_decision_written_with_ttl(self):
        bridge, mock_r = _mock_bridge()
        bridge.push_decision(_trace())
        assert mock_r.setex.called
        ttl_arg = mock_r.setex.call_args[0][1]
        assert ttl_arg == _DECISION_TTL

    def test_decision_payload_has_all_spendlens_fields(self):
        bridge, mock_r = _mock_bridge()
        bridge.push_decision(_trace())
        payload = json.loads(mock_r.setex.call_args[0][2])
        required = {
            "trace_id", "signal_id", "timestamp", "headline", "supplier",
            "category", "severity", "hades_passed", "hades_notes",
            "trend_regime", "trend_vix", "pattern_confidence",
            "zeus_reasoning", "zeus_approved", "trade_placed",
            "symbol", "side", "fill_price", "pnl_pct",
            "killed_at_stage", "kill_reason", "source",
        }
        assert required.issubset(payload.keys())

    def test_decision_added_to_recent_list(self):
        bridge, mock_r = _mock_bridge()
        bridge.push_decision(_trace())
        mock_r.lpush.assert_called_once_with(_DECISIONS_LIST_KEY, "tr-abc123")

    def test_recent_list_trimmed_to_50(self):
        bridge, mock_r = _mock_bridge()
        bridge.push_decision(_trace())
        mock_r.ltrim.assert_called_once_with(_DECISIONS_LIST_KEY, 0, 49)


# ── Supplier risk payload ──────────────────────────────────────────────────────

class TestSupplierRiskPayload:
    def test_supplier_risk_written_with_ttl(self):
        bridge, mock_r = _mock_bridge()
        bridge.push_supplier_risk(_filtered_signal())
        assert mock_r.setex.called
        ttl_arg = mock_r.setex.call_args[0][1]
        assert ttl_arg == _SUPPLIER_TTL

    def test_supplier_risk_payload_fields(self):
        bridge, mock_r = _mock_bridge()
        bridge.push_supplier_risk(_filtered_signal("NVIDIA", score=0.85))
        payload = json.loads(mock_r.setex.call_args[0][2])
        assert payload["supplier"] == "NVIDIA"
        assert payload["compliance_score"] == pytest.approx(0.85)
        assert "risk_level" in payload
        assert "assessed_at" in payload
        assert payload["source"] == "zeus_hades"

    def test_empty_supplier_does_not_write(self):
        bridge, mock_r = _mock_bridge()
        bridge.push_supplier_risk(_filtered_signal(supplier=""))
        mock_r.setex.assert_not_called()


# ── Compliance → risk level mapping ───────────────────────────────────────────

class TestRiskLevelMapping:
    def test_ofac_is_critical(self):
        assert RedisBridge._compliance_to_risk(0.0, ofac=True, esg=False) == "CRITICAL"

    def test_low_score_is_high(self):
        assert RedisBridge._compliance_to_risk(0.4, ofac=False, esg=False) == "HIGH"

    def test_esg_flag_is_medium(self):
        assert RedisBridge._compliance_to_risk(0.8, ofac=False, esg=True) == "MEDIUM"

    def test_medium_score_is_medium(self):
        assert RedisBridge._compliance_to_risk(0.7, ofac=False, esg=False) == "MEDIUM"

    def test_clean_is_low(self):
        assert RedisBridge._compliance_to_risk(1.0, ofac=False, esg=False) == "LOW"

    def test_ofac_overrides_everything(self):
        assert RedisBridge._compliance_to_risk(1.0, ofac=True, esg=False) == "CRITICAL"


# ── Fire-and-forget — failures must not raise ─────────────────────────────────

class TestFireAndForget:
    def test_redis_failure_on_macro_does_not_raise(self):
        bridge, mock_r = _mock_bridge()
        mock_r.set.side_effect = ConnectionError("Redis down")
        bridge.push_macro(_macro())  # must not raise

    def test_redis_failure_on_decision_does_not_raise(self):
        bridge, mock_r = _mock_bridge()
        mock_r.setex.side_effect = ConnectionError("Redis down")
        bridge.push_decision(_trace())  # must not raise

    def test_disabled_bridge_does_not_write(self):
        bridge, mock_r = _mock_bridge()
        bridge._enabled = False
        bridge.push_macro(_macro())
        bridge.push_decision(_trace())
        bridge.push_supplier_risk(_filtered_signal())
        mock_r.set.assert_not_called()
        mock_r.setex.assert_not_called()
