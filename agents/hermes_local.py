"""
Hermes Local — SEC EDGAR + Finnhub signal fetcher.

Replaces the external Hermes service. Runs as a scheduled task inside Zeus.
Writes to the same `signals` table Icarus already consumes — no schema changes.

Sources:
  - SEC EDGAR latest 8-K filings (earnings, M&A, material events) — no API key
  - Finnhub company news + general market news — free tier, 60 req/min

Schedule: every 30 minutes via APScheduler (wired into main.py startup).
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from datetime import datetime, timezone

import requests as _requests

logger = logging.getLogger("hermes_local")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")
EDGAR_USER_AGENT = "Pantheon OS eugnmueller@googlemail.com"  # required by SEC fair-use policy

# Tickers we actively watch — EDGAR lookups are per-CIK so we also maintain a
# symbol→CIK map for the most important names. Finnhub news is ticker-based.
WATCHLIST: list[str] = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
    "AMD", "INTC", "TSM", "AVGO", "QCOM",
    "JPM", "GS", "MS", "BAC",
    "XOM", "CVX",
    "SPY", "QQQ",
]

# SEC EDGAR CIK map for watchlist tickers (CIK is the stable identifier).
# Populated lazily from company_tickers.json on first use.
_CIK_CACHE: dict[str, str] = {}

# ---------------------------------------------------------------------------
# Category / urgency classification helpers
# ---------------------------------------------------------------------------

def _classify_filing(form_type: str, title: str) -> tuple[str, str, bool]:
    """Return (hermes_signal_type, urgency, is_significant) for an EDGAR filing."""
    title_lower = title.lower()
    if form_type in ("8-K", "8-K/A"):
        if any(k in title_lower for k in ("earnings", "results", "guidance", "revenue", "profit")):
            return "EARNINGS", "HIGH", True
        if any(k in title_lower for k in ("merger", "acquisition", "acquir", "purchase agreement")):
            return "ACQUISITION", "HIGH", True
        if any(k in title_lower for k in ("sec investigation", "subpoena", "enforcement", "fine", "penalty")):
            return "REGULATORY", "HIGH", True
        if any(k in title_lower for k in ("ceo", "cfo", "director", "officer", "resign", "appoint")):
            return "OTHER", "MEDIUM", False
        return "OTHER", "MEDIUM", False
    if form_type in ("10-Q", "10-K"):
        return "EARNINGS", "MEDIUM", False
    return "OTHER", "LOW", False


def _classify_news(headline: str, category: str) -> tuple[str, str, bool]:
    """Return (hermes_signal_type, urgency, is_significant) for a Finnhub news item."""
    h = headline.lower()
    if any(k in h for k in ("earnings", "beats", "misses", "revenue", "eps", "guidance", "outlook")):
        return "EARNINGS", "HIGH", True
    if any(k in h for k in ("acqui", "merger", "buyout", "takeover", "deal")):
        return "ACQUISITION", "HIGH", True
    if any(k in h for k in ("fda", "sec ", "doj", "ftc", "fine", "lawsuit", "investigation", "penalty")):
        return "REGULATORY", "HIGH", True
    if any(k in h for k in ("layoff", "cut", "hiring", "headcount")):
        return "LAYOFFS_HIRING", "MEDIUM", False
    if any(k in h for k in ("partnership", "contract", "agreement", "collaborate")):
        return "PARTNERSHIP", "MEDIUM", False
    if any(k in h for k in ("launch", "release", "product", "announce")):
        return "PRODUCT_RELEASE", "LOW", False
    return "OTHER", "LOW", False


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def _signal_id(source: str, unique_key: str) -> str:
    """Stable UUID-shaped ID from source + unique key — prevents duplicate inserts."""
    h = hashlib.md5(f"{source}:{unique_key}".encode()).hexdigest()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


# ---------------------------------------------------------------------------
# SEC EDGAR
# ---------------------------------------------------------------------------

def _load_cik_map() -> None:
    """Lazy-load symbol→CIK from EDGAR's company_tickers.json."""
    global _CIK_CACHE
    if _CIK_CACHE:
        return
    try:
        resp = _requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers={"User-Agent": EDGAR_USER_AGENT},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        for entry in data.values():
            ticker = entry.get("ticker", "").upper()
            cik = str(entry.get("cik_str", "")).zfill(10)
            if ticker:
                _CIK_CACHE[ticker] = cik
        logger.info("[EDGAR] CIK map loaded: %d tickers", len(_CIK_CACHE))
    except Exception as exc:
        logger.warning("[EDGAR] Failed to load CIK map: %s", exc)


def fetch_edgar_filings(max_per_ticker: int = 3) -> list[dict]:
    """
    Fetch recent 8-K filings for watchlist tickers from EDGAR EFTS full-text search.
    Returns list of signal dicts ready to upsert into Supabase.
    """
    _load_cik_map()
    signals: list[dict] = []
    seen: set[str] = set()

    for ticker in WATCHLIST:
        cik = _CIK_CACHE.get(ticker)
        if not cik:
            continue
        try:
            sub_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
            resp = _requests.get(
                sub_url,
                headers={"User-Agent": EDGAR_USER_AGENT},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            recent = data.get("filings", {}).get("recent", {})
            forms       = recent.get("form", [])
            dates       = recent.get("filingDate", [])
            accessions  = recent.get("accessionNumber", [])
            descriptions = recent.get("primaryDocument", [])
            items_list  = recent.get("items", [])

            count = 0
            for i, form in enumerate(forms):
                if count >= max_per_ticker:
                    break
                if form not in ("8-K", "8-K/A", "10-Q"):
                    continue
                accession = accessions[i].replace("-", "")
                sig_id = _signal_id("edgar", accession)
                if sig_id in seen:
                    continue
                seen.add(sig_id)

                filing_date = dates[i] if i < len(dates) else ""
                item_desc   = items_list[i] if i < len(items_list) else ""
                title = f"{ticker} {form}: {item_desc}" if item_desc else f"{ticker} {form} filing"
                source_url  = (
                    f"https://www.sec.gov/Archives/edgar/data/"
                    f"{int(cik)}/{accession}/{descriptions[i] if i < len(descriptions) else ''}"
                )

                sig_type, urgency, is_sig = _classify_filing(form, title)

                # Skip low-value filings during market hours to reduce noise
                if not is_sig and urgency == "LOW":
                    continue

                try:
                    published_at = datetime.strptime(filing_date, "%Y-%m-%d").replace(
                        tzinfo=timezone.utc
                    ).isoformat()
                except ValueError:
                    published_at = datetime.now(timezone.utc).isoformat()

                signals.append({
                    "signal_id":          sig_id,
                    "headline":           title,
                    "summary":            f"SEC {form} filing for {ticker}. Item: {item_desc}",
                    "source_url":         source_url,
                    "published_at":       published_at,
                    "category":           "earnings_surprise" if sig_type == "EARNINGS" else
                                          "regulatory_action" if sig_type == "REGULATORY" else
                                          "positive_news"     if sig_type in ("ACQUISITION", "PARTNERSHIP", "PRODUCT_RELEASE") else
                                          "neutral",
                    "severity":           "HIGH" if urgency == "HIGH" else "MEDIUM" if urgency == "MEDIUM" else "LOW",
                    "affected_tickers":   [ticker],
                    "raw_text":           title,
                    "supplier":           "SEC EDGAR",
                    "hermes_signal_type": sig_type,
                    "urgency":            urgency,
                    "is_significant":     is_sig,
                    "consumed_by_icarus": False,
                })
                count += 1

            # Respect EDGAR rate limit — 10 req/s max
            time.sleep(0.15)

        except Exception as exc:
            logger.warning("[EDGAR] %s fetch failed: %s", ticker, exc)

    logger.info("[EDGAR] Fetched %d signals for %d tickers", len(signals), len(WATCHLIST))
    return signals


# ---------------------------------------------------------------------------
# Finnhub
# ---------------------------------------------------------------------------

def fetch_finnhub_news(lookback_hours: int = 1) -> list[dict]:
    """
    Fetch recent company news from Finnhub for watchlist tickers.
    Falls back gracefully if API key is missing or rate-limited.
    """
    if not FINNHUB_API_KEY:
        logger.warning("[FINNHUB] No FINNHUB_API_KEY — skipping")
        return []

    signals: list[dict] = []
    seen: set[str] = set()

    now = int(time.time())
    from_ts = now - (lookback_hours * 3600)

    for ticker in WATCHLIST:
        try:
            url = (
                f"https://finnhub.io/api/v1/company-news"
                f"?symbol={ticker}"
                f"&from={datetime.utcfromtimestamp(from_ts).strftime('%Y-%m-%d')}"
                f"&to={datetime.utcfromtimestamp(now).strftime('%Y-%m-%d')}"
                f"&token={FINNHUB_API_KEY}"
            )
            resp = _requests.get(url, timeout=10)
            if resp.status_code == 429:
                logger.warning("[FINNHUB] Rate limited — pausing 60s")
                time.sleep(60)
                resp = _requests.get(url, timeout=10)
            resp.raise_for_status()

            for item in resp.json() or []:
                article_id = str(item.get("id", ""))
                sig_id = _signal_id("finnhub", article_id or item.get("url", ""))
                if sig_id in seen:
                    continue
                seen.add(sig_id)

                headline = item.get("headline", "")
                if not headline:
                    continue

                # Filter to items published within our lookback window
                pub_ts = item.get("datetime", 0)
                if pub_ts < from_ts:
                    continue

                sig_type, urgency, is_sig = _classify_news(headline, item.get("category", ""))

                # Skip obvious noise
                if urgency == "LOW" and not is_sig:
                    continue

                published_at = datetime.utcfromtimestamp(pub_ts).replace(
                    tzinfo=timezone.utc
                ).isoformat()

                signals.append({
                    "signal_id":          sig_id,
                    "headline":           headline,
                    "summary":            item.get("summary", "")[:500],
                    "source_url":         item.get("url", ""),
                    "published_at":       published_at,
                    "category":           "earnings_surprise" if sig_type == "EARNINGS" else
                                          "regulatory_action" if sig_type == "REGULATORY" else
                                          "positive_news"     if sig_type in ("ACQUISITION", "PARTNERSHIP", "PRODUCT_RELEASE", "FUNDING") else
                                          "neutral",
                    "severity":           "HIGH" if urgency == "HIGH" else "MEDIUM" if urgency == "MEDIUM" else "LOW",
                    "affected_tickers":   [ticker],
                    "raw_text":           f"{headline}\n{item.get('summary', '')}",
                    "supplier":           f"Finnhub/{item.get('source', 'news')}",
                    "hermes_signal_type": sig_type,
                    "urgency":            urgency,
                    "is_significant":     is_sig,
                    "consumed_by_icarus": False,
                })

            # Finnhub free tier: 60 req/min → ~1 req/s safe
            time.sleep(1.1)

        except Exception as exc:
            logger.warning("[FINNHUB] %s fetch failed: %s", ticker, exc)

    logger.info("[FINNHUB] Fetched %d signals for %d tickers", len(signals), len(WATCHLIST))
    return signals


# ---------------------------------------------------------------------------
# General market news (Finnhub /news endpoint — no ticker required)
# ---------------------------------------------------------------------------

def fetch_finnhub_market_news() -> list[dict]:
    """Fetch general market-moving news from Finnhub (forex, crypto, general categories)."""
    if not FINNHUB_API_KEY:
        return []

    signals: list[dict] = []
    seen: set[str] = set()
    cutoff = int(time.time()) - 3600  # last hour only

    for category in ("general", "merger"):
        try:
            url = f"https://finnhub.io/api/v1/news?category={category}&token={FINNHUB_API_KEY}"
            resp = _requests.get(url, timeout=10)
            resp.raise_for_status()

            for item in resp.json() or []:
                pub_ts = item.get("datetime", 0)
                if pub_ts < cutoff:
                    continue
                headline = item.get("headline", "")
                if not headline:
                    continue

                sig_id = _signal_id("finnhub_mkt", str(item.get("id", item.get("url", headline))))
                if sig_id in seen:
                    continue
                seen.add(sig_id)

                sig_type, urgency, is_sig = _classify_news(headline, category)
                if urgency == "LOW":
                    continue

                published_at = datetime.utcfromtimestamp(pub_ts).replace(
                    tzinfo=timezone.utc
                ).isoformat()

                # For market-wide news, extract tickers mentioned in headline
                tickers_mentioned = [t for t in WATCHLIST if t in headline.upper().split()]

                signals.append({
                    "signal_id":          sig_id,
                    "headline":           headline,
                    "summary":            item.get("summary", "")[:500],
                    "source_url":         item.get("url", ""),
                    "published_at":       published_at,
                    "category":           "macro_shift" if category == "general" else
                                          "positive_news" if sig_type == "ACQUISITION" else "neutral",
                    "severity":           "HIGH" if urgency == "HIGH" else "MEDIUM",
                    "affected_tickers":   tickers_mentioned,
                    "raw_text":           f"{headline}\n{item.get('summary', '')}",
                    "supplier":           f"Finnhub/{item.get('source', category)}",
                    "hermes_signal_type": sig_type,
                    "urgency":            urgency,
                    "is_significant":     is_sig,
                    "consumed_by_icarus": False,
                })

            time.sleep(1.1)
        except Exception as exc:
            logger.warning("[FINNHUB] market news (%s) failed: %s", category, exc)

    logger.info("[FINNHUB] Market news: %d signals", len(signals))
    return signals


# ---------------------------------------------------------------------------
# Supabase writer
# ---------------------------------------------------------------------------

def _upsert_signals(signals: list[dict]) -> int:
    """Upsert signals into Supabase. Returns count of new rows inserted."""
    if not signals:
        return 0
    try:
        import core.supabase_client as supa
        client = supa.get_client()
        # upsert on signal_id — safe to re-run, won't double-insert
        client.table("signals").upsert(signals, on_conflict="signal_id").execute()
        logger.info("[HERMES_LOCAL] Upserted %d signals", len(signals))
        return len(signals)
    except Exception as exc:
        logger.error("[HERMES_LOCAL] Supabase upsert failed: %s", exc)
        return 0


# ---------------------------------------------------------------------------
# Main entry point — called by scheduler
# ---------------------------------------------------------------------------

def run_cycle() -> dict[str, int]:
    """
    Full fetch cycle: EDGAR filings + Finnhub company news + Finnhub market news.
    Called every 30 minutes by the scheduler in main.py.
    Returns counts per source.
    """
    logger.info("[HERMES_LOCAL] Starting fetch cycle")

    edgar_sigs   = fetch_edgar_filings(max_per_ticker=3)
    finnhub_sigs = fetch_finnhub_news(lookback_hours=1)
    market_sigs  = fetch_finnhub_market_news()

    all_signals = edgar_sigs + finnhub_sigs + market_sigs

    # Deduplicate across sources by signal_id
    seen: set[str] = set()
    deduped = []
    for s in all_signals:
        if s["signal_id"] not in seen:
            seen.add(s["signal_id"])
            deduped.append(s)

    total = _upsert_signals(deduped)

    result = {
        "edgar":   len(edgar_sigs),
        "finnhub": len(finnhub_sigs),
        "market":  len(market_sigs),
        "total":   total,
    }
    logger.info("[HERMES_LOCAL] Cycle complete: %s", result)
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    from pathlib import Path

    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    result = run_cycle()
    print(result)
