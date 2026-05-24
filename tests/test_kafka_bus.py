"""
tests/test_kafka_bus.py — Kafka bus offline-safe behaviour

All tests run without a real Kafka broker.
KAFKA_ENABLED=false disables the bus entirely.
When KAFKA_ENABLED=true but broker unreachable, functions return False/[] silently.
"""

from __future__ import annotations

import os
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_signal():
    from core.types import RawSignal, SignalCategory, Severity
    return RawSignal(
        signal_id         = "test-sig-001",
        source_url        = "https://example.com",
        headline          = "Test headline",
        summary           = "Test summary",
        published_at      = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
        category          = SignalCategory.EARNINGS_SURPRISE,
        severity          = Severity.HIGH,
        affected_tickers  = ["AAPL"],
        raw_text          = "Test headline Test summary",
        supplier          = "TestSupplier",
        hermes_signal_type= "EARNINGS",
    )


def _make_trace():
    from core.types import DecisionTrace
    return DecisionTrace(
        trace_id        = "trace-001",
        signal_id       = "sig-001",
        timestamp       = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
        headline        = "Test trade",
        supplier        = "TestSupplier",
        category        = "earnings_surprise",
        severity        = "HIGH",
        hades_passed    = True,
        zeus_approved   = True,
        zeus_reasoning  = "Strong signal",
        trade_placed    = True,
        symbol          = "AAPL",
        killed_at_stage = None,
        kill_reason     = None,
        pnl_pct         = None,
    )


# ---------------------------------------------------------------------------
# KAFKA_ENABLED=false — everything is a no-op
# ---------------------------------------------------------------------------

class TestKafkaDisabled:
    def setup_method(self):
        import core.kafka_bus as kb
        kb.ENABLED = False
        kb._producer = None

    def teardown_method(self):
        import core.kafka_bus as kb
        kb.ENABLED = True
        kb._producer = None

    def test_publish_raw_signal_disabled(self):
        from core.kafka_bus import publish_raw_signal
        assert publish_raw_signal(_make_signal()) is False

    def test_publish_decision_trace_disabled(self):
        from core.kafka_bus import publish_decision_trace
        assert publish_decision_trace(_make_trace()) is False

    def test_consume_raw_signals_disabled(self):
        from core.kafka_bus import consume_raw_signals
        assert consume_raw_signals() == []

    def test_is_available_disabled(self):
        from core.kafka_bus import is_available
        assert is_available() is False


# ---------------------------------------------------------------------------
# Broker unreachable — functions fail gracefully
# ---------------------------------------------------------------------------

class TestKafkaOffline:
    def setup_method(self):
        import core.kafka_bus as kb
        kb.ENABLED = True
        kb._producer = None

    def teardown_method(self):
        import core.kafka_bus as kb
        kb._producer = None

    def test_publish_raw_signal_no_broker(self):
        """publish_raw_signal returns False when broker is unreachable."""
        with patch("core.kafka_bus._get_producer", return_value=None):
            from core.kafka_bus import publish_raw_signal
            assert publish_raw_signal(_make_signal()) is False

    def test_publish_decision_trace_no_broker(self):
        with patch("core.kafka_bus._get_producer", return_value=None):
            from core.kafka_bus import publish_decision_trace
            assert publish_decision_trace(_make_trace()) is False

    def test_consume_raw_signals_no_broker(self):
        """consume_raw_signals returns [] when KafkaConsumer raises on connect."""
        import sys
        import types
        # Inject a fake kafka module whose KafkaConsumer raises on instantiation
        fake_kafka = types.ModuleType("kafka")
        fake_kafka.KafkaConsumer = MagicMock(side_effect=Exception("no broker"))
        with patch.dict(sys.modules, {"kafka": fake_kafka}):
            from core.kafka_bus import consume_raw_signals
            assert consume_raw_signals() == []

    def test_is_available_no_broker(self):
        """is_available returns False when KafkaAdminClient raises."""
        import sys
        import types
        fake_admin_mod = types.ModuleType("kafka.admin")
        fake_admin_mod.KafkaAdminClient = MagicMock(side_effect=Exception("no broker"))
        fake_kafka = types.ModuleType("kafka")
        fake_kafka.admin = fake_admin_mod
        with patch.dict(sys.modules, {"kafka": fake_kafka, "kafka.admin": fake_admin_mod}):
            from core.kafka_bus import is_available
            assert is_available() is False

    def test_consume_import_error(self):
        """If kafka-python not installed at all, consume returns []."""
        import sys
        with patch.dict(sys.modules, {"kafka": None}):
            from core.kafka_bus import consume_raw_signals
            assert consume_raw_signals() == []


# ---------------------------------------------------------------------------
# Happy path — mocked producer/consumer
# ---------------------------------------------------------------------------

class TestKafkaHappyPath:
    def setup_method(self):
        import core.kafka_bus as kb
        kb.ENABLED = True
        kb._producer = None

    def teardown_method(self):
        import core.kafka_bus as kb
        kb._producer = None

    def test_publish_raw_signal_success(self):
        mock_producer = MagicMock()
        with patch("core.kafka_bus._get_producer", return_value=mock_producer):
            from core.kafka_bus import publish_raw_signal
            result = publish_raw_signal(_make_signal())
        assert result is True
        mock_producer.send.assert_called_once()
        mock_producer.flush.assert_called_once()

    def test_publish_decision_trace_success(self):
        mock_producer = MagicMock()
        with patch("core.kafka_bus._get_producer", return_value=mock_producer):
            from core.kafka_bus import publish_decision_trace
            result = publish_decision_trace(_make_trace())
        assert result is True
        mock_producer.send.assert_called_once()
        call_kwargs = mock_producer.send.call_args
        assert call_kwargs[0][0] == "zeus.decision_traces"

    def test_consume_raw_signals_returns_signals(self):
        import sys
        import types
        sig = _make_signal()
        from core.kafka_bus import _signal_to_dict
        raw_payload = _signal_to_dict(sig)

        mock_msg = MagicMock()
        mock_msg.value = raw_payload

        mock_consumer = MagicMock()
        mock_consumer.__iter__ = MagicMock(return_value=iter([mock_msg]))

        fake_kafka = types.ModuleType("kafka")
        fake_kafka.KafkaConsumer = MagicMock(return_value=mock_consumer)
        fake_kafka.TopicPartition = MagicMock()
        with patch.dict(sys.modules, {"kafka": fake_kafka}):
            from core.kafka_bus import consume_raw_signals
            results = consume_raw_signals()

        assert len(results) == 1
        assert results[0].signal_id == "test-sig-001"
        assert results[0].headline == "Test headline"

    def test_is_available_success(self):
        import sys
        import types
        mock_admin = MagicMock()
        fake_admin_mod = types.ModuleType("kafka.admin")
        fake_admin_mod.KafkaAdminClient = MagicMock(return_value=mock_admin)
        fake_kafka = types.ModuleType("kafka")
        fake_kafka.admin = fake_admin_mod
        with patch.dict(sys.modules, {"kafka": fake_kafka, "kafka.admin": fake_admin_mod}):
            from core.kafka_bus import is_available
            assert is_available() is True
        mock_admin.close.assert_called_once()


# ---------------------------------------------------------------------------
# Serialisation round-trip
# ---------------------------------------------------------------------------

class TestSerialisation:
    def test_signal_roundtrip(self):
        from core.kafka_bus import _signal_to_dict, _dict_to_signal
        sig = _make_signal()
        d = _signal_to_dict(sig)
        restored = _dict_to_signal(d)
        assert restored.signal_id == sig.signal_id
        assert restored.headline == sig.headline
        assert restored.category == sig.category
        assert restored.severity == sig.severity
        assert restored.affected_tickers == sig.affected_tickers
        assert restored.supplier == sig.supplier

    def test_signal_roundtrip_no_published_at(self):
        from core.kafka_bus import _dict_to_signal
        d = {
            "signal_id": "x", "source_url": "", "headline": "h",
            "summary": "", "published_at": None, "category": "neutral",
            "severity": "LOW", "affected_tickers": [], "raw_text": "",
            "supplier": "", "hermes_signal_type": "",
        }
        sig = _dict_to_signal(d)
        assert sig.published_at is not None

    def test_trace_to_dict_fields(self):
        from core.kafka_bus import _trace_to_dict
        trace = _make_trace()
        d = _trace_to_dict(trace)
        assert d["trace_id"] == "trace-001"
        assert d["zeus_approved"] is True
        assert d["symbol"] == "AAPL"
        assert "timestamp" in d


# ---------------------------------------------------------------------------
# Icarus integration — publishes on fetch
# ---------------------------------------------------------------------------

class TestIcarusKafkaIntegration:
    def test_icarus_fetch_publishes_to_kafka(self):
        """IcarusAgent.fetch() calls publish_raw_signal for each signal."""
        from agents.icarus import IcarusAgent
        agent = IcarusAgent(api_key="test")

        mock_signals = [_make_signal()]
        with patch.object(agent, "_fetch_briefing", return_value=mock_signals):
            with patch("core.kafka_bus.publish_raw_signal", return_value=True) as mock_pub:
                result = agent.fetch()

        assert len(result) == 1
        mock_pub.assert_called_once_with(mock_signals[0])

    def test_icarus_fetch_no_publish_on_empty(self):
        """No publish calls when Hermes returns no signals."""
        from agents.icarus import IcarusAgent
        agent = IcarusAgent(api_key="test")

        with patch.object(agent, "_fetch_briefing", return_value=[]):
            with patch("core.kafka_bus.publish_raw_signal") as mock_pub:
                result = agent.fetch()

        assert result == []
        mock_pub.assert_not_called()


# ---------------------------------------------------------------------------
# ZEUS run_once integration — kafka available vs unavailable
# ---------------------------------------------------------------------------

class TestZeusKafkaIntegration:
    def test_run_once_uses_direct_fetch_when_kafka_down(self, mock_zeus):
        """When Kafka unavailable, run_once falls back to direct Icarus.fetch."""
        with patch("core.kafka_bus.is_available", return_value=False):
            with patch.object(mock_zeus.icarus, "fetch", return_value=[]) as mock_fetch:
                mock_zeus.run_once()
        mock_fetch.assert_called_once()

    def test_run_once_uses_kafka_when_available(self, mock_zeus):
        """When Kafka available and has signals, skips direct Icarus.fetch."""
        sig = _make_signal()
        with patch("core.kafka_bus.is_available", return_value=True):
            with patch("core.kafka_bus.consume_raw_signals", return_value=[sig]):
                with patch.object(mock_zeus.icarus, "fetch", return_value=[]) as mock_fetch:
                    runs = mock_zeus.run_once()
        # consume_raw_signals returned a signal so direct fetch is not needed
        mock_fetch.assert_not_called()
        assert len(runs) == 1

    def test_run_once_fetches_live_when_kafka_empty(self, mock_zeus):
        """When Kafka available but empty, also calls Icarus.fetch as fallback."""
        with patch("core.kafka_bus.is_available", return_value=True):
            with patch("core.kafka_bus.consume_raw_signals", return_value=[]):
                with patch.object(mock_zeus.icarus, "fetch", return_value=[]) as mock_fetch:
                    mock_zeus.run_once()
        mock_fetch.assert_called_once()


@pytest.fixture
def mock_zeus():
    """Minimal ZeusOrchestrator with all external calls mocked out."""
    import sys
    from unittest.mock import patch, MagicMock

    mocks = {
        "ib_insync": MagicMock(),
        "chromadb":  MagicMock(),
        "anthropic": MagicMock(),
    }
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = MagicMock(
        content=[MagicMock(text='{"approved": true, "confidence": 0.7, "reasoning": "ok", "override": false, "override_reason": ""}')]
    )
    mock_bridge = MagicMock()

    with patch.dict(sys.modules, mocks):
        with patch("anthropic.Anthropic", return_value=mock_client):
            with patch("core.knowledge_base.KnowledgeBase.__init__", return_value=None):
                with patch("core.knowledge_base.KnowledgeBase.store_decision"):
                    with patch("core.redis_bridge.RedisBridge", return_value=mock_bridge):
                        with patch("core.watchdog.Watchdog.start"):
                            with patch("core.kafka_bus.publish_decision_trace", return_value=False):
                                from agents.zeus import ZeusOrchestrator, ZeusConfig
                                config = ZeusConfig(
                                    paper_trading=True,
                                    mock_execution=True,
                                    use_llm_reasoning=False,
                                )
                                zeus = ZeusOrchestrator(config)
                                zeus.argus._state.total_equity = 100_000
                                yield zeus
