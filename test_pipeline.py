"""
Full pipeline smoke test — no IB Gateway required.

Two modes:
  python test_pipeline.py           → inject mock signals (no Hermes API key needed)
  python test_pipeline.py --live    → pull real signals from Hermes (needs HERMES_API_KEY)
"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime

from core.logging_setup import configure_logging

configure_logging(logging.DEBUG)

from agents.execution_mock import MockExecutionAgent
from agents.pattern import PatternAgent
from agents.trend import TrendAgent

from agents.hades import HadesAgent
from agents.icarus import IcarusAgent, RawSignal, Severity, SignalCategory

logger = logging.getLogger("test")


def make_test_signal(signal_id: str, category: SignalCategory, tickers: list[str], headline: str = "") -> RawSignal:
    return RawSignal(
        signal_id=signal_id,
        source_url="https://mock",
        headline=headline or f"Test signal: {category.value}",
        summary="Simulated event for pipeline smoke test.",
        published_at=datetime.utcnow(),
        category=category,
        severity=Severity.HIGH,
        affected_tickers=tickers,
        raw_text=f"Test signal {category.value} affecting {' '.join(tickers)}",
    )


def run_pipeline(signals: list[RawSignal], label: str) -> None:
    hades = HadesAgent()
    trend = TrendAgent()
    pattern = PatternAgent()
    execution = MockExecutionAgent()

    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"  {len(signals)} signal(s) to process")
    print(f"{'='*60}\n")

    for raw in signals:
        print(f"[SIGNAL] {raw.headline[:80]}")
        print(f"         supplier={raw.supplier or 'n/a'} category={raw.category.value} tickers={raw.affected_tickers}")

        filtered = hades.filter(raw)
        if filtered is None:
            print("  → Hades: KILL\n")
            continue
        print(f"  → Hades: PASS (compliance={filtered.compliance_score:.2f})")

        macro = trend.analyze(filtered)
        if macro.suppress:
            print(f"  → Trend: SUPPRESS — {macro.suppress_reason}\n")
            continue
        print(f"  → Trend: PASS (regime={macro.regime} VIX={macro.vix:.1f})")

        sized = pattern.size(filtered, macro)
        if sized.skip:
            print(f"  → Pattern: SKIP — {sized.skip_reason}\n")
            continue
        print(f"  → Pattern: size={sized.position_size_pct*100:.2f}% confidence={sized.confidence:.2f}")

        result = execution.place(sized)
        print(f"  → Execution: {result.side} {result.symbol} @ {result.fill_price} | id={result.order_id}")
        pattern.record_trade(sized, result)
        print()


def run_mock_tests():
    signals = [
        make_test_signal("t-001", SignalCategory.POSITIVE_NEWS, ["AAPL"], "Apple partnership announced"),
        make_test_signal("t-002", SignalCategory.SUPPLIER_DISRUPTION, ["TSM"], "TSMC fab disruption reported"),
        RawSignal(
            signal_id="t-003",
            source_url="https://mock",
            headline="RUSAL announces production expansion",
            summary="RUSAL expands aluminium output.",
            published_at=datetime.utcnow(),
            category=SignalCategory.POSITIVE_NEWS,
            severity=Severity.MEDIUM,
            affected_tickers=["RUAL"],
            raw_text="RUSAL announces record aluminium production",
        ),
        make_test_signal("t-004", SignalCategory.EARNINGS_SURPRISE, ["NVDA"], "NVIDIA beats earnings by 40%"),
        make_test_signal("t-005", SignalCategory.REGULATORY_ACTION, ["MSFT"], "Microsoft antitrust investigation opened"),
    ]
    run_pipeline(signals, "Mock Signal Tests")


def run_live_hermes():
    api_key = os.getenv("HERMES_API_KEY", "")
    if not api_key:
        print("ERROR: HERMES_API_KEY not set. Run: set HERMES_API_KEY=your_key")
        return

    print("Fetching live signals from Hermes...")
    icarus = IcarusAgent(api_key=api_key)
    signals = icarus.fetch()

    if not signals:
        print("No significant signals returned by Hermes /briefing right now.")
        print("Try --company NVIDIA to query a specific supplier.")
        return

    run_pipeline(signals, "Live Hermes Signals")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ZEUS Pipeline Smoke Test")
    parser.add_argument("--live", action="store_true", help="Pull real signals from Hermes")
    parser.add_argument("--company", type=str, help="Query a specific Hermes supplier (e.g. NVIDIA)")
    args = parser.parse_args()

    if args.company:
        api_key = os.getenv("HERMES_API_KEY", "")
        icarus = IcarusAgent(api_key=api_key)
        signals = icarus.fetch_company(args.company)
        run_pipeline(signals, f"Hermes signals for: {args.company}")
    elif args.live:
        run_live_hermes()
    else:
        run_mock_tests()
