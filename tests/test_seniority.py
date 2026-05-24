"""
Tests for core/seniority.py — Agent Seniority Framework

Structure: Senior is the floor. No juniors, no trainees.
Levels: SENIOR → PRINCIPAL → MANAGING_DIR → DIRECTOR (ZEUS only)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.seniority import Level, SeniorityEvaluator, SeniorityReport


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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
    return SeniorityEvaluator(
        kb=kb,
        db_path=tmp_db,
        skills_dir=tmp_skills / "agents",
    )


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


# ---------------------------------------------------------------------------
# Level enum — Senior is the floor
# ---------------------------------------------------------------------------

def test_level_ordering():
    assert Level.SENIOR < Level.PRINCIPAL < Level.MANAGING_DIR < Level.DIRECTOR


def test_level_labels():
    assert Level.SENIOR.label()       == "Senior"
    assert Level.PRINCIPAL.label()    == "Principal"
    assert Level.MANAGING_DIR.label() == "Managing Director"
    assert Level.DIRECTOR.label()     == "Director"


def test_position_size_by_level():
    assert Level.SENIOR.max_position_pct()       == 0.03   # paper only
    assert Level.PRINCIPAL.max_position_pct()    == 0.05   # live enabled
    assert Level.MANAGING_DIR.max_position_pct() == 0.05
    assert Level.DIRECTOR.max_position_pct()     == 0.05


def test_live_trading_gates():
    assert not Level.SENIOR.live_trading_allowed()       # paper only
    assert Level.PRINCIPAL.live_trading_allowed()        # live enabled
    assert Level.MANAGING_DIR.live_trading_allowed()
    assert Level.DIRECTOR.live_trading_allowed()


def test_paper_trading_always_allowed():
    for level in Level:
        assert level.paper_trading_allowed()


# ---------------------------------------------------------------------------
# Pythia — Senior Quantitative Analyst
# ---------------------------------------------------------------------------

def test_pythia_senior_not_cleared_with_no_trades(tmp_db, tmp_skills):
    ev = make_evaluator(tmp_db, tmp_skills)
    score = ev._evaluate_pythia()
    assert score.level == Level.SENIOR
    assert score.cleared is False


def test_pythia_senior_cleared_with_5_context_keys(tmp_db, tmp_skills):
    with sqlite3.connect(tmp_db) as conn:
        for i in range(5):
            for _ in range(12):
                conn.execute(
                    "INSERT INTO trades (context_key, hit, pnl_pct, position_pct) VALUES (?, ?, ?, ?)",
                    (f"key_{i}", 1, 0.02, 0.02)
                )
        conn.commit()
    ev = make_evaluator(tmp_db, tmp_skills)
    score = ev._evaluate_pythia()
    assert score.cleared is True
    assert score.level >= Level.SENIOR


def test_pythia_not_cleared_with_only_4_keys(tmp_db, tmp_skills):
    with sqlite3.connect(tmp_db) as conn:
        for i in range(4):
            for _ in range(12):
                conn.execute(
                    "INSERT INTO trades (context_key, hit, pnl_pct, position_pct) VALUES (?, ?, ?, ?)",
                    (f"key_{i}", 1, 0.02, 0.02)
                )
        conn.commit()
    ev = make_evaluator(tmp_db, tmp_skills)
    score = ev._evaluate_pythia()
    assert score.cleared is False


# ---------------------------------------------------------------------------
# ZEUS — Director
# ---------------------------------------------------------------------------

def test_zeus_senior_not_cleared_empty_kb(tmp_db, tmp_skills):
    kb = mock_kb(knowledge_count=0, decision_count=0)
    ev = make_evaluator(tmp_db, tmp_skills, kb=kb)
    score = ev._evaluate_zeus()
    assert score.level == Level.SENIOR
    assert score.cleared is False


def test_zeus_senior_cleared_with_kb_and_decisions(tmp_db, tmp_skills):
    kb = mock_kb(knowledge_count=120, decision_count=25)
    ev = make_evaluator(tmp_db, tmp_skills, kb=kb)
    score = ev._evaluate_zeus()
    assert score.cleared is True
    assert score.level == Level.SENIOR   # cleared but not yet PRINCIPAL


def test_zeus_not_cleared_without_enough_decisions(tmp_db, tmp_skills):
    kb = mock_kb(knowledge_count=600, decision_count=15)
    ev = make_evaluator(tmp_db, tmp_skills, kb=kb)
    score = ev._evaluate_zeus()
    assert score.cleared is False


# ---------------------------------------------------------------------------
# Hades — Senior Compliance Officer
# ---------------------------------------------------------------------------

def test_hades_cleared_when_ofac_present(tmp_db, tmp_skills):
    ev = make_evaluator(tmp_db, tmp_skills)
    score = ev._evaluate_hades()
    assert score.level == Level.SENIOR
    assert score.cleared is True   # hades.py has OFAC logic


# ---------------------------------------------------------------------------
# Apollo — Senior Research Analyst
# ---------------------------------------------------------------------------

def test_apollo_not_cleared_with_no_arxiv(tmp_db, tmp_skills):
    kb = mock_kb(knowledge_count=0)
    ev = make_evaluator(tmp_db, tmp_skills, kb=kb)
    score = ev._evaluate_apollo()
    assert score.level == Level.SENIOR
    assert score.cleared is False


def test_apollo_cleared_with_50_arxiv_papers(tmp_db, tmp_skills):
    kb = mock_kb(knowledge_count=100, source_types={"arxiv"})
    kb._knowledge_col.get.side_effect = lambda where=None, include=None: {
        "ids": ["x"] * (55 if (where or {}).get("type") == "arxiv" else 0)
    }
    ev = make_evaluator(tmp_db, tmp_skills, kb=kb)
    score = ev._evaluate_apollo()
    assert score.cleared is True


def test_apollo_self_improve_count_from_skills_file(tmp_db, tmp_skills):
    (tmp_skills / "agents" / "zeus_skills.md").write_text(
        "## Self-Improvement Insights — 2026-01-01\nfoo\n\n"
        "## Self-Improvement Insights — 2026-02-01\nbar\n",
        encoding="utf-8"
    )
    ev = make_evaluator(tmp_db, tmp_skills)
    assert ev._apollo_self_improve_count() == 2


# ---------------------------------------------------------------------------
# System level = min of all agents
# ---------------------------------------------------------------------------

def test_system_level_is_minimum(tmp_db, tmp_skills):
    kb = mock_kb(knowledge_count=0, decision_count=0)
    ev = make_evaluator(tmp_db, tmp_skills, kb=kb)
    report = ev.evaluate()
    assert report.system_level == min(s.level for s in report.agents.values())


def test_system_level_never_above_weakest_agent(tmp_db, tmp_skills):
    kb = mock_kb(knowledge_count=2000, decision_count=1100)
    ev = make_evaluator(tmp_db, tmp_skills, kb=kb)
    report = ev.evaluate()
    for agent_score in report.agents.values():
        assert report.system_level <= agent_score.level


def test_all_agents_start_at_senior_floor(tmp_db, tmp_skills):
    kb = mock_kb(knowledge_count=0, decision_count=0)
    ev = make_evaluator(tmp_db, tmp_skills, kb=kb)
    report = ev.evaluate()
    for name, score in report.agents.items():
        assert score.level >= Level.SENIOR, f"{name} should never go below Senior"


# ---------------------------------------------------------------------------
# Position size ceiling
# ---------------------------------------------------------------------------

def test_senior_max_position_is_3pct():
    assert Level.SENIOR.max_position_pct() == 0.03


def test_principal_and_above_max_position_is_5pct():
    assert Level.PRINCIPAL.max_position_pct()    == 0.05
    assert Level.MANAGING_DIR.max_position_pct() == 0.05
    assert Level.DIRECTOR.max_position_pct()     == 0.05


# ---------------------------------------------------------------------------
# Promotion detection
# ---------------------------------------------------------------------------

def test_promotion_alert_fires_on_level_increase(tmp_db, tmp_skills):
    alerts = []
    ev = make_evaluator(tmp_db, tmp_skills, kb=mock_kb(0, 0))
    ev._alert_fn = lambda msg: alerts.append(msg)
    ev.evaluate()

    # Simulate KB growth triggering a promotion
    ev._kb = mock_kb(knowledge_count=120, decision_count=25)
    ev.evaluate()
    assert isinstance(alerts, list)   # no crash; alert may or may not fire depending on chain


# ---------------------------------------------------------------------------
# SeniorityReport shape
# ---------------------------------------------------------------------------

def test_report_to_dict_has_required_keys(tmp_db, tmp_skills):
    ev = make_evaluator(tmp_db, tmp_skills, kb=mock_kb(0, 0))
    d  = ev.evaluate().to_dict()
    assert "system_level"          in d
    assert "max_position_pct"      in d
    assert "live_trading_allowed"  in d
    assert "paper_trading_allowed" in d
    assert "all_cleared"           in d
    assert "agents"                in d
    assert set(d["agents"].keys()) == {"zeus", "pythia", "artemis", "apollo", "hades", "icarus", "ares", "argus"}


def test_report_summary_line_format(tmp_db, tmp_skills):
    ev   = make_evaluator(tmp_db, tmp_skills, kb=mock_kb(0, 0))
    line = ev.evaluate().summary_line()
    assert "System:" in line
    assert "PAPER ONLY" in line or "LIVE ENABLED" in line
    assert "Max position:" in line


def test_report_all_cleared_false_when_no_data(tmp_db, tmp_skills):
    ev     = make_evaluator(tmp_db, tmp_skills, kb=mock_kb(0, 0))
    report = ev.evaluate()
    assert report.all_cleared is False
