"""
core/shadow_learning.py — Shadow Learning Layer

Four components that close the feedback loop between ZEUS decisions and real outcomes:

  1. OutcomeResolver   — polls open positions, detects closes, backfills pnl_pct
                         into Supabase trades table + ChromaDB decision traces.
                         Called by Argus.refresh() on every portfolio tick.

  2. PromotionGate     — Pythia's learned hit rate is only "trusted" when n >= MIN_SAMPLES.
                         Below that, falls back to the Bayesian-shrunk default.
                         Wraps _lookup_stats so Pythia always gets a calibrated estimate.

  3. Backtester        — replays the historical KB entries (earnings, insider trades,
                         EDGAR 8-K) through Hades → Pythia to pre-populate hit rate
                         context keys before paper trading begins.
                         Run once via POST /run/backtest.

  4. ReplayEngine      — re-runs any saved DecisionTrace through a new version of ZEUS
                         reasoning (prompt A/B testing) without touching live trading.
                         Returns a ReplayResult for comparison.

Import rule: imports from core.types only. Never imports from agents/*.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from core.types import (
    DecisionTrace, FilteredSignal, MacroContext, MarketRegime,
    RawSignal, Severity, SignalCategory,
)

logger = logging.getLogger("shadow_learning")

# Promotion gate: minimum closed trades before hit rate is trusted
_MIN_SAMPLES = 10

# Bayesian prior: shrink toward 0.50 with weight equivalent to 10 observations
_PRIOR_WEIGHT  = 10
_PRIOR_WIN_RATE = 0.50


# ── 1. OutcomeResolver ────────────────────────────────────────────────────────

class OutcomeResolver:
    """
    Detects when open IBKR positions close and backfills P&L into:
      - Supabase `trades` table (so Pythia's hit rate stats update)
      - ChromaDB `decisions` collection (so ZEUS's KB reflects real outcomes)

    Called by Argus.refresh() on every portfolio tick. Safe to call frequently —
    only acts when a position actually changed from open to closed.
    """

    def __init__(self, knowledge_base=None):
        self._kb          = knowledge_base   # shared KnowledgeBase instance
        self._open_orders: dict[str, float] = {}   # order_id → fill_price

    def track_open(self, order_id: str, fill_price: float, symbol: str) -> None:
        """Register a newly placed order so we can detect when it closes."""
        self._open_orders[order_id] = fill_price
        logger.debug("[OUTCOME] Tracking open order %s %s @ %.2f", order_id, symbol, fill_price)

    def resolve_closed(self, order_id: str, exit_price: float,
                       side: str, closed_at: Optional[datetime] = None) -> Optional[float]:
        """
        Called when Argus detects a position has closed.
        Calculates pnl_pct, writes to Supabase + ChromaDB, removes from tracking.
        Returns pnl_pct or None if order_id was not tracked.
        """
        fill_price = self._open_orders.pop(order_id, None)
        if fill_price is None:
            logger.debug("[OUTCOME] order_id %s not in tracking — skipping", order_id)
            return None

        closed_at = closed_at or datetime.now(timezone.utc)
        is_long   = side.upper() == "BUY"

        if fill_price == 0:
            return None

        pnl_pct = (exit_price - fill_price) / fill_price
        if not is_long:
            pnl_pct = -pnl_pct
        pnl_pct = round(pnl_pct, 6)
        hit     = pnl_pct > 0

        # Write to Supabase
        self._backfill_supabase(order_id, pnl_pct, hit, closed_at)

        # Write to ChromaDB KB (for ZEUS's self-improvement loop)
        if self._kb is not None:
            try:
                self._kb.update_outcome(order_id, pnl_pct)
            except Exception as exc:
                logger.warning("[OUTCOME] KB update_outcome failed: %s", exc)

        logger.info(
            "[OUTCOME] Resolved %s: side=%s fill=%.2f exit=%.2f pnl=%.2f%% hit=%s",
            order_id, side, fill_price, exit_price, pnl_pct * 100, hit,
        )
        return pnl_pct

    def resolve_all_from_portfolio(self, portfolio_positions: list[dict],
                                   current_prices: dict[str, float]) -> list[float]:
        """
        Batch resolution: given the current open positions and current prices,
        resolve any tracked orders that are no longer in the portfolio.
        Returns list of resolved pnl_pcts.
        """
        open_symbols = {p.get("symbol") for p in portfolio_positions}
        resolved = []

        for order_id in list(self._open_orders.keys()):
            # If we have a position for this order_id still open, skip
            # (simplified: check by order_id prefix matching symbol)
            symbol = self._symbol_for_order(order_id, portfolio_positions)
            if symbol and symbol in open_symbols:
                continue

            # Position no longer open — resolve at current price
            if symbol and symbol in current_prices:
                pnl = self.resolve_closed(order_id, current_prices[symbol], "BUY")
                if pnl is not None:
                    resolved.append(pnl)

        return resolved

    @property
    def open_count(self) -> int:
        return len(self._open_orders)

    @staticmethod
    def _backfill_supabase(order_id: str, pnl_pct: float, hit: bool,
                           closed_at: datetime) -> None:
        import os
        if not (os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY")):
            return
        try:
            import core.supabase_client as supa
            supa.update_trade_pnl(order_id, pnl_pct, hit, closed_at)
        except Exception as exc:
            logger.warning("[OUTCOME] Supabase backfill failed: %s", exc)

    @staticmethod
    def _symbol_for_order(order_id: str, positions: list[dict]) -> Optional[str]:
        for p in positions:
            if p.get("order_id") == order_id:
                return p.get("symbol")
        return None


# ── 2. PromotionGate ──────────────────────────────────────────────────────────

class PromotionGate:
    """
    Wraps Pythia's raw hit rate stats with a promotion check and Bayesian shrinkage.

    Rules:
      - n < MIN_SAMPLES (10): use Bayesian-shrunk estimate, flag as "learning"
      - n >= MIN_SAMPLES: use observed hit rate, flag as "trusted"
      - Always apply Bayesian shrinkage toward 0.50 to prevent overfitting on small n

    Bayesian formula:
      adjusted_win_rate = (n × observed + PRIOR_WEIGHT × PRIOR) / (n + PRIOR_WEIGHT)

    This prevents a 3/3 run (100% win rate) from producing reckless sizing.
    """

    @staticmethod
    def evaluate(raw_stats: Optional[dict]) -> dict:
        """
        Takes raw stats dict {'n': int, 'hit_rate': float} or None.
        Returns enriched dict with confidence, trust level, and sizing guidance.
        """
        if raw_stats is None or raw_stats.get("n", 0) == 0:
            return {
                "confidence":   _PRIOR_WIN_RATE,
                "n":            0,
                "trusted":      False,
                "status":       "cold_start",
                "shrunk_rate":  _PRIOR_WIN_RATE,
            }

        n             = raw_stats["n"]
        observed_rate = raw_stats.get("hit_rate", _PRIOR_WIN_RATE)

        # Bayesian shrinkage toward prior
        shrunk_rate = (
            (n * observed_rate + _PRIOR_WEIGHT * _PRIOR_WIN_RATE)
            / (n + _PRIOR_WEIGHT)
        )
        shrunk_rate = round(shrunk_rate, 4)

        trusted = n >= _MIN_SAMPLES
        status  = "trusted" if trusted else "learning"

        logger.debug(
            "[GATE] n=%d observed=%.2f shrunk=%.2f trusted=%s",
            n, observed_rate, shrunk_rate, trusted,
        )

        return {
            "confidence":  shrunk_rate,
            "n":           n,
            "trusted":     trusted,
            "status":      status,
            "shrunk_rate": shrunk_rate,
            "raw_rate":    observed_rate,
        }

    @staticmethod
    def is_trusted(context_key: str, db_path: Optional[Path] = None) -> bool:
        """Quick check: does this context key have enough data to be trusted?"""
        stats = PromotionGate._load_stats(context_key, db_path)
        return stats is not None and stats.get("n", 0) >= _MIN_SAMPLES

    @staticmethod
    def _load_stats(context_key: str, db_path: Optional[Path]) -> Optional[dict]:
        import os
        if os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY"):
            try:
                import core.supabase_client as supa
                row = supa.get_hit_rates(context_key)
                if row:
                    return {"n": row.get("closed_trades", 0),
                            "hit_rate": row.get("hit_rate") or _PRIOR_WIN_RATE}
            except Exception:
                pass
        if db_path and db_path.exists():
            try:
                with sqlite3.connect(db_path) as conn:
                    row = conn.execute(
                        "SELECT COUNT(*), AVG(CASE WHEN hit=1 THEN 1.0 ELSE 0.0 END) "
                        "FROM trades WHERE context_key=? AND hit IS NOT NULL",
                        (context_key,),
                    ).fetchone()
                if row and row[0]:
                    return {"n": row[0], "hit_rate": row[1] or _PRIOR_WIN_RATE}
            except Exception:
                pass
        return None


# ── 3. Backtester ─────────────────────────────────────────────────────────────

@dataclass
class BacktestResult:
    total_signals:   int = 0
    hades_killed:    int = 0
    pythia_sized:    int = 0
    skipped_low_conf: int = 0
    context_keys_seeded: set = field(default_factory=set)
    errors:          list = field(default_factory=list)
    started_at:      str  = ""
    finished_at:     str  = ""

    def summary(self) -> dict:
        return {
            "total_signals":        self.total_signals,
            "hades_killed":         self.hades_killed,
            "pythia_sized":         self.pythia_sized,
            "skipped_low_conf":     self.skipped_low_conf,
            "context_keys_seeded":  len(self.context_keys_seeded),
            "errors":               self.errors,
            "started_at":           self.started_at,
            "finished_at":          self.finished_at,
        }


class Backtester:
    """
    Replays historical KB entries through the Hades → Pythia pipeline to
    pre-populate context key statistics before paper trading begins.

    Sources replayed:
      - earnings:{ticker}:{date} entries → EARNINGS_SURPRISE signals
      - form4:{ticker}:{date} entries    → POSITIVE_NEWS signals (insider buy = bullish)
      - edgar:8k:{entity} entries        → SUPPLIER_DISRUPTION signals

    Each signal is run through:
      1. Hades compliance check (OFAC/ESG — some may be killed)
      2. Pythia sizing (records a synthetic trade with outcome inferred from KB text)

    This gives Pythia ~200-400 seeded data points so the promotion gate
    has real data to work with from day one instead of all cold-starts.

    The backtester never touches Ares — no orders are placed.
    """

    def __init__(self, hades_agent, pythia_agent, macro_context: MacroContext):
        self._hades  = hades_agent
        self._pythia = pythia_agent
        self._macro  = macro_context

    def run(self, knowledge_base) -> BacktestResult:
        """
        Pull historical entries from the KB and replay them through the pipeline.
        Returns a BacktestResult summary.
        """
        result = BacktestResult(started_at=datetime.now(timezone.utc).isoformat())
        logger.info("[BACKTEST] Starting historical replay.")

        if knowledge_base is None:
            result.errors.append("No knowledge_base provided")
            result.finished_at = datetime.now(timezone.utc).isoformat()
            return result

        # Query KB for each historical source type
        for query, category in [
            ("earnings history reported EPS surprise beat miss",    SignalCategory.EARNINGS_SURPRISE),
            ("insider transaction open-market purchase buy signal",  SignalCategory.POSITIVE_NEWS),
            ("supply chain disruption 8-K filed shortage",          SignalCategory.SUPPLIER_DISRUPTION),
        ]:
            try:
                entries = knowledge_base.query_knowledge(query, n_results=50)
                for text in entries:
                    self._replay_entry(text, category, result)
            except Exception as exc:
                msg = f"KB query failed for '{query[:30]}': {exc}"
                logger.warning("[BACKTEST] %s", msg)
                result.errors.append(msg)

        result.finished_at = datetime.now(timezone.utc).isoformat()
        logger.info(
            "[BACKTEST] Complete — signals=%d killed=%d sized=%d keys=%d errors=%d",
            result.total_signals, result.hades_killed, result.pythia_sized,
            len(result.context_keys_seeded), len(result.errors),
        )
        return result

    def _replay_entry(self, text: str, category: SignalCategory,
                      result: BacktestResult) -> None:
        """Replay one KB text entry as a synthetic signal through Hades → Pythia."""
        result.total_signals += 1

        # Build a synthetic RawSignal from the KB text
        ticker   = self._extract_ticker(text)
        outcome  = self._infer_outcome(text, category)
        raw      = self._make_raw_signal(text, category, ticker)

        # Hades compliance check
        try:
            filtered = self._hades.filter(raw)
        except Exception as exc:
            result.errors.append(f"Hades error: {exc}")
            return

        if filtered is None:
            result.hades_killed += 1
            return

        # Pythia sizing
        try:
            sized = self._pythia.size(filtered, self._macro)
        except Exception as exc:
            result.errors.append(f"Pythia error: {exc}")
            return

        if sized.skip:
            result.skipped_low_conf += 1
            return

        # Record a synthetic trade result with inferred outcome
        from core.types import TradeResult
        synthetic_result = TradeResult(
            order_id    = f"backtest-{datetime.now(timezone.utc).timestamp():.0f}",
            symbol      = ticker or "UNKNOWN",
            side        = "BUY" if category != SignalCategory.SUPPLIER_DISRUPTION else "SELL",
            qty         = 1,
            fill_price  = 100.0,
            stop_loss_price   = 97.0,
            take_profit_price = 106.0,
            status      = "simulated",
            pnl_pct     = outcome,
        )

        try:
            self._pythia.record_trade(sized, synthetic_result)
            result.pythia_sized += 1
            key = f"{category.value}|{self._macro.regime.value}|{self._vix_band()}"
            result.context_keys_seeded.add(key)
        except Exception as exc:
            result.errors.append(f"record_trade error: {exc}")

    @staticmethod
    def _extract_ticker(text: str) -> Optional[str]:
        """Extract ticker from KB entry text (e.g. 'Earnings history: NVDA — 2024-01-15')."""
        import re
        m = re.search(r':\s([A-Z]{2,5})\s[—\-]', text)
        return m.group(1) if m else None

    @staticmethod
    def _infer_outcome(text: str, category: SignalCategory) -> float:
        """
        Infer a synthetic P&L outcome from the KB text content.
        This is not a real outcome — it seeds Pythia's priors with directional
        signals extracted from historical descriptions.
        """
        text_lower = text.lower()

        if category == SignalCategory.EARNINGS_SURPRISE:
            if "beat" in text_lower and "positive" in text_lower:
                return 0.04   # +4% — earnings beat, positive price reaction
            if "miss" in text_lower or "negative" in text_lower:
                return -0.03  # -3% — miss, negative reaction
            return 0.01       # slight positive default

        if category == SignalCategory.POSITIVE_NEWS:
            # Insider buying is historically a weak-to-moderate bullish signal
            if "purchase" in text_lower or "buy" in text_lower:
                return 0.025
            return 0.01

        if category == SignalCategory.SUPPLIER_DISRUPTION:
            return -0.04      # supply chain disruptions historically negative

        return 0.0

    def _vix_band(self) -> str:
        vix = self._macro.vix
        if vix < 15: return "low"
        if vix < 25: return "medium"
        if vix < 35: return "high"
        return "extreme"

    @staticmethod
    def _make_raw_signal(text: str, category: SignalCategory,
                         ticker: Optional[str]) -> RawSignal:
        import uuid
        headline = text[:120].replace("\n", " ")
        return RawSignal(
            signal_id       = str(uuid.uuid4()),
            source_url      = "backtest://kb",
            headline        = headline,
            summary         = headline,
            published_at    = datetime.now(timezone.utc),
            category        = category,
            severity        = Severity.MEDIUM,
            affected_tickers= [ticker] if ticker else ["AAPL"],
            raw_text        = text[:500],
            supplier        = "BacktestReplay",
        )


# ── 4. ReplayEngine ───────────────────────────────────────────────────────────

@dataclass
class ReplayResult:
    trace_id:          str
    original_approved: bool
    replay_approved:   bool
    original_reasoning: str
    replay_reasoning:  str
    agreement:         bool    # did new ZEUS agree with original decision?
    replay_at:         str     = ""

    def changed_mind(self) -> bool:
        return not self.agreement


class ReplayEngine:
    """
    Re-runs saved DecisionTraces through a new version of ZEUS reasoning
    to A/B test prompt changes, knowledge base updates, or model upgrades.

    Use cases:
      1. After updating zeus_skills.md — verify ZEUS makes better decisions
         on past signals without touching live trading
      2. After KB bootstrap — confirm ZEUS now approves signals it previously
         skipped due to lack of context
      3. Before going live — replay last 30 days of paper trades to validate
         the approval rate is stable

    The ReplayEngine never calls Ares — it only exercises the ZEUS LLM step.
    """

    def __init__(self, zeus_agent):
        self._zeus = zeus_agent

    def replay_trace(self, trace: DecisionTrace) -> Optional[ReplayResult]:
        """
        Re-run a single DecisionTrace through the current ZEUS reasoning.
        Returns a ReplayResult comparing old vs new decision.
        """
        if not trace.headline:
            return None

        # Reconstruct a minimal FilteredSignal from the trace
        try:
            signal   = self._trace_to_filtered_signal(trace)
            macro    = self._trace_to_macro_context(trace)
            sized    = self._trace_to_sized_signal(trace, signal, macro)
        except Exception as exc:
            logger.warning("[REPLAY] Failed to reconstruct signal from trace %s: %s",
                           trace.trace_id, exc)
            return None

        # Run through current ZEUS decision logic
        try:
            new_decision = self._zeus.decide(sized)
        except Exception as exc:
            logger.warning("[REPLAY] ZEUS decide failed for trace %s: %s",
                           trace.trace_id, exc)
            return None

        result = ReplayResult(
            trace_id           = trace.trace_id,
            original_approved  = trace.zeus_approved,
            replay_approved    = new_decision.get("approved", False),
            original_reasoning = trace.zeus_reasoning or "",
            replay_reasoning   = new_decision.get("reasoning", ""),
            agreement          = trace.zeus_approved == new_decision.get("approved", False),
            replay_at          = datetime.now(timezone.utc).isoformat(),
        )

        if result.changed_mind():
            logger.info(
                "[REPLAY] trace=%s MIND CHANGED: was %s → now %s",
                trace.trace_id,
                "APPROVED" if trace.zeus_approved else "REJECTED",
                "APPROVED" if result.replay_approved else "REJECTED",
            )

        return result

    def replay_recent(self, knowledge_base, limit: int = 30) -> list[ReplayResult]:
        """
        Replay the most recent N decision traces.
        Returns list of ReplayResults for analysis.
        """
        results = []
        try:
            raw = knowledge_base.get_recent_decisions(limit=limit)
            metas = raw.get("metadatas", [])
            docs  = raw.get("documents", [])
        except Exception as exc:
            logger.warning("[REPLAY] Failed to load recent decisions: %s", exc)
            return results

        for meta, doc in zip(metas, docs):
            try:
                trace = self._meta_to_trace(meta, doc)
                result = self.replay_trace(trace)
                if result:
                    results.append(result)
            except Exception as exc:
                logger.debug("[REPLAY] Skipping trace: %s", exc)

        agreement_rate = sum(1 for r in results if r.agreement) / len(results) if results else 0.0
        logger.info(
            "[REPLAY] Replayed %d traces — agreement rate: %.0f%%",
            len(results), agreement_rate * 100,
        )
        return results

    def agreement_rate(self, results: list[ReplayResult]) -> float:
        if not results:
            return 0.0
        return sum(1 for r in results if r.agreement) / len(results)

    # ── Private helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _trace_to_filtered_signal(trace: DecisionTrace) -> FilteredSignal:
        import uuid
        try:
            category = SignalCategory(trace.category)
        except (ValueError, TypeError):
            category = SignalCategory.POSITIVE_NEWS

        try:
            severity = Severity(trace.severity) if trace.severity else Severity.MEDIUM
        except (ValueError, TypeError):
            severity = Severity.MEDIUM

        raw = RawSignal(
            signal_id        = trace.signal_id or str(uuid.uuid4()),
            source_url       = "replay://kb",
            headline         = trace.headline or "",
            summary          = trace.headline or "",
            published_at     = trace.timestamp,
            category         = category,
            severity         = severity,
            affected_tickers = [trace.symbol] if trace.symbol else ["AAPL"],
            raw_text         = trace.headline or "",
            supplier         = trace.supplier or "ReplayEngine",
        )
        return FilteredSignal(original=raw, compliance_score=1.0)

    @staticmethod
    def _trace_to_macro_context(trace: DecisionTrace) -> MacroContext:
        try:
            regime = MarketRegime(trace.trend_regime) if trace.trend_regime else MarketRegime.SIDEWAYS
        except (ValueError, TypeError):
            regime = MarketRegime.SIDEWAYS

        return MacroContext(
            fetched_at      = trace.timestamp,
            regime          = regime,
            vix             = trace.trend_vix or 18.0,
            sp500_1m_return = 0.0,
        )

    @staticmethod
    def _trace_to_sized_signal(trace: DecisionTrace,
                               signal: FilteredSignal,
                               macro: MacroContext):
        from core.types import SizedSignal
        return SizedSignal(
            original          = signal,
            macro             = macro,
            confidence        = trace.pattern_confidence or 0.55,
            position_size_pct = trace.pattern_size_pct or 0.02,
        )

    @staticmethod
    def _meta_to_trace(meta: dict, doc: str) -> DecisionTrace:
        return DecisionTrace(
            trace_id           = meta.get("trace_id", ""),
            signal_id          = meta.get("signal_id"),
            timestamp          = datetime.fromisoformat(meta["timestamp"])
                                 if meta.get("timestamp") else datetime.now(timezone.utc),
            headline           = doc.split("\n")[0].replace("Signal: ", ""),
            supplier           = meta.get("supplier", ""),
            category           = meta.get("category", ""),
            severity           = meta.get("severity", ""),
            hades_passed       = True,
            hades_notes        = [],
            trend_suppressed   = False,
            trend_regime       = meta.get("regime"),
            trend_vix          = float(meta.get("vix", 18.0)),
            pattern_confidence = float(meta.get("pattern_confidence", 0.55))
                                 if meta.get("pattern_confidence") else 0.55,
            pattern_size_pct   = float(meta.get("pattern_size_pct", 0.02))
                                 if meta.get("pattern_size_pct") else 0.02,
            zeus_reasoning     = meta.get("zeus_reasoning", ""),
            zeus_approved      = meta.get("approved") in (True, "True"),
            zeus_override      = False,
            zeus_override_reason = None,
            trade_placed       = meta.get("trade_placed", False),
            symbol             = meta.get("symbol"),
            side               = meta.get("side"),
            fill_price         = float(meta.get("fill_price", 0)) if meta.get("fill_price") else None,
            pnl_pct            = float(meta.get("pnl_pct", 0)) if meta.get("pnl_pct") else None,
            killed_at_stage    = meta.get("killed_at_stage"),
            kill_reason        = meta.get("kill_reason"),
        )
