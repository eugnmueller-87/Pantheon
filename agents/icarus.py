"""
Agent 1 — Icarus Signal Watcher

Primary path — reads unconsumed signals from Supabase.
Hermes (Railway) is RETIRED — signals are written directly to Supabase
by whatever ingestion process replaces it (or manually for testing).

The Hermes API fallback code is kept but disabled by default.
Set HERMES_FALLBACK_ENABLED=true in env to re-enable it temporarily
(e.g. during migration of a new crawler service).

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
    # OpenAI is private — route to MSFT (largest listed beneficiary, ~49% stake)
    "OpenAI": "MSFT",
}

# Private companies / non-tradeable tickers — drop signals cleanly before Apollo lookup.
# These would otherwise resolve to phantom tickers (e.g. OPENAI-USD) and waste LLM calls.
_PRIVATE_COMPANY_SUPPLIERS: frozenset[str] = frozenset({
    "OpenAI",          # private — resolves to OPENAI-USD phantom ticker
    "Anthropic",       # private
    "xAI",             # private (Elon Musk's AI co)
    "Mistral AI",      # private
    "Cohere",          # private
    "Stability AI",    # private
    "Hugging Face",    # private
})

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

    def incr(self, key: str) -> int:
        return int(self._cmd("INCR", key))

    def get(self, key: str) -> str | None:
        return self._cmd("GET", key)


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

# Static regulatory filings — never actionable, always waste an LLM call
_FILING_KEYWORDS = (
    "10-q", "10-k", "10q", "10k",
    "annual report", "annual filing",
    "quarterly report", "quarterly filing",
    "proxy statement", "def 14a",
    "form 4",   # insider ownership forms (not the same as insider *trades*)
    "8-k/a",    # amended filing — stale by definition
)


def _is_static_filing(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in _FILING_KEYWORDS)


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
    # Drop signals from private companies before they waste an LLM call
    if supplier in _PRIVATE_COMPANY_SUPPLIERS:
        logger.info("[ICARUS] Dropping private-company signal (%s): %s", supplier, item.get("title", "")[:60])
        return None
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

    title = item.get("title", "")
    if _is_static_filing(title):
        logger.info("[ICARUS] Dropping static filing: %s", title[:80])
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

# Quality learning — Redis keys
# icarus:quality:{pattern}:seen   — how many times Icarus sent this pattern
# icarus:quality:{pattern}:approved — how many times Zeus approved it
_QUALITY_MIN_SAMPLES = 10    # need at least this many before filtering
_QUALITY_MAX_REJECT  = 0.85  # suppress pattern if rejection rate >= 85%


def _quality_pattern(sig: "RawSignal") -> str:
    """Stable key identifying a signal pattern: hermes_type + first keyword of headline."""
    signal_type = sig.hermes_signal_type or "OTHER"
    # First meaningful word from headline (skip articles/conjunctions)
    skip = {"the", "a", "an", "in", "of", "for", "on", "and", "or", "is"}
    keyword = next(
        (w.lower() for w in sig.headline.split() if w.lower() not in skip and len(w) > 3),
        "unknown",
    )
    return f"{signal_type}:{keyword}"


class _SignalQualityFilter:
    """
    Learns which signal patterns Zeus consistently rejects and suppresses them
    before they burn an LLM call. Stats persist in Redis (same Upstash instance).

    Pattern key = hermes_signal_type + first meaningful headline keyword.
    A pattern is suppressed once it has >=10 samples with >=85% rejection rate.
    """

    def __init__(self, redis: _UpstashRedis | None):
        self._redis = redis

    def should_suppress(self, sig: "RawSignal") -> bool:
        """Return True if this pattern is reliably rejected by Zeus."""
        if self._redis is None:
            return False
        pattern = _quality_pattern(sig)
        try:
            seen_raw     = self._redis.get(f"icarus:quality:{pattern}:seen")
            approved_raw = self._redis.get(f"icarus:quality:{pattern}:approved")
            seen     = int(seen_raw)     if seen_raw     else 0
            approved = int(approved_raw) if approved_raw else 0
            if seen < _QUALITY_MIN_SAMPLES:
                return False
            rejection_rate = (seen - approved) / seen
            if rejection_rate >= _QUALITY_MAX_REJECT:
                logger.info(
                    "[ICARUS] Suppressing low-quality pattern '%s' "
                    "(rejection rate %.0f%% over %d samples)",
                    pattern, rejection_rate * 100, seen,
                )
                return True
        except Exception:
            pass
        return False

    def record_seen(self, sig: "RawSignal") -> None:
        if self._redis is None:
            return
        pattern = _quality_pattern(sig)
        try:
            self._redis.incr(f"icarus:quality:{pattern}:seen")
            self._redis.incr("icarus:quality:totals:seen")
        except Exception:
            pass

    def record_approved(self, sig: "RawSignal") -> None:
        if self._redis is None:
            return
        pattern = _quality_pattern(sig)
        try:
            self._redis.incr(f"icarus:quality:{pattern}:approved")
            self._redis.incr("icarus:quality:totals:approved")
        except Exception:
            pass


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
        self._quality = _SignalQualityFilter(self._redis)
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
        """Healthy when Supabase is reachable (Hermes Railway is retired)."""
        try:
            import core.supabase_client as supa
            supa.get_client()  # verify connection — raises if env vars missing
            return AgentHealth.HEALTHY
        except Exception:
            return AgentHealth.FAILED

    # ------------------------------------------------------------------
    # Primary public API
    # ------------------------------------------------------------------

    def fetch(self) -> list[RawSignal]:
        """
        Fetch new signals from Supabase (written by the Hermes Supabase worker).
        Hermes Railway API fallback is only used when HERMES_FALLBACK_ENABLED=true.
        Publishes to Kafka regardless of which path was used.
        """
        signals = self._fetch_from_supabase()

        # Optional Railway fallback — disabled by default (Hermes retired)
        if not signals and os.getenv("HERMES_FALLBACK_ENABLED", "").lower() == "true":
            logger.info("[ICARUS] No unconsumed signals in Supabase — trying Hermes API fallback.")
            try:
                signals = self._fetch_from_hermes_and_persist()
            except Exception as exc:
                logger.warning("[ICARUS] Hermes fallback unavailable: %s", exc)

        logger.info("[ICARUS] %d new signal(s) ready for Zeus.", len(signals))

        if signals:
            from core.kafka_bus import publish_raw_signal
            for sig in signals:
                publish_raw_signal(sig)

        return signals

    def fetch_company(self, company: str) -> list[RawSignal]:
        """Ad-hoc company query — reads from Supabase filtered by supplier name."""
        try:
            import core.supabase_client as supa
            client = supa.get_client()
            res = (
                client.table("signals")
                .select(
                    "signal_id, hermes_id, source_url, headline, summary, "
                    "published_at, category, severity, affected_tickers, "
                    "raw_text, supplier, hermes_signal_type, urgency, is_significant"
                )
                .ilike("supplier", f"%{company}%")
                .eq("consumed_by_icarus", False)
                .order("published_at", desc=False)
                .limit(50)
                .execute()
            )
            rows = res.data or []
            return self._map_supabase_rows(rows)
        except Exception as exc:
            logger.error("[ICARUS] fetch_company(%s) failed: %s", company, exc)
            return []

    def search(self, query: str) -> list[RawSignal]:
        """Full-text search — reads from Supabase signals table via ilike on headline."""
        try:
            import core.supabase_client as supa
            client = supa.get_client()
            res = (
                client.table("signals")
                .select(
                    "signal_id, hermes_id, source_url, headline, summary, "
                    "published_at, category, severity, affected_tickers, "
                    "raw_text, supplier, hermes_signal_type, urgency, is_significant"
                )
                .ilike("headline", f"%{query}%")
                .eq("consumed_by_icarus", False)
                .order("published_at", desc=False)
                .limit(50)
                .execute()
            )
            rows = res.data or []
            return self._map_supabase_rows(rows)
        except Exception as exc:
            logger.error("[ICARUS] search(%s) failed: %s", query, exc)
            return []

    # ------------------------------------------------------------------
    # Supabase primary path
    # ------------------------------------------------------------------

    def _fetch_from_supabase(self) -> list[RawSignal]:
        """
        Read unconsumed signals from Supabase, map them to RawSignal,
        apply quality filters, and mark consumed in one atomic DB call.
        """
        _USE_SUPABASE = bool(
            os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        )
        if not _USE_SUPABASE:
            return []

        try:
            import core.supabase_client as supa
            rows = supa.get_unconsumed_signals(limit=100)
        except Exception as exc:
            logger.warning("[ICARUS] Supabase read failed: %s", exc)
            return []

        if not rows:
            return []

        signals, consumed_ids = self._map_supabase_rows(rows, mark_consumed=False)

        # Mark all processed rows consumed in one atomic DB call
        if consumed_ids:
            import core.supabase_client as supa
            n = supa.mark_signals_consumed(consumed_ids)
            logger.info("[ICARUS] Supabase: %d signal(s) fetched, %d row(s) marked consumed.", len(signals), n)

        return signals

    def _map_supabase_rows(
        self,
        rows: list[dict],
        mark_consumed: bool = False,
    ) -> tuple[list[RawSignal], list[str]]:
        """
        Shared mapper: convert Supabase signals rows → (RawSignal list, consumed_id list).
        consumed_id list contains the signal_id of every row that was processed
        (including dropped ones) so callers can mark them all consumed atomically.
        """
        signals: list[RawSignal] = []
        consumed_ids: list[str] = []

        for row in rows:
            # Build a Hermes-style item dict from the DB columns
            item = {
                "id":             row.get("hermes_id") or row.get("signal_id", ""),
                "url":            row.get("source_url", ""),
                "title":          row.get("headline", ""),
                "summary":        row.get("summary", ""),
                "published":      row.get("published_at", ""),
                "signal_type":    row.get("hermes_signal_type", "OTHER"),
                "urgency":        row.get("urgency", "LOW"),
                "is_significant": row.get("is_significant", False),
                "supplier":       row.get("supplier", ""),
            }
            sig = _map_signal(item)
            if sig is None:
                consumed_ids.append(row["signal_id"])  # filtered — still mark consumed
                continue

            # Use the Supabase UUID so decision traces correlate correctly
            sig.signal_id = row["signal_id"]

            # Use pre-resolved tickers if the writer stored them
            db_tickers = row.get("affected_tickers") or []
            if db_tickers:
                sig.affected_tickers = db_tickers

            if self._quality.should_suppress(sig):
                consumed_ids.append(row["signal_id"])
                continue

            self._quality.record_seen(sig)

            # Live ticker resolution if still empty
            if not sig.affected_tickers and sig.supplier:
                try:
                    resolved = self._ticker_resolver(sig.supplier)
                    if resolved:
                        sig.affected_tickers = [resolved]
                        logger.info("[ICARUS] Resolved %s → %s", sig.supplier, resolved)
                    else:
                        logger.warning("[ICARUS] No ticker for '%s' — Zeus will reject", sig.supplier)
                except Exception as exc:
                    logger.warning("[ICARUS] Ticker lookup failed for '%s': %s", sig.supplier, exc)

            signals.append(sig)
            consumed_ids.append(row["signal_id"])

        return signals, consumed_ids

    # ------------------------------------------------------------------
    # Hermes Railway API fallback (disabled by default — Railway retired)
    # Enable with HERMES_FALLBACK_ENABLED=true for temporary migration use
    # ------------------------------------------------------------------

    def _fetch_from_hermes_and_persist(self) -> list[RawSignal]:
        """Poll Hermes /briefing and persist results to Supabase."""
        resp = requests.get(f"{self._base_url}/briefing", headers=self._headers, timeout=20)
        resp.raise_for_status()
        data  = resp.json()
        items = data.get("signals", data.get("items", []))
        return self._parse_hermes_items(items, persist=True)

    def _fetch_briefing(self) -> list[RawSignal]:
        """Called by Zeus when Kafka is up but empty — reads Supabase directly."""
        return self._fetch_from_supabase()

    def _parse_hermes_items(self, items: list[dict], persist: bool = False) -> list[RawSignal]:
        """
        Map raw Hermes dicts to RawSignal.  Optionally persist each item to
        Supabase (idempotent via hermes_id unique constraint).
        """
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
            if self._quality.should_suppress(sig):
                continue
            self._quality.record_seen(sig)
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

        result = [sig for sig, _ in pre]

        # Persist to Supabase so the audit trail is always complete
        if persist:
            self._persist_to_supabase(items, result)

        return result

    def _persist_to_supabase(self, raw_items: list[dict], signals: list[RawSignal]) -> None:
        """
        Write Hermes items to Supabase signals table (idempotent).
        Uses the upsert_hermes_signal RPC so duplicate hermes_ids are silently skipped.
        Marks inserted rows immediately consumed (we just processed them inline).
        """
        _USE_SUPABASE = bool(
            os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        )
        if not _USE_SUPABASE:
            return

        # Build a quick lookup: hermes_id → RawSignal (for resolved tickers)
        sig_by_raw_id: dict[str, RawSignal] = {}
        for sig in signals:
            # signal_id is the sanitised UUID — we need the original Hermes id
            # We stored hermes_signal_type on sig; raw id is harder to recover,
            # so we index by headline+supplier as a key (good enough for matching)
            sig_by_raw_id[f"{sig.headline}|{sig.supplier}"] = sig

        try:
            import core.supabase_client as supa
            _SEVERITY_MAP = {1: "LOW", 2: "MEDIUM", 3: "HIGH", 4: "CRITICAL"}
            _CATEGORY_ENUMS = {
                "supplier_disruption", "positive_news", "earnings_surprise",
                "regulatory_action", "macro_shift",
            }

            for item in raw_items:
                signal_type = item.get("signal_type", "OTHER")
                from core.types import SignalCategory
                category_val = _HERMES_TYPE_MAP.get(signal_type, SignalCategory.NEUTRAL).value
                if category_val not in _CATEGORY_ENUMS:
                    continue  # skip neutral — Icarus already filters these

                try:
                    published_at = datetime.fromisoformat(
                        item.get("published", "").replace("Z", "+00:00")
                    )
                except Exception:
                    published_at = datetime.now(timezone.utc)

                sig_key = f"{item.get('title', '')}|{item.get('supplier', '')}"
                matched_sig = sig_by_raw_id.get(sig_key)
                tickers = matched_sig.affected_tickers if matched_sig else []

                urgency  = item.get("urgency", "LOW")
                severity_level = "HIGH" if (
                    item.get("is_significant") and urgency == "HIGH"
                ) else urgency if urgency in ("HIGH", "MEDIUM", "LOW") else "LOW"

                supa.upsert_hermes_signal({
                    "hermes_id":          item.get("id", ""),
                    "source_url":         item.get("url", ""),
                    "headline":           item.get("title", ""),
                    "summary":            item.get("summary", ""),
                    "published_at":       published_at.isoformat(),
                    "category":           category_val,
                    "severity":           severity_level,
                    "affected_tickers":   tickers,
                    "raw_text":           f"{item.get('title', '')} {item.get('summary', '')}",
                    "supplier":           item.get("supplier", ""),
                    "hermes_signal_type": signal_type,
                    "urgency":            urgency,
                    "is_significant":     bool(item.get("is_significant", False)),
                })
        except Exception as exc:
            logger.warning("[ICARUS] Supabase persist failed (non-fatal): %s", exc)

    # ------------------------------------------------------------------
    # Legacy alias — kept so Zeus's direct _fetch_briefing call still works
    # ------------------------------------------------------------------

    def _parse_items(self, items: list[dict]) -> list[RawSignal]:
        return self._parse_hermes_items(items, persist=False)

    def record_signal_outcome(self, sig: RawSignal, approved: bool) -> None:
        """Called by Zeus after each decision so Icarus can learn which patterns get approved."""
        if approved:
            self._quality.record_approved(sig)

    def approval_rate(self) -> float | None:
        """Current approval rate across all patterns — used by seniority evaluation."""
        if self._redis is None:
            return None
        try:
            # Sum across all tracked patterns via a scan-like approach:
            # we stored per-pattern keys, so query the overall counters
            total_seen     = self._redis.get("icarus:quality:totals:seen")
            total_approved = self._redis.get("icarus:quality:totals:approved")
            seen     = int(total_seen)     if total_seen     else 0
            approved = int(total_approved) if total_approved else 0
            if seen == 0:
                return None
            return approved / seen
        except Exception:
            return None
