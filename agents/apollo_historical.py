"""
Apollo Historical Ingestion — one-shot bootstrap pipeline.

Loads 4 years of historical data into the shared KB so Pythia starts
with a real pattern foundation instead of zero.

Data sources (all public, no auth required except FRED API key):
  1. Earnings history     — yfinance (EPS reported vs estimate, price reaction)
  2. SEC Form 4 insiders  — SEC EDGAR XBRL API (insider buy/sell transactions)
  3. FRED macro series    — Federal Reserve FRED API (rates, spreads, VIX, sentiment)
  4. SEC 8-K supply chain — EDGAR full-text search (material supply chain events)

Run once before paper trading begins:
  from agents.apollo_historical import HistoricalIngestionPipeline
  pipeline = HistoricalIngestionPipeline(kb=zeus.kb)
  summary = pipeline.run()

Or via the /run/research endpoint with ?historical=true (wired in zeus.py).

Import rule: imports from core.types and core.agent_knowledge only.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests
import yfinance as yf

from core.agent_knowledge import AgentKnowledgeBase

logger = logging.getLogger("apollo.historical")

# Core trading universe — these get the deepest historical coverage
_CORE_UNIVERSE = [
    "NVDA", "TSM", "SAP", "SIEGY", "ASML", "INTC", "AMD",
    "QCOM", "BASFY", "DTEGY", "AAPL", "MSFT", "AMZN", "META",
]

# FRED series to ingest
_FRED_SERIES = {
    "FEDFUNDS":       "Fed Funds Rate — monetary policy baseline",
    "T10Y2Y":         "10Y-2Y yield curve spread — recession indicator",
    "BAMLH0A0HYM2":   "HY credit spread — risk appetite proxy",
    "VIXCLS":         "VIX daily close — market fear gauge",
    "UMCSENT":        "University of Michigan consumer sentiment",
    "DGS10":          "10-year Treasury yield",
    "DPCREDIT":       "Discount window primary credit rate",
}

# EDGAR supply chain keywords
_SUPPLY_CHAIN_KEYWORDS = [
    "supply chain disruption", "supplier shortage", "component shortage",
    "manufacturing delay", "logistics disruption", "raw material shortage",
]

_FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
_EDGAR_SEARCH = "https://efts.sec.gov/LATEST/search-index"

# Look back 4 years
_LOOKBACK_YEARS = 4


class HistoricalIngestionPipeline:
    """
    One-shot historical data bootstrap.
    Designed to run before paper trading begins — idempotent, safe to re-run.
    All failures are logged and skipped — never halts the pipeline.
    """

    def __init__(self, knowledge_base=None):
        self._kb = knowledge_base
        self._fred_api_key = os.getenv("FRED_API_KEY", "")
        self._start_date = (
            datetime.now(timezone.utc) - timedelta(days=_LOOKBACK_YEARS * 365)
        ).strftime("%Y-%m-%d")
        self.kb = AgentKnowledgeBase("apollo")

    def run(self) -> dict:
        """
        Run the full historical ingestion pipeline.
        Returns a summary dict with counts and any errors.
        """
        logger.info("[APOLLO-HIST] Starting historical ingestion — lookback %d years from %s",
                    _LOOKBACK_YEARS, self._start_date)

        summary = {
            "started_at":        datetime.now(timezone.utc).isoformat(),
            "earnings_loaded":   0,
            "insider_trades":    0,
            "fred_series":       0,
            "supply_chain_8k":   0,
            "errors":            [],
        }

        # 1. Earnings history
        try:
            n = self._ingest_earnings_history()
            summary["earnings_loaded"] = n
            logger.info("[APOLLO-HIST] Earnings: %d records loaded.", n)
        except Exception as exc:
            msg = f"Earnings ingestion failed: {exc}"
            logger.warning("[APOLLO-HIST] %s", msg)
            summary["errors"].append(msg)

        # 2. SEC Form 4 insider transactions
        try:
            n = self._ingest_form4_insiders()
            summary["insider_trades"] = n
            logger.info("[APOLLO-HIST] Form 4 insider trades: %d records loaded.", n)
        except Exception as exc:
            msg = f"Form 4 ingestion failed: {exc}"
            logger.warning("[APOLLO-HIST] %s", msg)
            summary["errors"].append(msg)

        # 3. FRED macro data
        try:
            n = self._ingest_fred_macro()
            summary["fred_series"] = n
            logger.info("[APOLLO-HIST] FRED: %d series loaded.", n)
        except Exception as exc:
            msg = f"FRED ingestion failed: {exc}"
            logger.warning("[APOLLO-HIST] %s", msg)
            summary["errors"].append(msg)

        # 4. SEC 8-K supply chain events
        try:
            n = self._ingest_edgar_supply_chain()
            summary["supply_chain_8k"] = n
            logger.info("[APOLLO-HIST] EDGAR 8-K supply chain: %d events loaded.", n)
        except Exception as exc:
            msg = f"EDGAR ingestion failed: {exc}"
            logger.warning("[APOLLO-HIST] %s", msg)
            summary["errors"].append(msg)

        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        total = (summary["earnings_loaded"] + summary["insider_trades"] +
                 summary["fred_series"] + summary["supply_chain_8k"])
        logger.info(
            "[APOLLO-HIST] Complete — %d total records, %d errors.",
            total, len(summary["errors"]),
        )
        return summary

    # ------------------------------------------------------------------
    # 1. Earnings history via yfinance
    # ------------------------------------------------------------------

    def _ingest_earnings_history(self) -> int:
        """
        For each ticker in the core universe, load quarterly earnings history:
        reported EPS, estimated EPS, surprise %, and 5-day price reaction.
        Stores as KB entries so ZEUS has context when an earnings signal arrives.
        """
        if self._kb is None:
            return 0

        added = 0
        for ticker in _CORE_UNIVERSE:
            try:
                added += self._load_ticker_earnings(ticker)
                time.sleep(0.5)   # be polite to yfinance
            except Exception as exc:
                logger.warning("[APOLLO-HIST] Earnings failed for %s: %s", ticker, exc)

        return added

    def _load_ticker_earnings(self, ticker: str) -> int:
        """Load earnings history for one ticker and store in KB."""
        t = yf.Ticker(ticker)
        added = 0

        # Earnings dates with EPS data
        try:
            earnings = t.earnings_dates
            if earnings is None or earnings.empty:
                return 0

            # Filter to lookback window
            cutoff = datetime.now(timezone.utc) - timedelta(days=_LOOKBACK_YEARS * 365)
            earnings = earnings[earnings.index > cutoff]

            # Price history for reaction calculation
            hist = t.history(period=f"{_LOOKBACK_YEARS}y", interval="1d")

            for date, row in earnings.iterrows():
                try:
                    reported = row.get("Reported EPS")
                    estimated = row.get("EPS Estimate")
                    surprise_pct = row.get("Surprise(%)")

                    if reported is None or estimated is None:
                        continue

                    # Calculate 5-day price reaction after earnings
                    date_str = date.strftime("%Y-%m-%d")
                    price_reaction = self._calc_price_reaction(hist, date, days=5)

                    direction = "BEAT" if (surprise_pct or 0) > 0 else "MISS"
                    text = (
                        f"Earnings history: {ticker} — {date_str}\n"
                        f"Result: {direction} | Reported EPS: {reported:.3f} | "
                        f"Estimated: {estimated:.3f} | Surprise: {surprise_pct:.1f}%\n"
                        f"5-day price reaction: {price_reaction:+.2f}%\n"
                        f"Pattern: {ticker} earnings {direction} historically causes "
                        f"{'positive' if price_reaction > 0 else 'negative'} price movement "
                        f"of {abs(price_reaction):.1f}% over 5 days."
                    )

                    doc_id = f"earnings:{ticker}:{date_str}"
                    self._kb.add_literature(
                        title=f"Earnings {direction}: {ticker} {date_str}",
                        text=text,
                        source=f"yfinance:earnings:{ticker}",
                        doc_id=doc_id,
                    )
                    added += 1
                except Exception:
                    continue

        except Exception as exc:
            logger.debug("[APOLLO-HIST] Earnings parse failed %s: %s", ticker, exc)

        return added

    @staticmethod
    def _calc_price_reaction(hist, earnings_date, days: int = 5) -> float:
        """Calculate price % change over N days following an earnings date."""
        try:
            # Find the trading day on or after earnings date
            future = hist[hist.index > earnings_date]
            if len(future) < days:
                return 0.0
            entry = float(future["Close"].iloc[0])
            exit_ = float(future["Close"].iloc[min(days - 1, len(future) - 1)])
            if entry == 0:
                return 0.0
            return round((exit_ - entry) / entry * 100, 2)
        except Exception:
            return 0.0

    # ------------------------------------------------------------------
    # 2. SEC Form 4 insider transactions via EDGAR
    # ------------------------------------------------------------------

    def _ingest_form4_insiders(self) -> int:
        """
        Query SEC EDGAR for Form 4 insider transactions (open-market purchases)
        for the core universe. Cluster buying (3+ insiders within 30 days) is
        the high-confidence signal — store these prominently in the KB.
        """
        if self._kb is None:
            return 0

        added = 0
        # EDGAR company search by ticker — resolve to CIK first
        for ticker in _CORE_UNIVERSE:
            try:
                added += self._load_form4_for_ticker(ticker)
                time.sleep(1.0)   # EDGAR rate limit: 10 req/sec
            except Exception as exc:
                logger.warning("[APOLLO-HIST] Form 4 failed for %s: %s", ticker, exc)

        return added

    def _load_form4_for_ticker(self, ticker: str) -> int:
        """Load Form 4 filings for one ticker."""
        # Resolve ticker to CIK via EDGAR company search
        cik = self._resolve_cik(ticker)
        if not cik:
            return 0

        # Fetch recent Form 4 filings
        url = f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json"
        headers = {"User-Agent": f"ZEUS Trading System {os.getenv('EDGAR_USER_AGENT', 'pantheon-os@example.com')}"}

        try:
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code != 200:
                return 0

            data = resp.json()
            filings = data.get("filings", {}).get("recent", {})
            forms = filings.get("form", [])
            dates = filings.get("filingDate", [])
            accessions = filings.get("accessionNumber", [])

        except Exception as exc:
            logger.debug("[APOLLO-HIST] EDGAR submissions failed %s: %s", ticker, exc)
            return 0

        added = 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=_LOOKBACK_YEARS * 365)

        for form, date_str, accession in zip(forms, dates, accessions):
            if form != "4":
                continue
            try:
                filing_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if filing_date < cutoff:
                    break  # filings are sorted newest first — stop when out of window
            except Exception:
                continue

            try:
                transaction = self._parse_form4(cik, accession, headers)
                if transaction and transaction.get("is_purchase"):
                    text = (
                        f"Insider transaction: {ticker} — {date_str}\n"
                        f"Type: Open-market PURCHASE\n"
                        f"Insider: {transaction.get('insider_name', 'Unknown')} "
                        f"({transaction.get('insider_title', 'Unknown')})\n"
                        f"Shares: {transaction.get('shares', 0):,.0f} @ "
                        f"${transaction.get('price', 0):.2f}\n"
                        f"Value: ${transaction.get('value', 0):,.0f}\n"
                        f"Signal: Insider buying {ticker} at market — historically "
                        f"bullish signal, especially when multiple insiders buy within 30 days."
                    )
                    doc_id = f"form4:{ticker}:{date_str}:{accession[:8]}"
                    self._kb.add_literature(
                        title=f"Insider Buy: {ticker} {date_str}",
                        text=text,
                        source=f"sec:form4:{ticker}",
                        doc_id=doc_id,
                    )
                    added += 1
            except Exception:
                continue

            time.sleep(0.15)   # EDGAR rate limit

        return added

    def _resolve_cik(self, ticker: str) -> Optional[str]:
        """Resolve a ticker symbol to an SEC CIK number."""
        try:
            url = "https://efts.sec.gov/LATEST/search-index"
            params = {"q": ticker, "dateRange": "custom",
                      "startdt": "2020-01-01", "forms": "4"}
            headers = {"User-Agent": f"ZEUS Trading System {os.getenv('EDGAR_USER_AGENT', 'pantheon-os@example.com')}"}
            resp = requests.get(url, params=params, headers=headers, timeout=10)
            if resp.status_code != 200:
                return None
            hits = resp.json().get("hits", {}).get("hits", [])
            if hits:
                return hits[0].get("_source", {}).get("entity_id", "")
        except Exception:
            pass

        # Fallback: company facts endpoint
        try:
            url = f"https://data.sec.gov/submissions/CIK{ticker}.json"
            headers = {"User-Agent": f"ZEUS Trading System {os.getenv('EDGAR_USER_AGENT', 'pantheon-os@example.com')}"}
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                return resp.json().get("cik", "")
        except Exception:
            pass

        return None

    @staticmethod
    def _parse_form4(cik: str, accession: str, headers: dict) -> Optional[dict]:
        """Parse a Form 4 filing to extract transaction details."""
        try:
            acc_formatted = accession.replace("-", "")
            url = (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
                   f"{acc_formatted}/{accession}-index.htm")
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code != 200:
                return None

            # Simple extraction — look for transaction code "P" (open market purchase)
            text = resp.text
            is_purchase = "TransactionCode>P<" in text or ">P</transactionCode>" in text.lower()

            return {"is_purchase": is_purchase}
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 3. FRED macro data
    # ------------------------------------------------------------------

    def _ingest_fred_macro(self) -> int:
        """
        Download 4 years of daily FRED data for key macro series.
        Stored as structured KB entries Artemis can reference for regime context.
        """
        if self._kb is None:
            return 0

        if not self._fred_api_key:
            logger.info("[APOLLO-HIST] FRED_API_KEY not set — using yfinance VIX fallback only.")
            return self._ingest_vix_fallback()

        added = 0
        for series_id, description in _FRED_SERIES.items():
            try:
                added += self._load_fred_series(series_id, description)
                time.sleep(0.3)
            except Exception as exc:
                logger.warning("[APOLLO-HIST] FRED %s failed: %s", series_id, exc)

        return added

    def _load_fred_series(self, series_id: str, description: str) -> int:
        """Download one FRED series and store summary statistics in KB."""
        params = {
            "series_id":       series_id,
            "observation_start": self._start_date,
            "api_key":         self._fred_api_key,
            "file_type":       "json",
            "frequency":       "d",
            "aggregation_method": "avg",
        }
        resp = requests.get(_FRED_BASE, params=params, timeout=20)
        resp.raise_for_status()

        observations = resp.json().get("observations", [])
        if not observations:
            return 0

        # Extract valid numeric observations
        values = []
        for obs in observations:
            try:
                v = float(obs["value"])
                values.append((obs["date"], v))
            except (ValueError, KeyError):
                continue

        if not values:
            return 0

        # Build a summary of the series for the KB
        vals_only = [v for _, v in values]
        recent_vals = vals_only[-30:] if len(vals_only) >= 30 else vals_only
        recent_avg = sum(recent_vals) / len(recent_vals)
        all_avg = sum(vals_only) / len(vals_only)
        min_val = min(vals_only)
        max_val = max(vals_only)
        latest_date, latest_val = values[-1]

        text = (
            f"FRED Macro Series: {series_id} — {description}\n"
            f"Coverage: {values[0][0]} to {latest_date} ({len(values)} observations)\n"
            f"Latest value: {latest_val:.4f} (as of {latest_date})\n"
            f"30-day average: {recent_avg:.4f}\n"
            f"4-year range: {min_val:.4f} – {max_val:.4f} (avg: {all_avg:.4f})\n"
            f"Trend: {'RISING' if latest_val > recent_avg else 'FALLING'} "
            f"vs 30-day average\n"
            f"Interpretation: {description}. "
            f"Current level of {latest_val:.4f} is "
            f"{'above' if latest_val > all_avg else 'below'} the 4-year average of {all_avg:.4f}."
        )

        doc_id = f"fred:{series_id}:{latest_date}"
        self._kb.add_literature(
            title=f"FRED {series_id}: {description[:50]}",
            text=text,
            source=f"fred:{series_id}",
            doc_id=doc_id,
        )
        return 1

    def _ingest_vix_fallback(self) -> int:
        """Load VIX history via yfinance when FRED API key is not set."""
        if self._kb is None:
            return 0
        try:
            vix = yf.Ticker("^VIX")
            hist = vix.history(period=f"{_LOOKBACK_YEARS}y", interval="1wk")
            if hist.empty:
                return 0

            closes = hist["Close"].dropna().tolist()
            dates = [str(d.date()) for d in hist.index]

            recent_avg = sum(closes[-12:]) / min(12, len(closes))
            all_avg = sum(closes) / len(closes)
            latest_date = dates[-1]
            latest_val = closes[-1]

            text = (
                f"VIX Historical Data (yfinance) — Weekly closes\n"
                f"Coverage: {dates[0]} to {latest_date} ({len(closes)} weeks)\n"
                f"Latest: {latest_val:.2f} (as of {latest_date})\n"
                f"12-week average: {recent_avg:.2f}\n"
                f"4-year range: {min(closes):.2f} – {max(closes):.2f} "
                f"(avg: {all_avg:.2f})\n"
                f"Regime context: VIX > 25 = elevated stress, > 35 = crisis. "
                f"Current {latest_val:.2f} is {'elevated' if latest_val > 20 else 'normal'}."
            )

            self._kb.add_literature(
                title="VIX Historical Summary (4 years)",
                text=text,
                source="yfinance:vix",
                doc_id=f"vix:summary:{latest_date}",
            )
            return 1
        except Exception as exc:
            logger.warning("[APOLLO-HIST] VIX fallback failed: %s", exc)
            return 0

    # ------------------------------------------------------------------
    # 4. SEC EDGAR 8-K supply chain events
    # ------------------------------------------------------------------

    def _ingest_edgar_supply_chain(self) -> int:
        """
        Search SEC EDGAR full-text search for 8-K filings mentioning
        supply chain disruptions. These are the ground truth events that
        Hermes would have signalled — gives ZEUS historical context.
        """
        if self._kb is None:
            return 0

        added = 0
        headers = {"User-Agent": f"ZEUS Trading System {os.getenv('EDGAR_USER_AGENT', 'pantheon-os@example.com')}"}

        for keyword in _SUPPLY_CHAIN_KEYWORDS[:3]:   # limit to 3 to avoid rate limiting
            try:
                added += self._search_edgar_8k(keyword, headers)
                time.sleep(2.0)   # EDGAR is strict about rate limits
            except Exception as exc:
                logger.warning("[APOLLO-HIST] EDGAR 8-K search failed '%s': %s", keyword, exc)

        return added

    def _search_edgar_8k(self, keyword: str, headers: dict) -> int:
        """Search EDGAR for 8-K filings matching a supply chain keyword."""
        params = {
            "q":         f'"{keyword}"',
            "dateRange": "custom",
            "startdt":   self._start_date,
            "forms":     "8-K",
            "_source":   "file_date,entity_name,file_num,period_of_report",
        }

        try:
            resp = requests.get(_EDGAR_SEARCH, params=params, headers=headers, timeout=20)
            if resp.status_code != 200:
                return 0

            hits = resp.json().get("hits", {}).get("hits", [])
        except Exception as exc:
            logger.debug("[APOLLO-HIST] EDGAR search failed: %s", exc)
            return 0

        added = 0
        for hit in hits[:10]:   # max 10 per keyword
            try:
                source = hit.get("_source", {})
                entity = source.get("entity_name", "Unknown")
                filed = source.get("file_date", "")
                period = source.get("period_of_report", "")

                text = (
                    f"SEC 8-K Supply Chain Event: {entity}\n"
                    f"Filed: {filed} | Period: {period}\n"
                    f"Keyword match: '{keyword}'\n"
                    f"Context: This company filed a material event report (8-K) "
                    f"referencing '{keyword}'. Such disclosures typically precede "
                    f"supply chain-driven price movements in affected downstream companies."
                )

                doc_id = f"edgar:8k:{entity[:20]}:{filed}:{keyword[:10]}"
                self._kb.add_literature(
                    title=f"8-K Supply Chain: {entity[:40]} ({filed})",
                    text=text,
                    source="sec:8k:supply_chain",
                    doc_id=doc_id,
                )
                added += 1
            except Exception:
                continue

        return added
