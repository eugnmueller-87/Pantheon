"""
EXP / level system — a COSMETIC motivation layer over the seniority gate.

Golden rule: EXP never affects position sizing or live-trading authority. The
authoritative gate is the (tier, level) in core/seniority.py, derived from the
real winning-trade count. This module only computes and stores flair (XP bars,
streaks) for the dashboard.

Two independent XP tracks (the user's design):

  PAPER track — the climb to Senior.
    Four tiers (TRAINEE, JUNIOR, INTERMEDIATE, SENIOR), each 10 levels, each
    level = 10 successful trades. The displayed paper level is band-capped into
    the agent's authoritative seniority tier so flair never outruns the gate
    (a Trainee can't show a Senior badge no matter how much raw XP it banked).
    Levels run 1..40 (10 per tier).

  REAL-MONEY track — begins only once the agent is Senior and real money is
    armed. A SEPARATE ledger and a SEPARATE bar, leveling on real-money
    performance. It does not interact with the paper climb at all.

  - XP is minted only on realized outcomes (closed trades) + role events.
  - Lifetime XP is monotonic (a trade award is floored at 0).
  - Awards are idempotent on a deterministic source_ref.

The pure functions here are unit-tested in tests/test_exp.py with no DB.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("exp")

# ── Level curve ──────────────────────────────────────────────────────────────
# Four paper tiers × 10 levels = 40 levels on the paper climb.
LEVELS_PER_TIER = 10
PAPER_TIERS = 4
MAX_LEVEL = LEVELS_PER_TIER * PAPER_TIERS   # 40


def xp_for_level(level: int) -> int:
    """Cumulative XP required to REACH `level`. L1=500, L2≈1515, L10≈19905."""
    if level <= 1:
        return 0
    return round(500 * (level ** 1.6))


def exp_level_uncapped(total_xp: float) -> int:
    """Largest level L (1..MAX_LEVEL) whose threshold <= total_xp."""
    lvl = 1
    for L in range(2, MAX_LEVEL + 1):
        if total_xp >= xp_for_level(L):
            lvl = L
        else:
            break
    return lvl


# Seniority TIER → (min displayed level on entry, max displayed level).
# Mirrors core/seniority.py Tier: TRAINEE=0, JUNIOR=1, INTERMEDIATE=2, SENIOR=3.
# Each tier owns a contiguous block of 10 levels on the 1..40 paper ladder.
RANK_BANDS = {
    0: (1, 10),    # TRAINEE
    1: (11, 20),   # JUNIOR
    2: (21, 30),   # INTERMEDIATE
    3: (31, 40),   # SENIOR
}


def cap_level_to_rank(raw_level: int, tier_int: int) -> int:
    """Clamp the raw curve level into the agent's seniority-tier band, so EXP
    flair never outruns the authoritative tier gate."""
    lo, hi = RANK_BANDS.get(int(tier_int), (1, MAX_LEVEL))
    return max(lo, min(raw_level, hi))


def bar_fill(total_xp: float, displayed_level: int) -> float:
    """Fraction 0..1 through the DISPLAYED level (uses the capped level so the
    fill, the L{n} label, and the rendered tier never disagree)."""
    if displayed_level >= MAX_LEVEL:
        return 1.0
    base = xp_for_level(displayed_level)
    nxt = xp_for_level(displayed_level + 1)
    span = nxt - base
    if span <= 0:
        return 0.0
    return max(0.0, min(1.0, (total_xp - base) / span))


# ── Trade XP formula ─────────────────────────────────────────────────────────
def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(v, hi))


def trade_xp(
    *,
    won: bool,
    r_multiple: float,
    confidence: float,
    position_pct: float,
    win_streak: int,
) -> int:
    """XP for one closed trade. position_pct is a FRACTION (0.03 = 3%), verified
    against ares.py (account_val * position_size_pct). Floored at 0 so lifetime
    XP never drops."""
    base_xp = 100
    if won:
        outcome_xp = round(60 * _clamp(r_multiple, 0, 5))
    else:
        outcome_xp = -round(20 * _clamp(abs(r_multiple), 0, 3))
    conviction_mult = 1 + 0.5 * (confidence - 0.5)         # 0.75 .. 1.25
    size_mult = 1 + position_pct                            # 1.00 .. 1.05
    streak_bonus = min(max(win_streak - 1, 0), 5) * 25 if won else 0
    return max(0, round((base_xp + outcome_xp) * conviction_mult * size_mult) + streak_bonus)


def r_multiple(pnl_pct: float, fill_price: Optional[float], stop_loss: Optional[float],
               position_pct: float) -> float:
    """R = pnl_pct / risk_pct. risk_pct from the stop distance, falling back to
    position_pct when no stop is set. Guards divide-by-zero."""
    risk_pct = None
    if fill_price and stop_loss and fill_price > 0:
        risk_pct = abs(fill_price - stop_loss) / fill_price
    if not risk_pct or risk_pct <= 0:
        risk_pct = position_pct if position_pct > 0 else 0.03
    return pnl_pct / risk_pct if risk_pct else 0.0


# Milestone / role award amounts (one-time use deterministic source_ref).
MILESTONE_XP = {
    "first_live_trade": 500,
    "trades_50":        400,
    "equity_peak":      300,
    "promotion":        1000,   # the RANK UP jackpot (paper tier/level up)
    "real_money_unlock": 2000,  # reaching Senior — the big one
}

# Event types that live on the SEPARATE real-money ledger. Real-money XP is
# tracked under these so it never mixes with the paper climb.
REAL_MONEY_EVENT_TYPES = {"real_trade", "real_milestone"}


@dataclass
class AgentExpState:
    """Derived rollup for one agent — what gets upserted to agent_exp.

    `seniority_tier_int` is the authoritative tier (0..3) used to band-cap the
    displayed paper level. `real_money_xp` is the independent real-money track,
    which only starts accruing after Senior + arm."""
    agent_name: str
    total_xp: int = 0
    seniority_tier_int: int = 0           # 0..3 (TRAINEE..SENIOR) — caps paper level
    lifetime_wins: int = 0
    lifetime_losses: int = 0
    current_win_streak: int = 0
    best_win_streak: int = 0
    progress_to_next_pct: float = 0.0
    real_money_xp: int = 0               # separate track — real capital only
    real_money_wins: int = 0
    real_money_losses: int = 0
    metadata: dict = field(default_factory=dict)

    def rollup(self) -> dict:
        raw = exp_level_uncapped(self.total_xp)
        displayed = cap_level_to_rank(raw, self.seniority_tier_int)
        fill = bar_fill(self.total_xp, displayed)
        base = xp_for_level(displayed)
        nxt = xp_for_level(displayed + 1)

        # Real-money bar — independent curve, no tier cap (the agent is already
        # Senior to be here at all).
        rm_level = exp_level_uncapped(self.real_money_xp)
        rm_fill = bar_fill(self.real_money_xp, rm_level)
        return {
            "agent_name":           self.agent_name,
            "total_xp":             int(self.total_xp),
            "exp_level":            displayed,
            "xp_into_level":        int(self.total_xp - base),
            "xp_to_next":           max(0, nxt - base),
            "progress_to_next_pct": round(self.progress_to_next_pct, 2),
            "lifetime_wins":        self.lifetime_wins,
            "lifetime_losses":      self.lifetime_losses,
            "current_win_streak":   self.current_win_streak,
            "best_win_streak":      self.best_win_streak,
            "seniority_level_int":  self.seniority_tier_int,   # stored column name kept
            "real_money_xp":        int(self.real_money_xp),
            "real_money_level":     rm_level,
            "real_money_wins":      self.real_money_wins,
            "real_money_losses":    self.real_money_losses,
            # bar fills exposed for the dashboard; not stored columns but handy
            "_bar_fill":            round(fill, 4),
            "_real_money_bar_fill": round(rm_fill, 4),
        }


def award_xp(agent_name: str, event_type: str, xp: int, source_ref: str,
             metadata: Optional[dict] = None) -> bool:
    """Append one idempotent XP award to agent_exp_ledger, then recompute the
    agent_exp rollup. Returns True if the award was newly inserted, False if it
    was a duplicate (idempotency hit) or persistence is unavailable.

    Idempotency relies on UNIQUE(agent_name, event_type, source_ref) — the DB
    rejects replays; we treat a unique-violation as a no-op.
    """
    try:
        import core.supabase_client as supa
    except Exception:
        return False

    inserted = supa.insert_exp_ledger({
        "agent_name": agent_name,
        "event_type": event_type,
        "xp":         int(xp),
        "source_ref": source_ref,
        "metadata":   metadata or {},
    })
    if inserted:
        try:
            recompute_agent_exp(agent_name)
        except Exception as exc:
            logger.warning("[EXP] recompute failed for %s: %s", agent_name, exc)
    return inserted


def recompute_agent_exp(agent_name: str) -> Optional[dict]:
    """Re-derive the agent_exp rollup from the full ledger + current seniority.

    Streak is computed from trade-close events ordered by their close time
    (carried in ledger metadata as closed_at) — closes can arrive out of order,
    so we sort rather than trust arrival order.
    """
    import core.supabase_client as supa

    ledger = supa.fetch_exp_ledger(agent_name)
    if ledger is None:
        return None

    # Split the ledger: paper track vs the SEPARATE real-money track.
    paper = [e for e in ledger if e.get("event_type") not in REAL_MONEY_EVENT_TYPES]
    real  = [e for e in ledger if e.get("event_type") in REAL_MONEY_EVENT_TYPES]

    total_xp      = sum(int(e.get("xp", 0)) for e in paper)
    real_money_xp = sum(int(e.get("xp", 0)) for e in real)

    # Paper win/loss + streak from "trade" events, ordered by closed_at.
    trade_events = [e for e in paper if e.get("event_type") == "trade"]
    trade_events.sort(key=lambda e: (e.get("metadata") or {}).get("closed_at") or e.get("created_at") or "")
    wins = losses = streak = best = 0
    for e in trade_events:
        won = bool((e.get("metadata") or {}).get("won"))
        if won:
            wins += 1
            streak += 1
            best = max(best, streak)
        else:
            losses += 1
            streak = 0

    # Real-money win/loss (no streak band — its own simple tally).
    rm_wins = sum(1 for e in real
                  if e.get("event_type") == "real_trade" and (e.get("metadata") or {}).get("won"))
    rm_losses = sum(1 for e in real
                    if e.get("event_type") == "real_trade" and not (e.get("metadata") or {}).get("won"))

    # Authoritative tier (0..3) caps the displayed paper level.
    tier_int = supa.fetch_agent_seniority_level(agent_name) or 0

    state = AgentExpState(
        agent_name=agent_name,
        total_xp=total_xp,
        seniority_tier_int=tier_int,
        lifetime_wins=wins,
        lifetime_losses=losses,
        current_win_streak=streak,
        best_win_streak=best,
        progress_to_next_pct=supa.fetch_agent_progress_pct(agent_name) or 0.0,
        real_money_xp=real_money_xp,
        real_money_wins=rm_wins,
        real_money_losses=rm_losses,
    )
    row = state.rollup()
    supa.upsert_agent_exp({k: v for k, v in row.items() if not k.startswith("_")})
    return row
