"""
Agent 4 — Pythia: Pattern Learning & Position Sizing
The Oracle of Delphi — reads patterns, predicts outcomes.

Storage: Supabase (primary) with SQLite fallback when SUPABASE_URL is not set.
This allows local development without a Supabase account.
Imports only from core.types — never from other agents.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.agent_knowledge import AgentKnowledgeBase
from core.types import (
    AgentHealth,
    FilteredSignal,
    MacroContext,
    SizedSignal,
    TradeResult,
)

logger = logging.getLogger("pythia")

DB_PATH           = Path("data/trade_log.db")
_MIN_SAMPLES      = int(os.getenv("PYTHIA_MIN_SAMPLES", "10"))
_DEFAULT_SIZE_PCT = float(os.getenv("PYTHIA_DEFAULT_SIZE_PCT", "0.02"))
_MIN_CONFIDENCE   = float(os.getenv("PYTHIA_MIN_CONFIDENCE", "0.45"))

# ── Cold-start exploration ────────────────────────────────────────────────────
# The deadlock: with no closed trades, every signal hits stats=None → confidence
# floors at 0.55 → ZEUS reads "no conviction" and vetoes → no trades close → stats
# stays None forever. To break it, allow a BOUNDED number of paper-only seeding
# trades at a confidence just above the floor so ZEUS will let them through and
# Pythia gathers its first real hit-rate data. Self-terminates once the budget is
# spent (or real history exists). Never fires with real money.
_EXPLORATION_TRADES     = int(os.getenv("PYTHIA_EXPLORATION_TRADES", "20"))
_EXPLORATION_CONFIDENCE = float(os.getenv("PYTHIA_EXPLORATION_CONFIDENCE", "0.62"))


class PythiaAgent:
    def __init__(self, db_path: Path = DB_PATH, milestone_manager=None):
        self.db_path        = db_path
        self._milestone     = milestone_manager
        self.kb             = AgentKnowledgeBase("pythia")
        self._use_supabase  = bool(
            os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        )

        if self._use_supabase:
            logger.info("[PYTHIA] Using Supabase for trade storage.")
        else:
            logger.info("[PYTHIA] No Supabase config — falling back to SQLite.")
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_db()

    def health(self) -> AgentHealth:
        if self._use_supabase:
            try:
                import core.supabase_client as supa
                supa.get_client()
                return AgentHealth.HEALTHY
            except Exception:
                return AgentHealth.DEGRADED
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("SELECT 1")
            return AgentHealth.HEALTHY
        except Exception:
            return AgentHealth.FAILED

    def size(self, signal: FilteredSignal, macro: MacroContext) -> SizedSignal:
        key   = self._context_key(signal, macro)
        stats = self._lookup_stats(key)

        is_exploration = False
        if stats is None:
            # No-history setups default to 0.55 = tier 2 (lowest-conviction, min
            # size). Do NOT raise this default — that would encode "no evidence"
            # as high conviction.
            confidence   = 0.55
            position_pct = _DEFAULT_SIZE_PCT
            # Cold-start exploration: break the no-trades→no-data deadlock with a
            # bounded number of PAPER-ONLY seeding trades at a confidence just
            # above the floor, so ZEUS lets them through and Pythia gathers its
            # first hit-rates. Self-terminates once the budget is spent. Gated
            # hard on paper mode — never bumps confidence with real money.
            if self._exploration_active():
                confidence     = _EXPLORATION_CONFIDENCE
                is_exploration = True
                logger.info(
                    "[PYTHIA] cold-start EXPLORATION (paper) key=%s conf=%.2f size=%.2f%% — seeding history",
                    key, confidence, position_pct * 100,
                )
        else:
            confidence   = stats["hit_rate"]
            edge         = max(0.0, confidence - 0.5) * 2
            position_pct = min(0.05, _DEFAULT_SIZE_PCT + edge * 0.03)
            logger.info("[PYTHIA] key=%s n=%d hit_rate=%.2f size=%.2f%%",
                        key, stats["n"], confidence, position_pct * 100)

        # Milestone hard cap — never exceed stage's max position size
        if self._milestone:
            stage_cfg    = self._milestone.config
            position_pct = min(position_pct, stage_cfg.max_position_pct)
            tier = 1 if confidence >= 0.70 else 2 if confidence >= 0.55 else 3
            if tier not in stage_cfg.allowed_tiers:
                return SizedSignal(
                    original=signal, macro=macro,
                    confidence=confidence, position_size_pct=0.0,
                    skip=True,
                    skip_reason=f"Stage {stage_cfg.stage.value}: tier {tier} not allowed (need {stage_cfg.allowed_tiers})",
                    is_exploration=is_exploration,
                )

        skip = confidence < _MIN_CONFIDENCE
        return SizedSignal(
            original          = signal,
            macro             = macro,
            confidence        = confidence,
            position_size_pct = position_pct,
            skip              = skip,
            skip_reason       = f"confidence {confidence:.2f} < threshold" if skip else None,
            is_exploration    = is_exploration,
        )

    def _exploration_active(self) -> bool:
        """True iff cold-start exploration should fire for a no-history signal:
        paper mode AND fewer closed trades than the exploration budget. Gated
        hard on paper_trading so this can never bump confidence on real money."""
        if _EXPLORATION_TRADES <= 0:
            return False
        try:
            from config.settings import load_settings
            if not load_settings().get("paper_trading", True):
                return False  # real money — exploration disabled, full stop
        except Exception:
            return False      # can't confirm paper mode → fail safe (no bump)
        return self._total_closed_trades() < _EXPLORATION_TRADES

    def record_trade(self, sized: SizedSignal, result: TradeResult) -> None:
        key    = self._context_key(sized.original, sized.macro)
        hit    = (result.pnl_pct > 0) if result.pnl_pct is not None else None
        regime = sized.macro.regime.value if hasattr(sized.macro.regime, "value") else str(sized.macro.regime)

        if self._use_supabase:
            self._insert_supabase(sized, result, key, hit, regime)
        else:
            self._insert_sqlite(
                trade_id    = result.order_id or str(uuid.uuid4()),
                signal_id   = sized.signal_id,
                context_key = key,
                category    = sized.category.value,
                regime      = regime,
                vix_band    = self._vix_band(sized.macro.vix),
                confidence  = sized.confidence,
                position_pct= sized.position_size_pct,
                symbol      = result.symbol,
                side        = result.side,
                fill_price  = result.fill_price,
                pnl_pct     = result.pnl_pct,
                hit         = hit,
                recorded_at = datetime.now(timezone.utc).isoformat(),
            )

    # ── Storage backends ───────────────────────────────────────────────────────

    def _insert_supabase(self, sized: SizedSignal, result: TradeResult, key: str, hit, regime: str) -> None:
        import core.supabase_client as supa
        from core.types import SignalCategory
        side = result.side if result.side else (
            "SELL" if sized.category == SignalCategory.SUPPLIER_DISRUPTION else "BUY"
        )
        supa.insert_trade({
            "order_id":     result.order_id or str(uuid.uuid4()),
            "signal_id":    sized.signal_id or None,
            "context_key":  key,
            "category":     sized.category.value,
            "regime":       regime,
            "vix_band":     self._vix_band(sized.macro.vix),
            "confidence":   sized.confidence,
            "position_pct": sized.position_size_pct,
            "symbol":       result.symbol,
            "side":         side,
            "fill_price":   result.fill_price if result.fill_price and result.fill_price == result.fill_price else None,
            "stop_loss":    result.stop_loss_price,
            "take_profit":  result.take_profit_price,
            "qty":          result.qty,
            "pnl_pct":      result.pnl_pct,
            "hit":          hit,
            "recorded_at":  datetime.now(timezone.utc).isoformat(),
        })

    def _lookup_stats(self, key: str) -> Optional[dict]:
        if self._use_supabase:
            return self._lookup_stats_supabase(key)
        return self._lookup_stats_sqlite(key)

    def _lookup_stats_supabase(self, key: str) -> Optional[dict]:
        import core.supabase_client as supa
        row = supa.get_hit_rates(key)
        if row and row.get("closed_trades", 0) >= _MIN_SAMPLES:
            return {"n": row["closed_trades"], "hit_rate": row["hit_rate"] or 0.5}
        return None

    def _lookup_stats_sqlite(self, key: str) -> Optional[dict]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("""
                SELECT COUNT(*) AS n,
                       AVG(CASE WHEN hit = 1 THEN 1.0 ELSE 0.0 END) AS hit_rate
                FROM trades
                WHERE context_key = ? AND hit IS NOT NULL
            """, (key,)).fetchone()
        if row and row[0] >= _MIN_SAMPLES:
            return {"n": row[0], "hit_rate": row[1] if row[1] is not None else 0.5}
        return None

    def _total_closed_trades(self) -> int:
        """Count of all closed (outcome-known) trades — drives the exploration
        budget. Best-effort: on any storage error, return a large number so we
        do NOT keep exploring blindly when we can't confirm the count."""
        try:
            if self._use_supabase:
                import core.supabase_client as supa
                rows = supa.get_client().table("trades") \
                    .select("trade_id", count="exact") \
                    .not_.is_("hit", "null") \
                    .execute()
                return rows.count if rows.count is not None else len(rows.data or [])
            with sqlite3.connect(self.db_path) as conn:
                return conn.execute(
                    "SELECT COUNT(*) FROM trades WHERE hit IS NOT NULL"
                ).fetchone()[0]
        except Exception as exc:
            logger.warning("[PYTHIA] closed-trade count failed (%s) — pausing exploration", exc)
            return _EXPLORATION_TRADES  # treat as budget-exhausted, fail safe

    # ── SQLite fallback ────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id     TEXT,
                    signal_id    TEXT,
                    context_key  TEXT,
                    category     TEXT,
                    regime       TEXT,
                    vix_band     TEXT,
                    confidence   REAL,
                    position_pct REAL,
                    symbol       TEXT,
                    side         TEXT,
                    fill_price   REAL,
                    pnl_pct      REAL,
                    hit          INTEGER,
                    recorded_at  TEXT
                )
            """)
            conn.commit()

    def _insert_sqlite(self, **kwargs) -> None:
        cols         = ", ".join(kwargs.keys())
        placeholders = ", ".join("?" for _ in kwargs)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(f"INSERT INTO trades ({cols}) VALUES ({placeholders})", list(kwargs.values()))
            conn.commit()

    # ── Helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _context_key(signal: FilteredSignal, macro: MacroContext) -> str:
        regime   = macro.regime.value if hasattr(macro.regime, "value") else str(macro.regime)
        vix_band = PythiaAgent._vix_band(macro.vix)
        return f"{signal.category.value}|{regime}|{vix_band}"

    @staticmethod
    def _vix_band(vix: float) -> str:
        if vix < 15: return "low"
        if vix < 25: return "medium"
        if vix < 35: return "high"
        return "extreme"
