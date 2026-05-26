"""
Agent 1 — Icarus Signal Watcher
Pulls classified signals directly from Hermes (live on Railway).
Imports only from core.types — never from other agents.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from core.agent_knowledge import AgentKnowledgeBase
from core.types import AgentHealth, RawSignal, Severity, SignalCategory

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
    # Extended common suppliers
    "Cisco": "CSCO", "Cisco Systems": "CSCO",
    "Meta": "META", "Meta Platforms": "META",
    "Google": "GOOGL", "Alphabet": "GOOGL",
    "Netflix": "NFLX", "AMD": "AMD",
    "Broadcom": "AVGO", "Texas Instruments": "TXN",
    "Micron": "MU", "Micron Technology": "MU",
    "ASML": "ASML", "Applied Materials": "AMAT",
    "Lam Research": "LRCX", "KLA": "KLAC",
    "Taiwan Semiconductor": "TSM",
    "JPMorgan": "JPM", "Goldman Sachs": "GS",
    "Bank of America": "BAC", "Morgan Stanley": "MS",
    "Salesforce": "CRM", "Oracle": "ORCL",
    "IBM": "IBM", "Accenture": "ACN",
    "Palantir": "PLTR", "Snowflake": "SNOW",
    "Zoom": "ZM", "Zoom Video": "ZM", "Zoom Video Communications": "ZM",
    "Workday": "WDAY", "ServiceNow": "NOW", "HubSpot": "HUBS",
    "Okta": "OKTA", "CrowdStrike": "CRWD", "Palo Alto Networks": "PANW",
    "Zscaler": "ZS", "Fortinet": "FTNT", "SentinelOne": "S",
    "MongoDB": "MDB", "Elastic": "ESTC", "Confluent": "CFLT",
    "Cloudflare": "NET", "Datadog": "DDOG", "Dynatrace": "DT",
    "UiPath": "PATH", "Veeva Systems": "VEEV", "Workiva": "WK",
    "Uber": "UBER", "Airbnb": "ABNB",
    "Boeing": "BA", "Lockheed Martin": "LMT",
    "ExxonMobil": "XOM", "Chevron": "CVX",
    "Pfizer": "PFE", "Johnson & Johnson": "JNJ",
    "Novo Nordisk": "NVO", "AstraZeneca": "AZN",
    "ASML Holding": "ASML",
}

_APOLLO_TICKER_MAP_PATH = Path("data/ticker_map.json")

# Division/product names that Hermes sends → parent company ticker
# These are NOT in the default map because they're not company names
_DIVISION_PARENT_MAP: dict[str, str] = {
    "Google Cloud": "GOOGL", "Google Cloud Platform": "GOOGL",
    "Amazon Web Services": "AMZN", "AWS": "AMZN",
    "Microsoft Azure": "MSFT", "Azure": "MSFT",
    "Microsoft 365": "MSFT", "Microsoft Teams": "MSFT",
    "Apple Silicon": "AAPL", "Apple Services": "AAPL",
    "Meta AI": "META", "WhatsApp": "META", "Instagram": "META",
    "YouTube": "GOOGL", "DeepMind": "GOOGL",
    "Waymo": "GOOGL",
    "Alexa": "AMZN", "Amazon Prime": "AMZN",
    "Tesla Energy": "TSLA", "Tesla Autopilot": "TSLA",
    "Nvidia AI": "NVDA", "CUDA": "NVDA",
    "Workday": "WDAY", "Workday HCM": "WDAY",
    "Zoom": "ZM", "Zoom Video": "ZM",
    "Salesforce Einstein": "CRM",
    "ServiceNow": "NOW",
    "Palantir AIP": "PLTR",
    "Snowflake Cortex": "SNOW",
}

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class _UpstashRedis:
    """Minimal Upstash REST client for the SISMEMBER / SADD / EXPIRE commands."""

    def __init__(self, url: str, token: str):
        import httpx
        self._url     = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._client  = httpx.Client(timeout=3.0)

    def _cmd(self, *args):
        resp = self._client.post(self._url, headers=self._headers, json=list(args))
        resp.raise_for_status()
        return resp.json().get("result")

    def sismember(self, key: str, member: str) -> bool:
        return bool(self._cmd("SISMEMBER", key, member))

    def sadd(self, key: str, *members: str) -> int:
        return int(self._cmd("SADD", key, *members))

    def expire(self, key: str, seconds: int) -> int:
        return int(self._cmd("EXPIRE", key, seconds))


def _sanitize_signal_id(raw_id: str) -> str:
    """Ensure signal_id is a valid UUID — generate one if Hermes sends a truncated ID."""
    if raw_id and _UUID_RE.match(raw_id.strip()):
        return raw_id.strip()
    # Deterministic UUID from the raw ID so same signal always gets same UUID
    return str(uuid.uuid5(uuid.NAMESPACE_URL, raw_id or str(uuid.uuid4())))


def _resolve_ticker(supplier: str) -> str | None:
    """Look up ticker: division map → hardcoded map → Apollo's live ticker_map.json."""
    # Division/product names first — they'd never match the company map
    ticker = _DIVISION_PARENT_MAP.get(supplier)
    if ticker:
        return ticker
    ticker = _SUPPLIER_TICKER_MAP.get(supplier)
    if ticker:
        return ticker
    try:
        data = json.loads(_APOLLO_TICKER_MAP_PATH.read_text(encoding="utf-8"))
        ticker = data.get(supplier)
        if ticker:
            return ticker
        lower = supplier.lower()
        for name, t in data.items():
            if name.lower() == lower or name.lower() in lower or lower in name.lower():
                return t
    except Exception:
        pass
    return None


_MAX_SIGNAL_AGE_HOURS = 168  # 7 days — wider window while building Pythia trade history


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
    ticker   = _resolve_ticker(supplier)
    tickers  = [ticker] if ticker else []

    try:
        published_at = datetime.fromisoformat(item["published"].replace("Z", "+00:00"))
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
    except Exception:
        published_at = datetime.now(timezone.utc)

    age_hours = (datetime.now(timezone.utc) - published_at).total_seconds() / 3600
    if age_hours > _MAX_SIGNAL_AGE_HOURS:
        logger.info("[ICARUS] Dropping stale signal (%dh old): %s", int(age_hours), item.get("title", "")[:60])
        return None

    return RawSignal(
        signal_id         = _sanitize_signal_id(item.get("id", "")),
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


_SEEN_REDIS_KEY = "icarus:seen_signals"
_SEEN_TTL_SECONDS = 7 * 24 * 3600  # matches _MAX_SIGNAL_AGE_HOURS


class IcarusAgent:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = HERMES_BASE_URL,
        ticker_resolver=None,
    ):
        self._api_key  = api_key or os.getenv("HERMES_API_KEY", "")
        self._base_url = base_url.rstrip("/")
        self._seen: set[str] = set()  # in-memory fallback
        self._redis = self._init_redis()
        self.kb = AgentKnowledgeBase("icarus")
        # Optional callable(supplier_name) -> ticker | None injected by Zeus
        # Falls back to the static _resolve_ticker when not provided
        self._ticker_resolver = ticker_resolver or _resolve_ticker
        if not self._api_key:
            logger.warning("[ICARUS] HERMES_API_KEY not set — requests will be rejected.")

    def _init_redis(self):
        """Connect to Upstash Redis for persistent seen-set. Returns None if unavailable."""
        try:
            url   = os.getenv("UPSTASH_REDIS_REST_URL", "")
            token = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
            if not url or not token:
                return None
            # Lightweight wrapper — no dependency beyond httpx (already installed)
            return _UpstashRedis(url, token)
        except Exception as exc:
            logger.debug("[ICARUS] Redis init failed (using in-memory seen set): %s", exc)
            return None

    def _is_seen(self, sid: str) -> bool:
        if self._redis:
            try:
                return bool(self._redis.sismember(_SEEN_REDIS_KEY, sid))
            except Exception:
                pass
        return sid in self._seen

    def _mark_seen(self, sid: str) -> None:
        self._seen.add(sid)
        if self._redis:
            try:
                self._redis.sadd(_SEEN_REDIS_KEY, sid)
                self._redis.expire(_SEEN_REDIS_KEY, _SEEN_TTL_SECONDS)
            except Exception:
                pass

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
        from concurrent.futures import ThreadPoolExecutor, as_completed

        # First pass: map all items, collect those needing live ticker resolution
        pre: list[tuple[RawSignal, str]] = []  # (signal, supplier_needing_lookup)
        for item in items:
            sid = item.get("id", "")
            if self._is_seen(sid):
                continue
            self._mark_seen(sid)
            sig = _map_signal(item)
            if sig is None:
                continue
            if not sig.affected_tickers and sig.supplier:
                pre.append((sig, sig.supplier))
            else:
                pre.append((sig, ""))

        # Parallel live lookup for unknown suppliers (non-blocking for known ones)
        needs_lookup = [(sig, sup) for sig, sup in pre if sup]
        if needs_lookup:
            with ThreadPoolExecutor(max_workers=min(len(needs_lookup), 4)) as ex:
                futures = {ex.submit(self._ticker_resolver, sup): sig for sig, sup in needs_lookup}
                for future in as_completed(futures, timeout=10):
                    sig = futures[future]
                    try:
                        resolved = future.result()
                        if resolved:
                            sig.affected_tickers = [resolved]
                            logger.info("[ICARUS] Live-resolved ticker: %s → %s", sig.supplier, resolved)
                        else:
                            logger.warning("[ICARUS] No ticker for '%s' — Zeus will reject as unexecutable", sig.supplier)
                    except Exception as exc:
                        logger.warning("[ICARUS] Ticker lookup failed for '%s': %s", sig.supplier, exc)

        return [sig for sig, _ in pre]
