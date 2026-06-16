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
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import anthropic

from agents.apollo import ApolloAgent
from agents.ares import AresAgent
from agents.ares_mock import AresMockAgent
from agents.argus import ArgusAgent
from agents.artemis import ArtemisAgent
from agents.hades import HadesAgent
from agents.icarus import IcarusAgent
from agents.pythia import PythiaAgent
from core.circuit_breaker import CircuitBreaker
from core.knowledge_base import KnowledgeBase
from core.milestone_manager import MilestoneManager
from core.redis_bridge import RedisBridge
from core.seniority import SeniorityEvaluator, SeniorityReport
from core.types import (
    AgentHealth,
    DecisionTrace,
    FilteredSignal,
    HealthReport,
    MacroContext,
    MarketRegime,
    PipelineStatus,
    RawSignal,
    SizedSignal,
    TradeResult,
)
from core.watchdog import Watchdog

logger = logging.getLogger("zeus")


@dataclass
class ZeusConfig:
    max_portfolio_drawdown_pct:   float = 0.08
    max_open_positions:           int   = 3    # settings is authoritative — see build_zeus()
    max_open_positions_per_ticker: int  = 1    # never hold > 1 position per ticker
    max_deployed_pct:             float = 0.90 # cap: never deploy >90% of equity (keep cash buffer)
    ticker_cooldown_hours:        float = 48.0 # hours before re-trading same ticker
    paper_trading:                bool  = True
    mock_execution:               bool  = True
    min_zeus_confidence:          float = 0.55
    use_llm_reasoning:            bool  = True
    use_debate:                   bool  = True   # bull/bear debate before Director verdict
    debate_rounds:                int   = 1      # bull→bear pairs per signal
    debate_max_tokens:            int   = 400    # output cap per debate call
    # Single source of truth for "how much money we have". Feeds BOTH the
    # milestone stage/tier gate AND € position sizing, so the two can never
    # reason about different balances. None = fail closed (raise), never guess.
    # (Was two fields — starting_equity + default_account_equity — that could
    #  drift and silently misclassify the stage; consolidated to one.)
    default_account_equity:       Optional[float] = None
    stop_loss_pct:                float = 0.03
    take_profit_pct:              float = 0.06
    ib_paper_port:                int   = 4002
    ib_live_port:                 int   = 4001
    # Single source of truth for the Anthropic model IDs (was hardcoded in 4
    # call sites). claude-sonnet-4-6 is the current Sonnet. Overridable via
    # settings.json so an account-specific / upgraded ID changes in one place.
    anthropic_model_director:     str   = "claude-sonnet-4-6"
    anthropic_model_debate:       str   = "claude-sonnet-4-6"


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

    def kill(self, stage: str, reason: str, trace: "DecisionTrace | None" = None) -> "PipelineRun":
        self.killed_at_stage = stage
        self.kill_reason = reason
        if trace is not None:
            self.trace = trace
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
        # Settings is authoritative for these knobs — override ZeusConfig defaults
        # so callers don't need to explicitly pass them through ZeusConfig.
        from config.settings import load_settings
        _s = load_settings()
        if self.config.max_open_positions == 3 and "max_open_positions" in _s:
            self.config.max_open_positions = int(_s["max_open_positions"])
        if self.config.max_open_positions_per_ticker == 1:
            self.config.max_open_positions_per_ticker = int(_s.get("max_open_positions_per_ticker", 1))
        if self.config.max_deployed_pct == 0.90:
            self.config.max_deployed_pct = float(_s.get("max_deployed_pct", 0.90))
        if self.config.ticker_cooldown_hours == 48.0:
            self.config.ticker_cooldown_hours = float(_s.get("ticker_cooldown_hours", 48.0))
        if self.config.use_debate is True and "use_debate" in _s:
            self.config.use_debate = bool(_s["use_debate"])
        if self.config.debate_rounds == 1:
            self.config.debate_rounds = int(_s.get("debate_rounds", 1))
        if self.config.debate_max_tokens == 400:
            self.config.debate_max_tokens = int(_s.get("debate_max_tokens", 400))
        if self.config.anthropic_model_director == "claude-sonnet-4-6":
            self.config.anthropic_model_director = str(_s.get("anthropic_model_director", "claude-sonnet-4-6"))
        if self.config.anthropic_model_debate == "claude-sonnet-4-6":
            self.config.anthropic_model_debate = str(_s.get("anthropic_model_debate", "claude-sonnet-4-6"))

        # Account equity — single source of truth for stage gate AND € sizing.
        # Prefer explicit config; else settings.json. FAIL CLOSED if neither
        # provides it: a money-sizing system must never guess a balance.
        if self.config.default_account_equity is None:
            _eq = _s.get("account_equity", _s.get("default_account_equity"))
            self.config.default_account_equity = float(_eq) if _eq is not None else None
        if self.config.default_account_equity is None or self.config.default_account_equity <= 0:
            raise ValueError(
                "account equity is not configured — set 'default_account_equity' "
                "(or 'account_equity') in settings.json or pass it to ZeusConfig. "
                "Refusing to start with an unknown balance (fail closed)."
            )

        self.status = PipelineStatus.RUNNING

        # Core infrastructure
        self.kb        = KnowledgeBase()
        self.cb        = CircuitBreaker(failure_threshold=3, window_seconds=300, reset_timeout=120)
        self.watchdog  = Watchdog(alert_fn=self._send_alert)
        self.milestone = MilestoneManager(
            account_equity=self.config.default_account_equity,
            alert_fn=self._send_alert,
        )

        # Agents — ZEUS holds the only references
        # Apollo first so its get_ticker resolver can be injected into Icarus
        self.apollo    = ApolloAgent(knowledge_base=self.kb)
        self.icarus    = IcarusAgent(
            ticker_resolver=self.apollo.get_ticker,
        )
        self.hades     = HadesAgent()
        self.artemis   = ArtemisAgent()
        self.pythia    = PythiaAgent(milestone_manager=self.milestone)

        # ── Seniority evaluator (built early — its verdict gates real money) ──
        self.seniority = SeniorityEvaluator(kb=self.kb, alert_fn=self._send_alert)
        self._seniority_report: Optional[SeniorityReport] = None

        # ── HARD real-money gate ──────────────────────────────────────────────
        # Settings can REQUEST live trading (paper_trading=False), but the team
        # only earns it by reaching the Senior tier AND an operator arming it.
        # If either is missing, force paper mode here — before Ares/Argus are
        # built — so no real order can ever be placed prematurely. This is the
        # teeth behind "no real money until they're senior."
        _gate_forced_paper = False
        if not self.config.paper_trading:
            from core.seniority import real_money_live
            startup_report = self.seniority.evaluate()
            if not real_money_live(startup_report.system_tier):
                logger.warning(
                    "[ZEUS] Real money REQUESTED but BLOCKED — %s armed=%s. "
                    "Forcing PAPER mode. (Reach Senior + set ARM_REAL_MONEY=true to enable.)",
                    startup_report.summary_line().split(" | ")[0],
                    os.getenv("ARM_REAL_MONEY", "false"),
                )
                self.config.paper_trading = True
                self.config.mock_execution = True
                _gate_forced_paper = True
            else:
                logger.warning("[ZEUS] Real money LIVE — system is Senior and armed. Real capital at risk.")

        # Resolve IB port once: env > settings.json > ZeusConfig default (4002/4001).
        # SAFETY: when the gate forced paper mode, IGNORE any IB_PORT override —
        # an explicit IB_PORT=4001 must never route a blocked system to the live
        # broker port. The paper port is non-negotiable in that state.
        _ib_port_default = self.config.ib_paper_port if self.config.paper_trading else self.config.ib_live_port
        if _gate_forced_paper:
            _ib_port = self.config.ib_paper_port
        else:
            _ib_port = int(os.getenv("IB_PORT", str(_ib_port_default)))
            # Belt-and-suspenders: in paper mode the resolved port can never be live.
            if self.config.paper_trading and _ib_port == self.config.ib_live_port:
                logger.warning("[ZEUS] IB_PORT=%d is the LIVE port but system is paper — overriding to paper port %d",
                               _ib_port, self.config.ib_paper_port)
                _ib_port = self.config.ib_paper_port

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
                port=_ib_port,
                stop_loss_pct=self.config.stop_loss_pct,
                take_profit_pct=self.config.take_profit_pct,
            )
        )
        self.argus     = ArgusAgent(
            max_drawdown_pct=self.config.max_portfolio_drawdown_pct,
            on_kill=self._emergency_halt,
            milestone_manager=self.milestone,
            default_account_equity=self.config.default_account_equity,
            ib_host=os.getenv("IB_HOST", "ibgateway"),
            ib_port=_ib_port,
            mock=self.config.mock_execution,  # mirror mock_execution — no IB in mock mode
        )

        # Shadow learning layer — wire KB into Argus's OutcomeResolver
        self.argus.set_knowledge_base(self.kb)

        # LLM client for reasoning step
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set — LLM reasoning will fail. Check your .env.")
        self._claude = anthropic.Anthropic(api_key=api_key)
        self._verify_model_or_die()
        self.bridge  = RedisBridge()   # SpendLens intelligence feed

        self._register_watchdog()
        self.watchdog.start()

        # Evaluate on startup — log current readiness
        self._run_seniority_evaluation()

        # Intra-run state — reset each run_once() cycle
        self._run_approved_trades: list[dict] = []  # tickers/sides approved this cycle

        logger.info(
            "[ZEUS] Initialised — paper=%s mock=%s llm_reasoning=%s",
            self.config.paper_trading, self.config.mock_execution, self.config.use_llm_reasoning,
        )

    def _verify_model_or_die(self) -> None:
        """Boot-time dry-run of the director model. A bad/unauthorized model ID
        must be a LOUD failure here — otherwise every LLM call throws at runtime
        and ZEUS silently falls back to Pattern-only scoring, masking the cause.
        Skipped when LLM reasoning is off (mock mode) or ZEUS_SKIP_MODEL_CHECK=1
        (offline/tests)."""
        if not self.config.use_llm_reasoning:
            return
        if os.getenv("ZEUS_SKIP_MODEL_CHECK") == "1":
            logger.info("[ZEUS] Model dry-run skipped (ZEUS_SKIP_MODEL_CHECK=1)")
            return
        model = self.config.anthropic_model_director
        try:
            self._claude.messages.create(
                model=model, max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
            logger.info("[ZEUS] Model dry-run OK — %s reachable", model)
        except Exception as exc:
            logger.critical(
                "[ZEUS] Model dry-run FAILED for '%s': %s. Refusing to start — "
                "fix anthropic_model_director in settings.json / the API key.", model, exc)
            raise RuntimeError(f"Anthropic model '{model}' unusable: {exc}") from exc

    def _budget_equity(self) -> float:
        """Equity the budget cap is computed against. Prefer LIVE equity from
        Argus so the cap tracks gains/drawdown; fall back to the configured
        starting balance when Argus hasn't refreshed yet (0.0 / None / error),
        logging a WARN so the fallback is visible."""
        try:
            live = self.argus.portfolio_state().total_equity
        except Exception as exc:
            logger.warning("[ZEUS] could not read live equity (%s) — using config equity", exc)
            live = None
        if live and live > 0:
            return float(live)
        logger.warning("[ZEUS] live equity unavailable — budget cap on config €%.0f",
                       self.config.default_account_equity)
        return float(self.config.default_account_equity)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_once(self) -> list[PipelineRun]:
        if self.status != PipelineStatus.RUNNING:
            logger.warning("[ZEUS] Pipeline is %s — skipping run.", self.status.value)
            return []

        # Fetch signals directly from Icarus (Supabase)
        raw_signals = self.cb.call("icarus", fn=self.icarus.fetch, fallback=[])
        logger.info("[ZEUS] Icarus returned %d signal(s).", len(raw_signals))

        # Reset intra-run state so each cycle starts fresh
        self._run_approved_trades = []

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
        from datetime import datetime, timezone

        from core.shadow_learning import Backtester
        from core.types import MacroContext, MarketRegime

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
        # Public view: ranks only, no criteria details (knowledge base content stays private)
        return {
            "system_level":         report.system_tier.label(),
            "system_rank":          f"{report.system_tier.label()} L{report.system_level}",
            "system_tier_int":      int(report.system_tier),
            "real_money_unlocked":  report.real_money_unlocked,
            "real_money_armed":     report.armed,
            "live_trading_allowed": report.live_trading_allowed,
            "max_position_pct":     report.max_position_pct,
            "evaluated_at":         report.evaluated_at.isoformat(),
            "agents": {
                name: {
                    "tier":      score.tier.label(),
                    "level":     score.level,
                    "rank":      score.rank_label(),
                    "wins":      score.wins,
                    "level_int": int(score.tier),   # back-compat: tier ordinal
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
        run_id = str(uuid.uuid4())
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
            return run.kill("hades", trace.kill_reason, trace)
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
            return run.kill("artemis", trace.kill_reason, trace)
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
            return run.kill("pythia", trace.kill_reason, trace)
        run.sized_signal = sized

        # Stage 3b — Concentration control
        # Reject before LLM to avoid burning tokens on a structurally-blocked ticker.
        conc_kill = self._check_concentration(sized)
        if conc_kill:
            trace.killed_at_stage = "concentration"
            trace.kill_reason     = conc_kill
            self._write_trace(trace)
            return run.kill("concentration", conc_kill, trace)

        # Stage 4 — Pre-decision enrichment: Apollo researches the company,
        # giving Zeus real fundamentals + recent news before the LLM decides.
        ticker = sized.affected_tickers[0] if sized.affected_tickers else ""
        live_context = ""
        ticker_history: list[dict] = []
        if ticker:
            try:
                live_context = self.apollo.enrich_signal(ticker, sized.supplier)
            except Exception as exc:
                logger.warning("[ZEUS] Signal enrichment failed for %s: %s", ticker, exc)
            try:
                ticker_history = self.kb.query_ticker_history(ticker, n_results=5)
            except Exception as exc:
                logger.warning("[ZEUS] Ticker history query failed for %s: %s", ticker, exc)

        # Stage 4 — ZEUS LLM reasoning + KB query (the final judge)
        approved, reasoning, override_size = self._zeus_evaluate(
            sized, macro, trace,
            live_context=live_context,
            intra_run_trades=list(self._run_approved_trades),
            ticker_history=ticker_history,
        )
        trace.zeus_reasoning = reasoning
        trace.zeus_approved  = approved
        if override_size is not None:
            trace.zeus_override        = True
            trace.zeus_override_reason = f"ZEUS resized from {sized.position_size_pct:.3f} to {override_size:.3f}"
            sized.position_size_pct    = override_size

        # Teach Icarus — feedback loop so it learns which patterns get approved
        self.icarus.record_signal_outcome(raw, approved)

        if not approved:
            trace.killed_at_stage = "zeus"
            trace.kill_reason     = "ZEUS LLM reasoning rejected trade"
            self._write_trace(trace)
            return run.kill("zeus", trace.kill_reason, trace)

        # Stage 5 — Portfolio headroom check
        if self.argus.open_position_count() >= self.config.max_open_positions:
            trace.killed_at_stage = "zeus"
            trace.kill_reason     = "max open positions reached"
            self._write_trace(trace)
            return run.kill("zeus", trace.kill_reason, trace)

        # Stage 5a — Budget gate: ZEUS must not approve trades it can't fund.
        # The goal is to GROW the account, not over-commit it. Check the real
        # wallet (DB-based committed capital vs equity) before approving — this
        # also fails SAFE when IB is down (committed_capital reads the DB, not a
        # live position count that silently returns 0 when the broker is gone).
        equity        = self._budget_equity()
        committed     = self.argus.committed_capital()
        trade_cost    = equity * sized.position_size_pct
        max_deployed  = equity * self.config.max_deployed_pct
        if committed + trade_cost > max_deployed:
            trace.killed_at_stage = "zeus"
            trace.kill_reason = (
                f"budget: would deploy €{committed + trade_cost:,.0f} of €{equity:,.0f} "
                f"(cap €{max_deployed:,.0f} @ {self.config.max_deployed_pct:.0%}); "
                f"€{self.argus.available_cash():,.0f} cash free"
            )
            logger.info("[ZEUS] Trade rejected on budget — %s", trace.kill_reason)
            self._write_trace(trace)
            return run.kill("zeus", trace.kill_reason, trace)

        # Stage 5b — Seniority position size ceiling
        if self._seniority_report is not None:
            max_pct = self._seniority_report.max_position_pct
            if sized.position_size_pct > max_pct:
                logger.info(
                    "[ZEUS] Position capped by seniority: %.2f%% → %.2f%% (system=%s)",
                    sized.position_size_pct * 100, max_pct * 100,
                    self._seniority_report.summary_line().split(" | ")[0],
                )
                sized.position_size_pct = max_pct

        # Stage 6 — Execute (with pre-flight health gate and pending-order drain)
        ares_healthy = self.ares.health() == AgentHealth.HEALTHY

        if ares_healthy:
            # Drain any previously queued approvals before placing the new one
            try:
                drained = self.ares.reconcile_pending()
                if drained:
                    logger.info("[ZEUS] Reconciled %d pending order(s) before new placement", drained)
            except Exception as _rec_exc:
                logger.warning("[ZEUS] reconcile_pending failed (non-fatal): %s", _rec_exc)

        if ares_healthy:
            result: TradeResult = self.cb.call(
                "ares",
                fn=lambda: self.ares.place(sized),
                fallback=TradeResult(
                    order_id="failed", symbol="", side="", fill_price=None, qty=0, status="circuit_open"
                ),
            )
        else:
            # IB is down — enqueue instead of attempting the doomed 3-retry storm
            logger.info("[ZEUS] IB pre-flight DEGRADED — queuing approval for signal %s", sized.signal_id)
            result = self.ares.place(sized)  # place() will hit _enqueue() via connectivity check
        run.trade_result = result

        trace.trade_placed = result.status not in ("circuit_open", "skipped", "error")
        trace.symbol       = result.symbol
        trace.side         = result.side
        trace.fill_price   = result.fill_price

        # Record this approval so subsequent signals in this cycle know about it
        if trace.trade_placed:
            self._run_approved_trades.append({
                "ticker": result.symbol,
                "side":   result.side,
                "sector": sized.category.value,
                "supplier": sized.supplier,
            })
            # In mock mode: register position with Argus immediately (no IB poll needed)
            if self.config.mock_execution and result.fill_price and result.qty:
                pos_side = "LONG" if result.side == "BUY" else "SHORT"
                self.argus.add_mock_position(
                    symbol=result.symbol, side=pos_side,
                    qty=result.qty, avg_cost=result.fill_price,
                    current_price=result.fill_price,
                    stop_loss_price=result.stop_loss_price,
                    take_profit_price=result.take_profit_price,
                )

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
        live_context: str = "",
        intra_run_trades: Optional[list] = None,
        ticker_history: Optional[list] = None,
    ) -> tuple[bool, str, Optional[float]]:
        """
        Query the KB, build the Director prompt, call Claude, parse response.
        Returns (approved, reasoning_text, override_position_size_or_None).
        """
        if not self.config.use_llm_reasoning:
            return sized.confidence >= self.config.min_zeus_confidence, "LLM reasoning disabled.", None

        kb_context = self._build_kb_context(sized, macro, trace)
        self_critique = self._load_self_critique()

        # Adversarial debate — bull builds the case, bear rebuts it (seeing the
        # bull's argument). ZEUS reads both before adjudicating. Returns ("","")
        # if debate is disabled or a call fails — ZEUS then reasons single-shot.
        bull_case, bear_case = self._run_debate(sized, macro, kb_context, live_context)

        prompt = self._build_director_prompt(
            sized, macro, kb_context,
            live_context=live_context,
            intra_run_trades=intra_run_trades or [],
            self_critique=self_critique,
            ticker_history=ticker_history or [],
            bull_case=bull_case,
            bear_case=bear_case,
        )

        try:
            response = self._claude.messages.create(
                model=self.config.anthropic_model_director,
                max_tokens=1500,
                messages=[{"role": "user", "content": prompt}],
            )
            self._record_token_usage(response.usage, sized.affected_tickers[0] if sized.affected_tickers else "unknown")
            approved, reasoning, override_size = self._parse_llm_response(
                response.content[0].text.strip(), sized
            )
            # Fold the debate into the auditable reasoning text (no schema change).
            if bull_case or bear_case:
                reasoning += (
                    f" | DEBATE — BULL: {bull_case[:300]}"
                    f" || BEAR: {bear_case[:300]}"
                )
            return approved, reasoning, override_size
        except Exception as exc:
            logger.error("[ZEUS] LLM reasoning failed: %s — defaulting to Pattern score.", exc)
            fallback_approved = sized.confidence >= self.config.min_zeus_confidence
            return fallback_approved, f"LLM call failed ({exc}). Used Pattern confidence fallback.", None

    def _run_debate(
        self,
        sized: SizedSignal,
        macro: MacroContext,
        kb_context: str,
        live_context: str = "",
    ) -> tuple[str, str]:
        """
        Run a one-round adversarial debate before the Director verdict.

        Bull argues the strongest case to TAKE the trade. Bear then rebuts it,
        seeing the bull's argument (sequential, so it's genuinely adversarial
        rather than two independent opinions).

        Strictly additive: returns ("", "") if debate is disabled or any call
        fails — ZEUS then falls back to single-shot reasoning, exactly as before.
        The debate produces evidence; ZEUS remains the sole judge.
        """
        if not self.config.use_debate:
            return "", ""

        ticker = sized.affected_tickers[0] if sized.affected_tickers else "unknown"

        bull_case = ""
        bear_case = ""
        try:
            bull_prompt = self._build_debate_prompt(
                "BULL", sized, macro, kb_context, live_context, opponent_case=""
            )
            bull_resp = self._claude.messages.create(
                model=self.config.anthropic_model_debate,
                max_tokens=self.config.debate_max_tokens,
                messages=[{"role": "user", "content": bull_prompt}],
            )
            self._record_token_usage(bull_resp.usage, f"{ticker}:bull")
            bull_case = bull_resp.content[0].text.strip()
        except Exception as exc:
            logger.warning("[ZEUS] Bull debate call failed: %s — proceeding without it.", exc)
            return "", ""

        try:
            bear_prompt = self._build_debate_prompt(
                "BEAR", sized, macro, kb_context, live_context, opponent_case=bull_case
            )
            bear_resp = self._claude.messages.create(
                model=self.config.anthropic_model_debate,
                max_tokens=self.config.debate_max_tokens,
                messages=[{"role": "user", "content": bear_prompt}],
            )
            self._record_token_usage(bear_resp.usage, f"{ticker}:bear")
            bear_case = bear_resp.content[0].text.strip()
        except Exception as exc:
            logger.warning("[ZEUS] Bear debate call failed: %s — using bull case only.", exc)

        logger.info(
            "[ZEUS] Debate complete for %s — bull=%d chars, bear=%d chars",
            ticker, len(bull_case), len(bear_case),
        )
        return bull_case, bear_case

    def _build_debate_prompt(
        self,
        side: str,
        sized: SizedSignal,
        macro: MacroContext,
        kb_context: str,
        live_context: str,
        opponent_case: str,
    ) -> str:
        """Build a bull- or bear-side debate prompt. `side` is 'BULL' or 'BEAR'."""
        live_block = f"\nLIVE FUNDAMENTALS (Apollo):\n{live_context}\n" if live_context else ""

        if side == "BULL":
            role = (
                "You are the BULL analyst. Build the strongest evidence-based case to TAKE "
                "this long trade. Ground every claim in the signal, the macro regime, the live "
                "fundamentals, and KB precedent below. Do not invent numbers. End by naming the "
                "single biggest risk to your own thesis — the honest bull names the bear's best shot."
            )
            opponent_block = ""
        else:
            role = (
                "You are the BEAR analyst. The bull's case is below. Rebut it. Argue the strongest "
                "evidence-based case to REJECT or skip this trade: why the event may not move this "
                "ticker as claimed, why the regime or sizing could be wrong, what failure modes the "
                "bull ignored. Ground every claim in the evidence. Do not invent numbers."
            )
            opponent_block = f"\n--- BULL'S CASE (rebut this) ---\n{opponent_case}\n"

        return f"""{role}

═══════════════════════════════════════════════
SIGNAL
═══════════════════════════════════════════════
Headline:  {sized.headline}
Supplier:  {sized.supplier}
Category:  {sized.category.value}
Severity:  {sized.severity.value}
Tickers:   {sized.affected_tickers}

MACRO: regime={macro.regime.value} | VIX={macro.vix:.1f} | SPY 1m={macro.sp500_1m_return*100:.1f}%
QUANT: Pythia confidence={sized.confidence:.2f}, proposed size={sized.position_size_pct*100:.2f}%
{live_block}
═══════════════════════════════════════════════
KNOWLEDGE BASE & PRECEDENT
═══════════════════════════════════════════════
{kb_context}
{opponent_block}
Write 3-5 tight sentences. No preamble, no JSON — just your argument."""

    def _record_token_usage(self, usage, symbol: str) -> None:
        # Sonnet 4.6 pricing: $3/MTok input, $15/MTok output
        input_tok  = getattr(usage, "input_tokens", 0) or 0
        output_tok = getattr(usage, "output_tokens", 0) or 0
        cost_usd   = round((input_tok * 3 + output_tok * 15) / 1_000_000, 6)
        logger.info("[ZEUS] LLM tokens — in=%d out=%d cost=$%.4f symbol=%s",
                    input_tok, output_tok, cost_usd, symbol)
        try:
            from datetime import datetime, timezone

            import core.supabase_client as supa
            supa.get_client().table("llm_usage").insert({
                "model":       self.config.anthropic_model_director,
                "symbol":      symbol,
                "input_tokens":  input_tok,
                "output_tokens": output_tok,
                "cost_usd":    cost_usd,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
        except Exception as exc:
            logger.debug("[ZEUS] llm_usage insert skipped: %s", exc)

    def _load_self_critique(self) -> str:
        """Read Zeus's own skill file — accumulated self-critique from Apollo's improvement loop."""
        try:
            skills_path = Path("knowledge/agents/zeus_skills.md")
            if skills_path.exists():
                content = skills_path.read_text(encoding="utf-8").strip()
                if content:
                    return content
        except Exception:
            pass
        return ""

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
        live_context: str = "",
        intra_run_trades: Optional[list] = None,
        self_critique: str = "",
        ticker_history: Optional[list] = None,
        bull_case: str = "",
        bear_case: str = "",
    ) -> str:
        """Assemble the full Director governance prompt from signal + portfolio state."""
        open_positions  = self.argus.open_position_count()
        portfolio_state = self.argus.portfolio_state()
        current_dd      = portfolio_state.current_drawdown_pct
        equity          = portfolio_state.total_equity
        seniority_level = (
            f"{self._seniority_report.system_tier.label()} L{self._seniority_report.system_level}"
            if self._seniority_report else "Trainee L1"
        )

        # Format trades already approved this cycle
        if intra_run_trades:
            trades_this_cycle = "\n".join(
                f"  • {t['ticker']} {t['side']} ({t['sector']}, supplier: {t['supplier']})"
                for t in intra_run_trades
            )
        else:
            trades_this_cycle = "  None yet this cycle."

        # Self-critique section — Zeus reads his own accumulated biases
        self_critique_section = ""
        if self_critique:
            self_critique_section = f"""
═══════════════════════════════════════════════
YOUR KNOWN BIASES & SELF-CRITIQUE (from Apollo's analysis of your past decisions)
═══════════════════════════════════════════════
{self_critique}

Apply this self-knowledge. If you are about to make a decision that matches a known failure pattern, flag it explicitly.
"""

        # Ticker decision history — compact fingerprints of past approvals
        ticker_history_section = ""
        if ticker_history:
            lines = []
            for h in ticker_history:
                vix_val = h.get("vix", 0)
                vix_str = f"{float(vix_val):.1f}" if vix_val else "?"
                lines.append(
                    f"  [{h.get('timestamp','')}] regime={h.get('regime','?')} VIX={vix_str} "
                    f"→ outcome={h.get('outcome','?')} | why: {h.get('why','?')}"
                )
            ticker_history_section = f"""
═══════════════════════════════════════════════
YOUR DECISION HISTORY FOR THIS TICKER
═══════════════════════════════════════════════
{chr(10).join(lines)}

Key question: Are the circumstances now the same as when you last acted on this ticker?
Compare regime, VIX, and signal type. If outcome was a loss, has anything changed that
invalidates the previous thesis? If position is still open, is this a double-down?
"""

        # Live company intelligence from Apollo
        live_context_section = ""
        if live_context:
            live_context_section = f"""
═══════════════════════════════════════════════
LIVE COMPANY INTELLIGENCE (fetched by Apollo right now)
═══════════════════════════════════════════════
{live_context}
"""

        # Adversarial debate — bull built the case, bear rebutted it
        debate_section = ""
        if bull_case or bear_case:
            debate_section = f"""
═══════════════════════════════════════════════
ANALYST DEBATE (your team argued both sides before you decide)
═══════════════════════════════════════════════
BULL — case to take the trade:
{bull_case or '(no bull case produced)'}

BEAR — case to reject the trade:
{bear_case or '(no bear case produced)'}

Weigh both. Do not simply side with whoever wrote more. The bear's strongest
point is what you must answer before approving.
"""

        return f"""You are ZEUS — Director of Portfolio Management for Pantheon OS, an autonomous trading operation.

Your role is not to rubber-stamp your analysts' recommendations. Your role is to govern the portfolio with genuine intelligence. You have memory. You know what you approved 5 minutes ago. You know your own failure patterns. You use all of that.

Your team has done their work:
- Hades (Compliance) cleared this signal
- Artemis (Macro Strategist) confirmed market conditions
- Pythia (Quant Analyst) sized the position from historical pattern data
- Apollo (Research) fetched live company fundamentals right now

Your job is Director-level governance — stress-testing the team's work, applying your self-knowledge, and making a decision you will be held accountable for.
{self_critique_section}
═══════════════════════════════════════════════
SIGNAL BRIEF (from Icarus)
═══════════════════════════════════════════════
Headline:   {sized.headline}
Supplier:   {sized.supplier}
Category:   {sized.category.value}
Severity:   {sized.severity.value}
Tickers:    {sized.affected_tickers}
{live_context_section}{debate_section}
═══════════════════════════════════════════════
TEAM ASSESSMENT SUMMARY
═══════════════════════════════════════════════
Compliance (Hades):    score {sized.original.compliance_score:.2f}/1.0 | {'; '.join(sized.original.notes) or 'clean'}
Macro (Artemis):       regime={macro.regime.value} | VIX={macro.vix:.1f} | SPY 1m={macro.sp500_1m_return*100:.1f}%
                       sector momentum: {macro.sector_momentum}
Quant sizing (Pythia): pattern confidence={sized.confidence:.2f} | proposed size={sized.position_size_pct*100:.2f}%

═══════════════════════════════════════════════
PORTFOLIO STATE & THIS CYCLE'S DECISIONS (from Argus)
═══════════════════════════════════════════════
Current equity:      €{equity:,.2f}
Current drawdown:    {current_dd*100:.2f}%
Open positions:      {open_positions}
System seniority:    {seniority_level}

Already approved this cycle:
{trades_this_cycle}
{ticker_history_section}
═══════════════════════════════════════════════
KNOWLEDGE BASE & PRECEDENT
═══════════════════════════════════════════════
{kb_context}

═══════════════════════════════════════════════
YOUR DIRECTOR-LEVEL GOVERNANCE QUESTIONS
═══════════════════════════════════════════════
Before deciding, you must address:

1. INVESTMENT THESIS: Does this signal-to-trade logic hold? Does this event type move this specific ticker in the expected direction, in what timeframe, and is that supported by the live fundamentals Apollo just fetched?

2. PORTFOLIO FIT: You can see what you already approved this cycle. Does adding this position increase concentration risk? Are you building a correlated book without realising it?

3. SELF-AWARENESS: Does this decision match any of your known failure patterns from your self-critique above? If so, is the evidence strong enough to override your bias?

4. ASSUMPTION STRESS TEST: What would have to be true for Pythia's confidence to be wrong? Is the regime classification reliable right now? Is the sample size sufficient?

5. ASYMMETRY: Is the risk/reward favorable? A 3% stop vs 6% target requires >33% win rate. Does the data support that for this specific ticker and signal type?

6. DEBATE RESOLUTION: Your bull and bear analysts argued above. State the bear's strongest single objection, then decide whether it is disqualifying or already priced in. Do not approve while leaving the bear's best point unanswered.

Based on this, make your final decision.

Respond in this exact JSON format — no markdown, no fences, raw JSON only:
{{
  "approved": true or false,
  "confidence": 0.0 to 1.0,
  "position_size_override": null or decimal (e.g. 0.025 for 2.5% — only when Pythia's size needs correction),
  "governance_flags": ["list any concerns even if approving — empty array if none"],
  "reasoning": "3-5 sentences covering: thesis assessment using live fundamentals, portfolio fit given this cycle's trades, self-awareness check, final rationale"
}}"""

    def _parse_llm_response(
        self,
        raw: str,
        sized: SizedSignal,
    ) -> tuple[bool, str, Optional[float]]:
        """Parse Claude's JSON response into (approved, reasoning, override_size)."""
        # Find the outermost balanced JSON object
        data = None
        start = raw.find("{")
        if start != -1:
            depth = 0
            for i, ch in enumerate(raw[start:], start):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            data = json.loads(raw[start:i + 1])
                        except json.JSONDecodeError:
                            pass
                        break
        if data is None:
            raise ValueError("No JSON found in Claude response")

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
        self.bridge.push_decision(trace)

    def _run_seniority_evaluation(self) -> None:
        try:
            prev = getattr(self, "_last_exp_ranks", {})
            self._seniority_report = self.seniority.evaluate()
            logger.info("[ZEUS] Seniority: %s", self._seniority_report.summary_line())
            if os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY"):
                import core.supabase_client as supa
                supa.upsert_agent_seniority(
                    scores={k: v.to_dict() for k, v in self._seniority_report.agents.items()},
                    system_level_int=int(self._seniority_report.system_tier),
                )
                self._award_promotion_xp(prev)
            # Track (tier, level) so a later eval can detect tier-ups AND level-ups.
            self._last_exp_ranks = {
                name: (int(score.tier), score.level)
                for name, score in self._seniority_report.agents.items()
            }
        except Exception as exc:
            logger.warning("[ZEUS] Seniority evaluation failed: %s", exc)

    def _award_promotion_xp(self, prev_ranks: dict) -> None:
        """Mint the +1000 RANK UP jackpot for any agent whose (tier, level) rose,
        plus the big real_money_unlock milestone the first time an agent reaches
        the Senior tier. Deterministic source_refs keep every award idempotent.
        Cosmetic only — never touches sizing or the live-trading gate."""
        try:
            from core.exp import MILESTONE_XP, award_xp, recompute_agent_exp
            from core.seniority import Tier
        except Exception:
            return
        for name, score in self._seniority_report.agents.items():
            new_rank = (int(score.tier), score.level)
            old_rank = prev_ranks.get(name)
            # Recompute EXP each eval so the band-cap tracks the current tier,
            # even when there's no promotion this cycle.
            try:
                recompute_agent_exp(name)
            except Exception:
                pass
            if old_rank is not None and new_rank > old_rank:
                award_xp(
                    name, "promotion", MILESTONE_XP["promotion"],
                    source_ref=f"promo:{name}:{score.tier.label()}:{score.level}",
                    metadata={"from": list(old_rank), "to": list(new_rank)},
                )
                logger.info("[ZEUS] RANK UP — %s → %s L%d (+%d XP)",
                            name, score.tier.label(), score.level, MILESTONE_XP["promotion"])
                # Reaching Senior tier for the first time: the real-money unlock.
                if score.tier >= Tier.SENIOR and (old_rank is None or old_rank[0] < int(Tier.SENIOR)):
                    award_xp(
                        name, "real_milestone", MILESTONE_XP["real_money_unlock"],
                        source_ref=f"real_unlock:{name}",
                        metadata={"event": "reached_senior_real_money_unlocked"},
                    )
                    logger.info("[ZEUS] 🔓 %s reached SENIOR — real money UNLOCKED (arm to enable)", name)

    def _emergency_halt(self, reason: str) -> None:
        self.halt(reason=f"drawdown kill — {reason}")

    def _send_alert(self, message: str) -> None:
        try:
            self.argus.send_alert(message)
        except Exception:
            logger.warning("[ZEUS] Alert delivery failed: %s", message)

    def _check_concentration(self, sized: SizedSignal) -> Optional[str]:
        """
        Returns a kill reason string if this signal should be blocked for concentration
        reasons, or None if it should proceed.

        Checks:
          (a) ticker already has >= max_open_positions_per_ticker open positions
          (b) ticker was traded within ticker_cooldown_hours
        """
        ticker = sized.affected_tickers[0] if sized.affected_tickers else ""
        if not ticker:
            return None

        # (a) Open position cap per ticker
        cap = self.config.max_open_positions_per_ticker
        open_for_ticker = sum(
            1 for s in self.argus._state.snapshots if s.symbol == ticker
        )
        if open_for_ticker >= cap:
            return (
                f"concentration: {ticker} already has {open_for_ticker} open position(s) "
                f"(cap={cap})"
            )

        # (b) Cooldown — check intra-run approved trades first (fast path, no I/O)
        cooldown = timedelta(hours=self.config.ticker_cooldown_hours)
        now = datetime.now(timezone.utc)
        for t in self._run_approved_trades:
            if t.get("ticker") == ticker:
                return (
                    f"concentration: {ticker} already approved this cycle "
                    f"(cooldown={self.config.ticker_cooldown_hours}h)"
                )

        # Check KB for recent approved trades on this ticker
        try:
            history = self.kb.query_ticker_history(ticker, n_results=10)
            for h in history:
                if h.get("outcome") not in ("approved", "trade_placed"):
                    continue
                ts_raw = h.get("timestamp", "")
                if not ts_raw:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if (now - ts) < cooldown:
                        return (
                            f"concentration: {ticker} traded {(now - ts).total_seconds() / 3600:.1f}h ago "
                            f"(cooldown={self.config.ticker_cooldown_hours}h)"
                        )
                except (ValueError, TypeError):
                    continue
        except Exception as exc:
            logger.debug("[ZEUS] Concentration KB check failed for %s: %s", ticker, exc)

        return None

    def _register_watchdog(self) -> None:
        self.watchdog.register("zeus",    self.health)
        self.watchdog.register("icarus",  self.icarus.health)
        self.watchdog.register("hades",   self.hades.health)
        self.watchdog.register("artemis", self.artemis.health)
        self.watchdog.register("pythia",  self.pythia.health)
        self.watchdog.register("ares",    self.ares.health)
        self.watchdog.register("argus",   self.argus.health)
        self.watchdog.register("apollo",  self.apollo.health)
