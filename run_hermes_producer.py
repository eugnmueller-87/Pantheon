"""
Hermes Producer — standalone signal fetcher for Pantheon OS.

Runs as its own process (own failure domain, own memory) separate from Zeus.
Writes EDGAR + Finnhub signals into Supabase every HERMES_LOCAL_INTERVAL seconds
(default 1800 = 30 min).  Icarus in Zeus reads from that table.

Usage:
    python run_hermes_producer.py          # normal prod loop
    python -m agents.hermes_local          # one-shot manual run
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from core.logging_setup import configure_logging  # noqa: E402 — after load_dotenv

configure_logging()
logger = logging.getLogger("hermes_producer")

_INTERVAL = int(os.getenv("HERMES_LOCAL_INTERVAL", "1800"))


def main() -> None:
    logger.info("[HERMES_PRODUCER] Starting — cycle every %ds", _INTERVAL)
    from agents.hermes_local import run_cycle

    cycle = 0
    while True:
        cycle += 1
        logger.info("[HERMES_PRODUCER] Cycle %d begin", cycle)
        try:
            result = run_cycle()
            logger.info("[HERMES_PRODUCER] Cycle %d complete: %s", cycle, result)
        except Exception:
            logger.exception("[HERMES_PRODUCER] Cycle %d failed — continuing", cycle)
        logger.info("[HERMES_PRODUCER] Sleeping %ds until next cycle", _INTERVAL)
        time.sleep(_INTERVAL)


if __name__ == "__main__":
    main()
