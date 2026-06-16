"""
The dashboard backend derives a `binding_constraint` per killed signal so the
LIVE tab shows WHICH rule bound the kill, not just the stage.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from dashboard.backend.server import binding_constraint as bc


def test_no_stage_returns_none():
    assert bc(None, "anything") is None


def test_icarus_is_out_of_universe():
    assert bc("icarus", "out_of_universe: SpaceX") == "out_of_universe"
    assert bc("icarus", "") == "out_of_universe"


def test_hades_maps_compliance_and_sanctions():
    assert bc("hades", "OFAC blocklist hit") == "sanctions"
    assert bc("hades", "ESG screen failed") == "compliance_block"


def test_artemis_macro_and_vix():
    assert bc("artemis", "macro data unavailable") == "macro_unavailable"
    assert bc("artemis", "Extreme VIX=41: all signals suppressed") == "vix_extreme"
    assert bc("trend", "bear regime + positive news") == "bear_regime"


def test_pythia_tier_vs_cold_start():
    assert bc("pattern", "Stage SEED: tier 2 not allowed") == "tier_blocked_at_seed"
    assert bc("pythia", "confidence 0.55 < threshold") == "cold_start_low_conf"


def test_concentration_variants():
    assert bc("concentration", "NVDA already has 1 open position(s) (cap=1)") == "ticker_cap"
    assert bc("concentration", "sector tech cap reached") == "sector_cap"
    assert bc("concentration", "ticker cooldown active") == "cooldown"


def test_zeus_variants():
    assert bc("zeus", "budget: would deploy €4500 of €4000") == "budget"
    assert bc("zeus", "Director confidence 0.36 below floor 0.55") == "confidence_below_floor"
    assert bc("zeus", "insufficient reasoning quality") == "reasoning_quality"
    assert bc("zeus", "ZEUS LLM reasoning rejected trade") == "confidence_below_floor"


def test_unknown_stage_surfaced_verbatim():
    assert bc("mystery", "x") == "mystery"
