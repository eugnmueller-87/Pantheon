"""
core/seniority.py — Agent Seniority Framework (tier × level model)

Agents are NOT born senior. They start as TRAINEES and earn their way up by
producing successful (winning) closed trades. Seniority is the authoritative
gate on capital: paper-only until an agent reaches the top tier, real money
only once the whole system is SENIOR *and* an operator manually arms it.

────────────────────────────────────────────────────────────────────────────
THE LADDER
────────────────────────────────────────────────────────────────────────────
Four PAPER tiers, climbed in order. Within each tier there are 10 levels.

    TRAINEE       lvl 1–10   paper   max 1%
    JUNIOR        lvl 1–10   paper   max 2%
    INTERMEDIATE  lvl 1–10   paper   max 3%
    SENIOR        lvl 1–10   paper   max 3%   → unlocks REAL money (disarmed)

Progression rule (the one the user specified):
    • 10 successful trades  → +1 level
    • level 10 of a tier    → promote to the next tier at level 1
So one tier = 100 successful trades; reaching SENIOR (entering its level 1)
takes 300 successful trades; maxing SENIOR (level 10) takes 400.

"Successful trade" = a closed trade that won (hit = 1). Losses don't advance a
level (they're tracked for the win rate / EXP streak elsewhere) — you climb on
wins, exactly as written down.

────────────────────────────────────────────────────────────────────────────
REAL MONEY
────────────────────────────────────────────────────────────────────────────
Reaching SENIOR only UNLOCKS real money. It does not start it. Live orders
require BOTH:
    • system seniority tier == SENIOR  (every agent at SENIOR), AND
    • the operator has armed it  (ARM_REAL_MONEY=true)
Until both hold, the system is paper-only. Once real money is live, a SEPARATE
real-money XP bar tracks performance on real capital (see core/exp.py — the
real-money ledger is independent of the paper climb).

System tier = min tier across all agents (the team is only as senior as its
most junior member). Within a tier the system level = min level across agents.

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

# Progression constants — the rule the user specified.
WINS_PER_LEVEL = 10          # 10 successful (winning) trades advance one level
LEVELS_PER_TIER = 10         # 10 levels per tier, then promote to the next tier
WINS_PER_TIER = WINS_PER_LEVEL * LEVELS_PER_TIER   # 100 wins to clear a tier


# ---------------------------------------------------------------------------
# Tier definition — the seniority ladder
# ---------------------------------------------------------------------------

class Tier(IntEnum):
    TRAINEE      = 0   # floor — everyone starts here
    JUNIOR       = 1
    INTERMEDIATE = 2
    SENIOR       = 3   # top — unlocks real money (when armed)

    def label(self) -> str:
        return {
            Tier.TRAINEE:      "Trainee",
            Tier.JUNIOR:       "Junior",
            Tier.INTERMEDIATE: "Intermediate",
            Tier.SENIOR:       "Senior",
        }[self]

    def max_position_pct(self) -> float:
        """Hard position-size ceiling enforced by the system tier (paper).
        Sizing grows cautiously as the team proves itself."""
        return {
            Tier.TRAINEE:      0.01,   # 1% — learning
            Tier.JUNIOR:       0.02,   # 2%
            Tier.INTERMEDIATE: 0.03,   # 3%
            Tier.SENIOR:       0.03,   # 3% — proven; real money uses its own cap
        }[self]

    def real_money_unlocked(self) -> bool:
        """SENIOR unlocks real money. It does NOT by itself start it — arming
        is a separate operator action (see real_money_live())."""
        return self >= Tier.SENIOR


# Top of the ladder — reaching this tier's level 10 is full paper mastery.
MAX_TIER = Tier.SENIOR
REAL_MONEY_MAX_POSITION_PCT = 0.05   # cap once real money is armed & live


# ---------------------------------------------------------------------------
# Real-money arming — a deliberate, operator-controlled switch
# ---------------------------------------------------------------------------

def real_money_armed() -> bool:
    """True only if the operator has explicitly armed real-money trading.
    Reaching SENIOR is necessary but NOT sufficient — this is the human gate."""
    return os.getenv("ARM_REAL_MONEY", "false").strip().lower() in ("true", "1", "yes")


def real_money_live(system_tier: "Tier") -> bool:
    """The single authoritative check for whether real orders may be placed:
    the team is fully SENIOR *and* an operator has armed it."""
    return system_tier.real_money_unlocked() and real_money_armed()


# ---------------------------------------------------------------------------
# Tier × level helpers
# ---------------------------------------------------------------------------

def level_from_wins(wins: int) -> int:
    """Within-tier level 1..10 derived from a tier-local winning-trade count.
    0 wins → level 1; 10 wins → level 2; 90 wins → level 10 (capped)."""
    if wins < 0:
        wins = 0
    return min(LEVELS_PER_TIER, 1 + wins // WINS_PER_LEVEL)


def tier_and_level_from_total_wins(total_wins: int) -> tuple["Tier", int]:
    """Map a lifetime successful-trade count onto (tier, level 1..10).

    100 wins per tier; within a tier, 10 wins per level. Past the top tier the
    level saturates at 10 (you can't out-rank Senior on the paper ladder)."""
    if total_wins < 0:
        total_wins = 0
    tier_idx = min(int(MAX_TIER), total_wins // WINS_PER_TIER)
    tier = Tier(tier_idx)
    if tier == MAX_TIER:
        # Cap progress inside the top tier — no overflow past SENIOR lvl 10.
        wins_in_tier = total_wins - int(MAX_TIER) * WINS_PER_TIER
        return tier, level_from_wins(wins_in_tier)
    wins_in_tier = total_wins - tier_idx * WINS_PER_TIER
    return tier, level_from_wins(wins_in_tier)


def wins_to_next_level(total_wins: int) -> int:
    """How many more successful trades until the next level (0 if maxed)."""
    tier, level = tier_and_level_from_total_wins(total_wins)
    if tier == MAX_TIER and level >= LEVELS_PER_TIER:
        return 0
    return WINS_PER_LEVEL - (total_wins % WINS_PER_LEVEL)


def level_progress_pct(total_wins: int) -> float:
    """Fraction 0..1 through the current level (for the paper XP bar fill)."""
    tier, level = tier_and_level_from_total_wins(total_wins)
    if tier == MAX_TIER and level >= LEVELS_PER_TIER:
        return 1.0
    return (total_wins % WINS_PER_LEVEL) / WINS_PER_LEVEL


# ---------------------------------------------------------------------------
# Per-agent score card
# ---------------------------------------------------------------------------

@dataclass
class AgentScore:
    agent:        str
    tier:         Tier
    level:        int                       # 1..10 within the tier
    wins:         int              = 0       # lifetime successful (winning) trades
    cleared:      bool             = False   # meets baseline operational criteria
    criteria:     dict[str, bool]  = field(default_factory=dict)
    notes:        list[str]        = field(default_factory=list)
    evaluated_at: datetime         = field(default_factory=lambda: datetime.now(timezone.utc))

    def passed(self, criterion: str) -> None:
        self.criteria[criterion] = True

    def failed(self, criterion: str, reason: str = "") -> None:
        self.criteria[criterion] = False
        if reason:
            self.notes.append(f"{criterion}: {reason}")

    def rank_label(self) -> str:
        return f"{self.tier.label()} L{self.level}"

    def to_dict(self) -> dict:
        return {
            "agent":          self.agent,
            "tier":           self.tier.label(),
            "tier_int":       int(self.tier),
            "level":          self.level,
            "wins":           self.wins,
            "rank":           self.rank_label(),
            "cleared":        self.cleared,
            "criteria":       self.criteria,
            "notes":          self.notes,
            "evaluated_at":   self.evaluated_at.isoformat(),
            # back-compat alias for older readers expecting a flat ordinal
            "level_int":      int(self.tier),
        }


# ---------------------------------------------------------------------------
# System-wide report
# ---------------------------------------------------------------------------

@dataclass
class SeniorityReport:
    agents:        dict[str, AgentScore]
    system_tier:   Tier
    system_level:  int
    armed:         bool
    all_cleared:   bool
    evaluated_at:  datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def real_money_unlocked(self) -> bool:
        return self.system_tier.real_money_unlocked()

    @property
    def live_trading_allowed(self) -> bool:
        return real_money_live(self.system_tier)

    @property
    def max_position_pct(self) -> float:
        if self.live_trading_allowed:
            return REAL_MONEY_MAX_POSITION_PCT
        return self.system_tier.max_position_pct()

    def to_dict(self) -> dict:
        return {
            "system_tier":          self.system_tier.label(),
            "system_tier_int":      int(self.system_tier),
            "system_level":         self.system_level,
            "system_rank":          f"{self.system_tier.label()} L{self.system_level}",
            "max_position_pct":     self.max_position_pct,
            "paper_trading_allowed": True,
            "real_money_unlocked":  self.real_money_unlocked,
            "real_money_armed":     self.armed,
            "live_trading_allowed": self.live_trading_allowed,
            "all_cleared":          self.all_cleared,
            "evaluated_at":         self.evaluated_at.isoformat(),
            "agents":               {k: v.to_dict() for k, v in self.agents.items()},
        }

    def summary_line(self) -> str:
        parts = [f"{name.upper()}: {s.rank_label()}" for name, s in self.agents.items()]
        if self.live_trading_allowed:
            clearance = "LIVE (ARMED)"
        elif self.real_money_unlocked:
            clearance = "REAL UNLOCKED (DISARMED)"
        else:
            clearance = "PAPER ONLY"
        return (
            f"System: {self.system_tier.label()} L{self.system_level} | "
            + " | ".join(parts)
            + f" | Max position: {self.max_position_pct*100:.0f}%"
            + f" | {clearance}"
        )


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------

class SeniorityEvaluator:
    """
    Evaluates all agents and returns a SeniorityReport.

    Rank is earned, not granted: an agent's (tier, level) is derived purely
    from its count of SUCCESSFUL closed trades (10 wins/level, 10 levels/tier).
    A `cleared` flag separately records whether the agent meets its baseline
    operational criteria (KB loaded, OFAC present, kill switch wired, …); an
    un-cleared agent is held at TRAINEE L1 regardless of win count, because a
    misconfigured agent has no business climbing.

    Read-only: never mutates trade/KB state.
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
        self._last_ranks: dict[str, tuple[Tier, int]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(self) -> SeniorityReport:
        # Authoritative source of progression: per-agent winning-trade counts.
        wins_by_agent = self._wins_by_agent()

        scores = {
            "zeus":    self._score("zeus",    wins_by_agent, self._clear_zeus),
            "pythia":  self._score("pythia",  wins_by_agent, self._clear_pythia),
            "artemis": self._score("artemis", wins_by_agent, self._clear_artemis),
            "apollo":  self._score("apollo",  wins_by_agent, self._clear_apollo),
            "hades":   self._score("hades",   wins_by_agent, self._clear_hades),
            "icarus":  self._score("icarus",  wins_by_agent, self._clear_icarus),
            "ares":    self._score("ares",    wins_by_agent, self._clear_ares),
            "argus":   self._score("argus",   wins_by_agent, self._clear_argus),
        }

        system_tier  = Tier(min(int(s.tier) for s in scores.values()))
        # System level = min level among the agents sitting in the system tier.
        in_tier      = [s.level for s in scores.values() if s.tier == system_tier]
        system_level = min(in_tier) if in_tier else 1
        all_cleared  = all(s.cleared for s in scores.values())

        report = SeniorityReport(
            agents=scores,
            system_tier=system_tier,
            system_level=system_level,
            armed=real_money_armed(),
            all_cleared=all_cleared,
        )

        self._check_promotions(scores)
        logger.info("[SENIORITY] %s", report.summary_line())
        return report

    # ------------------------------------------------------------------
    # Scoring — rank from wins, gated by clearance
    # ------------------------------------------------------------------

    def _score(self, agent: str, wins_by_agent: dict[str, int], clear_fn) -> AgentScore:
        """Build an agent's score: derive (tier, level) from its winning-trade
        count, then apply the clearance check. An un-cleared agent is pinned to
        TRAINEE L1 — you can't climb on a broken configuration."""
        wins = int(wins_by_agent.get(agent, 0))
        tier, level = tier_and_level_from_total_wins(wins)
        score = AgentScore(agent=agent, tier=tier, level=level, wins=wins)

        cleared = clear_fn(score)
        score.cleared = cleared
        if not cleared:
            # Hold a misconfigured agent at the floor regardless of any wins.
            score.tier = Tier.TRAINEE
            score.level = 1
            score.failed("baseline_operational",
                         "agent not cleared — held at Trainee L1 until baseline met")
        return score

    def _wins_by_agent(self) -> dict[str, int]:
        """Successful (winning) closed-trade count credited to each agent.

        The trade ledger attributes trades to the orchestrator (ZEUS) — every
        agent participates in every trade, so the whole team climbs together on
        the system's realized wins. If a per-agent attribution column appears
        later, this is the one place to split it out.
        """
        total_wins = self._winning_trade_count()
        agents = ["zeus", "pythia", "artemis", "apollo",
                  "hades", "icarus", "ares", "argus"]
        return {a: total_wins for a in agents}

    def _winning_trade_count(self) -> int:
        """Closed trades that WON (hit = 1). This is the progression currency —
        you level up on successes, not on signal volume or stubbed metrics."""
        rows = self._sqlite_query("SELECT COUNT(*) FROM trades WHERE hit = 1")
        try:
            return int(rows[0][0]) if rows and rows[0] else 0
        except (TypeError, ValueError, IndexError):
            return 0

    def _closed_trade_count(self) -> int:
        """Closed trades with any realized outcome (hit IS NOT NULL). Kept for
        callers/tests that ask 'how many trades have actually resolved'."""
        rows = self._sqlite_query("SELECT COUNT(*) FROM trades WHERE hit IS NOT NULL")
        try:
            return int(rows[0][0]) if rows and rows[0] else 0
        except (TypeError, ValueError, IndexError):
            return 0

    # ------------------------------------------------------------------
    # Baseline clearance checks — "is this agent even configured to trade?"
    # These no longer grant rank; they only decide whether an agent may climb.
    # ------------------------------------------------------------------

    def _clear_zeus(self, score: AgentScore) -> bool:
        kb_chunks = self._kb_chunk_count()
        decisions = self._decision_count()
        ok = kb_chunks >= 100 and decisions >= 20
        if ok:
            score.passed("kb_min_100_chunks")
            score.passed("pipeline_ran_20_decisions")
        else:
            score.failed("kb_min_100_chunks",         f"have {kb_chunks}/100 — run Apollo research cycle")
            score.failed("pipeline_ran_20_decisions",  f"have {decisions}/20 — run pipeline")
        return ok

    def _clear_pythia(self, score: AgentScore) -> bool:
        context_keys = self._pythia_context_key_count(min_trades=10)
        ok = context_keys >= 5
        if ok:
            score.passed("5_context_keys_learned")
        else:
            score.failed("5_context_keys_learned", f"have {context_keys}/5 — needs more trades")
        return ok

    def _clear_artemis(self, score: AgentScore) -> bool:
        has_fred = self._kb_has_source_type("fred_macro")
        if has_fred:
            score.passed("fred_macro_history_in_kb")
        else:
            score.failed("fred_macro_history_in_kb", "FRED data not ingested — Apollo historical run needed")
        return has_fred

    def _clear_apollo(self, score: AgentScore) -> bool:
        arxiv_papers = self._kb_count_by_type("arxiv")
        ok = arxiv_papers >= 50
        if ok:
            score.passed("50_arxiv_papers_ingested")
        else:
            score.failed("50_arxiv_papers_ingested", f"have {arxiv_papers}/50 — trigger Apollo research cycle")
        return ok

    def _clear_hades(self, score: AgentScore) -> bool:
        ofac_file = Path("agents/hades.py")
        has_ofac = (
            ofac_file.exists()
            and "ofac" in ofac_file.read_text(encoding="utf-8", errors="ignore").lower()
        )
        if has_ofac:
            score.passed("ofac_esg_lksg_compliance_active")
        else:
            score.failed("ofac_esg_lksg_compliance_active", "OFAC logic missing from hades.py")
        return has_ofac

    def _clear_icarus(self, score: AgentScore) -> bool:
        ticker_map_size = self._ticker_map_size()
        dedup_active = self._icarus_dedup_active()
        ok = ticker_map_size >= 20 and dedup_active
        if ok:
            score.passed("ticker_map_20_entries")
            score.passed("deduplication_logic_active")
        else:
            score.failed("ticker_map_20_entries",      f"have {ticker_map_size}/20")
            score.failed("deduplication_logic_active",  "" if dedup_active else "dedup missing")
        return ok

    def _clear_ares(self, score: AgentScore) -> bool:
        ares_file = Path("agents/ares.py")
        ibkr_configured = self._ibkr_port_in_code()
        ok = ares_file.exists() and ibkr_configured
        if ok:
            score.passed("ibkr_integration_present")
            score.passed("bracket_order_logic_deployed")
        else:
            score.failed("ibkr_integration_present",     "" if ares_file.exists() else "ares.py missing")
            score.failed("bracket_order_logic_deployed", "" if ibkr_configured else "IBKR port not configured")
        return ok

    def _clear_argus(self, score: AgentScore) -> bool:
        kill_present = self._argus_kill_switch_present()
        supabase_active = bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY"))
        ok = kill_present and supabase_active
        if ok:
            score.passed("drawdown_kill_switch_active")
            score.passed("portfolio_state_persisted_to_supabase")
        else:
            score.failed("drawdown_kill_switch_active",
                         "" if kill_present else "kill switch missing in argus.py")
            score.failed("portfolio_state_persisted_to_supabase",
                         "" if supabase_active else "SUPABASE env vars not set")
        return ok

    # ------------------------------------------------------------------
    # Promotion detection + alerts (tier OR level increase)
    # ------------------------------------------------------------------

    def _check_promotions(self, scores: dict[str, AgentScore]) -> None:
        for name, score in scores.items():
            prev = self._last_ranks.get(name)
            cur = (score.tier, score.level)
            if prev is not None and cur > prev:
                prev_tier, prev_level = prev
                kind = "TIER PROMOTION" if score.tier > prev_tier else "LEVEL UP"
                msg = (
                    f"PANTHEON {kind}\n"
                    f"{name.upper()}: {prev_tier.label()} L{prev_level} "
                    f"→ {score.tier.label()} L{score.level}"
                )
                logger.info("[SENIORITY] %s", msg)
                if self._alert_fn:
                    try:
                        self._alert_fn(msg)
                    except Exception:
                        pass
            self._last_ranks[name] = cur

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

    def _ibkr_port_in_code(self) -> bool:
        path = Path("agents/ares.py")
        text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
        return any(port in text for port in ("4004", "4001", "4002", "IB_PORT"))

    def _argus_kill_switch_present(self) -> bool:
        path = Path("agents/argus.py")
        if not path.exists():
            return False
        text = path.read_text(encoding="utf-8", errors="ignore")
        return "max_drawdown_pct" in text and "_on_kill" in text

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
