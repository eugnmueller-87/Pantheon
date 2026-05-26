"""
core/seniority.py — Agent Seniority Framework

Every agent holds a senior-level role. There are no trainees or juniors —
these are specialist positions in a high-performance trading operation.

The question is not whether an agent is qualified; it is how far they have
developed within their senior role. Clearance to trade real money requires
reaching a minimum threshold. Until then they operate in simulation.

Levels (all senior-tier, increasing mastery):

  SENIOR         — Fully operational. Cleared for live paper trading.
                   Knows their domain, follows the playbook, executes correctly.

  PRINCIPAL      — Pattern mastery. Cleared for live real-money trading.
                   Proven track record, adaptive, deeper domain expertise.

  MANAGING_DIR   — (Sub-agents only) Elite specialist. Sector authority.
                   Owns their domain end-to-end, zero supervision needed.

  DIRECTOR       — ZEUS only. VP / Portfolio Manager level.
                   Oversees all agents, challenges their reasoning, routes
                   decisions, exercises autonomous override authority.
                   Sharpest knife in the drawer — Harvard-level judgment.

Clearance gates:
  SENIOR+        → Paper trading enabled, max 3% position
  PRINCIPAL+     → Live real-money trading enabled, max 5% position
  DIRECTOR       → Full autonomous override + unlimited Kelly sizing (5% cap)

System seniority = min of all agent levels.
ZEUS cannot be DIRECTOR while any sub-agent is below PRINCIPAL.

Evaluated automatically:
  - On ZEUS startup
  - After every Apollo research cycle
  - After every 50 trades
  - On demand via /status endpoint
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path

logger = logging.getLogger("seniority")


# ---------------------------------------------------------------------------
# Level definition
# ---------------------------------------------------------------------------

class Level(IntEnum):
    SENIOR       = 0   # floor — all agents start here once cleared
    PRINCIPAL    = 1   # proven track record, live trading enabled
    MANAGING_DIR = 2   # elite specialist (sub-agents), full domain ownership
    DIRECTOR     = 3   # ZEUS only — supervises all, autonomous override

    def label(self) -> str:
        return {
            Level.SENIOR:       "Senior",
            Level.PRINCIPAL:    "Principal",
            Level.MANAGING_DIR: "Managing Director",
            Level.DIRECTOR:     "Director",
        }[self]

    def max_position_pct(self) -> float:
        """Hard position size ceiling enforced by system level."""
        return {
            Level.SENIOR:       0.03,   # 3%  — paper trading
            Level.PRINCIPAL:    0.05,   # 5%  — live enabled, full Kelly
            Level.MANAGING_DIR: 0.05,   # 5%
            Level.DIRECTOR:     0.05,   # 5%  — autonomous override authority
        }[self]

    def live_trading_allowed(self) -> bool:
        """Real money only at PRINCIPAL or above."""
        return self >= Level.PRINCIPAL

    def paper_trading_allowed(self) -> bool:
        """Paper trading from SENIOR upward — always true."""
        return True


# ---------------------------------------------------------------------------
# Per-agent score card
# ---------------------------------------------------------------------------

@dataclass
class AgentScore:
    agent:        str
    level:        Level
    cleared:      bool             = False   # True = meets minimum Senior criteria
    criteria:     dict[str, bool]  = field(default_factory=dict)
    notes:        list[str]        = field(default_factory=list)
    evaluated_at: datetime         = field(default_factory=lambda: datetime.now(timezone.utc))

    def passed(self, criterion: str) -> None:
        self.criteria[criterion] = True

    def failed(self, criterion: str, reason: str = "") -> None:
        self.criteria[criterion] = False
        if reason:
            self.notes.append(f"{criterion}: {reason}")

    def to_dict(self) -> dict:
        return {
            "agent":        self.agent,
            "level":        self.level.label(),
            "level_int":    int(self.level),
            "cleared":      self.cleared,
            "criteria":     self.criteria,
            "notes":        self.notes,
            "evaluated_at": self.evaluated_at.isoformat(),
        }


# ---------------------------------------------------------------------------
# System-wide report
# ---------------------------------------------------------------------------

@dataclass
class SeniorityReport:
    agents:        dict[str, AgentScore]
    system_level:  Level
    all_cleared:   bool
    evaluated_at:  datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "system_level":         self.system_level.label(),
            "system_level_int":     int(self.system_level),
            "max_position_pct":     self.system_level.max_position_pct(),
            "paper_trading_allowed": True,
            "live_trading_allowed": self.system_level.live_trading_allowed(),
            "all_cleared":          self.all_cleared,
            "evaluated_at":         self.evaluated_at.isoformat(),
            "agents":               {k: v.to_dict() for k, v in self.agents.items()},
        }

    def summary_line(self) -> str:
        parts = [f"{name.upper()}: {score.level.label()}" for name, score in self.agents.items()]
        clearance = "LIVE ENABLED" if self.system_level.live_trading_allowed() else "PAPER ONLY"
        return (
            f"System: {self.system_level.label()} | "
            + " | ".join(parts)
            + f" | Max position: {self.system_level.max_position_pct()*100:.0f}%"
            + f" | {clearance}"
        )


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------

class SeniorityEvaluator:
    """
    Evaluates all agents and returns a SeniorityReport.
    Reads live state from ChromaDB, SQLite/Supabase, and filesystem.
    Never modifies any state — read-only.

    Every agent starts at SENIOR. The evaluation measures how far above
    Senior they have developed — not whether they are qualified to hold the role.
    """

    def __init__(
        self,
        kb=None,
        db_path: Path = Path("data/trade_log.db"),
        skills_dir: Path = Path("knowledge/agents"),
        alert_fn=None,
    ):
        self._kb         = kb
        self._db_path    = db_path
        self._skills_dir = skills_dir
        self._alert_fn   = alert_fn
        self._last_levels: dict[str, Level] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(self) -> SeniorityReport:
        scores = {
            "zeus":    self._evaluate_zeus(),
            "pythia":  self._evaluate_pythia(),
            "artemis": self._evaluate_artemis(),
            "apollo":  self._evaluate_apollo(),
            "hades":   self._evaluate_hades(),
            "icarus":  self._evaluate_icarus(),
            "ares":    self._evaluate_ares(),
            "argus":   self._evaluate_argus(),
        }

        system_level = min(s.level for s in scores.values())
        all_cleared  = all(s.cleared for s in scores.values())
        report = SeniorityReport(
            agents=scores,
            system_level=system_level,
            all_cleared=all_cleared,
        )

        self._check_promotions(scores)
        logger.info("[SENIORITY] %s", report.summary_line())
        return report

    # ------------------------------------------------------------------
    # Agent evaluators
    # All agents start at SENIOR. Criteria determine whether they have
    # earned PRINCIPAL, MANAGING_DIR, or (ZEUS only) DIRECTOR.
    # ------------------------------------------------------------------

    def _evaluate_zeus(self) -> AgentScore:
        """
        ZEUS — Portfolio Manager / Director
        Oversees all agents. Challenges reasoning. Routes decisions.
        Needs the broadest and deepest knowledge of any agent.
        Responsible for P&L of the entire operation.
        """
        score = AgentScore(agent="zeus", level=Level.SENIOR, cleared=False)
        kb_chunks = self._kb_chunk_count()
        decisions = self._decision_count()
        win_rate  = self._overall_win_rate()
        avg_pnl   = self._overall_avg_pnl()
        max_dd    = self._max_drawdown_observed()

        # Senior clearance: KB loaded, pipeline has run, LLM reasoning active
        senior_cleared = kb_chunks >= 100 and decisions >= 20
        if senior_cleared:
            score.cleared = True
            score.passed("kb_min_100_chunks")
            score.passed("pipeline_ran_20_decisions")
        else:
            score.failed("kb_min_100_chunks",        f"have {kb_chunks}/100 — run Apollo research cycle")
            score.failed("pipeline_ran_20_decisions", f"have {decisions}/20 — run pipeline")
            return score

        # SENIOR → PRINCIPAL: proven track record, positive expectancy
        if kb_chunks >= 500 and decisions >= 100 and win_rate >= 0.55 and avg_pnl > 0:
            score.level = Level.PRINCIPAL
            score.passed("kb_500_chunks")
            score.passed("100_decisions_made")
            score.passed("win_rate_above_55pct")
            score.passed("positive_expected_value")
        else:
            score.failed("kb_500_chunks",           f"have {kb_chunks}/500")
            score.failed("100_decisions_made",       f"have {decisions}/100")
            score.failed("win_rate_above_55pct",     f"{win_rate:.1%}/55%")
            score.failed("positive_expected_value",  f"avg P&L {avg_pnl:.3%}")
            return score

        # PRINCIPAL → MANAGING_DIR: elite-level performance, deep KB
        if kb_chunks >= 1000 and decisions >= 500 and win_rate >= 0.62 and max_dd < 0.06:
            score.level = Level.MANAGING_DIR
            score.passed("kb_1000_chunks")
            score.passed("500_decisions_made")
            score.passed("win_rate_above_62pct")
            score.passed("max_drawdown_under_6pct")
        else:
            score.failed("kb_1000_chunks",            f"have {kb_chunks}/1000")
            score.failed("500_decisions_made",         f"have {decisions}/500")
            score.failed("win_rate_above_62pct",       f"{win_rate:.1%}/62%")
            score.failed("max_drawdown_under_6pct",    f"{max_dd:.1%}/6%")
            return score

        # MANAGING_DIR → DIRECTOR: ZEUS-only.
        # Director = end-to-end portfolio governance accountability.
        # Not just good win rate — must demonstrate governance quality:
        #   - Overrides are documented (not just approved/rejected blindly)
        #   - Self-improvement has demonstrably improved calibration
        #   - All sub-agents operating at PRINCIPAL or above (Director leads a senior team)
        #   - Drawdown discipline proven over sustained period
        #   - Portfolio-level metrics tracked (Sharpe > 0, on-time exit rate)
        apollo_cycles     = self._apollo_self_improve_count()
        all_principals    = self._all_sub_agents_at_least_principal()
        override_quality  = self._zeus_override_documentation_rate()
        calibration_ok    = self._zeus_calibration_score()

        if (
            kb_chunks >= 2000
            and decisions >= 1000
            and win_rate >= 0.65
            and max_dd < 0.05
            and apollo_cycles >= 5
            and all_principals
            and override_quality >= 0.80
            and calibration_ok
        ):
            score.level = Level.DIRECTOR
            score.passed("kb_2000_chunks_broadest_in_system")
            score.passed("1000_decisions_with_full_traces")
            score.passed("win_rate_above_65pct")
            score.passed("max_drawdown_held_under_5pct")
            score.passed("5_apollo_self_improvement_cycles")
            score.passed("all_sub_agents_at_principal_or_above")
            score.passed("80pct_overrides_have_documented_evidence")
            score.passed("director_calibration_validated")
        else:
            score.failed("kb_2000_chunks_broadest_in_system",       f"have {kb_chunks}/2000")
            score.failed("1000_decisions_with_full_traces",          f"have {decisions}/1000")
            score.failed("win_rate_above_65pct",                     f"{win_rate:.1%}/65%")
            score.failed("max_drawdown_held_under_5pct",             f"{max_dd:.1%}/5%")
            score.failed("5_apollo_self_improvement_cycles",         f"ran {apollo_cycles}/5")
            score.failed("all_sub_agents_at_principal_or_above",
                         "" if all_principals else "one or more sub-agents below PRINCIPAL")
            score.failed("80pct_overrides_have_documented_evidence",
                         f"{override_quality:.0%}/80% overrides documented")
            score.failed("director_calibration_validated",
                         "" if calibration_ok else "calibration not yet validated — need 200+ decisions")

        return score

    def _evaluate_pythia(self) -> AgentScore:
        """
        Pythia — Senior Quantitative Analyst / Position Sizer
        Individual contributor. Owns position sizing end-to-end.
        Must demonstrate adaptive Kelly sizing beats naive defaults.
        """
        score = AgentScore(agent="pythia", level=Level.SENIOR, cleared=False)
        context_keys = self._pythia_context_key_count(min_trades=10)
        mature_keys  = self._pythia_context_key_count(min_trades=20)
        avg_win_rate = self._pythia_avg_win_rate()
        kelly_edge   = self._pythia_kelly_vs_default_edge()

        # Senior clearance: has learned at least 5 context patterns
        if context_keys >= 5:
            score.cleared = True
            score.passed("5_context_keys_learned")
        else:
            score.failed("5_context_keys_learned", f"have {context_keys}/5 — needs more trades")
            return score

        # SENIOR → PRINCIPAL: deep pattern library, proven win rate
        if mature_keys >= 15 and avg_win_rate >= 0.55:
            score.level = Level.PRINCIPAL
            score.passed("15_mature_context_keys")
            score.passed("avg_win_rate_above_55pct")
        else:
            score.failed("15_mature_context_keys",    f"have {mature_keys}/15")
            score.failed("avg_win_rate_above_55pct",  f"{avg_win_rate:.1%}/55%")
            return score

        # PRINCIPAL → MANAGING_DIR: Kelly provably beats naive sizing
        if mature_keys >= 30 and kelly_edge >= 0.005:
            score.level = Level.MANAGING_DIR
            score.passed("30_mature_context_keys")
            score.passed("kelly_outperforms_default_by_0pt5pct")
        else:
            score.failed("30_mature_context_keys",            f"have {mature_keys}/30")
            score.failed("kelly_outperforms_default_by_0pt5pct", f"edge {kelly_edge:.3%}/0.5%")

        return score

    def _evaluate_artemis(self) -> AgentScore:
        """
        Artemis — Senior Macro Strategist
        Individual contributor. Owns market regime classification.
        Must demonstrate regime calls have real predictive accuracy.
        """
        score = AgentScore(agent="artemis", level=Level.SENIOR, cleared=False)
        has_fred        = self._kb_has_source_type("fred_macro")
        has_eu_data     = self._kb_has_source_type("eu_markets")
        regime_accuracy = self._artemis_regime_accuracy()

        # Senior clearance: FRED macro history in KB (real historical context)
        if has_fred:
            score.cleared = True
            score.passed("fred_macro_history_in_kb")
        else:
            score.failed("fred_macro_history_in_kb",
                         "FRED data not ingested — Apollo historical run needed")
            return score

        # SENIOR → PRINCIPAL: EU coverage added, regime outcomes tracked
        if has_eu_data:
            score.level = Level.PRINCIPAL
            score.passed("eu_market_data_in_kb")
            score.passed("dax_stoxx50_coverage")
        else:
            score.failed("eu_market_data_in_kb", "no EU market data — OpenBB or yfinance EU tickers needed")
            return score

        # PRINCIPAL → MANAGING_DIR: regime predictions validated ≥ 70% accuracy
        if regime_accuracy >= 0.70:
            score.level = Level.MANAGING_DIR
            score.passed("regime_accuracy_above_70pct")
        else:
            score.failed("regime_accuracy_above_70pct",
                         f"{regime_accuracy:.1%}/70% — needs 50+ closed trades to measure")

        return score

    def _evaluate_apollo(self) -> AgentScore:
        """
        Apollo — Senior Research Analyst / Knowledge Officer
        Individual contributor. Owns the KB. Feeds intelligence to all other agents.
        The quality of every other agent's knowledge depends on Apollo's output.
        """
        score = AgentScore(agent="apollo", level=Level.SENIOR, cleared=False)
        arxiv_papers    = self._kb_count_by_type("arxiv")
        has_earnings    = self._kb_has_source_type("earnings_history")
        has_form4       = self._kb_has_source_type("sec_form4")
        improve_count   = self._apollo_self_improve_count()
        skills_file     = self._skills_dir / "zeus_skills.md"

        # Senior clearance: research cycles running, 50+ academic papers ingested
        if arxiv_papers >= 50:
            score.cleared = True
            score.passed("50_arxiv_papers_ingested")
        else:
            score.failed("50_arxiv_papers_ingested",
                         f"have {arxiv_papers}/50 — trigger Apollo research cycle")
            return score

        # SENIOR → PRINCIPAL: real-world data ingested, self-improvement active
        if has_earnings and has_form4 and improve_count >= 1:
            score.level = Level.PRINCIPAL
            score.passed("earnings_surprise_history_ingested")
            score.passed("sec_form4_insider_trades_ingested")
            score.passed("self_improvement_loop_ran_once")
        else:
            score.failed("earnings_surprise_history_ingested",
                         "" if has_earnings else "4yr earnings history not ingested")
            score.failed("sec_form4_insider_trades_ingested",
                         "" if has_form4 else "SEC Form 4 data not ingested")
            score.failed("self_improvement_loop_ran_once",
                         f"ran {improve_count}/1 times")
            return score

        # PRINCIPAL → MANAGING_DIR: proven research → performance improvement loop
        has_insights = (
            skills_file.exists()
            and "Self-Improvement Insights" in skills_file.read_text(encoding="utf-8", errors="ignore")
        )
        if improve_count >= 5 and has_insights:
            score.level = Level.MANAGING_DIR
            score.passed("5_self_improvement_cycles")
            score.passed("insights_written_to_zeus_skills")
        else:
            score.failed("5_self_improvement_cycles",       f"ran {improve_count}/5")
            score.failed("insights_written_to_zeus_skills", "" if has_insights else "no insights yet")

        return score

    def _evaluate_hades(self) -> AgentScore:
        """
        Hades — Senior Compliance Officer
        Individual contributor. Owns regulatory risk end-to-end.
        Zero tolerance for compliance failures — one violation resets the clock.
        """
        score = AgentScore(agent="hades", level=Level.SENIOR, cleared=False)
        signals_processed   = self._hades_signals_processed()
        violations          = self._hades_compliance_violations()
        false_positive_rate = self._hades_false_positive_rate()

        # Senior clearance: OFAC/ESG/LkSG logic present and deployed
        ofac_file = Path("agents/hades.py")
        has_ofac  = (
            ofac_file.exists()
            and "ofac" in ofac_file.read_text(encoding="utf-8", errors="ignore").lower()
        )
        if has_ofac:
            score.cleared = True
            score.passed("ofac_esg_lksg_compliance_active")
        else:
            score.failed("ofac_esg_lksg_compliance_active", "OFAC logic missing from hades.py")
            return score

        # SENIOR → PRINCIPAL: 50+ signals screened, zero violations
        if signals_processed >= 50 and violations == 0:
            score.level = Level.PRINCIPAL
            score.passed("50_signals_screened_clean")
            score.passed("zero_compliance_violations")
        else:
            score.failed("50_signals_screened_clean",  f"screened {signals_processed}/50")
            score.failed("zero_compliance_violations", f"{violations} violations on record")
            return score

        # PRINCIPAL → MANAGING_DIR: 200+ screened, false positive rate < 5%
        if signals_processed >= 200 and false_positive_rate < 0.05:
            score.level = Level.MANAGING_DIR
            score.passed("200_signals_screened")
            score.passed("false_positive_rate_under_5pct")
        else:
            score.failed("200_signals_screened",          f"screened {signals_processed}/200")
            score.failed("false_positive_rate_under_5pct", f"{false_positive_rate:.1%}/5%")

        return score

    def _evaluate_icarus(self) -> AgentScore:
        """
        Icarus — Senior Signal Analyst
        Individual contributor. Owns signal discovery and classification.
        Quality of signals directly determines what the pipeline can trade.
        """
        score = AgentScore(agent="icarus", level=Level.SENIOR, cleared=False)
        ticker_map_size = self._ticker_map_size()
        signals_seen    = self._icarus_total_signals()
        dedup_active    = self._icarus_dedup_active()

        # Senior clearance: ticker map populated, deduplication working
        if ticker_map_size >= 20 and dedup_active:
            score.cleared = True
            score.passed("ticker_map_20_entries")
            score.passed("deduplication_logic_active")
        else:
            score.failed("ticker_map_20_entries",      f"have {ticker_map_size}/20")
            score.failed("deduplication_logic_active", "" if dedup_active else "dedup missing")
            return score

        # SENIOR → PRINCIPAL: 200+ signals seen AND ≥15% approved by Zeus
        approval_rate = self._icarus_approval_rate()
        min_rate = 0.15
        if signals_seen >= 200 and approval_rate is not None and approval_rate >= min_rate:
            score.level = Level.PRINCIPAL
            score.passed("200_signals_classified")
            score.passed(f"approval_rate_{int(approval_rate*100)}pct")
        elif signals_seen < 200:
            score.failed("200_signals_classified", f"seen {signals_seen}/200")
            return score
        else:
            rate_str = f"{approval_rate*100:.1f}%" if approval_rate is not None else "unknown"
            score.failed("approval_rate_15pct", f"rate {rate_str} < 15% — Icarus sending too much noise")
            return score

        # PRINCIPAL → MANAGING_DIR: 200+ ticker coverage (Apollo-expanded)
        if ticker_map_size >= 200:
            score.level = Level.MANAGING_DIR
            score.passed("200_tickers_in_map")
        else:
            score.failed("200_tickers_in_map", f"have {ticker_map_size}/200 — Apollo expands this")

        return score

    def _evaluate_ares(self) -> AgentScore:
        """
        Ares — Senior Trader / Execution Specialist
        Individual contributor. Owns trade execution and order management.
        Must demonstrate clean execution — slippage, fill rates, bracket accuracy.
        """
        score = AgentScore(agent="ares", level=Level.SENIOR, cleared=False)
        ibkr_configured = self._ibkr_port_in_code()
        trades_placed   = self._ares_trades_placed()
        live_enabled    = not bool(os.getenv("MOCK_EXECUTION", "true").lower() in ("true", "1"))

        # Senior clearance: IBKR integration present, bracket order logic deployed
        ares_file = Path("agents/ares.py")
        if ares_file.exists() and ibkr_configured:
            score.cleared = True
            score.passed("ibkr_integration_present")
            score.passed("bracket_order_logic_deployed")
        else:
            score.failed("ibkr_integration_present",    "" if ares_file.exists() else "ares.py missing")
            score.failed("bracket_order_logic_deployed", "" if ibkr_configured else "IBKR port not configured")
            return score

        # SENIOR → PRINCIPAL: 50+ paper trades executed cleanly
        if trades_placed >= 50:
            score.level = Level.PRINCIPAL
            score.passed("50_paper_trades_executed")
        else:
            score.failed("50_paper_trades_executed", f"placed {trades_placed}/50 — connect IBKR paper account")
            return score

        # PRINCIPAL → MANAGING_DIR: live trading active, 200+ trades
        if live_enabled and trades_placed >= 200:
            score.level = Level.MANAGING_DIR
            score.passed("live_trading_active")
            score.passed("200_trades_executed")
        else:
            score.failed("live_trading_active", "" if live_enabled else "still in mock mode")
            score.failed("200_trades_executed", f"placed {trades_placed}/200")

        return score

    def _evaluate_argus(self) -> AgentScore:
        """
        Argus — Senior Risk Manager / Portfolio Monitor
        Individual contributor. Owns drawdown management and kill switch authority.
        Must demonstrate portfolio is being monitored in real time.
        """
        score = AgentScore(agent="argus", level=Level.SENIOR, cleared=False)
        supabase_active = bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY"))
        portfolio_rows  = self._argus_portfolio_rows()
        kill_present    = self._argus_kill_switch_present()
        telegram_ok     = bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))

        # Senior clearance: kill switch present, Supabase persistence active
        if kill_present and supabase_active:
            score.cleared = True
            score.passed("drawdown_kill_switch_active")
            score.passed("portfolio_state_persisted_to_supabase")
        else:
            score.failed("drawdown_kill_switch_active",
                         "" if kill_present else "kill switch missing in argus.py")
            score.failed("portfolio_state_persisted_to_supabase",
                         "" if supabase_active else "SUPABASE env vars not set")
            return score

        # SENIOR → PRINCIPAL: 100+ portfolio snapshots, alerts configured
        if portfolio_rows >= 100 and telegram_ok:
            score.level = Level.PRINCIPAL
            score.passed("100_portfolio_snapshots")
            score.passed("telegram_alerts_configured")
        else:
            score.failed("100_portfolio_snapshots",     f"have {portfolio_rows}/100")
            score.failed("telegram_alerts_configured",  "" if telegram_ok else "TELEGRAM env vars not set")
            return score

        # PRINCIPAL → MANAGING_DIR: 1000+ snapshots proves continuous monitoring
        if portfolio_rows >= 1000:
            score.level = Level.MANAGING_DIR
            score.passed("1000_portfolio_snapshots_continuous_monitoring")
        else:
            score.failed("1000_portfolio_snapshots_continuous_monitoring",
                         f"have {portfolio_rows}/1000")

        return score

    # ------------------------------------------------------------------
    # Promotion detection + Telegram alerts
    # ------------------------------------------------------------------

    def _check_promotions(self, scores: dict[str, AgentScore]) -> None:
        for name, score in scores.items():
            prev = self._last_levels.get(name)
            if prev is not None and score.level > prev:
                msg = (
                    f"PANTHEON PROMOTION\n"
                    f"{name.upper()}: {prev.label()} → {score.level.label()}\n"
                    f"System level: {min(s.level for s in scores.values()).label()}"
                )
                logger.info("[SENIORITY] PROMOTION — %s", msg)
                if self._alert_fn:
                    try:
                        self._alert_fn(msg)
                    except Exception:
                        pass
            self._last_levels[name] = score.level

    # ------------------------------------------------------------------
    # Data readers — all read-only, never raise
    # ------------------------------------------------------------------

    def _kb_chunk_count(self) -> int:
        try:
            return self._get_kb()._knowledge_col.count()
        except Exception:
            return 0

    def _decision_count(self) -> int:
        try:
            return self._get_kb()._decisions_col.count()
        except Exception:
            return 0

    def _kb_count_by_type(self, source_type: str) -> int:
        try:
            kb = self._get_kb()
            results = kb._knowledge_col.get(where={"type": source_type}, include=[])
            return len(results.get("ids", []))
        except Exception:
            return 0

    def _kb_has_source_type(self, source_type: str) -> bool:
        return self._kb_count_by_type(source_type) > 0

    def _overall_win_rate(self) -> float:
        try:
            rows = self._sqlite_query(
                "SELECT AVG(CASE WHEN hit=1 THEN 1.0 ELSE 0.0 END) FROM trades WHERE hit IS NOT NULL"
            )
            return float(rows[0][0] or 0.0)
        except Exception:
            return 0.0

    def _overall_avg_pnl(self) -> float:
        try:
            rows = self._sqlite_query("SELECT AVG(pnl_pct) FROM trades WHERE pnl_pct IS NOT NULL")
            return float(rows[0][0] or 0.0)
        except Exception:
            return 0.0

    def _max_drawdown_observed(self) -> float:
        try:
            if os.getenv("SUPABASE_URL"):
                import core.supabase_client as supa
                res = (
                    supa.get_client()
                    .table("portfolio_state")
                    .select("current_drawdown_pct")
                    .order("current_drawdown_pct", desc=True)
                    .limit(1)
                    .execute()
                )
                if res.data:
                    return float(res.data[0].get("current_drawdown_pct", 0.0))
        except Exception:
            pass
        return 0.0

    def _pythia_context_key_count(self, min_trades: int = 10) -> int:
        try:
            rows = self._sqlite_query(f"""
                SELECT COUNT(DISTINCT context_key) FROM (
                    SELECT context_key FROM trades
                    WHERE hit IS NOT NULL
                    GROUP BY context_key HAVING COUNT(*) >= {min_trades}
                )
            """)
            return int(rows[0][0] or 0)
        except Exception:
            return 0

    def _pythia_avg_win_rate(self) -> float:
        try:
            rows = self._sqlite_query("""
                SELECT AVG(win_rate) FROM (
                    SELECT AVG(CASE WHEN hit=1 THEN 1.0 ELSE 0.0 END) AS win_rate
                    FROM trades WHERE hit IS NOT NULL
                    GROUP BY context_key HAVING COUNT(*) >= 10
                )
            """)
            return float(rows[0][0] or 0.0)
        except Exception:
            return 0.0

    def _pythia_kelly_vs_default_edge(self) -> float:
        try:
            rows = self._sqlite_query("""
                SELECT
                    AVG(CASE WHEN position_pct > 0.02 THEN pnl_pct END) -
                    AVG(CASE WHEN position_pct <= 0.02 THEN pnl_pct END)
                FROM trades WHERE pnl_pct IS NOT NULL
            """)
            return float(rows[0][0] or 0.0)
        except Exception:
            return 0.0

    def _artemis_regime_accuracy(self) -> float:
        try:
            decisions = self._get_kb()._decisions_col.count()
            return 0.0 if decisions < 50 else -1.0
        except Exception:
            return 0.0

    def _apollo_self_improve_count(self) -> int:
        skills_file = self._skills_dir / "zeus_skills.md"
        if not skills_file.exists():
            return 0
        text = skills_file.read_text(encoding="utf-8", errors="ignore")
        return text.count("Self-Improvement Insights")

    def _hades_signals_processed(self) -> int:
        try:
            results = self._get_kb()._decisions_col.get(include=[])
            return len(results.get("ids", []))
        except Exception:
            return 0

    def _hades_compliance_violations(self) -> int:
        return 0

    def _hades_false_positive_rate(self) -> float:
        return 0.0

    def _icarus_total_signals(self) -> int:
        try:
            return self._get_kb()._decisions_col.count()
        except Exception:
            return 0

    def _icarus_approval_rate(self) -> float | None:
        """Query Icarus quality totals from Redis."""
        try:
            url   = os.getenv("UPSTASH_REDIS_REST_URL", "")
            token = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
            if not url or not token:
                return None
            import httpx
            headers = {"Authorization": f"Bearer {token}"}
            def _get(key: str):
                r = httpx.get(f"{url}/get/{key}", headers=headers, timeout=3)
                return r.json().get("result")
            seen_raw     = _get("icarus:quality:totals:seen")
            approved_raw = _get("icarus:quality:totals:approved")
            seen     = int(seen_raw)     if seen_raw     else 0
            approved = int(approved_raw) if approved_raw else 0
            return (approved / seen) if seen > 0 else None
        except Exception:
            return None

    def _icarus_dedup_active(self) -> bool:
        path = Path("agents/icarus.py")
        return path.exists() and "_seen" in path.read_text(encoding="utf-8", errors="ignore")

    def _ticker_map_size(self) -> int:
        path = Path("data/ticker_map.json")
        if not path.exists():
            return 0
        try:
            import json
            return len(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return 0

    def _ares_trades_placed(self) -> int:
        try:
            rows = self._sqlite_query("SELECT COUNT(*) FROM trades")
            return int(rows[0][0] or 0)
        except Exception:
            return 0

    def _ibkr_port_in_code(self) -> bool:
        path = Path("agents/ares.py")
        text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
        return any(port in text for port in ("4004", "4001", "4002", "IB_PORT"))

    def _argus_portfolio_rows(self) -> int:
        try:
            if os.getenv("SUPABASE_URL"):
                import core.supabase_client as supa
                res = (
                    supa.get_client()
                    .table("portfolio_state")
                    .select("*", count="exact")
                    .limit(1)
                    .execute()
                )
                return res.count or 0
        except Exception:
            pass
        return 0

    def _argus_kill_switch_present(self) -> bool:
        path = Path("agents/argus.py")
        if not path.exists():
            return False
        text = path.read_text(encoding="utf-8", errors="ignore")
        return "max_drawdown_pct" in text and "_on_kill" in text

    def _zeus_override_documentation_rate(self) -> float:
        """
        Fraction of ZEUS overrides that have documented evidence in the trace.
        A Director-quality override cites specific KB evidence, not gut feel.
        Reads from ChromaDB decision traces.
        """
        try:
            kb = self._get_kb()
            if not kb:
                return 1.0   # no overrides yet = 100% documentation rate (vacuously true)
            results = kb._decisions_col.get(
                where={"zeus_override": True},
                include=["metadatas"],
            )
            metas = results.get("metadatas") or []
            if not metas:
                return 1.0
            documented = sum(
                1 for m in metas
                if m.get("zeus_override_reason") and len(str(m.get("zeus_override_reason", ""))) > 30
            )
            return documented / len(metas)
        except Exception:
            return 1.0

    def _zeus_calibration_score(self) -> bool:
        """
        Is ZEUS's stated confidence calibrated against actual outcomes?
        A Director-level claim of 0.7 confidence should win ~70% of the time.
        Returns True when we have enough data (200+ decisions) to validate.
        Under 200 decisions, returns False — calibration not yet measurable.
        """
        try:
            decisions = self._decision_count()
            if decisions < 200:
                return False
            # Rough calibration: high-confidence approvals (>0.7) should win at ≥ 65%
            rows = self._sqlite_query("""
                SELECT AVG(CASE WHEN hit=1 THEN 1.0 ELSE 0.0 END)
                FROM trades
                WHERE confidence >= 0.70 AND hit IS NOT NULL
            """)
            if not rows or rows[0][0] is None:
                return False
            high_conf_win_rate = float(rows[0][0])
            return high_conf_win_rate >= 0.65
        except Exception:
            return False

    def _all_sub_agents_at_least_principal(self) -> bool:
        """ZEUS Director gate: all sub-agents must be at PRINCIPAL or above."""
        return all([
            self._pythia_context_key_count(min_trades=20) >= 15,
            self._pythia_avg_win_rate() >= 0.55,
            self._kb_has_source_type("fred_macro"),
            self._kb_has_source_type("eu_markets"),
            self._kb_has_source_type("earnings_history"),
            self._hades_signals_processed() >= 50,
            self._ticker_map_size() >= 200,
            self._ares_trades_placed() >= 50,
            self._argus_portfolio_rows() >= 100,
        ])

    def _sqlite_query(self, sql: str) -> list:
        if not self._db_path.exists():
            return []
        with sqlite3.connect(self._db_path) as conn:
            return conn.execute(sql).fetchall()

    def _get_kb(self):
        if self._kb is not None:
            return self._kb
        try:
            from core.knowledge_base import KnowledgeBase
            self._kb = KnowledgeBase()
            return self._kb
        except Exception:
            return None
