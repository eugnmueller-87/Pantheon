"""
Agent 1 — Icarus Signal Watcher
Pulls classified signals directly from Hermes (live on Railway).
Imports only from core.types — never from other agents.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import requests

from core.types import AgentHealth, RawSignal, SignalCategory, Severity
from core.agent_knowledge import AgentKnowledgeBase

logger = logging.getLogger("icarus")

HERMES_BASE_URL = "https://hermes-agent-production-114e.up.railway.app"

# Maps Hermes 11 signal types → ZEUS SignalCategory
_HERMES_TYPE_MAP: dict[str, SignalCategory] = {
    "SUPPLY_CHAIN":   SignalCategory.SUPPLIER_DISRUPTION,
    "REGULATORY":     SignalCategory.REGULATORY_ACTION,
    "EARNINGS":       SignalCategory.EARNINGS_SURPRISE,
    "PRICING_CHANGE": SignalCategory.SUPPLIER_DISRUPTION,
    "LAYOFFS_HIRING": SignalCategory.MACRO_SHIFT,
    "ACQUISITION":    SignalCategory.POSITIVE_NEWS,
    "FUNDING":        SignalCategory.POSITIVE_NEWS,
    "PRODUCT_RELEASE":SignalCategory.POSITIVE_NEWS,
    "PARTNERSHIP":    SignalCategory.POSITIVE_NEWS,
    "RESEARCH_PAPER": SignalCategory.NEUTRAL,
    "OTHER":          SignalCategory.NEUTRAL,
}

_URGENCY_MAP: dict[str, Severity] = {
    "HIGH":   Severity.HIGH,
    "MEDIUM": Severity.MEDIUM,
    "LOW":    Severity.LOW,
}

_SUPPLIER_TICKER_MAP: dict[str, str] = {
    "NVIDIA": "NVDA", "Apple": "AAPL", "Microsoft": "MSFT",
    "Amazon": "AMZN", "Tesla": "TSLA", "Intel": "INTC",
    "TSMC": "TSM", "Samsung": "SSNLF", "Qualcomm": "QCOM",
    "BASF": "BASFY", "Siemens": "SIEGY", "SAP": "SAP",
    "Deutsche Telekom": "DTEGY", "Volkswagen": "VWAGY",
    "BMW": "BMWYY", "Mercedes-Benz": "MBGYY",
}


def _map_signal(item: dict) -> Optional[RawSignal]:
    signal_type = item.get("signal_type", "OTHER")
    category = _HERMES_TYPE_MAP.get(signal_type, SignalCategory.NEUTRAL)
    if category == SignalCategory.NEUTRAL:
        return None

    urgency  = item.get("urgency", "LOW")
    severity = _URGENCY_MAP.get(urgency, Severity.LOW)
    if item.get("is_significant") and urgency == "HIGH":
        severity = Severity.CRITICAL

    supplier = item.get("supplier", "")
    ticker   = _SUPPLIER_TICKER_MAP.get(supplier)
    tickers  = [ticker] if ticker else []

    try:
        published_at = datetime.fromisoformat(item["published"].replace("Z", "+00:00"))
    except Exception:
        published_at = datetime.now(timezone.utc)

    return RawSignal(
        signal_id         = item.get("id", ""),
        source_url        = item.get("url", ""),
        headline          = item.get("title", ""),
        summary           = item.get("summary", ""),
        published_at      = published_at,
        category          = category,
        severity          = severity,
        affected_tickers  = tickers,
        raw_text          = f"{item.get('title', '')} {item.get('summary', '')}",
        supplier          = supplier,
        hermes_signal_type= signal_type,
    )


class IcarusAgent:
    def __init__(self, api_key: Optional[str] = None, base_url: str = HERMES_BASE_URL):
        self._api_key  = api_key or os.getenv("HERMES_API_KEY", "")
        self._base_url = base_url.rstrip("/")
        self._seen: set[str] = set()
        self.kb = AgentKnowledgeBase("icarus")
        if not self._api_key:
            logger.warning("[ICARUS] HERMES_API_KEY not set — requests will be rejected.")

    @property
    def _headers(self) -> dict:
        return {"x-api-key": self._api_key}

    def health(self) -> AgentHealth:
        try:
            r = requests.get(f"{self._base_url}/health", headers=self._headers, timeout=5)
            return AgentHealth.HEALTHY if r.status_code == 200 else AgentHealth.DEGRADED
        except Exception:
            return AgentHealth.FAILED

    def fetch(self) -> list[RawSignal]:
        signals: list[RawSignal] = []
        try:
            signals = self._fetch_briefing()
        except Exception as exc:
            logger.error("[ICARUS] Hermes /briefing failed: %s", exc)
        logger.info("[ICARUS] %d new signal(s) from Hermes.", len(signals))
        # Publish to Kafka event bus (no-op if Kafka unavailable)
        if signals:
            from core.kafka_bus import publish_raw_signal
            for sig in signals:
                publish_raw_signal(sig)
        return signals

    def fetch_company(self, company: str) -> list[RawSignal]:
        try:
            resp = requests.get(f"{self._base_url}/query/{company}", headers=self._headers, timeout=15)
            resp.raise_for_status()
            return self._parse_items(resp.json().get("items", []))
        except Exception as exc:
            logger.error("[ICARUS] /query/%s failed: %s", company, exc)
            return []

    def search(self, query: str) -> list[RawSignal]:
        try:
            resp = requests.get(f"{self._base_url}/search", headers=self._headers,
                                params={"q": query}, timeout=15)
            resp.raise_for_status()
            return self._parse_items(resp.json().get("results", []))
        except Exception as exc:
            logger.error("[ICARUS] /search failed: %s", exc)
            return []

    def _fetch_briefing(self) -> list[RawSignal]:
        resp = requests.get(f"{self._base_url}/briefing", headers=self._headers, timeout=20)
        resp.raise_for_status()
        data  = resp.json()
        items = data.get("signals", data.get("items", []))
        return self._parse_items(items)

    def _parse_items(self, items: list[dict]) -> list[RawSignal]:
        results: list[RawSignal] = []
        for item in items:
            sid = item.get("id", "")
            if sid in self._seen:
                continue
            self._seen.add(sid)
            sig = _map_signal(item)
            if sig is not None:
                results.append(sig)
        return results
