"""
core/kafka_bus.py — Kafka event bus wrapper

Thin, offline-safe wrapper around kafka-python.
If Kafka is unreachable (local dev, CI, broker down), all calls are no-ops
and the pipeline continues synchronously without error.

Topics:
  zeus.raw_signals      — Icarus → ZEUS   (7-day retention)
  zeus.decision_traces  — ZEUS → Apollo   (30-day retention)

Environment:
  KAFKA_BOOTSTRAP_SERVERS  — default "kafka:9092"
  KAFKA_ENABLED            — set to "false" to disable entirely (tests, local dev)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Iterator, Optional

logger = logging.getLogger("kafka_bus")

BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
ENABLED   = os.getenv("KAFKA_ENABLED", "true").lower() not in ("false", "0", "no")

TOPIC_RAW_SIGNALS     = "zeus.raw_signals"
TOPIC_DECISION_TRACES = "zeus.decision_traces"

_producer = None
_consumer = None


def _get_producer():
    global _producer
    if _producer is not None:
        return _producer
    try:
        from kafka import KafkaProducer
        _producer = KafkaProducer(
            bootstrap_servers=BOOTSTRAP,
            value_serializer=lambda v: json.dumps(v, default=str).encode(),
            acks="all",
            retries=3,
            request_timeout_ms=5000,
            connections_max_idle_ms=30000,
        )
        logger.info("[KAFKA] Producer connected — %s", BOOTSTRAP)
        return _producer
    except Exception as exc:
        logger.debug("[KAFKA] Producer unavailable (offline mode): %s", exc)
        return None


def publish_raw_signal(signal) -> bool:
    """
    Publish a RawSignal to zeus.raw_signals.
    Returns True if published, False if Kafka unavailable (pipeline continues either way).
    """
    if not ENABLED:
        return False
    try:
        producer = _get_producer()
        if producer is None:
            return False
        payload = _signal_to_dict(signal)
        producer.send(TOPIC_RAW_SIGNALS, value=payload, key=signal.signal_id.encode() if signal.signal_id else None)
        producer.flush(timeout=3)
        logger.debug("[KAFKA] Published raw_signal %s", signal.signal_id)
        return True
    except Exception as exc:
        logger.debug("[KAFKA] publish_raw_signal failed (offline mode): %s", exc)
        return False


def publish_decision_trace(trace) -> bool:
    """
    Publish a DecisionTrace to zeus.decision_traces.
    Returns True if published, False if Kafka unavailable.
    """
    if not ENABLED:
        return False
    try:
        producer = _get_producer()
        if producer is None:
            return False
        payload = _trace_to_dict(trace)
        producer.send(TOPIC_DECISION_TRACES, value=payload, key=trace.trace_id.encode() if trace.trace_id else None)
        producer.flush(timeout=3)
        logger.debug("[KAFKA] Published decision_trace %s", trace.trace_id)
        return True
    except Exception as exc:
        logger.debug("[KAFKA] publish_decision_trace failed (offline mode): %s", exc)
        return False


def consume_raw_signals(
    group_id: str = "zeus-pipeline",
    timeout_ms: int = 5000,
    max_records: int = 50,
) -> list:
    """
    Poll zeus.raw_signals for pending messages.
    Returns a list of RawSignal objects. Returns [] if Kafka unavailable.
    Used by ZEUS run_once() when Kafka is available.
    """
    if not ENABLED:
        return []
    try:
        from kafka import KafkaConsumer, TopicPartition
        from core.types import RawSignal, SignalCategory, Severity
        consumer = KafkaConsumer(
            TOPIC_RAW_SIGNALS,
            bootstrap_servers=BOOTSTRAP,
            group_id=group_id,
            auto_offset_reset="earliest",
            enable_auto_commit=True,
            value_deserializer=lambda b: json.loads(b.decode()),
            consumer_timeout_ms=timeout_ms,
            max_poll_records=max_records,
            session_timeout_ms=10000,
            request_timeout_ms=15000,
        )
        signals = []
        try:
            for msg in consumer:
                signals.append(_dict_to_signal(msg.value))
                if len(signals) >= max_records:
                    break
        finally:
            consumer.close()
        logger.info("[KAFKA] Consumed %d raw_signal(s)", len(signals))
        return signals
    except Exception as exc:
        logger.debug("[KAFKA] consume_raw_signals failed (offline mode): %s", exc)
        return []


def is_available() -> bool:
    """Quick liveness check — used by ZEUS to decide consume vs direct fetch."""
    if not ENABLED:
        return False
    try:
        from kafka.admin import KafkaAdminClient
        admin = KafkaAdminClient(
            bootstrap_servers=BOOTSTRAP,
            request_timeout_ms=2000,
            connections_max_idle_ms=5000,
        )
        admin.close()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _signal_to_dict(signal) -> dict:
    return {
        "signal_id":          signal.signal_id,
        "source_url":         signal.source_url,
        "headline":           signal.headline,
        "summary":            signal.summary,
        "published_at":       signal.published_at.isoformat() if signal.published_at else None,
        "category":           signal.category.value if hasattr(signal.category, "value") else signal.category,
        "severity":           signal.severity.name if hasattr(signal.severity, "name") else signal.severity,
        "affected_tickers":   signal.affected_tickers,
        "raw_text":           signal.raw_text,
        "supplier":           signal.supplier,
        "hermes_signal_type": signal.hermes_signal_type,
    }


def _dict_to_signal(d: dict):
    from core.types import RawSignal, SignalCategory, Severity
    from datetime import datetime, timezone
    try:
        pub = datetime.fromisoformat(d["published_at"]) if d.get("published_at") else datetime.now(timezone.utc)
    except Exception:
        pub = datetime.now(timezone.utc)
    return RawSignal(
        signal_id         = d.get("signal_id", ""),
        source_url        = d.get("source_url", ""),
        headline          = d.get("headline", ""),
        summary           = d.get("summary", ""),
        published_at      = pub,
        category          = SignalCategory(d["category"]) if d.get("category") else SignalCategory.NEUTRAL,
        severity          = Severity[d["severity"]] if d.get("severity") else Severity.LOW,
        affected_tickers  = d.get("affected_tickers", []),
        raw_text          = d.get("raw_text", ""),
        supplier          = d.get("supplier", ""),
        hermes_signal_type= d.get("hermes_signal_type", ""),
    )


def _trace_to_dict(trace) -> dict:
    return {
        "trace_id":          trace.trace_id,
        "signal_id":         trace.signal_id,
        "timestamp":         trace.timestamp.isoformat() if trace.timestamp else None,
        "headline":          trace.headline,
        "supplier":          trace.supplier,
        "category":          trace.category,
        "severity":          trace.severity,
        "hades_passed":      trace.hades_passed,
        "zeus_approved":     trace.zeus_approved,
        "zeus_reasoning":    trace.zeus_reasoning,
        "trade_placed":      trace.trade_placed,
        "symbol":            trace.symbol,
        "killed_at_stage":   trace.killed_at_stage,
        "kill_reason":       trace.kill_reason,
        "pnl_pct":           trace.pnl_pct,
    }
