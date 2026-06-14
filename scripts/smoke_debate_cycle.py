"""
Smoke test — one real ZEUS orchestrator cycle that places a SIMULATED trade
through the bull/bear debate path.

What this exercises (real, not mocked):
  Hades → Artemis → Pythia → ZEUS (bull → bear → director debate) → Ares → Argus

What is forced safe:
  mock_execution=True  → simulated fill, NO IB Gateway connection
  paper_trading=True   → never the live broker path
  use_debate=True      → the new adversarial debate runs for real

Costs ~3 Claude calls (~$0.03) and writes a few rows to Supabase/ChromaDB,
exactly as a production cycle would. Run from the repo root:

    python scripts/smoke_debate_cycle.py
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running as `python scripts/smoke_debate_cycle.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()  # pick up ANTHROPIC_API_KEY etc. from .env, like main.py does

from core.logging_setup import configure_logging

configure_logging(logging.INFO)

from agents.zeus import ZeusConfig, ZeusOrchestrator
from core.types import (
    FilteredSignal,
    MacroContext,
    MarketRegime,
    RawSignal,
    Severity,
    SignalCategory,
    SizedSignal,
)


def _banner(text: str) -> None:
    print(f"\n{'=' * 68}\n  {text}\n{'=' * 68}")


def make_signal() -> RawSignal:
    """A clean, high-conviction signal on a real ticker so enrichment works."""
    return RawSignal(
        signal_id=f"smoke-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
        source_url="https://mock/smoke",
        headline="NVIDIA beats Q3 earnings by 28%, raises full-year guidance",
        summary="NVDA reported revenue well above consensus and lifted guidance "
                "on continued data-center demand.",
        published_at=datetime.now(timezone.utc),
        category=SignalCategory.EARNINGS_SURPRISE,
        severity=Severity.HIGH,
        affected_tickers=["NVDA"],
        raw_text="NVIDIA Q3 earnings beat, guidance raised.",
        supplier="NVIDIA Corporation",
    )


def main() -> int:
    _banner("PART B — Live orchestrator cycle (mock execution, real debate)")

    # Force the simulation-safe config regardless of settings.json
    # (settings.json has mock_execution=false, which would need IB Gateway).
    config = ZeusConfig(
        paper_trading=True,
        mock_execution=True,
        use_debate=True,
        use_llm_reasoning=True,
    )

    print("Booting ZeusOrchestrator (watchdog + 8 agents)...")
    zeus = ZeusOrchestrator(config=config)
    print(f"  paper_trading={zeus.config.paper_trading}  "
          f"mock_execution={zeus.config.mock_execution}  "
          f"use_debate={zeus.config.use_debate}")

    signal = make_signal()
    _banner(f"Injecting signal: {signal.headline}")
    print(f"  ticker={signal.affected_tickers} category={signal.category.value} "
          f"severity={signal.severity.name}")

    # Run the REAL pipeline for this one signal.
    run = zeus._process_signal(signal)

    _banner("RESULT")
    trace = run.trace
    if run.killed_at_stage:
        print(f"  Signal KILLED at: {run.killed_at_stage}")
        print(f"  Reason: {run.kill_reason}")
    else:
        print("  Signal SURVIVED the full pipeline.")

    if trace is not None:
        print(f"\n  Hades passed:   {trace.hades_passed}")
        print(f"  Macro regime:   {trace.trend_regime}  VIX={trace.trend_vix}")
        print(f"  Pythia size:    {trace.pattern_size_pct * 100:.2f}%  "
              f"confidence={trace.pattern_confidence:.2f}")
        print(f"  ZEUS approved:  {trace.zeus_approved}")

        # The debate is folded into zeus_reasoning as "... | DEBATE — BULL: ... || BEAR: ..."
        reasoning = trace.zeus_reasoning or ""
        if "DEBATE —" in reasoning:
            verdict, debate = reasoning.split("| DEBATE —", 1)
            _banner("ZEUS DIRECTOR VERDICT")
            print("  " + verdict.strip())
            if "|| BEAR:" in debate:
                bull, bear = debate.split("|| BEAR:", 1)
                _banner("BULL CASE")
                print("  " + bull.replace("BULL:", "").strip())
                _banner("BEAR CASE")
                print("  " + bear.strip())
        else:
            _banner("ZEUS REASONING (no debate marker found)")
            print("  " + reasoning.strip())

    if run.trade_result is not None:
        r = run.trade_result
        _banner("SIMULATED TRADE")
        print(f"  {r.side} {r.symbol}  qty={r.qty}  fill={r.fill_price}  "
              f"status={r.status}  order_id={r.order_id}")
        if getattr(r, "stop_loss_price", None) and getattr(r, "take_profit_price", None):
            print(f"  stop_loss={r.stop_loss_price}  take_profit={r.take_profit_price}")

    # ── Part C — force the debate path directly via decide() ──────────────────
    # The full cycle above is correctly gated by the SEED milestone (tier-1 only,
    # needs Pythia confidence >= 0.70, which a cold local DB can't produce). To
    # actually exercise the bull/bear debate, feed ZEUS a high-confidence
    # SizedSignal directly — decide() runs the real _zeus_evaluate (debate + LLM).
    _banner("PART C — decide() with high-confidence signal (forces the debate)")
    sized = _high_conviction_sized(signal)
    print(f"  Pythia confidence (forced): {sized.confidence:.2f}  "
          f"size={sized.position_size_pct * 100:.2f}%")
    decision = zeus.decide(sized)

    reasoning = decision.get("reasoning", "")
    if "DEBATE —" in reasoning and "|| BEAR:" in reasoning:
        verdict, debate = reasoning.split("| DEBATE —", 1)
        bull, bear = debate.split("|| BEAR:", 1)
        _banner("ZEUS DIRECTOR VERDICT")
        print(f"  approved={decision.get('approved')}")
        print("  " + verdict.strip())
        _banner("BULL CASE")
        print("  " + bull.replace("BULL:", "").strip())
        _banner("BEAR CASE")
        print("  " + bear.strip())
    else:
        _banner("ZEUS DECISION (debate marker not found)")
        print(f"  approved={decision.get('approved')}")
        print("  " + reasoning.strip())

    print("\nSmoke cycle complete.")
    return 0


def _high_conviction_sized(raw: RawSignal) -> SizedSignal:
    """A pre-sized, high-confidence signal so decide() reaches the debate."""
    filtered = FilteredSignal(original=raw, compliance_score=1.0, notes=["clean"])
    macro = MacroContext(
        fetched_at=datetime.now(timezone.utc),
        regime=MarketRegime.BULL,
        vix=16.0,
        sp500_1m_return=0.04,
    )
    return SizedSignal(
        original=filtered, macro=macro,
        confidence=0.82, position_size_pct=0.03,
    )


if __name__ == "__main__":
    sys.exit(main())
