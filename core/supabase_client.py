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


def count_winning_trades() -> Optional[int]:
    """Number of trades that WON (hit = 1). This is the progression currency the
    seniority system levels up on. Returns None on error so the caller can fall
    back to SQLite (local dev) rather than silently treating an error as 0 wins."""
    try:
        res = (
            get_client()
            .table("trades")
            .select("order_id", count="exact")
            .eq("hit", 1)
            .execute()
        )
        return res.count or 0
    except Exception as exc:
        logger.error("[SUPABASE] count_winning_trades failed: %s", exc)
        return None


def count_hades_compliance_kills() -> Optional[int]:
    """How many signals Hades actually killed at the compliance stage — real
    runtime evidence the compliance gate is doing work (vs grepping hades.py for
    the string 'ofac'). None on error."""
    try:
        res = (
            get_client()
            .table("decision_traces")
            .select("trace_id", count="exact")
            .eq("killed_at_stage", "hades")
            .execute()
        )
        return res.count or 0
    except Exception as exc:
        logger.error("[SUPABASE] count_hades_compliance_kills failed: %s", exc)
        return None


def zeus_override_doc_rate(min_samples: int = 5) -> Optional[float]:
    """Of approved trades where ZEUS overrode the Pattern verdict, the share with
    a documented reason (non-empty, >= 40 chars). Returns 0.0-1.0, or None when
    fewer than min_samples overrides exist (insufficient sample → not evaluable,
    which the caller treats as 'does not clear')."""
    try:
        res = (
            get_client()
            .table("decision_traces")
            .select("zeus_override_reason")
            .eq("zeus_override", True)
            .execute()
        )
        rows = res.data or []
        if len(rows) < min_samples:
            return None
        documented = sum(1 for r in rows if len((r.get("zeus_override_reason") or "").strip()) >= 40)
        return documented / len(rows)
    except Exception as exc:
        logger.error("[SUPABASE] zeus_override_doc_rate failed: %s", exc)
        return None


def count_closed_trades() -> Optional[int]:
    """Number of trades with a realized outcome (hit IS NOT NULL). None on error."""
    try:
        res = (
            get_client()
            .table("trades")
            .select("order_id", count="exact")
            .not_.is_("hit", "null")
            .execute()
        )
        return res.count or 0
    except Exception as exc:
        logger.error("[SUPABASE] count_closed_trades failed: %s", exc)
        return None


def fetch_open_trades() -> list[dict]:
    """Return individual OPEN trade rows (hit IS NULL) for outcome resolution.

    Distinct from get_open_trades() below, which returns hit-rate AGGREGATES for
    Pythia. This returns the actual trade rows the OutcomeResolver needs to match
    against IB's live portfolio. Uses idx_trades_open ON trades(hit) WHERE hit IS NULL.
    """
    try:
        res = (
            get_client()
            .table("trades")
            .select("order_id, symbol, side, fill_price, stop_loss, take_profit, qty, recorded_at")
            .is_("hit", "null")
            .execute()
        )
        return res.data or []
    except Exception as exc:
        logger.error("[SUPABASE] fetch_open_trades failed: %s", exc)
        return []


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
    """Append a portfolio equity snapshot row (time-series, not a singleton).
    Called every Argus refresh — each call writes a new row so Grafana can
    plot the equity curve over time.
    """
    try:
        # Remove any stale singleton key from old code
        row = {k: v for k, v in state.items() if k != "state_id"}
        get_client().table("portfolio_state").insert(row).execute()
    except Exception as exc:
        logger.error("[SUPABASE] upsert_portfolio_state failed: %s", exc)


def insert_equity_snapshot(snapshot: dict) -> None:
    """Append one equity snapshot to the time-series (equity_snapshots, 008).

    Unlike portfolio_state (a singleton — latest snapshot only), this table is
    append-only so the dashboard can plot the equity curve over time. Each Argus
    refresh writes one row.
    """
    try:
        get_client().table("equity_snapshots").insert(snapshot).execute()
    except Exception as exc:
        logger.error("[SUPABASE] insert_equity_snapshot failed: %s", exc)


def upsert_portfolio_positions(positions: list[dict]) -> None:
    """Overwrite open positions. Delete all open rows then re-insert current snapshot."""
    try:
        client = get_client()
        # Clear all currently-open positions (closed_at IS NULL)
        client.table("portfolio_positions").delete().is_("closed_at", "null").execute()
        if positions:
            client.table("portfolio_positions").insert(positions).execute()
    except Exception as exc:
        logger.error("[SUPABASE] upsert_portfolio_positions failed: %s", exc)


# ── Decision traces ────────────────────────────────────────────────────────────

def insert_decision_trace(trace: dict) -> None:
    """Write a full pipeline audit trace (every signal, win or loss)."""
    try:
        # Drop signal_id to avoid FK constraint — it's a correlation ID, not a true FK
        row = {k: v for k, v in trace.items() if k != "signal_id"}
        get_client().table("decision_traces").insert(row).execute()
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


def upsert_hermes_signal(signal: dict) -> Optional[str]:
    """
    Hermes sink — idempotent insert via the upsert_hermes_signal DB function.
    `signal` must contain: hermes_id, source_url, headline, summary,
    published_at (ISO string), category, severity, affected_tickers (list),
    raw_text, supplier, hermes_signal_type, urgency, is_significant.
    Returns the signal_id UUID string or None on error.
    """
    try:
        res = get_client().rpc("upsert_hermes_signal", {
            "p_hermes_id":          signal["hermes_id"],
            "p_source_url":         signal.get("source_url", ""),
            "p_headline":           signal.get("headline", ""),
            "p_summary":            signal.get("summary", ""),
            "p_published_at":       signal["published_at"],
            "p_category":           signal["category"],
            "p_severity":           signal["severity"],
            "p_affected_tickers":   signal.get("affected_tickers", []),
            "p_raw_text":           signal.get("raw_text", ""),
            "p_supplier":           signal.get("supplier", ""),
            "p_hermes_signal_type": signal.get("hermes_signal_type", ""),
            "p_urgency":            signal.get("urgency", "LOW"),
            "p_is_significant":     signal.get("is_significant", False),
        }).execute()
        return res.data  # UUID string
    except Exception as exc:
        logger.error("[SUPABASE] upsert_hermes_signal failed: %s", exc)
        return None


def get_unconsumed_signals(limit: int = 100) -> list[dict]:
    """
    Icarus sink reader — fetch unconsumed signals Hermes has written.
    Returns rows ordered oldest-first so Icarus processes in arrival order.
    """
    try:
        res = (
            get_client()
            .table("signals")
            .select(
                "signal_id, hermes_id, source_url, headline, summary, "
                "published_at, category, severity, affected_tickers, "
                "raw_text, supplier, hermes_signal_type, urgency, is_significant"
            )
            .eq("consumed_by_icarus", False)
            .order("published_at", desc=False)
            .limit(limit)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        logger.error("[SUPABASE] get_unconsumed_signals failed: %s", exc)
        return []


def mark_signals_consumed(signal_ids: list[str]) -> int:
    """
    Mark a batch of signal_ids as consumed by Icarus.
    Returns the number of rows actually updated.
    """
    if not signal_ids:
        return 0
    try:
        res = get_client().rpc("mark_signals_consumed", {
            "signal_ids": signal_ids,
        }).execute()
        return res.data or 0
    except Exception as exc:
        logger.error("[SUPABASE] mark_signals_consumed failed: %s", exc)
        return 0


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
    """Look up ticker for a supplier — prefers NYSE/NASDAQ over OTC, ignores XETRA."""
    return get_ticker_for_market(supplier_name, preferred_exchange=None)


def get_ticker_for_market(
    supplier_name: str,
    preferred_exchange: Optional[str],
) -> Optional[str]:
    """Return the best ticker for supplier_name on the given exchange.

    Priority:
      1. preferred_exchange exact match (e.g. 'XETRA')
      2. NYSE → NASDAQ → OTC fallback chain
      3. Whatever is available if none of the above match
    Returns None if supplier is unknown.
    """
    try:
        res = (
            get_client()
            .table("ticker_map")
            .select("ticker, exchange")
            .eq("supplier_name", supplier_name)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return None

        by_exchange = {r["exchange"]: r["ticker"] for r in rows}

        if preferred_exchange and preferred_exchange in by_exchange:
            return by_exchange[preferred_exchange]

        for fallback in ("NYSE", "NASDAQ", "OTC"):
            if fallback in by_exchange:
                return by_exchange[fallback]

        return rows[0]["ticker"]
    except Exception as exc:
        logger.error("[SUPABASE] get_ticker_for_market failed: %s", exc)
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
        }, on_conflict="supplier_name,exchange").execute()
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


# ── Agent EXP (cosmetic level layer) ─────────────────────────────────────────

def fetch_trade_by_order_id(order_id: str) -> Optional[dict]:
    """SELECT a trade row back by order_id — the EXP mint needs fill_price,
    stop_loss, confidence, position_pct, which aren't in scope at the
    shadow_learning backfill call site. None if not found / on error."""
    try:
        res = (
            get_client().table("trades")
            .select("order_id, symbol, side, fill_price, stop_loss, confidence, position_pct, pnl_pct, hit, closed_at")
            .eq("order_id", order_id)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None
    except Exception as exc:
        logger.debug("[SUPABASE] fetch_trade_by_order_id failed: %s", exc)
        return None


def insert_exp_ledger(award: dict) -> bool:
    """Append one XP award (agent_exp_ledger). Returns True if newly inserted,
    False on a duplicate (UNIQUE agent_name+event_type+source_ref) or error.
    Idempotency is enforced by the DB constraint — a duplicate is a no-op."""
    try:
        get_client().table("agent_exp_ledger").insert(award).execute()
        return True
    except Exception as exc:
        msg = str(exc).lower()
        if "duplicate" in msg or "unique" in msg or "23505" in msg:
            return False  # idempotency hit — already awarded
        logger.error("[SUPABASE] insert_exp_ledger failed: %s", exc)
        return False


def fetch_exp_ledger(agent_name: str) -> Optional[list]:
    """All ledger rows for one agent (for recompute). None on error."""
    try:
        res = (
            get_client().table("agent_exp_ledger")
            .select("agent_name, event_type, xp, source_ref, metadata, created_at")
            .eq("agent_name", agent_name)
            .order("created_at", desc=False)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        logger.error("[SUPABASE] fetch_exp_ledger failed: %s", exc)
        return None


def upsert_agent_exp(row: dict) -> None:
    """Upsert the per-agent EXP rollup (agent_exp)."""
    try:
        row = {**row, "updated_at": datetime.now(timezone.utc).isoformat()}
        get_client().table("agent_exp").upsert(row, on_conflict="agent_name").execute()
    except Exception as exc:
        logger.error("[SUPABASE] upsert_agent_exp failed: %s", exc)


# ── Milestone / vault state (singleton row id=1) ────────────────────────────────

def fetch_milestone_state() -> Optional[dict]:
    """Load the persisted MilestoneManager state (vault origin + accounting).
    Returns the row dict, or None if no row exists yet / persistence unavailable.
    None means 'no stored origin' → the manager captures one and saves it."""
    try:
        res = (
            get_client().table("milestone_state")
            .select("milestone_origin, vault_balance, total_vaulted, crossed_stages")
            .eq("id", 1)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None
    except Exception as exc:
        logger.debug("[SUPABASE] fetch_milestone_state failed: %s", exc)
        return None


def upsert_milestone_state(state: dict) -> None:
    """Persist the singleton MilestoneManager state (id=1). Best-effort —
    a persistence failure must never crash the trading loop."""
    try:
        row = {"id": 1, **state, "updated_at": datetime.now(timezone.utc).isoformat()}
        get_client().table("milestone_state").upsert(row, on_conflict="id").execute()
    except Exception as exc:
        logger.error("[SUPABASE] upsert_milestone_state failed: %s", exc)


def fetch_agent_seniority_level(agent_name: str) -> Optional[int]:
    """Current authoritative seniority level_int for one agent, or None."""
    try:
        res = (
            get_client().table("agent_seniority")
            .select("level_int")
            .eq("agent_name", agent_name)
            .limit(1)
            .execute()
        )
        return res.data[0]["level_int"] if res.data else None
    except Exception as exc:
        logger.debug("[SUPABASE] fetch_agent_seniority_level failed: %s", exc)
        return None


def fetch_agent_progress_pct(agent_name: str) -> Optional[float]:
    """progress_pct toward the next rung if persisted on agent_seniority, else None."""
    try:
        res = (
            get_client().table("agent_seniority")
            .select("progress_pct")
            .eq("agent_name", agent_name)
            .limit(1)
            .execute()
        )
        if res.data and res.data[0].get("progress_pct") is not None:
            return float(res.data[0]["progress_pct"])
        return None
    except Exception:
        return None


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

        # Real money is live only at the Senior tier (int 3) AND when armed.
        from core.seniority import Tier, level_progress_pct, real_money_armed
        armed = real_money_armed()

        rows = []
        history_rows = []
        for name, score in scores.items():
            tier_int = score["tier_int"]            # 0..3 (TRAINEE..SENIOR)
            live_ok = tier_int >= int(Tier.SENIOR) and armed
            # Progress through the current level (0..100%) from the win count, so
            # the dashboard's bar and fetch_agent_progress_pct() are populated.
            progress_pct = round(level_progress_pct(int(score["wins"])) * 100, 2)
            row = {
                "agent_name":           name,
                "level":                score["tier"],      # tier name (enum-valued column)
                "level_int":            tier_int,           # tier ordinal (authoritative)
                "tier":                 score["tier"],
                "tier_level":           score["level"],     # 1..10 within the tier
                "rank":                 score["rank"],
                "wins":                 score["wins"],
                "progress_pct":         progress_pct,
                "cleared":              score["cleared"],
                "criteria":             score["criteria"],
                "notes":                score["notes"],
                "max_position_pct":     _tier_int_to_max_pos(tier_int, live_ok),
                "real_money_unlocked":  tier_int >= int(Tier.SENIOR),
                "real_money_armed":     armed,
                "live_trading_allowed": live_ok,
                "evaluated_at":         score["evaluated_at"],
                "updated_at":           now,
            }
            rows.append(row)

            prev_int = existing.get(name)
            if prev_int is None or tier_int != prev_int:
                history_rows.append({
                    "agent_name":  name,
                    "from_level":  _int_to_tier_label(prev_int) if prev_int is not None else None,
                    "to_level":    score["tier"],
                    "level_int":   tier_int,
                    "criteria":    score["criteria"],
                    "promoted_at": now,
                })

        client.table("agent_seniority").upsert(rows, on_conflict="agent_name").execute()
        if history_rows:
            client.table("agent_seniority_history").insert(history_rows).execute()

    except Exception as exc:
        logger.error("[SUPABASE] upsert_agent_seniority failed: %s", exc)


# Paper tiers cap at 3%; real money (Senior + armed) uses the 5% live cap.
_MAX_POS_BY_TIER: dict[int, float] = {0: 0.01, 1: 0.02, 2: 0.03, 3: 0.03}
_TIER_LABEL_BY_INT: dict[int, str] = {0: "Trainee", 1: "Junior", 2: "Intermediate", 3: "Senior"}
_REAL_MONEY_MAX_POS = 0.05


def _tier_int_to_max_pos(tier_int: int, live_ok: bool) -> float:
    if live_ok:
        return _REAL_MONEY_MAX_POS
    if tier_int not in _MAX_POS_BY_TIER:
        raise ValueError(f"Unknown seniority tier_int: {tier_int}")
    return _MAX_POS_BY_TIER[tier_int]


def _int_to_tier_label(tier_int: int) -> str:
    if tier_int not in _TIER_LABEL_BY_INT:
        raise ValueError(f"Unknown seniority tier_int: {tier_int}")
    return _TIER_LABEL_BY_INT[tier_int]


# ── Pending order queue ────────────────────────────────────────────────────────

def enqueue_pending_order(
    signal_id: str,
    symbol: str,
    side: str,
    payload: dict,
    approved_at: datetime,
    expires_at: datetime,
) -> Optional[dict]:
    """Insert a new PENDING order.  Silently ignores duplicate signal_id (idempotent)."""
    try:
        res = (
            get_client()
            .table("pending_orders")
            .upsert(
                {
                    "signal_id":   signal_id,
                    "symbol":      symbol,
                    "side":        side,
                    "payload":     payload,
                    "approved_at": approved_at.isoformat(),
                    "expires_at":  expires_at.isoformat(),
                    "status":      "PENDING",
                },
                on_conflict="signal_id",
                ignore_duplicates=True,
            )
            .execute()
        )
        return res.data[0] if res.data else None
    except Exception as exc:
        logger.error("[SUPABASE] enqueue_pending_order failed: %s", exc)
        return None


def get_retryable_pending_orders(now: datetime, limit: int = 10) -> list[dict]:
    """Return PENDING rows that have not yet expired, oldest-first."""
    try:
        res = (
            get_client()
            .table("pending_orders")
            .select("*")
            .eq("status", "PENDING")
            .gt("expires_at", now.isoformat())
            .order("approved_at", desc=False)
            .limit(limit)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        logger.error("[SUPABASE] get_retryable_pending_orders failed: %s", exc)
        return []


def set_pending_status(signal_id: str, status: str, **fields) -> None:
    """Update status (and any extra fields) for a pending_orders row."""
    try:
        update = {"status": status, "updated_at": datetime.now(timezone.utc).isoformat(), **fields}
        get_client().table("pending_orders").update(update).eq("signal_id", signal_id).execute()
    except Exception as exc:
        logger.error("[SUPABASE] set_pending_status failed: %s", exc)


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
