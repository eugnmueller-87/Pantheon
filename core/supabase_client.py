"""
Pantheon OS — Supabase Client

Single shared client for all Postgres operations.
Agents never import supabase directly — they call functions from this module.

Environment variables required:
  SUPABASE_URL              — https://YOUR_PROJECT_ID.supabase.co
  SUPABASE_SERVICE_ROLE_KEY — service role key (full DB access, backend only)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("supabase_client")

_client = None


def get_client():
    """Return the shared Supabase client, initialising once on first call."""
    global _client
    if _client is not None:
        return _client

    url   = os.getenv("SUPABASE_URL", "")
    key   = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set. "
            "See .env.example."
        )

    try:
        from supabase import create_client
        _client = create_client(url, key)
        logger.info("[SUPABASE] Client initialised — %s", url)
        return _client
    except ImportError:
        raise RuntimeError(
            "supabase-py not installed. Run: pip install supabase"
        )


# ── Trades ─────────────────────────────────────────────────────────────────────

def insert_trade(trade: dict) -> Optional[dict]:
    """Insert a completed trade. Returns the inserted row or None on error."""
    try:
        res = get_client().table("trades").insert(trade).execute()
        return res.data[0] if res.data else None
    except Exception as exc:
        logger.error("[SUPABASE] insert_trade failed: %s", exc)
        return None


def get_hit_rates(context_key: str) -> Optional[dict]:
    """
    Read from the trade_hit_rates materialized view.
    Returns {hit_rate, total_trades, closed_trades, avg_pnl_pct} or None.
    """
    try:
        res = (
            get_client()
            .table("trade_hit_rates")
            .select("hit_rate, total_trades, closed_trades, avg_pnl_pct")
            .eq("context_key", context_key)
            .execute()
        )
        rows = res.data if res and res.data else []
        return rows[0] if rows else None
    except Exception as exc:
        logger.error("[SUPABASE] get_hit_rates failed: %s", exc)
        return None


def get_open_trades(min_samples: int = 10) -> list[dict]:
    """Return context_key rows that have enough closed trades for Pythia."""
    try:
        res = (
            get_client()
            .table("trade_hit_rates")
            .select("context_key, hit_rate, closed_trades")
            .gte("closed_trades", min_samples)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        logger.error("[SUPABASE] get_open_trades failed: %s", exc)
        return []


def update_trade_pnl(order_id: str, pnl_pct: float, hit: bool, closed_at: datetime) -> None:
    """Backfill P&L when Argus closes a position."""
    try:
        get_client().table("trades").update({
            "pnl_pct":   pnl_pct,
            "hit":       hit,
            "closed_at": closed_at.isoformat(),
        }).eq("order_id", order_id).execute()
    except Exception as exc:
        logger.error("[SUPABASE] update_trade_pnl failed: %s", exc)


# ── Portfolio state ────────────────────────────────────────────────────────────

def upsert_portfolio_state(state: dict) -> None:
    """Write an Argus equity snapshot. Called every 5s by Argus.refresh()."""
    try:
        get_client().table("portfolio_state").insert(state).execute()
    except Exception as exc:
        logger.error("[SUPABASE] upsert_portfolio_state failed: %s", exc)


def upsert_portfolio_positions(positions: list[dict]) -> None:
    """Overwrite open positions. Called every Argus refresh."""
    if not positions:
        return
    try:
        get_client().table("portfolio_positions").upsert(
            positions, on_conflict="position_id"
        ).execute()
    except Exception as exc:
        logger.error("[SUPABASE] upsert_portfolio_positions failed: %s", exc)


# ── Decision traces ────────────────────────────────────────────────────────────

def insert_decision_trace(trace: dict) -> None:
    """Write a full pipeline audit trace (every signal, win or loss)."""
    try:
        get_client().table("decision_traces").insert(trace).execute()
    except Exception as exc:
        logger.error("[SUPABASE] insert_decision_trace failed: %s", exc)


def get_similar_traces(category: str, regime: str, limit: int = 5) -> list[dict]:
    """Pull recent traces for the same signal category + regime (for ZEUS reasoning)."""
    try:
        res = (
            get_client()
            .table("decision_traces")
            .select("headline, zeus_reasoning, zeus_approved, pnl_pct, kill_reason")
            .eq("category", category)
            .eq("trend_regime", regime)
            .order("timestamp", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        logger.error("[SUPABASE] get_similar_traces failed: %s", exc)
        return []


# ── Agent health ───────────────────────────────────────────────────────────────

def insert_agent_health(agent_name: str, status: str, message: str = "", error_count: int = 0) -> None:
    """Write a Watchdog health report row."""
    try:
        get_client().table("agent_health").insert({
            "agent_name":  agent_name,
            "status":      status,
            "message":     message,
            "error_count": error_count,
            "checked_at":  datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as exc:
        logger.error("[SUPABASE] insert_agent_health failed: %s", exc)


# ── Signals ────────────────────────────────────────────────────────────────────

def insert_signal(signal: dict) -> Optional[dict]:
    """Write a raw Icarus signal."""
    try:
        res = get_client().table("signals").insert(signal).execute()
        return res.data[0] if res.data else None
    except Exception as exc:
        logger.error("[SUPABASE] insert_signal failed: %s", exc)
        return None


# ── Knowledge documents (pgvector) ────────────────────────────────────────────

def upsert_knowledge_doc(doc: dict) -> None:
    """
    Upsert a knowledge document with optional embedding vector.
    doc must have: doc_id, collection, document_text, metadata
    embedding is optional (float list of length 1536).
    """
    try:
        get_client().table("knowledge_documents").upsert(
            doc, on_conflict="doc_id"
        ).execute()
    except Exception as exc:
        logger.error("[SUPABASE] upsert_knowledge_doc failed: %s", exc)


def search_knowledge(
    embedding: list[float],
    collection: str = "knowledge",
    limit: int = 5,
    min_similarity: float = 0.7,
) -> list[dict]:
    """
    Vector similarity search using pgvector RPC.
    Requires the match_knowledge_documents function in Supabase (003_rpc.sql).
    """
    try:
        res = get_client().rpc("match_knowledge_documents", {
            "query_embedding": embedding,
            "match_collection": collection,
            "match_count":     limit,
            "min_similarity":  min_similarity,
        }).execute()
        return res.data or []
    except Exception as exc:
        logger.error("[SUPABASE] search_knowledge failed: %s", exc)
        return []


# ── Ticker map ─────────────────────────────────────────────────────────────────

def get_ticker(supplier_name: str) -> Optional[str]:
    """Look up ticker symbol for a supplier name."""
    try:
        res = (
            get_client()
            .table("ticker_map")
            .select("ticker")
            .eq("supplier_name", supplier_name)
            .maybe_single()
            .execute()
        )
        return res.data["ticker"] if res.data else None
    except Exception as exc:
        logger.error("[SUPABASE] get_ticker failed: %s", exc)
        return None


def upsert_ticker(supplier_name: str, ticker: str, exchange: str, source: str = "yfinance") -> None:
    """Apollo uses this to add new supplier→ticker mappings."""
    try:
        get_client().table("ticker_map").upsert({
            "supplier_name": supplier_name,
            "ticker":        ticker,
            "exchange":      exchange,
            "source":        source,
            "verified":      False,
            "updated_at":    datetime.now(timezone.utc).isoformat(),
        }, on_conflict="supplier_name").execute()
    except Exception as exc:
        logger.error("[SUPABASE] upsert_ticker failed: %s", exc)


# ── Analytics queries (Grafana reads these via Postgres datasource directly,
#    but these helpers are available for QuantStats report generation) ──────────

def get_trades_for_report(days: int = 30) -> list[dict]:
    """Pull closed trades for the last N days — used by QuantStats daily report."""
    try:
        from datetime import timedelta
        from_dt = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        res = (
            get_client()
            .table("trades")
            .select("symbol, side, confidence, position_pct, pnl_pct, hit, recorded_at, closed_at, category, regime")
            .gte("recorded_at", from_dt)
            .not_.is_("pnl_pct", "null")
            .order("recorded_at", desc=False)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        logger.error("[SUPABASE] get_trades_for_report failed: %s", exc)
        return []


# ── Agent seniority ────────────────────────────────────────────────────────────

def upsert_agent_seniority(scores: dict, system_level_int: int) -> None:
    """
    Persist seniority scores after each SeniorityEvaluator.evaluate() call.
    Writes current state to agent_seniority and appends to history on level change.
    """
    try:
        client = get_client()
        now = datetime.now(timezone.utc).isoformat()

        # Fetch existing levels so we only append history on actual promotions
        existing_res = client.table("agent_seniority").select("agent_name, level_int").execute()
        existing = {row["agent_name"]: row["level_int"] for row in (existing_res.data or [])}

        rows = []
        history_rows = []
        for name, score in scores.items():
            row = {
                "agent_name":           name,
                "level":                score["level"],
                "level_int":            score["level_int"],
                "cleared":              score["cleared"],
                "criteria":             score["criteria"],
                "notes":                score["notes"],
                "max_position_pct":     _level_int_to_max_pos(score["level_int"]),
                "live_trading_allowed": score["level_int"] >= 1,
                "evaluated_at":         score["evaluated_at"],
                "updated_at":           now,
            }
            rows.append(row)

            prev_int = existing.get(name)
            if prev_int is None or score["level_int"] != prev_int:
                history_rows.append({
                    "agent_name":  name,
                    "from_level":  _int_to_level_label(prev_int) if prev_int is not None else None,
                    "to_level":    score["level"],
                    "level_int":   score["level_int"],
                    "criteria":    score["criteria"],
                    "promoted_at": now,
                })

        client.table("agent_seniority").upsert(rows, on_conflict="agent_name").execute()
        if history_rows:
            client.table("agent_seniority_history").insert(history_rows).execute()

    except Exception as exc:
        logger.error("[SUPABASE] upsert_agent_seniority failed: %s", exc)


_MAX_POS_BY_LEVEL: dict[int, float] = {0: 0.03, 1: 0.05, 2: 0.05, 3: 0.05}
_LABEL_BY_LEVEL: dict[int, str]  = {0: "Senior", 1: "Principal", 2: "Managing Director", 3: "Director"}


def _level_int_to_max_pos(level_int: int) -> float:
    if level_int not in _MAX_POS_BY_LEVEL:
        raise ValueError(f"Unknown seniority level_int: {level_int}")
    return _MAX_POS_BY_LEVEL[level_int]


def _int_to_level_label(level_int: int) -> str:
    if level_int not in _LABEL_BY_LEVEL:
        raise ValueError(f"Unknown seniority level_int: {level_int}")
    return _LABEL_BY_LEVEL[level_int]


def get_portfolio_equity_series(hours: int = 24) -> list[dict]:
    """Pull equity snapshots for the last N hours — Grafana equity chart."""
    try:
        from datetime import timedelta
        from_dt = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        res = (
            get_client()
            .table("portfolio_state")
            .select("total_equity, current_drawdown_pct, refreshed_at")
            .gte("refreshed_at", from_dt)
            .order("refreshed_at", desc=False)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        logger.error("[SUPABASE] get_portfolio_equity_series failed: %s", exc)
        return []
