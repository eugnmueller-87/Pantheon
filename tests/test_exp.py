"""
Quality gate — EXP / level system math (core/exp.py).

Pure-function tests, no DB. Covers the load-bearing invariants from
docs/DASHBOARD_PLAN.md §2:
  - level curve monotonic + worked examples
  - band capping (Senior agent can't show Director flair)
  - bar_fill uses the DISPLAYED (capped) level, not raw
  - trade_xp: wins ~3-4x a loss, floored at 0, streak bonus, conviction/size mults
  - r_multiple guards divide-by-zero
  - demotion round-trip: total_xp untouched, badge restores on re-promote
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


# ── Band capping ─────────────────────────────────────────────────────────────

class TestBandCapping:
    def test_senior_caps_at_9(self):
        # raw L10 but SENIOR (band 1-9) → displayed 9
        assert exp.cap_level_to_rank(10, 0) == 9

    def test_principal_floor_and_cap(self):
        assert exp.cap_level_to_rank(1, 1) == 10    # floor up to band min
        assert exp.cap_level_to_rank(99, 1) == 24   # cap at band max

    def test_director_band(self):
        assert exp.cap_level_to_rank(1, 3) == 40
        assert exp.cap_level_to_rank(50, 3) == 50


# ── bar_fill uses displayed level ────────────────────────────────────────────

class TestBarFill:
    def test_fill_within_range(self):
        xp = exp.xp_for_level(5) + (exp.xp_for_level(6) - exp.xp_for_level(5)) // 2
        f = exp.bar_fill(xp, 5)
        assert 0.4 < f < 0.6

    def test_fill_uses_capped_level_not_raw(self):
        # A SENIOR agent past raw L10's XP: displayed is capped at 9, and
        # bar_fill must be computed against level 9, not raw 10.
        xp = exp.xp_for_level(10) + 5000
        displayed = exp.cap_level_to_rank(exp.exp_level_uncapped(xp), 0)
        assert displayed == 9
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
        xp = exp.xp_for_level(12)  # well into PRINCIPAL band
        at_principal = exp.cap_level_to_rank(exp.exp_level_uncapped(xp), 1)
        demoted      = exp.cap_level_to_rank(exp.exp_level_uncapped(xp), 0)  # back to SENIOR cap
        repromoted   = exp.cap_level_to_rank(exp.exp_level_uncapped(xp), 1)
        assert demoted == 9              # capped down cosmetically
        assert at_principal == repromoted  # exact badge restored, XP never changed
        assert repromoted >= 10
