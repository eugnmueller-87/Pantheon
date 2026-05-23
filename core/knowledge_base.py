"""
core/knowledge_base.py — ZEUS Knowledge Base

Three layers:
  1. Curated knowledge  — trading fundamentals, macro playbooks, signal guides
                          loaded once at startup from knowledge/*.md files
  2. Decision memory    — every DecisionTrace ZEUS writes is stored here
                          agents query it to find similar past situations
  3. Outcome learning   — pnl_pct is backfilled into traces by Monitor
                          ZEUS queries this to understand what actually worked

Storage: ChromaDB (local, persistent). Falls back to in-memory if ChromaDB
unavailable so the pipeline never halts due to a KB failure.

All queries return ranked text chunks. ZEUS feeds these into its LLM prompt
as context before making a final trade decision.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.types import DecisionTrace

logger = logging.getLogger("knowledge_base")

KNOWLEDGE_DIR = Path("knowledge")
CHROMA_PATH   = Path("data/chroma")


class KnowledgeBase:
    """
    Wraps ChromaDB with two collections:
      - "knowledge"  : curated + literature content
      - "decisions"  : ZEUS decision traces + outcomes
    """

    def __init__(self, persist_path: Path = CHROMA_PATH):
        self._persist_path = persist_path
        self._client       = None
        self._knowledge_col = None
        self._decisions_col = None
        self._fallback: list[dict] = []   # in-memory fallback if ChromaDB fails
        self._init()

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def _init(self) -> None:
        try:
            import chromadb
            self._persist_path.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=str(self._persist_path))
            self._knowledge_col = self._client.get_or_create_collection(
                name="knowledge",
                metadata={"hnsw:space": "cosine"},
            )
            self._decisions_col = self._client.get_or_create_collection(
                name="decisions",
                metadata={"hnsw:space": "cosine"},
            )
            logger.info("[KB] ChromaDB initialised at %s", self._persist_path)
            self._load_curated_knowledge()
        except Exception as exc:
            logger.warning("[KB] ChromaDB unavailable (%s) — using in-memory fallback.", exc)
            self._client = None

    def _load_curated_knowledge(self) -> None:
        """Ingest all markdown files from knowledge/ into the knowledge collection."""
        if not KNOWLEDGE_DIR.exists():
            logger.info("[KB] No knowledge/ directory found — skipping curated load.")
            return

        loaded = 0
        for md_file in KNOWLEDGE_DIR.glob("*.md"):
            doc_id = f"curated:{md_file.stem}"
            # Skip if already loaded (idempotent)
            existing = self._knowledge_col.get(ids=[doc_id])
            if existing["ids"]:
                continue
            text = md_file.read_text(encoding="utf-8")
            chunks = self._chunk(text, chunk_size=800, overlap=100)
            for i, chunk in enumerate(chunks):
                self._knowledge_col.add(
                    ids=[f"{doc_id}:chunk{i}"],
                    documents=[chunk],
                    metadatas=[{"source": md_file.name, "type": "curated", "chunk": i}],
                )
            loaded += 1
            logger.info("[KB] Loaded curated knowledge: %s (%d chunks)", md_file.name, len(chunks))

        if loaded == 0:
            logger.info("[KB] All curated knowledge already loaded.")

    # ------------------------------------------------------------------
    # Public write API
    # ------------------------------------------------------------------

    def store_decision(self, trace: DecisionTrace) -> None:
        """Called by ZEUS after every pipeline run — writes to Supabase + ChromaDB."""
        doc  = self._trace_to_text(trace)
        meta = {
            "trace_id":   trace.trace_id,
            "signal_id":  trace.signal_id,
            "category":   trace.category,
            "regime":     trace.trend_regime,
            "vix":        trace.trend_vix,
            "approved":   str(trace.zeus_approved),
            "pnl_pct":    trace.pnl_pct or 0.0,
            "timestamp":  trace.timestamp.isoformat(),
        }

        # Always write to Supabase when configured (primary, durable store)
        self._store_decision_supabase(trace)

        # ChromaDB for vector similarity (local, optional)
        if self._decisions_col is not None:
            try:
                self._decisions_col.upsert(
                    ids=[trace.trace_id],
                    documents=[doc],
                    metadatas=[meta],
                )
                return
            except Exception as exc:
                logger.warning("[KB] ChromaDB store failed: %s", exc)
        self._fallback.append({"id": trace.trace_id, "doc": doc, "meta": meta})

    def _store_decision_supabase(self, trace: DecisionTrace) -> None:
        import os
        if not (os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY")):
            return
        try:
            import core.supabase_client as supa
            regime = trace.trend_regime or "unknown"
            supa.insert_decision_trace({
                "trace_id":           trace.trace_id,
                "signal_id":          trace.signal_id or None,
                "timestamp":          trace.timestamp.isoformat(),
                "headline":           trace.headline,
                "supplier":           trace.supplier,
                "category":           trace.category,
                "severity":           trace.severity,
                "hades_passed":       trace.hades_passed,
                "hades_notes":        list(trace.hades_notes),
                "trend_suppressed":   trace.trend_suppressed,
                "trend_regime":       regime if regime in ("bull","bear","sideways","unknown") else "unknown",
                "trend_vix":          trace.trend_vix,
                "pattern_confidence": trace.pattern_confidence,
                "pattern_size_pct":   trace.pattern_size_pct,
                "zeus_reasoning":     trace.zeus_reasoning,
                "zeus_approved":      trace.zeus_approved,
                "zeus_override":      trace.zeus_override,
                "zeus_override_reason": trace.zeus_override_reason,
                "trade_placed":       trace.trade_placed,
                "symbol":             trace.symbol,
                "side":               trace.side,
                "fill_price":         trace.fill_price,
                "pnl_pct":            trace.pnl_pct,
                "killed_at_stage":    trace.killed_at_stage,
                "kill_reason":        trace.kill_reason,
            })
        except Exception as exc:
            logger.warning("[KB] Supabase decision trace failed: %s", exc)

    def update_outcome(self, trace_id: str, pnl_pct: float) -> None:
        """Called by Monitor when a trade closes — backfills P&L into the trace."""
        if self._decisions_col is not None:
            try:
                existing = self._decisions_col.get(ids=[trace_id])
                if existing["ids"]:
                    meta = existing["metadatas"][0]
                    meta["pnl_pct"] = pnl_pct
                    self._decisions_col.update(ids=[trace_id], metadatas=[meta])
            except Exception as exc:
                logger.warning("[KB] outcome update failed: %s", exc)

    def add_literature(self, title: str, text: str, source: str = "crawled") -> None:
        """Add crawled financial literature to the knowledge collection."""
        if self._knowledge_col is None:
            return
        chunks = self._chunk(text, chunk_size=800, overlap=100)
        base_id = f"lit:{uuid.uuid4().hex[:8]}"
        for i, chunk in enumerate(chunks):
            self._knowledge_col.add(
                ids=[f"{base_id}:chunk{i}"],
                documents=[chunk],
                metadatas=[{"source": source, "title": title, "type": "literature", "chunk": i}],
            )
        logger.info("[KB] Added literature: %s (%d chunks)", title, len(chunks))

    # ------------------------------------------------------------------
    # Public query API
    # ------------------------------------------------------------------

    def query_knowledge(self, query: str, n_results: int = 5) -> list[str]:
        """
        Query curated + literature knowledge.
        Returns ranked text chunks for ZEUS to include in its LLM prompt.
        """
        if self._knowledge_col is None:
            return self._fallback_search(query)
        try:
            results = self._knowledge_col.query(
                query_texts=[query],
                n_results=min(n_results, self._knowledge_col.count() or 1),
            )
            return results["documents"][0] if results["documents"] else []
        except Exception as exc:
            logger.warning("[KB] query_knowledge failed: %s", exc)
            return []

    def query_similar_decisions(self, query: str, n_results: int = 5,
                                 min_pnl: Optional[float] = None) -> list[str]:
        """
        Find past ZEUS decisions similar to the current situation.
        Optionally filter to only winning decisions (min_pnl > 0).
        """
        if self._decisions_col is None:
            return []
        try:
            count = self._decisions_col.count()
            if count == 0:
                return []
            where = {"pnl_pct": {"$gte": min_pnl}} if min_pnl is not None else None
            kwargs = dict(
                query_texts=[query],
                n_results=min(n_results, count),
            )
            if where:
                kwargs["where"] = where
            results = self._decisions_col.query(**kwargs)
            return results["documents"][0] if results["documents"] else []
        except Exception as exc:
            logger.warning("[KB] query_similar_decisions failed: %s", exc)
            return []

    def query_outcomes_by_context(self, category: str, regime: str) -> dict:
        """
        Return aggregate win rate and avg P&L for a given signal
        category + market regime. Used by PatternAgent to supplement
        its SQLite stats with richer KB context.
        """
        if self._decisions_col is None:
            return {}
        try:
            results = self._decisions_col.get(
                where={"$and": [{"category": category}, {"regime": regime}, {"approved": "True"}]},
                include=["metadatas"],
            )
            metas = results.get("metadatas", [])
            if not metas:
                return {}
            pnls = [m["pnl_pct"] for m in metas if m.get("pnl_pct") != 0.0]
            wins = [p for p in pnls if p > 0]
            return {
                "n": len(pnls),
                "win_rate": len(wins) / len(pnls) if pnls else 0.0,
                "avg_pnl":  sum(pnls) / len(pnls) if pnls else 0.0,
            }
        except Exception as exc:
            logger.warning("[KB] query_outcomes failed: %s", exc)
            return {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _trace_to_text(trace: DecisionTrace) -> str:
        return (
            f"Signal: {trace.headline}\n"
            f"Supplier: {trace.supplier} | Category: {trace.category} | Severity: {trace.severity}\n"
            f"Regime: {trace.trend_regime} | VIX: {trace.trend_vix:.1f}\n"
            f"Hades passed: {trace.hades_passed} | Notes: {'; '.join(trace.hades_notes)}\n"
            f"Trend suppressed: {trace.trend_suppressed}\n"
            f"Pattern confidence: {trace.pattern_confidence:.2f} | Size: {trace.pattern_size_pct*100:.2f}%\n"
            f"ZEUS reasoning: {trace.zeus_reasoning}\n"
            f"ZEUS approved: {trace.zeus_approved} | Trade placed: {trace.trade_placed}\n"
            f"Symbol: {trace.symbol} | Side: {trace.side} | Fill: {trace.fill_price}\n"
            f"P&L: {trace.pnl_pct}\n"
            f"Kill stage: {trace.killed_at_stage} | Kill reason: {trace.kill_reason}"
        )

    @staticmethod
    def _chunk(text: str, chunk_size: int = 800, overlap: int = 100) -> list[str]:
        chunks, start = [], 0
        while start < len(text):
            end = start + chunk_size
            chunks.append(text[start:end])
            start += chunk_size - overlap
        return chunks

    def _fallback_search(self, query: str) -> list[str]:
        query_lower = query.lower()
        return [
            item["doc"] for item in self._fallback
            if any(w in item["doc"].lower() for w in query_lower.split())
        ][:5]
