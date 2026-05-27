"""
Load ZEUS runtime settings from config/settings.json.
Provides typed defaults so the system runs out of the box.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_SETTINGS_FILE = Path(__file__).parent / "settings.json"

_DEFAULTS: dict[str, Any] = {
    # Pipeline
    "paper_trading": True,
    "mock_execution": True,
    "max_drawdown_pct": 0.08,
    "max_open_positions": 3,        # €4k account — max 3 concurrent positions
    "poll_interval_seconds": 900,
    "webhook_port": 8080,
    "hermes_base_url": "https://hermes-agent-production-114e.up.railway.app",
    "hermes_feeds": [],
    # Signal source strategy: "supabase" (primary, Hermes writes here) or "api" (direct poll)
    "signal_source": "supabase",
    # IBKR
    "ib_host": "127.0.0.1",
    "ib_paper_port": 7497,
    "ib_live_port": 7496,
    "default_account_equity": 4_000.0,     # realistic starting capital — paper trades real-world constraints
    "starting_equity": 4_000.0,
    # Risk parameters — Ares bracket order
    "stop_loss_pct": 0.03,        # 3% stop
    "take_profit_pct": 0.06,      # 6% target (2:1 R/R)
    # Confidence thresholds
    "zeus_min_confidence": 0.55,
    "pythia_default_confidence": 0.55,
    "pythia_min_confidence": 0.45,
    "pythia_tier1_confidence": 0.70,
    # Macro thresholds — Artemis
    "vix_medium": 15.0,
    "vix_high": 25.0,
    "vix_extreme": 35.0,
    "bull_threshold": 0.02,       # SPY 1m return above this = bull
    "bear_threshold": -0.03,      # SPY 1m return below this = bear
}


def load_settings() -> dict[str, Any]:
    if _SETTINGS_FILE.exists():
        with open(_SETTINGS_FILE, encoding="utf-8") as f:
            user = json.load(f)
        return {**_DEFAULTS, **user}
    return dict(_DEFAULTS)
