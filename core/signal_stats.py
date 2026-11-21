"""
Signal frequency counters: how many setups FIRED vs were SKIPPED, and why.

One small in-memory helper shared by Icarus (core/fx_signals evaluations),
Ares (sizing / execution rejections) and ZEUS (risk-budget gate) without any
of them importing each other: they all record into the module-level
SIGNAL_STATS instance (or an injected SignalStats in tests).

Counters are kept per SOURCE ("icarus", "ares", "zeus", "pyramid") at two
scopes:
  - cycle:    reset by begin_cycle(source) at the start of each pipeline cycle
  - lifetime: since process start (or reset())

Skip reasons are the EXISTING human-readable strings (Evaluation.skipped,
BrokerRejectedError messages ...). classify_skip() maps them onto a short,
stable category for the summary line; the raw strings are not changed. No
dashboard, no persistence — read them in tests or from the log line:

    [ICARUS] signals: fired=1 skipped=5 (outside session=2, no breakout=2, min-lot overshoot=1)

Imports stdlib only.
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from typing import Optional

# (substring of the lower-cased reason, category). First match wins, so the
# more specific patterns come first.
_SKIP_RULES: tuple[tuple[str, str], ...] = (
    # Icarus / core.fx_signals Evaluation.skipped
    ("outside session",            "outside session"),
    ("no breakout",                "no breakout"),
    ("not stretched",              "not stretched"),
    ("over-stretched",             "over-stretched"),
    ("against trend",              "against trend"),
    ("allow_short=false",          "short disabled"),
    ("volatility blow-off",        "volatility veto"),
    ("volatility too",             "volatility veto"),
    ("not enough bars for atr",    "insufficient bars"),
    ("insufficient bars",          "insufficient bars"),
    ("stale data",                 "stale data"),
    ("zero price dispersion",      "zero dispersion"),
    ("data error",                 "data error"),
    ("no strategy configured",     "no strategy"),
    ("already emitted",            "duplicate"),
    # Ares / cTrader sizing (core.brokers.ctrader_instruments messages)
    ("below the venue minimum",    "min-lot overshoot"),
    ("esma cap",                   "ESMA cap"),
    ("conversion",                 "missing conversion"),
    ("position_size_pct=",         "zero size"),
    # ZEUS risk-budget gate
    ("risk budget",                "risk cap"),
    ("risk cap",                   "risk cap"),
)


def classify_skip(reason: Optional[str]) -> str:
    """Short, stable category for a skip reason (the raw text is unchanged)."""
    text = (reason or "").strip()
    low = text.lower()
    for needle, label in _SKIP_RULES:
        if needle in low:
            return label
    if not text:
        return "unknown"
    # Unknown reason: keep its head so it still groups sensibly.
    head = text.split(":")[0].split("(")[0].strip()
    return head[:40] or "unknown"


class SignalStats:
    """Fired / skipped counters per source, per cycle and per process lifetime."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cycle_fired:    Counter[str] = Counter()
        self._life_fired:     Counter[str] = Counter()
        self._cycle_skipped:  dict[str, Counter[str]] = {}
        self._life_skipped:   dict[str, Counter[str]] = {}

    # ── recording ───────────────────────────────────────────────────────────

    def begin_cycle(self, source: str) -> None:
        """Reset the CYCLE counters of one source (lifetime is kept)."""
        with self._lock:
            self._cycle_fired.pop(source, None)
            self._cycle_skipped.pop(source, None)

    def fired(self, source: str, n: int = 1) -> None:
        with self._lock:
            self._cycle_fired[source] += n
            self._life_fired[source] += n

    def skipped(self, source: str, reason: Optional[str], category: Optional[str] = None) -> str:
        """Count one skip; returns the category it was counted under."""
        cat = category or classify_skip(reason)
        with self._lock:
            self._cycle_skipped.setdefault(source, Counter())[cat] += 1
            self._life_skipped.setdefault(source, Counter())[cat] += 1
        return cat

    def reclassify_fired(self, source: str, reason: str, category: Optional[str] = None) -> str:
        """Turn one already-counted 'fired' into a skip (e.g. a de-duplicated
        setup Icarus will not forward). No-op on the fired side if it is 0."""
        with self._lock:
            if self._cycle_fired[source] > 0:
                self._cycle_fired[source] -= 1
            if self._life_fired[source] > 0:
                self._life_fired[source] -= 1
        return self.skipped(source, reason, category)

    def reset(self) -> None:
        with self._lock:
            self._cycle_fired.clear()
            self._life_fired.clear()
            self._cycle_skipped.clear()
            self._life_skipped.clear()

    # ── reading ─────────────────────────────────────────────────────────────

    def counts(self, source: str, scope: str = "cycle") -> dict:
        """{"fired": int, "skipped": int, "reasons": {category: count}}."""
        if scope not in ("cycle", "lifetime"):
            raise ValueError(f"scope must be 'cycle' or 'lifetime', got {scope!r}")
        with self._lock:
            fired = (self._cycle_fired if scope == "cycle" else self._life_fired).get(source, 0)
            reasons = dict((self._cycle_skipped if scope == "cycle" else self._life_skipped).get(source, {}))
        return {"fired": fired, "skipped": sum(reasons.values()), "reasons": reasons}

    def summary(self, source: str, scope: str = "cycle", fired_label: str = "fired") -> str:
        """'fired=1 skipped=5 (outside session=2, no breakout=2, min-lot overshoot=1)'."""
        c = self.counts(source, scope)
        line = f"{fired_label}={c['fired']} skipped={c['skipped']}"
        if c["reasons"]:
            ordered = sorted(c["reasons"].items(), key=lambda kv: (-kv[1], kv[0]))
            line += " (" + ", ".join(f"{k}={v}" for k, v in ordered) + ")"
        return line

    def has_activity(self, source: str, scope: str = "cycle") -> bool:
        c = self.counts(source, scope)
        return bool(c["fired"] or c["skipped"])

    def log_summary(self, logger: logging.Logger, source: str, prefix: str,
                    what: str = "signals", fired_label: str = "fired") -> str:
        """INFO line for the cycle, with the lifetime totals appended."""
        cycle = self.summary(source, "cycle", fired_label)
        life = self.counts(source, "lifetime")
        line = (f"{prefix} {what}: {cycle} | lifetime {fired_label}={life['fired']} "
                f"skipped={life['skipped']}")
        logger.info(line)
        return line


# Process-wide instance shared by Icarus / Ares / ZEUS.
SIGNAL_STATS = SignalStats()
