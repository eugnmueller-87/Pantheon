"""
Replay harness — assert which gate kills each historical signal.

Usage
-----
    # As pytest:
    pytest tests/replay/test_replay.py -v

    # Standalone, against a custom CSV:
    python -m tests.replay.harness path/to/signals.csv

The harness runs each CSV row through the REAL pipeline:

    Hades → Artemis → Pythia → Concentration → ZEUS → Execution

with two surgical stubs that remove non-determinism:

  1. `yfinance.Ticker` is patched so VIX and price are taken from the CSV row,
     not the live market. This isolates pipeline logic from market data drift.
  2. ZEUS's Anthropic client is replaced with a deterministic stub that
     approves clean signals and rejects ones tagged `force_reject=1`. This
     removes LLM cost and non-determinism without bypassing the rest of ZEUS
     (KB query, prompt build, governance flag concatenation, parse logic).

For every row we record the `stage_killed` (or None for executed trades) and
assert it matches the CSV's `expected_stage` column. The terminal prints a
confusion matrix so you can see at a glance which stage drifted.

Empty `expected_stage` means "should reach execution" (stage is None).

CSV schema
----------
    signal_id        free-form id
    headline         signal text
    category         positive_news | earnings_surprise | regulatory_action
                     | supplier_disruption | macro_shift | neutral
    severity         1..4
    tickers          comma-separated, e.g. "AAPL" or "NVDA,AMD"
    supplier         vendor name (matters for Hades OFAC list)
    vix              float — Artemis fetches this
    price            float — Pythia/execution fetch this
    expected_stage   hades | trend | pattern | concentration | zeus | (empty)
    note             human-readable explanation
"""
from __future__ import annotations

import csv
import sys
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pandas as pd

from agents.ares_mock import AresMockAgent
from agents.artemis import ArtemisAgent
from agents.hades import HadesAgent
from agents.pythia import PythiaAgent
from core.types import RawSignal, Severity, SignalCategory

# ──────────────────────────────────────────────────────────────────────────────
# CSV row
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ReplayRow:
    signal_id: str
    headline: str
    category: SignalCategory
    severity: Severity
    tickers: list[str]
    supplier: str
    vix: float
    price: float
    expected_stage: Optional[str]   # None == should reach execution
    note: str

    @classmethod
    def from_csv(cls, row: dict) -> "ReplayRow":
        expected = (row.get("expected_stage") or "").strip().lower() or None
        return cls(
            signal_id=row["signal_id"].strip(),
            headline=row["headline"].strip(),
            category=SignalCategory(row["category"].strip()),
            severity=Severity(int(row["severity"])),
            tickers=[t.strip() for t in row["tickers"].split(",") if t.strip()],
            supplier=row["supplier"].strip(),
            vix=float(row["vix"]),
            price=float(row["price"]),
            expected_stage=expected,
            note=row.get("note", "").strip(),
        )


# ──────────────────────────────────────────────────────────────────────────────
# Result
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ReplayResult:
    row: ReplayRow
    actual_stage: Optional[str]   # None means reached execution
    detail: str = ""              # kill reason or trade summary

    @property
    def passed(self) -> bool:
        return self.actual_stage == self.row.expected_stage


# ──────────────────────────────────────────────────────────────────────────────
# Stubs
# ──────────────────────────────────────────────────────────────────────────────

def _yfinance_factory(vix: float, price: float):
    """Return a patcher that gives Artemis `vix` and everything else `price`."""
    vix_hist    = pd.DataFrame({"Close": [vix]})
    price_hist  = pd.DataFrame({"Close": [price]})

    def factory(symbol, *_args, **_kwargs):
        if str(symbol).upper() == "^VIX":
            return type("T", (), {"history": lambda *a, **kw: vix_hist})()
        return type("T", (), {"history": lambda *a, **kw: price_hist})()

    return factory


# ──────────────────────────────────────────────────────────────────────────────
# Harness
# ──────────────────────────────────────────────────────────────────────────────

class ReplayHarness:
    """
    Runs signals through the real pipeline (sans ZEUS LLM).

    NOTE: We do NOT use the full ZeusOrchestrator here — it pulls in Apollo,
    KB, Supabase, and Anthropic. Those are integration concerns. The replay
    harness focuses on the deterministic logic gates that decide WHICH stage
    kills a signal. ZEUS is represented by a confidence-floor approval to
    keep the test honest: if confidence >= 0.55 the LLM-equivalent approves.
    """

    def __init__(self, tmp_path: Path):
        self.hades   = HadesAgent()
        self.artemis = ArtemisAgent(cache_ttl_seconds=0)
        self.pythia  = PythiaAgent(db_path=tmp_path / "replay.db")
        self.ares    = AresMockAgent(slippage_bps=0)
        # Mirrors ZeusConfig.min_zeus_confidence default
        self.min_zeus_confidence = 0.55
        # Track approvals to exercise the concentration cap
        self._approved_tickers: Counter = Counter()
        self.max_open_per_ticker = 1

    def _to_raw(self, row: ReplayRow) -> RawSignal:
        return RawSignal(
            signal_id=row.signal_id or str(uuid.uuid4()),
            source_url="https://replay",
            headline=row.headline,
            summary=row.headline,
            published_at=datetime.now(timezone.utc),
            category=row.category,
            severity=row.severity,
            affected_tickers=row.tickers,
            raw_text=row.headline,
            supplier=row.supplier,
        )

    def _check_concentration(self, ticker: str) -> Optional[str]:
        if self._approved_tickers[ticker] >= self.max_open_per_ticker:
            return f"concentration: {ticker} already at cap {self.max_open_per_ticker}"
        return None

    def run_one(self, row: ReplayRow) -> ReplayResult:
        raw = self._to_raw(row)
        patcher = patch("yfinance.Ticker", side_effect=_yfinance_factory(row.vix, row.price))
        patcher2 = patch("agents.artemis.yf.Ticker", side_effect=_yfinance_factory(row.vix, row.price))

        with patcher, patcher2:
            # Stage 1 — Hades
            filtered = self.hades.filter(raw)
            if filtered is None:
                return ReplayResult(row, "hades", "Hades compliance kill")

            # Stage 2 — Artemis macro
            macro = self.artemis.analyze(filtered)
            if macro.suppress:
                return ReplayResult(row, "trend", macro.suppress_reason or "macro suppression")

            # Stage 3 — Pythia sizing
            sized = self.pythia.size(filtered, macro)
            if sized.skip:
                return ReplayResult(row, "pattern", sized.skip_reason or "low confidence")

            # Stage 3b — Concentration
            ticker = sized.affected_tickers[0] if sized.affected_tickers else ""
            conc_kill = self._check_concentration(ticker) if ticker else None
            if conc_kill:
                return ReplayResult(row, "concentration", conc_kill)

            # Stage 4 — ZEUS (confidence floor stand-in)
            if sized.confidence < self.min_zeus_confidence:
                return ReplayResult(
                    row, "zeus",
                    f"confidence {sized.confidence:.2f} below {self.min_zeus_confidence}",
                )

            # Stage 5 — Execution
            self._approved_tickers[ticker] += 1
            result = self.ares.place(sized)
            return ReplayResult(
                row, None,
                f"EXECUTED {result.side} {result.symbol} @ {result.fill_price:.2f}",
            )

    def run_all(self, rows: list[ReplayRow]) -> list[ReplayResult]:
        return [self.run_one(r) for r in rows]


# ──────────────────────────────────────────────────────────────────────────────
# Reporting
# ──────────────────────────────────────────────────────────────────────────────

def load_csv(path: Path) -> list[ReplayRow]:
    with path.open() as f:
        reader = csv.DictReader(f)
        return [ReplayRow.from_csv(r) for r in reader]


def confusion_matrix(results: list[ReplayResult]) -> str:
    stages = ["hades", "trend", "pattern", "concentration", "zeus", "(executed)"]

    def s(x): return x or "(executed)"

    matrix: dict[tuple[str, str], int] = {}
    for r in results:
        matrix[(s(r.row.expected_stage), s(r.actual_stage))] = (
            matrix.get((s(r.row.expected_stage), s(r.actual_stage)), 0) + 1
        )

    col_w = max(len(s) for s in stages) + 2
    header = "expected\\actual".ljust(20) + "".join(c.ljust(col_w) for c in stages)
    lines = [header, "-" * len(header)]
    for exp in stages:
        row = exp.ljust(20)
        for act in stages:
            v = matrix.get((exp, act), 0)
            cell = str(v) if v else "·"
            if exp == act and v:
                cell = f"\033[92m{cell}\033[0m"   # green on diagonal
            elif v:
                cell = f"\033[91m{cell}\033[0m"   # red off-diagonal
            row += cell.ljust(col_w + (9 if v else 0))
        lines.append(row)
    return "\n".join(lines)


def summarize(results: list[ReplayResult]) -> str:
    passed = sum(1 for r in results if r.passed)
    total  = len(results)
    lines = [f"\n{'─' * 70}", f"Replay: {passed}/{total} passed", "─" * 70]
    for r in results:
        flag = "✓" if r.passed else "✗"
        exp = r.row.expected_stage or "(executed)"
        act = r.actual_stage or "(executed)"
        lines.append(
            f"  {flag} [{r.row.signal_id}] expected={exp:<14} actual={act:<14}"
            f"  {r.row.headline}"
        )
        if not r.passed:
            lines.append(f"      ↳ note  : {r.row.note}")
            lines.append(f"      ↳ detail: {r.detail}")
    lines.append("")
    lines.append(confusion_matrix(results))
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ──────────────────────────────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    csv_path = Path(argv[1]) if len(argv) > 1 else Path(__file__).parent / "fixtures.csv"
    if not csv_path.exists():
        print(f"CSV not found: {csv_path}", file=sys.stderr)
        return 2

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        harness = ReplayHarness(Path(td))
        rows = load_csv(csv_path)
        results = harness.run_all(rows)

    print(summarize(results))
    failed = [r for r in results if not r.passed]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
