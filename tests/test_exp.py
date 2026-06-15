"""
Quality gate — EXP / level system math (core/exp.py).

Pure-function tests, no DB. Covers the load-bearing invariants:
  - level curve monotonic + worked examples (1..40 over four paper tiers)
  - tier band capping (a Trainee can't show a Senior badge)
  - bar_fill uses the DISPLAYED (capped) level, not raw
  - trade_xp: wins ~3-4x a loss, floored at 0, streak bonus, conviction/size mults
  - r_multiple guards divide-by-zero
  - demotion round-trip: total_xp untouched, badge restores on re-promote
  - real-money track is separate (rollup splits paper vs real)
"""

import core.exp as exp

# ── Level curve ──────────────────────────────────────────────────────────────

class TestLevelCurve:
    def test_l1_is_zero(self):
        assert exp.xp_for_level(1) == 0

    def test_monotonic(self):
        prev = -1
        for L in range(1, exp.MAX_LEVEL + 1):
            v = exp.xp_for_level(L)
            assert v > prev
            prev = v

    def test_uncapped_level_from_xp(self):
        assert exp.exp_level_uncapped(0) == 1
        assert exp.exp_level_uncapped(exp.xp_for_level(2)) == 2
        assert exp.exp_level_uncapped(exp.xp_for_level(10)) == 10
        assert exp.exp_level_uncapped(exp.xp_for_level(10) - 1) == 9

    def test_clamped_to_max(self):
        assert exp.exp_level_uncapped(10**12) == exp.MAX_LEVEL


# ── Tier band capping ────────────────────────────────────────────────────────

class TestBandCapping:
    def test_trainee_caps_at_10(self):
        # raw L11 but TRAINEE (band 1-10) → displayed 10
        assert exp.cap_level_to_rank(11, 0) == 10

    def test_junior_floor_and_cap(self):
        assert exp.cap_level_to_rank(1, 1) == 11    # floor up to band min
        assert exp.cap_level_to_rank(99, 1) == 20   # cap at band max

    def test_intermediate_band(self):
        assert exp.cap_level_to_rank(1, 2) == 21
        assert exp.cap_level_to_rank(99, 2) == 30

    def test_senior_band(self):
        assert exp.cap_level_to_rank(1, 3) == 31
        assert exp.cap_level_to_rank(40, 3) == 40
        assert exp.cap_level_to_rank(99, 3) == 40   # MAX_LEVEL is 40

    def test_max_level_is_40(self):
        assert exp.MAX_LEVEL == 40


# ── bar_fill uses displayed level ────────────────────────────────────────────

class TestBarFill:
    def test_fill_within_range(self):
        xp = exp.xp_for_level(5) + (exp.xp_for_level(6) - exp.xp_for_level(5)) // 2
        f = exp.bar_fill(xp, 5)
        assert 0.4 < f < 0.6

    def test_fill_uses_capped_level_not_raw(self):
        # A TRAINEE agent past raw L11's XP: displayed is capped at 10, and
        # bar_fill must be computed against level 10, not raw 11.
        xp = exp.xp_for_level(11) + 5000
        displayed = exp.cap_level_to_rank(exp.exp_level_uncapped(xp), 0)
        assert displayed == 10
        f = exp.bar_fill(xp, displayed)
        assert f == 1.0  # past the top of its band → full bar, not negative

    def test_max_level_full(self):
        assert exp.bar_fill(10**12, exp.MAX_LEVEL) == 1.0


# ── trade_xp ─────────────────────────────────────────────────────────────────

class TestTradeXp:
    def test_flat_stopout_pays_discipline(self):
        # loss at 1R, 50% conf → still positive (base "showing up" credit)
        xp = exp.trade_xp(won=False, r_multiple=1.0, confidence=0.5, position_pct=0.02, win_streak=0)
        assert 70 <= xp <= 95

    def test_worked_example_2r(self):
        xp = exp.trade_xp(won=True, r_multiple=2.0, confidence=0.7, position_pct=0.02, win_streak=3)
        assert xp == 297

    def test_win_much_bigger_than_loss(self):
        win  = exp.trade_xp(won=True,  r_multiple=2.0, confidence=0.6, position_pct=0.02, win_streak=0)
        loss = exp.trade_xp(won=False, r_multiple=2.0, confidence=0.6, position_pct=0.02, win_streak=0)
        assert win > loss * 2

    def test_never_negative(self):
        xp = exp.trade_xp(won=False, r_multiple=3.0, confidence=0.5, position_pct=0.0, win_streak=0)
        assert xp >= 0

    def test_streak_bonus_caps(self):
        s6 = exp.trade_xp(won=True, r_multiple=1.0, confidence=0.5, position_pct=0.0, win_streak=6)
        s9 = exp.trade_xp(won=True, r_multiple=1.0, confidence=0.5, position_pct=0.0, win_streak=9)
        assert s6 == s9  # streak bonus capped at 5 increments

    def test_conviction_multiplier(self):
        lo = exp.trade_xp(won=True, r_multiple=2.0, confidence=0.5, position_pct=0.02, win_streak=0)
        hi = exp.trade_xp(won=True, r_multiple=2.0, confidence=0.9, position_pct=0.02, win_streak=0)
        assert hi > lo


# ── r_multiple ───────────────────────────────────────────────────────────────

class TestRMultiple:
    def test_from_stop_distance(self):
        # +6% pnl, stop 3% below a 100 fill → risk 3% → R = 2
        assert exp.r_multiple(0.06, 100.0, 97.0, 0.02) == 2.0

    def test_falls_back_to_position_pct(self):
        assert exp.r_multiple(0.04, None, None, 0.02) == 2.0

    def test_guards_zero(self):
        # zero stop distance → fall back, no divide-by-zero
        r = exp.r_multiple(0.05, 100.0, 100.0, 0.025)
        assert r == 2.0


# ── Demotion round-trip ──────────────────────────────────────────────────────

class TestDemotionRoundTrip:
    def test_total_xp_untouched_badge_restores(self):
        xp = exp.xp_for_level(15)  # well into JUNIOR band (11-20)
        at_junior  = exp.cap_level_to_rank(exp.exp_level_uncapped(xp), 1)
        demoted    = exp.cap_level_to_rank(exp.exp_level_uncapped(xp), 0)  # back to TRAINEE cap
        repromoted = exp.cap_level_to_rank(exp.exp_level_uncapped(xp), 1)
        assert demoted == 10               # capped down cosmetically to tier ceiling
        assert at_junior == repromoted     # exact badge restored, XP never changed
        assert repromoted >= 11


# ── Real-money track is separate ──────────────────────────────────────────────

class TestRealMoneyTrack:
    def test_rollup_splits_paper_and_real(self):
        # A Senior agent (tier_int 3) with both paper and real XP.
        state = exp.AgentExpState(
            agent_name="zeus",
            total_xp=exp.xp_for_level(33),       # paper: into Senior band
            seniority_tier_int=3,
            real_money_xp=exp.xp_for_level(3),   # real: separate curve
            real_money_wins=4,
            real_money_losses=1,
        )
        row = state.rollup()
        # Paper level band-capped into Senior (31-40).
        assert 31 <= row["exp_level"] <= 40
        # Real-money bar is independent and uncapped by tier.
        assert row["real_money_level"] == 3
        assert row["real_money_xp"] == exp.xp_for_level(3)
        assert row["real_money_wins"] == 4
        assert row["real_money_losses"] == 1
        assert "_real_money_bar_fill" in row

    def test_real_money_event_types_are_segregated(self):
        assert "real_trade" in exp.REAL_MONEY_EVENT_TYPES
        assert "real_milestone" in exp.REAL_MONEY_EVENT_TYPES
        assert "trade" not in exp.REAL_MONEY_EVENT_TYPES

    def test_real_unlock_milestone_defined(self):
        assert exp.MILESTONE_XP["real_money_unlock"] > 0
