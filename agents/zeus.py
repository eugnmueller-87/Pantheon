"""
ZEUS — Supreme Orchestrator Agent

Responsibilities:
  1. Own the pipeline — every agent is a child, no agent talks to another
  2. Query the Knowledge Base before making the final trade decision
  3. Run an LLM reasoning step (Claude) to evaluate the signal holistically
  4. Write a full DecisionTrace to the KB for every signal processed
  5. Manage circuit breakers — degrade gracefully if any agent fails
  6. Start and monitor the Watchdog for zero-outage operation

Import rule: zeus.py is the ONLY file allowed to import from agents/*.
All other agents import from core.types only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import anthropic

from core.types import (
    AgentHealth, DecisionTrace, FilteredSignal, HealthReport,
    MacroContext, MarketRegime, PipelineStatus, RawSignal,
    SizedSignal, TradeResult,
)
from core.knowledge_base import KnowledgeBase
from core.circuit_breaker import CircuitBreaker
from core.watchdog import Watchdog
from core.redis_bridge import RedisBridge
from core.milestone_manager import MilestoneManager
from core.seniority import SeniorityEvaluator, SeniorityReport

from agents.icarus import IcarusAgent
from agents.hades import HadesAgent
from agents.artemis import ArtemisAgent
from agents.pythia import PythiaAgent
from agents.ares import AresAgent
from agents.ares_mock import AresMockAgent
from agents.argus import ArgusAgent
from agents.apollo import ApolloAgent

logger = logging.getLogger("zeus")


@dataclass
class ZeusConfig:
    max_portfolio_drawdown_pct: float = 0.08
    max_open_positions:         int   = 10
    paper_trading:              bool  = True
    mock_execution:             bool  = True
    min_zeus_confidence:        float = 0.55
    use_llm_reasoning:          bool  = True
    starting_equity:            float = 100.0  # seed capital — MilestoneManager tracks from here
    hermes_base_url:            str   = "https://hermes-agent-production-114e.up.railway.app"
    default_account_equity:     float = 100_000.0
    stop_loss_pct:              float = 0.03
    take_profit_pct:            float = 0.06


@dataclass
class PipelineRun:
    """Full audit trail for one signal moving through the pipeline."""
    run_id:          str
    started_at:      datetime
    raw_signal:      Optional[RawSignal]      = None
    filtered_signal: Optional[FilteredSignal] = None
    macro_context:   Optional[MacroContext]   = None
    sized_signal:    Optional[SizedSignal]    = None
    trade_result:    Optional[TradeResult]    = None
    trace:           Optional[DecisionTrace]  = None
    killed_at_stage: Optional[str]            = None
    kill_reason:     Optional[str]            = None

    def kill(self, stage: str, reason: str) -> "PipelineRun":
        self.killed_at_stage = stage
        self.kill_reason = reason
        logger.info("[ZEUS] Signal killed at %s — %s", stage, reason)
        return self


class ZeusOrchestrator:
    """
    ZEUS owns the full pipeline.
    Circuit breakers protect every agent call.
    Watchdog runs as a background daemon.
    LLM reasoning runs before final trade approval.
    All decisions are written to the Knowledge Base.
    """

    def __init__(self, config: ZeusConfig | None = None):
        self.config = config or ZeusConfig()
        self.status = PipelineStatus.RUNNING

        # Core infrastructure
        self.kb        = KnowledgeBase()
        self.cb        = CircuitBreaker(failure_threshold=3, window_seconds=300, reset_timeout=120)
        self.watchdog  = Watchdog(alert_fn=self._send_alert)
        self.milestone = MilestoneManager(
            starting_equity=self.config.starting_equity,
            alert_fn=self._send_alert,
        )

        # Agents — ZEUS holds the only references
        self.icarus    = IcarusAgent(
            base_url=self.config.hermes_base_url,
            api_key=os.getenv("HERMES_API_KEY", ""),
        )
        self.hades     = HadesAgent()
        self.artemis   = ArtemisAgent()
        self.pythia    = PythiaAgent(milestone_manager=self.milestone)
        self.ares      = (
            AresMockAgent(
                account_equity=self.config.default_account_equity,
                stop_loss_pct=self.config.stop_loss_pct,
                take_profit_pct=self.config.take_profit_pct,
            )
            if self.config.mock_execution
            else AresAgent(
                paper=self.config.paper_trading,
                host=os.getenv("IB_HOST", "ibgateway"),
                stop_loss_pct=self.config.stop_loss_pct,
                take_profit_pct=self.config.take_profit_pct,
            )
        )
        self.argus     = ArgusAgent(
            max_drawdown_pct=self.config.max_portfolio_drawdown_pct,
            on_kill=self._emergency_halt,
            alert_fn=self._send_alert,
            milestone_manager=self.milestone,
            default_account_equity=self.config.default_account_equity,
            ib_host=os.getenv("IB_HOST", "ibgateway"),
            ib_port=int(os.getenv("IB_PORT", "4002")),
        )
        self.apollo    = ApolloAgent(knowledge_base=self.kb)

        # Shadow learning layer — wire KB into Argus's OutcomeResolver
        self.argus.set_knowledge_base(self.kb)

        # LLM client for reasoning step
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set — LLM reasoning will fail. Check your .env.")
        self._claude = anthropic.Anthropic(api_key=api_key)
        self.bridge  = RedisBridge()   # SpendLens intelligence feed

        # Seniority evaluator — evaluates all agents, gates position sizes
        self.seniority = SeniorityEvaluator(kb=self.kb, alert_fn=self._send_alert)
        self._seniority_report: Optional[SeniorityReport] = None

        self._register_watchdog()
        self.watchdog.start()

        # Evaluate on startup — log current readiness
        self._run_seniority_evaluation()

        logger.info(
            "[ZEUS] Initialised — paper=%s mock=%s llm_reasoning=%s",
            self.config.paper_trading, self.config.mock_execution, self.config.use_llm_reasoning,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_once(self) -> list[PipelineRun]:
        if self.status != PipelineStatus.RUNNING:
            logger.warning("[ZEUS] Pipeline is %s — skipping run.", self.status.value)
            return []

        # Fetch signals: prefer Kafka bus, fall back to direct Icarus call
        from core.kafka_bus import is_available as kafka_up, consume_raw_signals
        if kafka_up():
            raw_signals = consume_raw_signals()
            logger.info("[ZEUS] Kafka: consumed %d signal(s).", len(raw_signals))
            if not raw_signals:
                # Kafka available but empty — also fetch live so we never miss a cycle
                raw_signals = self.cb.call("icarus", fn=self.icarus.fetch, fallback=[])
        else:
            raw_signals = self.cb.call("icarus", fn=self.icarus.fetch, fallback=[])
            logger.info("[ZEUS] Icarus (direct) returned %d signal(s).", len(raw_signals))

        runs: list[PipelineRun] = []
        for sig in raw_signals:
            run = self._process_signal(sig)
            runs.append(run)

        self.cb.call("argus", fn=self.argus.refresh, fallback=None)
        return runs

    def run_research_cycle(self, historical: bool = False) -> dict:
        """Trigger Apollo's daily research cycle — ingest literature, update tickers, self-improve.

        Pass historical=True for the one-shot bootstrap that loads 4 years of
        earnings, Form 4, FRED macro, and EDGAR supply chain data before paper
        trading begins.
        """
        if historical:
            result = self.cb.call(
                "apollo",
                fn=self.apollo.run_historical_ingestion,
                fallback={"error": "circuit open"},
            )
        else:
            result = self.cb.call(
                "apollo",
                fn=self.apollo.run_research_cycle,
                fallback={"error": "circuit open"},
            )
        # Re-evaluate seniority after every research cycle — Apollo may have promoted agents
        self._run_seniority_evaluation()
        return result

    def decide(self, sized: SizedSignal) -> dict:
        """
        Expose the ZEUS LLM reasoning step as a standalone callable.
        Used by ReplayEngine to re-evaluate past DecisionTraces without
        going through the full pipeline.
        Returns {"approved": bool, "reasoning": str}.
        """
        dummy_trace = DecisionTrace(
            trace_id=str(uuid.uuid4()),
            signal_id=None,
            timestamp=datetime.now(timezone.utc),
            headline=sized.original.headline if sized.original else "",
            supplier=sized.original.supplier if sized.original else "",
            category=sized.original.category.value if sized.original else "",
            severity=sized.original.severity.value if sized.original else "",
            hades_passed=True, hades_notes=[],
            trend_suppressed=False, trend_regime=None, trend_vix=sized.macro.vix,
            pattern_confidence=sized.confidence, pattern_size_pct=sized.position_size_pct,
            zeus_reasoning="", zeus_approved=False, zeus_override=False,
            zeus_override_reason=None, trade_placed=False,
        )
        try:
            approved, reasoning, _ = self._zeus_evaluate(sized, sized.macro, dummy_trace)
            return {"approved": approved, "reasoning": reasoning}
        except Exception as exc:
            logger.warning("[ZEUS] decide() failed: %s", exc)
            return {"approved": False, "reasoning": str(exc)}

    def run_backtest(self) -> dict:
        """
        Replay historical KB entries through Hades → Pythia to pre-seed
        context key statistics before paper trading begins.
        Safe to run multiple times — synthetic trades are additive.
        """
        from core.shadow_learning import Backtester
        from core.types import MacroContext, MarketRegime
        from datetime import datetime, timezone

        macro = MacroContext(
            fetched_at=datetime.now(timezone.utc),
            regime=MarketRegime.SIDEWAYS,
            vix=18.0,
            sp500_1m_return=0.0,
        )
        bt = Backtester(
            hades_agent=self.hades,
            pythia_agent=self.pythia,
            macro_context=macro,
        )
        result = bt.run(self.kb)
        logger.info("[ZEUS] Backtest complete: %s", result.summary())
        return result.summary()

    def run_replay(self, limit: int = 30) -> dict:
        """
        Re-run the last N DecisionTraces through current ZEUS reasoning.
        Returns agreement rate and changed-mind cases for review.
        """
        from core.shadow_learning import ReplayEngine

        engine  = ReplayEngine(zeus_agent=self)
        results = engine.replay_recent(self.kb, limit=limit)
        changed = [r for r in results if r.changed_mind()]
        return {
            "replayed":       len(results),
            "agreement_rate": engine.agreement_rate(results),
            "changed_mind":   len(changed),
            "changed_cases":  [
                {
                    "trace_id":          r.trace_id,
                    "was":               "APPROVED" if r.original_approved else "REJECTED",
                    "now":               "APPROVED" if r.replay_approved else "REJECTED",
                    "original_reasoning": r.original_reasoning[:200],
                    "replay_reasoning":  r.replay_reasoning[:200],
                }
                for r in changed
            ],
        }

    def get_seniority_report(self) -> dict:
        """Return current seniority levels for all agents — safe to expose publicly."""
        if self._seniority_report is None:
            self._run_seniority_evaluation()
        report = self._seniority_report
        # Public view: levels only, no criteria details (knowledge base content stays private)
        return {
            "system_level":         report.system_level.label(),
            "live_trading_allowed": report.system_level.live_trading_allowed(),
            "max_position_pct":     report.system_level.max_position_pct(),
            "evaluated_at":         report.evaluated_at.isoformat(),
            "agents": {
                name: {
                    "level":     score.level.label(),
                    "level_int": int(score.level),
                }
                for name, score in report.agents.items()
            },
        }

    def halt(self, reason: str = "manual") -> None:
        self.status = PipelineStatus.HALTED
        try:
            self.ares.cancel_all_pending()
        except Exception as exc:
            logger.warning("[ZEUS] cancel_all_pending failed during halt: %s", exc)
        self._send_alert(f"ZEUS HALTED — {reason}")
        logger.critical("[ZEUS] HALT — %s", reason)

    def resume(self) -> None:
        if self.status == PipelineStatus.SHUTDOWN:
            raise RuntimeError("Cannot resume a shutdown ZEUS instance.")
        self.status = PipelineStatus.RUNNING
        logger.info("[ZEUS] Resumed.")

    def health(self) -> AgentHealth:
        if self.status == PipelineStatus.RUNNING:
            return AgentHealth.HEALTHY
        if self.status == PipelineStatus.HALTED:
            return AgentHealth.FAILED
        return AgentHealth.DEGRADED

    def get_health_reports(self) -> list[HealthReport]:
        return self.watchdog.poll_now()

    def get_milestone_status(self) -> dict:
        return self.milestone.status_dict()

    # ------------------------------------------------------------------
    # Pipeline
    # ------------------------------------------------------------------

    def _process_signal(self, raw: RawSignal) -> PipelineRun:
        run_id = str(uuid.uuid4())[:12]
        run    = PipelineRun(run_id=run_id, started_at=datetime.now(timezone.utc), raw_signal=raw)
        trace  = self._init_trace(run_id, raw)

        # Stage 1 — Hades compliance
        filtered: Optional[FilteredSignal] = self.cb.call(
            "hades",
            fn=lambda: self.hades.filter(raw),
            fallback=None,
        )
        trace.hades_passed = filtered is not None
        if filtered is None:
            trace.hades_notes = ["Hades killed or circuit open"]
            trace.kill_reason = "compliance block or Hades circuit open"
            trace.killed_at_stage = "hades"
            self._write_trace(trace)
            return run.kill("hades", trace.kill_reason)
        trace.hades_notes = filtered.notes
        run.filtered_signal = filtered
        self.bridge.push_supplier_risk(filtered)   # → SpendLens vendor risk

        # Stage 2 — Trend macro context
        macro: MacroContext = self.cb.call(
            "artemis",
            fn=lambda: self.artemis.analyze(filtered),
            fallback=MacroContext(
                fetched_at=datetime.now(timezone.utc),
                regime=MarketRegime.UNKNOWN,
                vix=20.0,
                sp500_1m_return=0.0,
                suppress=False,
            ),
        )
        trace.trend_regime     = macro.regime.value if hasattr(macro.regime, "value") else str(macro.regime)
        trace.trend_vix        = macro.vix
        trace.trend_suppressed = macro.suppress
        if macro.suppress:
            trace.killed_at_stage = "trend"
            trace.kill_reason     = macro.suppress_reason or "macro suppression"
            self._write_trace(trace)
            return run.kill("artemis", trace.kill_reason)
        run.macro_context = macro
        self.bridge.push_macro(macro)              # → SpendLens category strategy

        # Stage 3 — Pattern sizing
        sized: SizedSignal = self.cb.call(
            "pythia",
            fn=lambda: self.pythia.size(filtered, macro),
            fallback=SizedSignal(
                original=filtered, macro=macro,
                confidence=0.5, position_size_pct=0.01,
                skip=False,
            ),
        )
        trace.pattern_confidence = sized.confidence
        trace.pattern_size_pct   = sized.position_size_pct
        if sized.skip:
            trace.killed_at_stage = "pattern"
            trace.kill_reason     = sized.skip_reason or "low confidence"
            self._write_trace(trace)
            return run.kill("pythia", trace.kill_reason)
        run.sized_signal = sized

        # Stage 4 — ZEUS LLM reasoning + KB query (the final judge)
        approved, reasoning, override_size = self._zeus_evaluate(sized, macro, trace)
        trace.zeus_reasoning = reasoning
        trace.zeus_approved  = approved
        if override_size is not None:
            trace.zeus_override        = True
            trace.zeus_override_reason = f"ZEUS resized from {sized.position_size_pct:.3f} to {override_size:.3f}"
            sized.position_size_pct    = override_size

        if not approved:
            trace.killed_at_stage = "zeus"
            trace.kill_reason     = "ZEUS LLM reasoning rejected trade"
            self._write_trace(trace)
            return run.kill("zeus", trace.kill_reason)

        # Stage 5 — Portfolio headroom check
        if self.argus.open_position_count() >= self.config.max_open_positions:
            trace.killed_at_stage = "zeus"
            trace.kill_reason     = "max open positions reached"
            self._write_trace(trace)
            return run.kill("zeus", trace.kill_reason)

        # Stage 5b — Seniority position size ceiling
        if self._seniority_report is not None:
            max_pct = self._seniority_report.system_level.max_position_pct()
            if sized.position_size_pct > max_pct:
                logger.info(
                    "[ZEUS] Position capped by seniority: %.2f%% → %.2f%% (system=%s)",
                    sized.position_size_pct * 100, max_pct * 100,
                    self._seniority_report.system_level.label(),
                )
                sized.position_size_pct = max_pct

        # Stage 6 — Execute
        result: TradeResult = self.cb.call(
            "ares",
            fn=lambda: self.ares.place(sized),
            fallback=TradeResult(
                order_id="failed", symbol="", side="", fill_price=None, qty=0, status="circuit_open"
            ),
        )
        run.trade_result = result

        trace.trade_placed = result.status not in ("circuit_open", "skipped", "error")
        trace.symbol       = result.symbol
        trace.side         = result.side
        trace.fill_price   = result.fill_price

        # Feed outcome back to Pattern + KB
        self.cb.call("pythia", fn=lambda: self.pythia.record_trade(sized, result), fallback=None)
        self._write_trace(trace)
        run.trace = trace

        logger.info(
            "[ZEUS] Trade placed — %s %s @ %s | order_id=%s | confidence=%.2f",
            result.side, result.symbol, result.fill_price, result.order_id, sized.confidence,
        )
        return run

    # ------------------------------------------------------------------
    # ZEUS LLM reasoning — the final judge
    # ------------------------------------------------------------------

    def _zeus_evaluate(
        self,
        sized: SizedSignal,
        macro: MacroContext,
        trace: DecisionTrace,
    ) -> tuple[bool, str, Optional[float]]:
        """
        Query the KB, build the Director prompt, call Claude, parse response.
        Returns (approved, reasoning_text, override_position_size_or_None).
        """
        if not self.config.use_llm_reasoning:
            return sized.confidence >= self.config.min_zeus_confidence, "LLM reasoning disabled.", None

        kb_context   = self._build_kb_context(sized, macro, trace)
        prompt       = self._build_director_prompt(sized, macro, kb_context)

        try:
            response = self._claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=800,
                messages=[{"role": "user", "content": prompt}],
            )
            return self._parse_llm_response(response.content[0].text.strip(), sized)
        except Exception as exc:
            logger.error("[ZEUS] LLM reasoning failed: %s — defaulting to Pattern score.", exc)
            fallback_approved = sized.confidence >= self.config.min_zeus_confidence
            return fallback_approved, f"LLM call failed ({exc}). Used Pattern confidence fallback.", None

    def _build_kb_context(
        self,
        sized: SizedSignal,
        macro: MacroContext,
        trace: DecisionTrace,
    ) -> str:
        """Fetch KB doctrine, precedent, and outcome stats; return formatted context block."""
        kb_query = (
            f"{sized.category.value} signal in {macro.regime} market, "
            f"VIX {macro.vix:.1f}, supplier {sized.supplier}"
        )
        try:
            knowledge_chunks = self.kb.query_knowledge(kb_query, n_results=5)
        except Exception as exc:
            logger.warning("[ZEUS] KB knowledge query failed: %s", exc)
            knowledge_chunks = []
        try:
            past_decisions = self.kb.query_similar_decisions(kb_query, n_results=4)
        except Exception as exc:
            logger.warning("[ZEUS] KB decisions query failed: %s", exc)
            past_decisions = []
        try:
            outcome_stats = self.kb.query_outcomes_by_context(sized.category.value, trace.trend_regime)
        except Exception as exc:
            logger.warning("[ZEUS] KB outcomes query failed: %s", exc)
            outcome_stats = "unavailable"

        if not knowledge_chunks and not past_decisions:
            return "No KB context loaded yet — operating on first principles."
        return "\n\n".join([
            "--- TRADING DOCTRINE (KB) ---",
            *knowledge_chunks,
            "--- PRECEDENT: SIMILAR PAST DECISIONS ---",
            *past_decisions,
            f"--- STATISTICAL OUTCOMES FOR THIS CONTEXT ---\n{outcome_stats}",
        ])

    def _build_director_prompt(
        self,
        sized: SizedSignal,
        macro: MacroContext,
        kb_context: str,
    ) -> str:
        """Assemble the full Director governance prompt from signal + portfolio state."""
        open_positions  = self.argus.open_position_count()
        portfolio_state = self.argus.portfolio_state()
        current_dd      = portfolio_state.current_drawdown_pct
        equity          = portfolio_state.total_equity
        seniority_level = (
            self._seniority_report.system_level.label()
            if self._seniority_report else "Senior"
        )
        return f"""You are ZEUS — Director of Portfolio Management for Pantheon OS, an autonomous trading operation.

Your role is not to rubber-stamp your analysts' recommendations. Your role is to govern the portfolio. You challenge assumptions, identify what your team missed, ensure every investment decision fits the portfolio strategy, and exercise final approval authority with full accountability for outcomes.

Your team has already done their work:
- Hades (Compliance) cleared this signal
- Artemis (Macro Strategist) confirmed market conditions are acceptable
- Pythia (Quant Analyst) sized the position based on historical pattern data

Your job now is Director-level governance — not re-doing their analysis, but stress-testing it.

═══════════════════════════════════════════════
SIGNAL BRIEF (from Icarus)
═══════════════════════════════════════════════
Headline:   {sized.headline}
Supplier:   {sized.supplier}
Category:   {sized.category.value}
Severity:   {sized.severity.value}
Tickers:    {sized.affected_tickers}

═══════════════════════════════════════════════
TEAM ASSESSMENT SUMMARY
═══════════════════════════════════════════════
Compliance (Hades):    score {sized.original.compliance_score:.2f}/1.0 | {'; '.join(sized.original.notes) or 'clean'}
Macro (Artemis):       regime={macro.regime.value} | VIX={macro.vix:.1f} | SPY 1m={macro.sp500_1m_return*100:.1f}%
                       sector momentum: {macro.sector_momentum}
Quant sizing (Pythia): pattern confidence={sized.confidence:.2f} | proposed size={sized.position_size_pct*100:.2f}%

═══════════════════════════════════════════════
PORTFOLIO STATE (from Argus)
═══════════════════════════════════════════════
Current equity:    €{equity:,.2f}
Current drawdown:  {current_dd*100:.2f}%
Open positions:    {open_positions}
System seniority:  {seniority_level}

═══════════════════════════════════════════════
KNOWLEDGE BASE & PRECEDENT
═══════════════════════════════════════════════
{kb_context}

═══════════════════════════════════════════════
YOUR DIRECTOR-LEVEL GOVERNANCE QUESTIONS
═══════════════════════════════════════════════
Before approving, challenge the following:

1. INVESTMENT THESIS: Is the signal-to-trade logic sound? Does this event type historically move this ticker in the expected direction, and within what timeframe?

2. PORTFOLIO FIT: Does this trade fit the current portfolio? Consider concentration, sector exposure already open, and whether current drawdown warrants caution.

3. ASSUMPTION STRESS TEST: What would have to be true for Pythia's confidence estimate to be wrong? Is the sample size sufficient? Is the regime classification reliable right now?

4. ASYMMETRY CHECK: Is the risk/reward favorable? A 3% stop vs 6% target requires a >33% win rate to break even. Does Pythia's data support that?

5. WHAT THE TEAM MISSED: Is there anything in the KB context, sector dynamics, or macro environment that your analysts did not explicitly weight?

Based on this governance review, make your final portfolio decision.

Respond in this exact JSON format — no markdown, no fences, raw JSON only:
{{
  "approved": true or false,
  "confidence": 0.0 to 1.0,
  "position_size_override": null or decimal (e.g. 0.025 for 2.5% — use when Pythia's size needs correction),
  "governance_flags": ["list any concerns even if approving — empty array if none"],
  "reasoning": "3-5 sentences: investment thesis assessment, portfolio fit verdict, key risk identified, final decision rationale"
}}"""

    def _parse_llm_response(
        self,
        raw: str,
        sized: SizedSignal,
    ) -> tuple[bool, str, Optional[float]]:
        """Parse Claude's JSON response into (approved, reasoning, override_size)."""
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise ValueError("No JSON found in Claude response")
        data = json.loads(match.group())

        approved         = bool(data.get("approved", False))
        reasoning        = data.get("reasoning", "")
        governance_flags = data.get("governance_flags", [])
        override         = data.get("position_size_override")
        override_size    = float(override) if override is not None else None
        llm_confidence   = data.get("confidence", 1.0)

        if governance_flags:
            reasoning += " | FLAGS: " + "; ".join(governance_flags)

        if llm_confidence < self.config.min_zeus_confidence:
            approved   = False
            reasoning += f" [REJECTED: Director confidence {llm_confidence:.2f} below floor {self.config.min_zeus_confidence:.2f}]"

        if len(reasoning) < 80:
            logger.warning("[ZEUS] Director reasoning suspiciously short (%d chars) — treating as low confidence", len(reasoning))
            approved   = False
            reasoning += " [REJECTED: insufficient reasoning quality]"

        logger.info(
            "[ZEUS] Director decision — approved=%s confidence=%.2f override_size=%s flags=%s",
            approved, llm_confidence, override_size, governance_flags,
        )
        return approved, reasoning, override_size

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _init_trace(self, run_id: str, raw: RawSignal) -> DecisionTrace:
        return DecisionTrace(
            trace_id    = run_id,
            signal_id   = raw.signal_id,
            timestamp   = datetime.now(timezone.utc),
            headline    = raw.headline,
            supplier    = raw.supplier,
            category    = raw.category.value,
            severity    = raw.severity.name,
            hades_passed = False,
        )

    def _write_trace(self, trace: DecisionTrace) -> None:
        try:
            self.kb.store_decision(trace)
        except Exception as exc:
            logger.warning("[ZEUS] Failed to write trace to KB: %s", exc)
        self.bridge.push_decision(trace)           # → SpendLens Icarus AI feed
        from core.kafka_bus import publish_decision_trace
        publish_decision_trace(trace)              # → Kafka zeus.decision_traces (no-op if down)

    def _run_seniority_evaluation(self) -> None:
        try:
            self._seniority_report = self.seniority.evaluate()
            logger.info("[ZEUS] Seniority: %s", self._seniority_report.summary_line())
            if os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY"):
                import core.supabase_client as supa
                supa.upsert_agent_seniority(
                    scores={k: v.to_dict() for k, v in self._seniority_report.agents.items()},
                    system_level_int=int(self._seniority_report.system_level),
                )
        except Exception as exc:
            logger.warning("[ZEUS] Seniority evaluation failed: %s", exc)

    def _emergency_halt(self, reason: str) -> None:
        self.halt(reason=f"drawdown kill — {reason}")

    def _send_alert(self, message: str) -> None:
        try:
            self.argus.send_alert(message)
        except Exception:
            logger.warning("[ZEUS] Alert delivery failed: %s", message)

    def _register_watchdog(self) -> None:
        self.watchdog.register("zeus",    self.health)
        self.watchdog.register("icarus",  self.icarus.health)
        self.watchdog.register("hades",   self.hades.health)
        self.watchdog.register("artemis", self.artemis.health)
        self.watchdog.register("pythia",  self.pythia.health)
        self.watchdog.register("ares",    self.ares.health)
        self.watchdog.register("argus",   self.argus.health)
        self.watchdog.register("apollo",  self.apollo.health)
