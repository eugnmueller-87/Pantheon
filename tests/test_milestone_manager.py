"""
Quality gate — MilestoneManager (capital preservation + stage progression).

The MilestoneManager is the Iron Law enforcer. A bug here means:
  - Wrong position sizing stage → ZEUS over-risks capital
  - Wrong vault calculation → 30% rule violated
  - Wrong stage detection → trades placed in wrong risk regime

These tests verify stage transitions, vault math, and risk parameters.
"""

import pytest
from core.milestone_manager import MilestoneManager, Stage, STAGES, _stage_for_equity


# ── Stage detection ────────────────────────────────────────────────────────────

class TestStageDetection:
    def test_seed_stage_at_100(self):
        assert _stage_for_equity(100.0) == Stage.SEED

    def test_seed_stage_just_below_sprint(self):
        assert _stage_for_equity(999.99) == Stage.SEED

    def test_sprint_starts_at_1000(self):
        assert _stage_for_equity(1_000.0) == Stage.SPRINT

    def test_scale_starts_at_10000(self):
        assert _stage_for_equity(10_000.0) == Stage.SCALE

    def test_serious_starts_at_100000(self):
        assert _stage_for_equity(100_000.0) == Stage.SERIOUS

    def test_empire_starts_at_1_million(self):
        assert _stage_for_equity(1_000_000.0) == Stage.EMPIRE

    def test_empire_at_large_number(self):
        assert _stage_for_equity(5_000_000.0) == Stage.EMPIRE


# ── Initial state ──────────────────────────────────────────────────────────────

class TestInitialState:
    def test_starts_in_seed_stage(self):
        m = MilestoneManager(starting_equity=100.0)
        assert m.stage == Stage.SEED

    def test_config_matches_stage(self):
        m = MilestoneManager(starting_equity=100.0)
        assert m.config == STAGES[Stage.SEED]

    def test_vault_starts_at_zero(self):
        m = MilestoneManager(starting_equity=100.0)
        assert m.vault_balance == 0.0

    def test_engine_equity_matches_starting(self):
        m = MilestoneManager(starting_equity=100.0)
        assert m.engine_equity == 100.0

    def test_progress_at_start(self):
        m = MilestoneManager(starting_equity=100.0)
        # €100 in SEED (floor=100, target=1000): progress = 0%
        assert m.progress_pct() == 0.0


# ── Update without crossing ────────────────────────────────────────────────────

class TestUpdateNoCrossing:
    def test_update_within_stage_returns_none(self):
        m = MilestoneManager(starting_equity=100.0)
        result = m.update(500.0)
        assert result is None

    def test_update_tracks_current_equity(self):
        m = MilestoneManager(starting_equity=100.0)
        m.update(450.0)
        assert m.engine_equity == 450.0

    def test_vault_unchanged_within_stage(self):
        m = MilestoneManager(starting_equity=100.0)
        m.update(900.0)
        assert m.vault_balance == 0.0

    def test_progress_increases_with_equity(self):
        m = MilestoneManager(starting_equity=100.0)
        m.update(550.0)  # halfway through SEED (100→1000 = span of 900)
        # (550-100)/900 = 50%
        assert m.progress_pct() == pytest.approx(50.0, abs=0.1)


# ── Milestone crossing ─────────────────────────────────────────────────────────

class TestMilestoneCrossing:
    def test_crossing_seed_to_sprint_returns_stage(self):
        m = MilestoneManager(starting_equity=100.0)
        result = m.update(1_000.0)
        assert result == Stage.SPRINT

    def test_crossing_updates_current_stage(self):
        m = MilestoneManager(starting_equity=100.0)
        m.update(1_000.0)
        assert m.stage == Stage.SPRINT

    def test_crossing_calculates_vault_amount(self):
        m = MilestoneManager(starting_equity=100.0)
        m.update(1_000.0)
        # profit = 1000 - 100 = 900, vault = 900 * 0.30 = 270
        assert m.vault_balance == pytest.approx(270.0, abs=0.01)

    def test_vault_pct_is_always_30(self):
        assert MilestoneManager.VAULT_PCT == 0.30

    def test_second_crossing_accumulates_vault(self):
        m = MilestoneManager(starting_equity=100.0)
        m.update(1_000.0)   # SEED→SPRINT: profit=900, vault+=270
        m.update(10_000.0)  # SPRINT→SCALE: profit=9900, vault+=2970
        assert m.vault_balance == pytest.approx(270.0 + 2970.0, abs=1.0)

    def test_same_crossing_not_triggered_twice(self):
        alerts = []
        m = MilestoneManager(starting_equity=100.0, alert_fn=lambda msg: alerts.append(msg))
        m.update(1_000.0)   # first crossing → alert
        m.update(900.0)     # equity drops back
        m.update(1_000.0)   # same crossing — must NOT fire again
        assert len(alerts) == 1

    def test_crossing_fires_alert(self):
        alerts = []
        m = MilestoneManager(starting_equity=100.0, alert_fn=lambda msg: alerts.append(msg))
        m.update(1_000.0)
        assert len(alerts) == 1
        assert "MILESTONE CROSSED" in alerts[0]
        assert "SEED" in alerts[0]
        assert "SPRINT" in alerts[0]

    def test_crossing_alert_mentions_vault_amount(self):
        alerts = []
        m = MilestoneManager(starting_equity=100.0, alert_fn=lambda msg: alerts.append(msg))
        m.update(1_000.0)
        # Alert must tell user how much to move to vault
        assert "270" in alerts[0]

    def test_alert_contains_confirm_vault(self):
        alerts = []
        m = MilestoneManager(starting_equity=100.0, alert_fn=lambda msg: alerts.append(msg))
        m.update(1_000.0)
        assert "/confirm_vault" in alerts[0]

    def test_no_vault_if_no_profit(self):
        """Starting at 1000, crossing to sprint with 0 profit → 0 vault."""
        m = MilestoneManager(starting_equity=1_000.0)
        m.update(1_001.0)  # still SPRINT — no crossing
        assert m.vault_balance == 0.0


# ── Risk parameters per stage ──────────────────────────────────────────────────

class TestStageRiskParams:
    def test_seed_max_position_1pct(self):
        assert STAGES[Stage.SEED].max_position_pct == 0.01

    def test_sprint_max_position_2pct(self):
        assert STAGES[Stage.SPRINT].max_position_pct == 0.02

    def test_scale_max_position_3pct(self):
        assert STAGES[Stage.SCALE].max_position_pct == 0.03

    def test_serious_max_position_5pct(self):
        assert STAGES[Stage.SERIOUS].max_position_pct == 0.05

    def test_empire_max_position_3pct(self):
        # Empire tightens position size back down
        assert STAGES[Stage.EMPIRE].max_position_pct == 0.03

    def test_seed_kill_switch_5pct(self):
        assert STAGES[Stage.SEED].drawdown_kill_pct == 0.05

    def test_seed_no_leverage(self):
        assert STAGES[Stage.SEED].leverage == 1.0

    def test_sprint_no_leverage(self):
        assert STAGES[Stage.SPRINT].leverage == 1.0

    def test_scale_has_leverage(self):
        assert STAGES[Stage.SCALE].leverage > 1.0

    def test_seed_tier_1_only(self):
        assert STAGES[Stage.SEED].allowed_tiers == [1]

    def test_sprint_allows_tier_2(self):
        assert 2 in STAGES[Stage.SPRINT].allowed_tiers

    def test_seed_does_not_allow_tier_2(self):
        assert 2 not in STAGES[Stage.SEED].allowed_tiers

    def test_empire_kill_switch_tightens(self):
        # EMPIRE must be tighter than SERIOUS — protect the empire
        assert STAGES[Stage.EMPIRE].drawdown_kill_pct < STAGES[Stage.SERIOUS].drawdown_kill_pct

    def test_all_stages_have_30pct_vault(self):
        for stage, cfg in STAGES.items():
            assert cfg.vault_pct == 0.30, f"{stage.value} has wrong vault_pct"


# ── Status dict ───────────────────────────────────────────────────────────────

class TestStatusDict:
    def test_status_dict_has_required_keys(self):
        m = MilestoneManager(starting_equity=100.0)
        s = m.status_dict()
        required = {
            "stage", "label", "engine_equity", "vault_balance",
            "total_vaulted", "progress_pct", "target",
            "max_position_pct", "drawdown_kill_pct", "leverage",
            "allowed_tiers", "max_open_positions",
        }
        assert required.issubset(s.keys())

    def test_status_dict_stage_is_string(self):
        m = MilestoneManager(starting_equity=100.0)
        assert isinstance(m.status_dict()["stage"], str)

    def test_status_dict_equity_is_float(self):
        m = MilestoneManager(starting_equity=100.0)
        assert isinstance(m.status_dict()["engine_equity"], float)


# ── Config property ────────────────────────────────────────────────────────────

class TestConfigProperty:
    def test_config_updates_after_crossing(self):
        m = MilestoneManager(starting_equity=100.0)
        assert m.config.max_position_pct == 0.01    # SEED
        m.update(1_000.0)
        assert m.config.max_position_pct == 0.02    # SPRINT

    def test_config_returns_correct_allowed_tiers_after_crossing(self):
        m = MilestoneManager(starting_equity=100.0)
        m.update(1_000.0)   # SPRINT
        assert m.config.allowed_tiers == [1, 2]
