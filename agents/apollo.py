"""
Agent 7 — Apollo: Research & Knowledge Intelligence

Apollo is ZEUS's librarian and self-improvement engine. Responsibilities:
  1. Seed ChromaDB with live financial literature (arXiv q-fin, SSRN abstracts)
  2. Crawl earnings transcripts and SEC filings for ZEUS's trading universe
     via Hermes (reusing the existing Railway deployment)
  3. Maintain and expand the supplier→ticker mapping so Icarus has live coverage
  4. Run ZEUS's self-improvement loop: analyse decision traces, surface systematic
     biases, and write updated rules back to the skills files
  5. Report health and last-run status to the Watchdog

Import rule: imports from core.types and core.agent_knowledge only.
ZEUS calls apollo.run_research_cycle() on a daily schedule.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from core.types import AgentHealth
from core.agent_knowledge import AgentKnowledgeBase

logger = logging.getLogger("apollo")

# arXiv q-fin categories most relevant to ZEUS
_ARXIV_CATEGORIES = ["q-fin.TR", "q-fin.PM", "q-fin.ST", "q-fin.RM"]
_ARXIV_API        = "https://export.arxiv.org/api/query"
_ARXIV_MAX_PAPERS = int(os.getenv("APOLLO_ARXIV_MAX_PAPERS", "5"))  # per category per cycle

# SSRN — search via public URL (no auth required for abstracts)
_SSRN_SEARCH = "https://papers.ssrn.com/sol3/results.cfm"

# SEC EDGAR full-text search
_EDGAR_SEARCH = "https://efts.sec.gov/LATEST/search-index?q={query}&dateRange=custom&startdt={start}&forms=8-K,10-Q"

# Hermes base URL — reuse for earnings/SEC enrichment
_HERMES_BASE = os.getenv("HERMES_BASE_URL", "https://hermes-agent-production-114e.up.railway.app")

# Ticker universe Apollo maintains — Icarus reads from here
_TICKER_MAP_PATH = Path("data/ticker_map.json")

# Self-improvement: analyse every N decision traces
_SELF_IMPROVE_EVERY_N = int(os.getenv("APOLLO_SELF_IMPROVE_N", "50"))

_DEFAULT_TICKER_MAP: dict[str, str] = {
    # US Mega-cap
    "NVIDIA": "NVDA", "Apple": "AAPL", "Microsoft": "MSFT",
    "Alphabet": "GOOGL", "Amazon": "AMZN", "Meta": "META",
    "Tesla": "TSLA", "Broadcom": "AVGO", "Intel": "INTC",
    "AMD": "AMD", "Qualcomm": "QCOM", "Texas Instruments": "TXN",
    "Micron": "MU", "Applied Materials": "AMAT", "Lam Research": "LRCX",
    "ASML": "ASML", "TSMC": "TSM", "Samsung": "SSNLF",
    # German / European
    "BASF": "BASFY", "Siemens": "SIEGY", "SAP": "SAP",
    "Deutsche Telekom": "DTEGY", "Volkswagen": "VWAGY",
    "BMW": "BMWYY", "Mercedes-Benz": "MBGYY",
    "Infineon": "IFNNY", "Continental": "CTTAY",
    "Bayer": "BAYRY", "Daimler Truck": "DTRUY",
    "Airbus": "EADSY", "LVMH": "LVMUY",
    # Cloud & infra
    "Amazon Web Services": "AMZN", "Microsoft Azure": "MSFT",
    "Google Cloud": "GOOGL", "Snowflake": "SNOW",
    "Cloudflare": "NET", "Datadog": "DDOG",
    # Semiconductors / supply chain
    "Taiwan Semiconductor": "TSM", "SK Hynix": "000660.KS",
    "Western Digital": "WDC", "Seagate": "STX",
    "Marvell": "MRVL", "Monolithic Power": "MPWR",
    # Energy / commodities
    "Shell": "SHEL", "TotalEnergies": "TTE", "BP": "BP",
    "Rio Tinto": "RIO", "BHP": "BHP",
}


class ApolloAgent:
    """
    Research & knowledge intelligence agent.
    Called by ZEUS once per day (or on demand via /run endpoint).
    All methods are fire-and-forget safe — failures degrade gracefully.
    """

    def __init__(self, knowledge_base=None):
        self.kb   = AgentKnowledgeBase("apollo")
        self._zeus_kb  = knowledge_base   # shared KnowledgeBase instance injected by ZEUS
        self._hermes_key = os.getenv("HERMES_API_KEY", "")
        self._last_run:  Optional[datetime] = None
        self._last_error: Optional[str]    = None
        self._papers_added   = 0
        self._tickers_added  = 0
        self._traces_analysed = 0
        _TICKER_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_ticker_map()

    # ------------------------------------------------------------------
    # Public API — called by ZEUS
    # ------------------------------------------------------------------

    def health(self) -> AgentHealth:
        if self._last_error and self._last_run:
            delta = (datetime.now(timezone.utc) - self._last_run).total_seconds()
            if delta < 3600:
                return AgentHealth.DEGRADED
        return AgentHealth.HEALTHY

    def run_historical_ingestion(self) -> dict:
        """
        One-shot bootstrap: load 4 years of earnings, Form 4, FRED macro, and
        EDGAR supply chain data into the KB before paper trading begins.
        Safe to call multiple times — idempotent.
        """
        from agents.apollo_historical import HistoricalIngestionPipeline
        pipeline = HistoricalIngestionPipeline(knowledge_base=self._zeus_kb)
        summary = pipeline.run()
        logger.info("[APOLLO] Historical ingestion complete: %s", summary)
        return summary

    def run_research_cycle(self) -> dict:
        """
        Full daily research cycle. Returns a summary dict for logging.
        ZEUS calls this once per day via n8n schedule or /run endpoint.
        """
        logger.info("[APOLLO] Research cycle starting.")
        summary = {
            "started_at":      datetime.now(timezone.utc).isoformat(),
            "papers_added":    0,
            "tickers_updated": 0,
            "traces_analysed": 0,
            "errors":          [],
        }

        # 1. Ingest arXiv papers
        try:
            n = self._ingest_arxiv()
            summary["papers_added"] += n
            self._papers_added += n
        except Exception as exc:
            msg = f"arXiv ingestion failed: {exc}"
            logger.warning("[APOLLO] %s", msg)
            summary["errors"].append(msg)

        # 2. Update ticker map from yfinance symbol lookup
        try:
            n = self._refresh_ticker_map()
            summary["tickers_updated"] = n
            self._tickers_added += n
        except Exception as exc:
            msg = f"Ticker map refresh failed: {exc}"
            logger.warning("[APOLLO] %s", msg)
            summary["errors"].append(msg)

        # 3. Crawl earnings signals via Hermes for ZEUS's core universe
        try:
            n = self._ingest_hermes_earnings()
            summary["papers_added"] += n
        except Exception as exc:
            msg = f"Hermes earnings crawl failed: {exc}"
            logger.warning("[APOLLO] %s", msg)
            summary["errors"].append(msg)

        # 4. Self-improvement loop (runs when enough new traces exist)
        try:
            n = self._self_improve()
            summary["traces_analysed"] = n
            self._traces_analysed += n
        except Exception as exc:
            msg = f"Self-improvement loop failed: {exc}"
            logger.warning("[APOLLO] %s", msg)
            summary["errors"].append(msg)

        # 5. QuantStats daily performance report
        try:
            report_path = self._generate_quantstats_report()
            if report_path:
                summary["report_path"] = report_path
        except Exception as exc:
            msg = f"QuantStats report failed: {exc}"
            logger.warning("[APOLLO] %s", msg)
            summary["errors"].append(msg)

        self._last_run   = datetime.now(timezone.utc)
        self._last_error = summary["errors"][0] if summary["errors"] else None
        summary["finished_at"] = self._last_run.isoformat()
        logger.info(
            "[APOLLO] Cycle complete — papers=%d tickers=%d traces=%d errors=%d",
            summary["papers_added"], summary["tickers_updated"],
            summary["traces_analysed"], len(summary["errors"]),
        )
        return summary

    # ------------------------------------------------------------------
    # QuantStats daily performance report
    # ------------------------------------------------------------------

    def _generate_quantstats_report(self) -> Optional[str]:
        """
        Pull closed trades from Supabase, build a returns series,
        generate a QuantStats HTML tear sheet, and send the path via Telegram.
        Skips gracefully if quantstats or Supabase are not available.
        """
        import os
        if not (os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY")):
            logger.info("[APOLLO] Supabase not configured — skipping QuantStats report.")
            return None

        try:
            import quantstats as qs
            import pandas as pd
        except ImportError:
            logger.info("[APOLLO] quantstats not installed — skipping report.")
            return None

        import core.supabase_client as supa
        trades = supa.get_trades_for_report(days=90)
        if not trades:
            logger.info("[APOLLO] No closed trades yet — skipping QuantStats report.")
            return None

        # Build a daily returns series from closed trades
        df = pd.DataFrame(trades)
        df["closed_at"] = pd.to_datetime(df["closed_at"], utc=True)
        df = df.dropna(subset=["pnl_pct", "closed_at"])
        df = df.sort_values("closed_at")

        # Group by day, sum P&L (portfolio-level daily return)
        daily = df.groupby(df["closed_at"].dt.date)["pnl_pct"].sum()
        returns = pd.Series(daily.values, index=pd.to_datetime(daily.index))

        if len(returns) < 5:
            logger.info("[APOLLO] Fewer than 5 trading days — skipping report.")
            return None

        # Generate HTML tear sheet
        report_dir  = Path("data/reports")
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"pantheon_{datetime.now(timezone.utc).strftime('%Y%m%d')}.html"

        qs.extend_pandas()
        qs.reports.html(
            returns,
            output=str(report_path),
            title="Pantheon OS — Daily Performance Report",
            benchmark=None,
        )
        logger.info("[APOLLO] QuantStats report generated: %s", report_path)

        # Send Telegram notification
        telegram_token   = os.getenv("TELEGRAM_BOT_TOKEN")
        telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if telegram_token and telegram_chat_id:
            try:
                win_rate = (df["hit"] == True).mean() * 100
                avg_pnl  = df["pnl_pct"].mean() * 100
                msg = (
                    f"📊 Pantheon Daily Report\n"
                    f"Trades analysed: {len(df)}\n"
                    f"Win rate: {win_rate:.1f}%\n"
                    f"Avg P&L per trade: {avg_pnl:.2f}%\n"
                    f"Report: {report_path.name}"
                )
                requests.post(
                    f"https://api.telegram.org/bot{telegram_token}/sendMessage",
                    json={"chat_id": telegram_chat_id, "text": msg},
                    timeout=5,
                )
            except Exception as exc:
                logger.warning("[APOLLO] Telegram report notification failed: %s", exc)

        return str(report_path)

    def get_ticker(self, supplier_name: str) -> Optional[str]:
        """
        Resolve a supplier name to a ticker symbol.
        Reads from the live ticker map Apollo maintains.
        Falls back to yfinance search if not found locally.
        """
        ticker_map = self._load_ticker_map()
        # Exact match
        if supplier_name in ticker_map:
            return ticker_map[supplier_name]
        # Case-insensitive match
        lower = supplier_name.lower()
        for name, ticker in ticker_map.items():
            if name.lower() == lower:
                return ticker
        # Partial match (e.g. "NVIDIA Corporation" → "NVIDIA")
        for name, ticker in ticker_map.items():
            if name.lower() in lower or lower in name.lower():
                return ticker
        # Live yfinance fallback — persist result so next call is instant
        resolved = self._yfinance_lookup(supplier_name)
        if resolved:
            ticker_map[supplier_name] = resolved
            _TICKER_MAP_PATH.write_text(
                json.dumps(ticker_map, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            logger.info("[APOLLO] Auto-resolved and cached: %s → %s", supplier_name, resolved)
        return resolved

    def get_ticker_map(self) -> dict[str, str]:
        """Return the full current ticker map — used by Icarus."""
        return self._load_ticker_map()

    # ------------------------------------------------------------------
    # arXiv ingestion
    # ------------------------------------------------------------------

    def _ingest_arxiv(self) -> int:
        """Fetch recent q-fin papers from arXiv and add to shared KB."""
        if self._zeus_kb is None:
            return 0
        added = 0
        for cat in _ARXIV_CATEGORIES:
            try:
                papers = self._fetch_arxiv_category(cat)
                for paper in papers:
                    self._zeus_kb.add_literature(
                        title=paper["title"],
                        text=f"{paper['title']}\n\n{paper['abstract']}",
                        source=f"arxiv:{cat}",
                    )
                    added += 1
                    time.sleep(0.3)   # be polite to arXiv
            except Exception as exc:
                logger.warning("[APOLLO] arXiv %s failed: %s", cat, exc)
        logger.info("[APOLLO] arXiv ingestion: %d papers added.", added)
        return added

    def _fetch_arxiv_category(self, category: str) -> list[dict]:
        params = {
            "search_query": f"cat:{category}",
            "sortBy":       "submittedDate",
            "sortOrder":    "descending",
            "max_results":  _ARXIV_MAX_PAPERS,
        }
        resp = requests.get(_ARXIV_API, params=params, timeout=20)
        resp.raise_for_status()
        return self._parse_arxiv_atom(resp.text)

    @staticmethod
    def _parse_arxiv_atom(xml_text: str) -> list[dict]:
        papers = []
        try:
            root = ET.fromstring(xml_text)
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            for entry in root.findall("atom:entry", ns):
                title_el    = entry.find("atom:title", ns)
                summary_el  = entry.find("atom:summary", ns)
                if title_el is not None and summary_el is not None:
                    papers.append({
                        "title":    (title_el.text or "").strip().replace("\n", " "),
                        "abstract": (summary_el.text or "").strip().replace("\n", " "),
                    })
        except ET.ParseError as exc:
            logger.warning("[APOLLO] arXiv XML parse error: %s", exc)
        return papers

    # ------------------------------------------------------------------
    # Hermes earnings enrichment
    # ------------------------------------------------------------------

    def _ingest_hermes_earnings(self) -> int:
        """
        Query Hermes for EARNINGS signals across the core trading universe
        and add meaningful summaries to the shared KB.
        """
        if self._zeus_kb is None or not self._hermes_key:
            return 0
        added = 0
        core_universe = ["NVIDIA", "TSMC", "SAP", "Siemens", "ASML", "Intel", "AMD"]
        headers = {"x-api-key": self._hermes_key}
        for company in core_universe:
            try:
                resp = requests.get(
                    f"{_HERMES_BASE}/query/{company}",
                    headers=headers, timeout=15,
                )
                if resp.status_code != 200:
                    continue
                items = resp.json().get("items", [])
                earnings = [i for i in items if i.get("signal_type") == "EARNINGS"]
                for item in earnings[:3]:
                    text = f"Earnings report — {company}\n{item.get('title','')}\n{item.get('summary','')}"
                    self._zeus_kb.add_literature(
                        title=f"Earnings: {company} — {item.get('published','')[:10]}",
                        text=text,
                        source="hermes:earnings",
                    )
                    added += 1
                time.sleep(0.5)
            except Exception as exc:
                logger.warning("[APOLLO] Hermes earnings for %s failed: %s", company, exc)
        logger.info("[APOLLO] Hermes earnings: %d items added.", added)
        return added

    # ------------------------------------------------------------------
    # Ticker map maintenance
    # ------------------------------------------------------------------

    def _ensure_ticker_map(self) -> None:
        if not _TICKER_MAP_PATH.exists():
            _TICKER_MAP_PATH.write_text(
                json.dumps(_DEFAULT_TICKER_MAP, indent=2), encoding="utf-8"
            )
            logger.info("[APOLLO] Ticker map initialised with %d entries.", len(_DEFAULT_TICKER_MAP))

    def _load_ticker_map(self) -> dict[str, str]:
        try:
            return json.loads(_TICKER_MAP_PATH.read_text(encoding="utf-8"))
        except Exception:
            return dict(_DEFAULT_TICKER_MAP)

    def _refresh_ticker_map(self) -> int:
        """
        Validate existing entries are still tradeable and add any
        new suppliers discovered in recent Hermes signals.
        Returns count of new entries added.
        """
        ticker_map = self._load_ticker_map()
        original_count = len(ticker_map)

        # Pull recent Hermes briefing to find new suppliers
        if self._hermes_key:
            try:
                headers = {"x-api-key": self._hermes_key}
                resp = requests.get(
                    f"{_HERMES_BASE}/briefing", headers=headers, timeout=20
                )
                if resp.status_code == 200:
                    data  = resp.json()
                    items = data.get("signals", data.get("items", []))
                    for item in items:
                        supplier = item.get("supplier", "").strip()
                        if supplier and supplier not in ticker_map:
                            resolved = self._yfinance_lookup(supplier)
                            if resolved:
                                ticker_map[supplier] = resolved
                                logger.info("[APOLLO] New ticker resolved: %s → %s", supplier, resolved)
            except Exception as exc:
                logger.warning("[APOLLO] Ticker map Hermes fetch failed: %s", exc)

        added = len(ticker_map) - original_count
        if added > 0:
            _TICKER_MAP_PATH.write_text(
                json.dumps(ticker_map, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        return added

    def _yfinance_lookup(self, supplier_name: str) -> Optional[str]:
        """Try yfinance ticker search for a supplier name."""
        try:
            import yfinance as yf
            # yfinance search returns candidate tickers
            result = yf.Search(supplier_name, max_results=1)
            quotes = result.quotes
            if quotes:
                return quotes[0].get("symbol")
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Self-improvement loop
    # ------------------------------------------------------------------

    def _self_improve(self) -> int:
        """
        Query the shared KB for recent decision traces, identify systematic
        biases, and write updated insights to the zeus_skills.md file.
        Returns number of traces analysed.
        """
        if self._zeus_kb is None:
            return 0

        # Query all recent decisions via the public KB interface
        try:
            results = self._zeus_kb.get_recent_decisions(limit=_SELF_IMPROVE_EVERY_N)
        except Exception:
            return 0

        if not results or not results.get("metadatas"):
            return 0

        metas = results["metadatas"]
        n = len(metas)
        if n < 10:
            logger.info("[APOLLO] Only %d traces — skipping self-improvement (need 10+).", n)
            return 0

        insights = self._analyse_traces(metas)
        if insights:
            self._append_to_zeus_skills(insights)
        return n

    @staticmethod
    def _analyse_traces(metas: list[dict]) -> Optional[str]:
        """
        Simple statistical analysis of decision trace metadata.
        Returns a formatted insight string if patterns are found, else None.
        """
        from collections import defaultdict

        by_category: dict[str, list[float]] = defaultdict(list)
        by_regime:   dict[str, list[float]] = defaultdict(list)
        approved_count = 0

        for m in metas:
            pnl    = m.get("pnl_pct", 0.0) or 0.0
            cat    = m.get("category", "unknown")
            regime = m.get("regime", "unknown")
            if m.get("approved") is True or m.get("approved") == "True":
                approved_count += 1
                by_category[cat].append(pnl)
                by_regime[regime].append(pnl)

        if approved_count < 5:
            return None

        lines = [
            f"\n## Self-Improvement Insights — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
            f"Analysed {len(metas)} traces ({approved_count} approved).",
            "",
            "### Win rates by signal category:",
        ]
        for cat, pnls in sorted(by_category.items()):
            wins     = sum(1 for p in pnls if p > 0)
            win_rate = wins / len(pnls) if pnls else 0.0
            avg_pnl  = sum(pnls) / len(pnls) if pnls else 0.0
            lines.append(f"- {cat}: {win_rate:.0%} win rate, avg P&L {avg_pnl:+.2%} (n={len(pnls)})")

        lines.append("")
        lines.append("### Win rates by market regime:")
        for regime, pnls in sorted(by_regime.items()):
            wins     = sum(1 for p in pnls if p > 0)
            win_rate = wins / len(pnls) if pnls else 0.0
            avg_pnl  = sum(pnls) / len(pnls) if pnls else 0.0
            lines.append(f"- {regime}: {win_rate:.0%} win rate, avg P&L {avg_pnl:+.2%} (n={len(pnls)})")

        return "\n".join(lines)

    @staticmethod
    def _append_to_zeus_skills(insights: str) -> None:
        """Write insight block to zeus_skills.md for future KB ingestion."""
        skills_path = Path("knowledge/agents/zeus_skills.md")
        if not skills_path.exists():
            return
        current = skills_path.read_text(encoding="utf-8")
        # Replace existing insights block from same date if present
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        marker   = f"## Self-Improvement Insights — {date_str}"
        if marker in current:
            # Already wrote insights today — skip to avoid duplicate bloat
            return
        skills_path.write_text(current.rstrip() + "\n\n" + insights + "\n", encoding="utf-8")
        logger.info("[APOLLO] Self-improvement insights written to zeus_skills.md.")
