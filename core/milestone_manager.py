"""
Pantheon OS — MilestoneManager

The Iron Law: Never lose everything. Always protect the base.

Tracks equity milestones, automatically adjusts:
  - Pythia's position sizing limits
  - Argus's drawdown kill switch
  - Vault transfer alerts via Telegram

Milestone stages:
  SEED    €0       → €1,000    learn, prove the system
  SPRINT  €1,000   → €10,000   build confidence
  SCALE   €10,000  → €100,000  controlled growth
  SERIOUS €100,000 → €1,000,000 institutional grade
  EMPIRE  €1,000,000+           compound machines

Vault rule (hardcoded, never changes):
  - 30% of profits at each milestone crossing → Vault alert
  - Engine always keeps enough to trade
  - Vault money is never risked
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger("milestone_manager")


class Stage(Enum):
    SEED    = "SEED"      # €100    → €1,000
    SPRINT  = "SPRINT"    # €1,000  → €10,000
    SCALE   = "SCALE"     # €10,000 → €100,000
    SERIOUS = "SERIOUS"   # €100,000→ €1,000,000
    EMPIRE  = "EMPIRE"    # €1,000,000+


@dataclass
class StageConfig:
    """Risk parameters that Pythia + Argus read at runtime."""
    stage:              Stage
    equity_floor:       float   # minimum equity for this stage
    equity_target:      float   # milestone target
    max_position_pct:   float   # max % of Engine per trade
    drawdown_kill_pct:  float   # Argus emergency halt threshold
    max_open_positions: int     # concurrent open trades
    leverage:           float   # 1.0 = no leverage
    allowed_tiers:      list    # conviction tiers allowed (1=strongest)
    vault_pct:          float   # % of profits to vault at milestone crossing
    label:              str     # human-readable

    def describe(self) -> str:
        return (
            f"{self.label} | "
            f"max_pos={self.max_position_pct*100:.1f}% | "
            f"kill={self.drawdown_kill_pct*100:.0f}% DD | "
            f"leverage={self.leverage}x | "
            f"tiers={self.allowed_tiers}"
        )


# ── Stage definitions — the full playbook ──────────────────────────────────

STAGES: dict[Stage, StageConfig] = {
    Stage.SEED: StageConfig(
        stage              = Stage.SEED,
        equity_floor       = 100.0,
        equity_target      = 1_000.0,
        max_position_pct   = 0.01,      # 1% max — at €100 that's €1
        drawdown_kill_pct  = 0.05,      # tightest kill switch — 5%
        max_open_positions = 3,
        leverage           = 1.0,       # NO leverage at seed
        allowed_tiers      = [1],       # STRONG signals only
        vault_pct          = 0.30,      # 30% to vault at milestone
        label              = "🌱 SEED €100 → €1,000",
    ),
    Stage.SPRINT: StageConfig(
        stage              = Stage.SPRINT,
        equity_floor       = 1_000.0,
        equity_target      = 10_000.0,
        max_position_pct   = 0.02,      # 2% max
        drawdown_kill_pct  = 0.06,
        max_open_positions = 5,
        leverage           = 1.0,       # still no leverage
        allowed_tiers      = [1, 2],    # STRONG + MODERATE
        vault_pct          = 0.30,
        label              = "🔥 SPRINT €1,000 → €10,000",
    ),
    Stage.SCALE: StageConfig(
        stage              = Stage.SCALE,
        equity_floor       = 10_000.0,
        equity_target      = 100_000.0,
        max_position_pct   = 0.03,      # 3% max
        drawdown_kill_pct  = 0.07,
        max_open_positions = 8,
        leverage           = 1.5,       # careful 1.5x on tier-1 only
        allowed_tiers      = [1, 2],
        vault_pct          = 0.30,
        label              = "💎 SCALE €10,000 → €100,000",
    ),
    Stage.SERIOUS: StageConfig(
        stage              = Stage.SERIOUS,
        equity_floor       = 100_000.0,
        equity_target      = 1_000_000.0,
        max_position_pct   = 0.05,      # 5% max
        drawdown_kill_pct  = 0.08,
        max_open_positions = 10,
        leverage           = 2.0,       # 2x on tier-1 only
        allowed_tiers      = [1, 2, 3],
        vault_pct          = 0.30,
        label              = "🏦 SERIOUS €100,000 → €1,000,000",
    ),
    Stage.EMPIRE: StageConfig(
        stage              = Stage.EMPIRE,
        equity_floor       = 1_000_000.0,
        equity_target      = float("inf"),
        max_position_pct   = 0.03,      # tighter at large scale
        drawdown_kill_pct  = 0.05,      # tighter kill — protect the empire
        max_open_positions = 15,
        leverage           = 2.0,
        allowed_tiers      = [1, 2, 3],
        vault_pct          = 0.30,
        label              = "🚀 EMPIRE €1,000,000+",
    ),
}


def _stage_for_equity(equity: float) -> Stage:
    if equity >= 1_000_000: return Stage.EMPIRE
    if equity >= 100_000:   return Stage.SERIOUS
    if equity >= 10_000:    return Stage.SCALE
    if equity >= 1_000:     return Stage.SPRINT
    return Stage.SEED


class MilestoneManager:
    """
    Single source of truth for the current growth stage.
    Pythia and Argus read from this at every pipeline cycle.
    """

    VAULT_PCT = 0.30   # Iron Law — never changes

    def __init__(self, account_equity: float, alert_fn=None, persist: bool = True):
        """
        account_equity — the CURRENT account balance (single source of truth,
        the same value used for sizing). The stage/tier band derives from this
        live figure, so it tracks the real balance and graduates as it grows.

        The vault-profit ORIGIN is a separate, persisted baseline (captured once
        and reloaded on boot) so cumulative profit survives restarts — it is NOT
        the current equity and never drives the stage gate. See milestone_state.

        Fails CLOSED: a missing/invalid account_equity raises, rather than
        silently assuming a balance (a money-sizing system must not guess).
        """
        if account_equity is None or account_equity <= 0:
            raise ValueError(
                "MilestoneManager requires a positive account_equity — refusing "
                "to default a balance for a money-sizing system (fail closed)."
            )

        self._current_equity   = account_equity
        self._peak_equity      = account_equity
        self._alert_fn         = alert_fn   # Telegram/Argus alert function
        self._persist          = persist
        # Stage derives from CURRENT equity — never a frozen starting value.
        self._current_stage    = _stage_for_equity(account_equity)

        # Vault origin + accounting: load persisted state (capture-once), else
        # seed the origin to the current equity and persist it now.
        loaded = self._load_state() if persist else None
        if loaded:
            self._milestone_origin = float(loaded["milestone_origin"])
            self._vault_balance    = float(loaded.get("vault_balance", 0.0))
            self._total_vaulted    = float(loaded.get("total_vaulted", 0.0))
            self._crossed_stages   = {
                Stage(s) for s in (loaded.get("crossed_stages") or []) if s in Stage._value2member_map_
            }
            logger.info(
                "[MILESTONE] Loaded persisted state — origin=€%.2f vault=€%.2f equity=€%.2f stage=%s",
                self._milestone_origin, self._vault_balance, account_equity, self._current_stage.value,
            )
        else:
            self._milestone_origin = account_equity   # capture once
            self._vault_balance    = 0.0
            self._total_vaulted    = 0.0
            self._crossed_stages   = set()
            self._save_state()
            logger.info(
                "[MILESTONE] Initialised — origin captured €%.2f equity=€%.2f stage=%s",
                self._milestone_origin, account_equity, self._current_stage.value,
            )

    # ── Public API ──────────────────────────────────────────────────────────

    def update(self, equity: float) -> Optional[Stage]:
        """
        Call this every time Argus refreshes portfolio state.
        Returns the new Stage if a forward milestone was just crossed, else None.

        Equity drops (drawdowns) silently revert the current stage but never
        fire a vault alert — you don't get congratulated for losing money.
        """
        self._current_equity = equity
        if equity > self._peak_equity:
            self._peak_equity = equity

        new_stage = _stage_for_equity(equity)
        if new_stage == self._current_stage:
            return None

        # Upward crossing: equity rose into a new stage we haven't seen yet
        stage_order = list(Stage)
        is_forward = stage_order.index(new_stage) > stage_order.index(self._current_stage)
        if is_forward and new_stage not in self._crossed_stages:
            return self._handle_milestone_crossing(new_stage, equity)

        # Downward or already-crossed: silently adjust stage, no alert
        self._current_stage = new_stage
        return None

    @property
    def config(self) -> StageConfig:
        """Current risk parameters — Pythia and Argus read this."""
        return STAGES[self._current_stage]

    @property
    def stage(self) -> Stage:
        return self._current_stage

    @property
    def vault_balance(self) -> float:
        return self._vault_balance

    @property
    def engine_equity(self) -> float:
        return self._current_equity

    def progress_pct(self) -> float:
        """How far through the current stage (0–100%)."""
        cfg = self.config
        if cfg.equity_target == float("inf"):
            return 100.0
        span = cfg.equity_target - cfg.equity_floor
        done = self._current_equity - cfg.equity_floor
        return round(min(max(done / span * 100, 0), 100), 1)

    def status_dict(self) -> dict:
        cfg = self.config
        return {
            "stage":             self._current_stage.value,
            "label":             cfg.label,
            "engine_equity":     round(self._current_equity, 2),
            "vault_balance":     round(self._vault_balance, 2),
            "total_vaulted":     round(self._total_vaulted, 2),
            "progress_pct":      self.progress_pct(),
            "target":            cfg.equity_target,
            "max_position_pct":  cfg.max_position_pct,
            "drawdown_kill_pct": cfg.drawdown_kill_pct,
            "leverage":          cfg.leverage,
            "allowed_tiers":     cfg.allowed_tiers,
            "max_open_positions":cfg.max_open_positions,
        }

    # ── Internal ────────────────────────────────────────────────────────────

    def _handle_milestone_crossing(self, new_stage: Stage, equity: float) -> Stage:
        old_stage = self._current_stage
        self._current_stage = new_stage
        self._crossed_stages.add(new_stage)

        # Calculate vault transfer from the persisted origin baseline.
        # TODO(cost-basis): vault profit assumes no capital flows. Once real
        # deposits/withdrawals exist, track net_contributions so a deposit raises
        # basis instead of being skimmed as profit (profit = current - net_contrib).
        profit        = max(0.0, equity - self._milestone_origin)
        vault_amount  = round(profit * self.VAULT_PCT, 2)
        self._vault_balance  += vault_amount
        self._total_vaulted  += vault_amount
        self._save_state()   # persist the new vault totals + crossed stage

        msg = (
            f"🏆 MILESTONE CROSSED: {old_stage.value} → {new_stage.value}\n"
            f"Engine equity: €{equity:,.2f}\n"
            f"Profit so far: €{profit:,.2f}\n"
            f"▶ Transfer €{vault_amount:,.2f} to Vault now (30% of profit)\n"
            f"Vault total after transfer: €{self._vault_balance:,.2f}\n"
            f"New stage rules: {STAGES[new_stage].describe()}\n"
            f"Reply /confirm_vault when done."
        )

        logger.critical("[MILESTONE] %s", msg)
        if self._alert_fn:
            try:
                self._alert_fn(msg)
            except Exception as exc:
                logger.warning("[MILESTONE] Alert failed: %s", exc)

        return new_stage

    # ── Persistence (vault origin + accounting survive restarts) ──────────────

    def _load_state(self) -> Optional[dict]:
        """Load the persisted singleton state. None → no stored origin yet.
        Best-effort: any persistence error degrades to in-memory (None)."""
        try:
            import core.supabase_client as supa
            return supa.fetch_milestone_state()
        except Exception:
            return None

    def _save_state(self) -> None:
        """Persist the capture-once origin + vault accounting. Best-effort —
        never let a persistence failure interrupt the trading loop."""
        if not self._persist:
            return
        try:
            import core.supabase_client as supa
            supa.upsert_milestone_state({
                "milestone_origin": self._milestone_origin,
                "vault_balance":    self._vault_balance,
                "total_vaulted":    self._total_vaulted,
                "crossed_stages":   [s.value for s in self._crossed_stages],
            })
        except Exception as exc:
            logger.warning("[MILESTONE] persist failed: %s", exc)
