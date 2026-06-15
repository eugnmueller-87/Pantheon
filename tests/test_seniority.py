"""
Tests for core/seniority.py — Agent Seniority Framework (tier × level model)

Structure: agents EARN their way up. Floor is TRAINEE L1 (zero experience).
Tiers: TRAINEE → JUNIOR → INTERMEDIATE → SENIOR. Each tier has 10 levels;
10 successful (winning) trades advance one level; 10 levels advance a tier.
Real money unlocks at the SENIOR tier AND only when an operator arms it.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import pytest

from core.seniority import (
    WINS_PER_LEVEL,
    WINS_PER_TIER,
    SeniorityEvaluator,
    Tier,
    level_from_wins,
    level_progress_pct,
    real_money_armed,
    real_money_live,
    tier_and_level_from_total_wins,
    wins_to_next_level,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _disarm(monkeypatch):
    """Real money is disarmed by default in every test unless explicitly set."""
    monkeypatch.delenv("ARM_REAL_MONEY", raising=False)


@pytest.fixture
def tmp_db(tmp_path):
    db = tmp_path / "trade_log.db"
    with sqlite3.connect(db) as conn:
        conn.execute("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                context_key TEXT, category TEXT, regime TEXT, vix_band TEXT,
                confidence REAL, position_pct REAL, symbol TEXT, side TEXT,
                fill_price REAL, pnl_pct REAL, hit INTEGER, recorded_at TEXT
            )
        """)
        conn.commit()
    return db


@pytest.fixture
def tmp_skills(tmp_path):
    (tmp_path / "agents").mkdir()
    return tmp_path


def make_evaluator(tmp_db, tmp_skills, kb=None):
    return SeniorityEvaluator(kb=kb, db_path=tmp_db, skills_dir=tmp_skills / "agents")


def mock_kb(knowledge_count=0, decision_count=0, source_types=None):
    kb = MagicMock()
    kb._knowledge_col.count.return_value = knowledge_count
    kb._decisions_col.count.return_value = decision_count

    def get_by_type(where=None, include=None):
        t = (where or {}).get("type", "")
        ids = ["x"] if (source_types and t in source_types) else []
        return {"ids": ids}

    kb._knowledge_col.get.side_effect = get_by_type
    kb._decisions_col.get.return_value = {"ids": ["x"] * decision_count}
    return kb


def _insert_wins(db, n):
    with sqlite3.connect(db) as conn:
        for i in range(n):
            conn.execute("INSERT INTO trades (symbol, side, hit, pnl_pct) VALUES (?,?,?,?)",
                         (f"W{i}", "BUY", 1, 0.02))
        conn.commit()


def _insert_losses(db, n):
    with sqlite3.connect(db) as conn:
        for i in range(n):
            conn.execute("INSERT INTO trades (symbol, side, hit, pnl_pct) VALUES (?,?,?,?)",
                         (f"L{i}", "BUY", 0, -0.01))
        conn.commit()


# ---------------------------------------------------------------------------
# Tier enum — Trainee is the floor, Senior is the top
# ---------------------------------------------------------------------------

def test_tier_ordering():
    assert Tier.TRAINEE < Tier.JUNIOR < Tier.INTERMEDIATE < Tier.SENIOR


def test_tier_labels():
    assert Tier.TRAINEE.label()      == "Trainee"
    assert Tier.JUNIOR.label()       == "Junior"
    assert Tier.INTERMEDIATE.label() == "Intermediate"
    assert Tier.SENIOR.label()       == "Senior"


def test_position_caps_grow_with_tier():
    assert Tier.TRAINEE.max_position_pct()      == 0.01
    assert Tier.JUNIOR.max_position_pct()       == 0.02
    assert Tier.INTERMEDIATE.max_position_pct() == 0.03
    assert Tier.SENIOR.max_position_pct()       == 0.03


def test_only_senior_unlocks_real_money():
    assert not Tier.TRAINEE.real_money_unlocked()
    assert not Tier.JUNIOR.real_money_unlocked()
    assert not Tier.INTERMEDIATE.real_money_unlocked()
    assert Tier.SENIOR.real_money_unlocked()


# ---------------------------------------------------------------------------
# Progression math — 10 wins/level, 10 levels/tier
# ---------------------------------------------------------------------------

def test_zero_wins_is_trainee_l1():
    assert tier_and_level_from_total_wins(0) == (Tier.TRAINEE, 1)


def test_level_advances_every_10_wins():
    assert level_from_wins(0) == 1
    assert level_from_wins(9) == 1
    assert level_from_wins(10) == 2
    assert level_from_wins(95) == 10
    assert level_from_wins(999) == 10   # capped at 10 within a tier


def test_tier_advances_every_100_wins():
    assert tier_and_level_from_total_wins(99)  == (Tier.TRAINEE, 10)
    assert tier_and_level_from_total_wins(100) == (Tier.JUNIOR, 1)
    assert tier_and_level_from_total_wins(200) == (Tier.INTERMEDIATE, 1)
    assert tier_and_level_from_total_wins(300) == (Tier.SENIOR, 1)


def test_senior_saturates_at_l10():
    assert tier_and_level_from_total_wins(390) == (Tier.SENIOR, 10)
    assert tier_and_level_from_total_wins(400) == (Tier.SENIOR, 10)
    assert tier_and_level_from_total_wins(99999) == (Tier.SENIOR, 10)


def test_constants_are_consistent():
    assert WINS_PER_TIER == WINS_PER_LEVEL * 10
    assert WINS_PER_LEVEL == 10


def test_wins_to_next_level():
    assert wins_to_next_level(0) == 10
    assert wins_to_next_level(7) == 3
    assert wins_to_next_level(10) == 10
    assert wins_to_next_level(400) == 0   # maxed


def test_level_progress_pct():
    assert level_progress_pct(0) == 0.0
    assert level_progress_pct(5) == 0.5
    assert level_progress_pct(400) == 1.0


# ---------------------------------------------------------------------------
# Real-money arming gate
# ---------------------------------------------------------------------------

def test_disarmed_by_default():
    assert real_money_armed() is False
    assert real_money_live(Tier.SENIOR) is False


def test_arm_flag_required_and_senior_required(monkeypatch):
    monkeypatch.setenv("ARM_REAL_MONEY", "true")
    assert real_money_armed() is True
    assert real_money_live(Tier.SENIOR) is True
    # Senior is necessary too — arming a Junior team does NOT enable real money.
    assert real_money_live(Tier.JUNIOR) is False
    assert real_money_live(Tier.INTERMEDIATE) is False


def test_arm_flag_accepts_truthy_strings(monkeypatch):
    for val in ("true", "1", "yes", "TRUE", "Yes"):
        monkeypatch.setenv("ARM_REAL_MONEY", val)
        assert real_money_armed() is True
    for val in ("false", "0", "no", ""):
        monkeypatch.setenv("ARM_REAL_MONEY", val)
        assert real_money_armed() is False


# ---------------------------------------------------------------------------
# Evaluator — agents start at the floor and climb on real wins
# ---------------------------------------------------------------------------

def test_all_agents_start_at_trainee_floor(tmp_db, tmp_skills):
    # Zero wins, rich KB — must still be the floor (the original "born Senior" bug).
    kb = mock_kb(knowledge_count=2000, decision_count=2000,
                 source_types={"arxiv", "fred_macro", "earnings_history"})
    ev = make_evaluator(tmp_db, tmp_skills, kb=kb)
    report = ev.evaluate()
    for name, score in report.agents.items():
        assert score.tier == Tier.TRAINEE, f"{name} should start at Trainee"
        assert score.level == 1, f"{name} should start at L1"
    assert report.system_tier == Tier.TRAINEE
    assert report.system_level == 1


def test_no_real_money_with_zero_wins(tmp_db, tmp_skills):
    kb = mock_kb(knowledge_count=2000, decision_count=2000)
    ev = make_evaluator(tmp_db, tmp_skills, kb=kb)
    report = ev.evaluate()
    assert report.real_money_unlocked is False
    assert report.live_trading_allowed is False
    assert report.max_position_pct == 0.01   # Trainee cap


def test_cleared_agent_climbs_on_wins(tmp_db, tmp_skills):
    # 150 wins → Junior L6 for cleared agents (zeus/artemis/apollo/hades/icarus/ares).
    _insert_wins(tmp_db, 150)
    kb = mock_kb(knowledge_count=500, decision_count=500,
                 source_types={"arxiv", "fred_macro"})
    ev = make_evaluator(tmp_db, tmp_skills, kb=kb)
    report = ev.evaluate()
    z = report.agents["zeus"]
    assert z.cleared is True
    assert z.tier == Tier.JUNIOR
    assert z.level == 6   # 150 wins: tier=Junior, 50 wins in tier → L6
    assert z.wins == 150


def test_uncleared_agent_pinned_to_floor_despite_wins(tmp_db, tmp_skills):
    # Many wins, but a misconfigured agent (no KB → zeus clearance fails) is
    # held at Trainee L1 — you can't climb on a broken configuration.
    _insert_wins(tmp_db, 350)
    kb = mock_kb(knowledge_count=0, decision_count=0)   # zeus clearance fails
    ev = make_evaluator(tmp_db, tmp_skills, kb=kb)
    report = ev.evaluate()
    z = report.agents["zeus"]
    assert z.cleared is False
    assert z.tier == Tier.TRAINEE
    assert z.level == 1


def test_losses_do_not_advance(tmp_db, tmp_skills):
    _insert_wins(tmp_db, 20)     # 2 levels' worth
    _insert_losses(tmp_db, 500)  # losses must not count
    kb = mock_kb(knowledge_count=500, decision_count=500, source_types={"fred_macro"})
    ev = make_evaluator(tmp_db, tmp_skills, kb=kb)
    report = ev.evaluate()
    assert report.agents["artemis"].wins == 20
    assert report.agents["artemis"].tier == Tier.TRAINEE
    assert report.agents["artemis"].level == 3   # 20 wins → L3


def test_system_tier_is_floor_across_agents(tmp_db, tmp_skills):
    # 350 wins, but Pythia/Argus fail clearance → held at Trainee → system floor.
    _insert_wins(tmp_db, 350)
    kb = mock_kb(knowledge_count=500, decision_count=500, source_types={"fred_macro"})
    ev = make_evaluator(tmp_db, tmp_skills, kb=kb)
    report = ev.evaluate()
    assert report.system_tier == min(s.tier for s in report.agents.values())
    assert report.system_tier == Tier.TRAINEE   # Pythia/Argus drag it down


# ---------------------------------------------------------------------------
# Counts
# ---------------------------------------------------------------------------

def test_winning_trade_count_counts_only_wins(tmp_db, tmp_skills):
    _insert_wins(tmp_db, 7)
    _insert_losses(tmp_db, 13)
    ev = make_evaluator(tmp_db, tmp_skills, kb=mock_kb(0, 0))
    assert ev._winning_trade_count() == 7


def test_closed_trade_count_counts_wins_and_losses(tmp_db, tmp_skills):
    _insert_wins(tmp_db, 7)
    _insert_losses(tmp_db, 13)
    ev = make_evaluator(tmp_db, tmp_skills, kb=mock_kb(0, 0))
    assert ev._closed_trade_count() == 20


def test_win_count_reads_supabase_when_active(tmp_db, tmp_skills, monkeypatch):
    """Regression: in prod, trades live in Supabase, not the local SQLite file.
    The win count (which drives leveling) MUST read Supabase when it's active —
    reading the empty SQLite file left every agent stuck at 0 wins forever."""
    import core.supabase_client as supa

    # SQLite is empty (mirrors prod: no local trade_log.db rows) ...
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc")
    # ... but Supabase reports real wins.
    monkeypatch.setattr(supa, "count_winning_trades", lambda: 120, raising=False)
    monkeypatch.setattr(supa, "count_closed_trades", lambda: 200, raising=False)

    ev = make_evaluator(tmp_db, tmp_skills, kb=mock_kb(0, 0))
    assert ev._winning_trade_count() == 120   # from Supabase, NOT the empty SQLite
    assert ev._closed_trade_count() == 200


def test_win_count_falls_back_to_sqlite_on_supabase_error(tmp_db, tmp_skills, monkeypatch):
    """If the Supabase query errors (returns None), fall back to SQLite rather
    than silently treating the error as 0 wins."""
    import core.supabase_client as supa
    _insert_wins(tmp_db, 5)
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc")
    monkeypatch.setattr(supa, "count_winning_trades", lambda: None, raising=False)  # error
    ev = make_evaluator(tmp_db, tmp_skills, kb=mock_kb(0, 0))
    assert ev._winning_trade_count() == 5   # SQLite fallback, not 0


# ---------------------------------------------------------------------------
# SeniorityReport shape
# ---------------------------------------------------------------------------

def test_report_to_dict_has_required_keys(tmp_db, tmp_skills):
    ev = make_evaluator(tmp_db, tmp_skills, kb=mock_kb(0, 0))
    d = ev.evaluate().to_dict()
    for k in ("system_tier", "system_tier_int", "system_level", "system_rank",
              "max_position_pct", "paper_trading_allowed", "real_money_unlocked",
              "real_money_armed", "live_trading_allowed", "all_cleared", "agents"):
        assert k in d, f"missing {k}"
    assert set(d["agents"].keys()) == {
        "zeus", "pythia", "artemis", "apollo", "hades", "icarus", "ares", "argus"
    }


def test_agent_dict_has_tier_level_rank(tmp_db, tmp_skills):
    _insert_wins(tmp_db, 150)
    ev = make_evaluator(tmp_db, tmp_skills,
                        kb=mock_kb(500, 500, source_types={"fred_macro"}))
    d = ev.evaluate().to_dict()
    art = d["agents"]["artemis"]
    for k in ("tier", "tier_int", "level", "wins", "rank", "level_int"):
        assert k in art
    assert art["rank"] == f"{art['tier']} L{art['level']}"
    assert art["level_int"] == art["tier_int"]   # back-compat alias


def test_report_summary_line_format(tmp_db, tmp_skills):
    ev = make_evaluator(tmp_db, tmp_skills, kb=mock_kb(0, 0))
    line = ev.evaluate().summary_line()
    assert "System:" in line
    assert "PAPER ONLY" in line
    assert "Max position:" in line


def test_summary_shows_unlocked_disarmed(tmp_db, tmp_skills):
    # All agents Senior + cleared but NOT armed → "REAL UNLOCKED (DISARMED)".
    _insert_wins(tmp_db, 300)
    kb = mock_kb(2000, 2000, source_types={"arxiv", "fred_macro", "earnings_history"})
    ev = make_evaluator(tmp_db, tmp_skills, kb=kb)
    # Force every agent cleared by stubbing the clearance checks.
    for attr in ("_clear_zeus", "_clear_pythia", "_clear_artemis", "_clear_apollo",
                 "_clear_hades", "_clear_icarus", "_clear_ares", "_clear_argus"):
        setattr(ev, attr, lambda score: True)
    report = ev.evaluate()
    assert report.system_tier == Tier.SENIOR
    assert report.real_money_unlocked is True
    assert report.live_trading_allowed is False   # not armed
    assert "REAL UNLOCKED (DISARMED)" in report.summary_line()


def test_summary_shows_live_when_armed(tmp_db, tmp_skills, monkeypatch):
    monkeypatch.setenv("ARM_REAL_MONEY", "true")
    _insert_wins(tmp_db, 300)
    kb = mock_kb(2000, 2000, source_types={"arxiv", "fred_macro", "earnings_history"})
    ev = make_evaluator(tmp_db, tmp_skills, kb=kb)
    for attr in ("_clear_zeus", "_clear_pythia", "_clear_artemis", "_clear_apollo",
                 "_clear_hades", "_clear_icarus", "_clear_ares", "_clear_argus"):
        setattr(ev, attr, lambda score: True)
    report = ev.evaluate()
    assert report.system_tier == Tier.SENIOR
    assert report.live_trading_allowed is True
    assert report.max_position_pct == 0.05      # real-money cap
    assert "LIVE (ARMED)" in report.summary_line()


# ---------------------------------------------------------------------------
# Promotion detection (tier OR level increase)
# ---------------------------------------------------------------------------

def test_promotion_alert_fires_on_level_increase(tmp_db, tmp_skills):
    alerts = []
    kb = mock_kb(500, 500, source_types={"fred_macro"})
    ev = make_evaluator(tmp_db, tmp_skills, kb=kb)
    ev._alert_fn = lambda msg: alerts.append(msg)

    _insert_wins(tmp_db, 5)     # Trainee L1
    ev.evaluate()
    _insert_wins(tmp_db, 10)    # now 15 wins → Trainee L2 (level up)
    ev.evaluate()
    assert any("LEVEL UP" in m or "TIER PROMOTION" in m for m in alerts)
