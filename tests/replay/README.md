# Replay Harness

A signal-replay harness for the Pantheon pipeline. Drop historical signals into
a CSV, declare which gate **should** kill each one, and run:

```bash
pytest tests/replay/test_replay.py -v            # CI-style
python -m tests.replay.harness                    # CLI with confusion matrix
python -m tests.replay.harness path/to/other.csv  # custom dataset
```

## Why this exists

The dashboard tells you *what happened* — N trades, X% approval rate. It does
not tell you *why* a signal died at one stage instead of another. When you
change `_MIN_SAMPLES` in Pythia or the VIX threshold in Artemis, you want to
know immediately whether the binding constraint for each signal moved.

This harness gives you that. Every row says: this signal SHOULD die at stage
`X` (or reach execution). The test fails if the actual binding constraint
shifts.

## How it works

It runs each signal through the **real** Hades / Artemis / Pythia / Ares-mock
agents, with two surgical stubs:

1. `yfinance.Ticker` is patched so VIX and price come from the CSV row, not
   the live market.
2. ZEUS's LLM call is replaced with the same confidence-floor (`>= 0.55`) it
   uses as a fallback when the LLM fails. This keeps ZEUS deterministic
   without inventing fake approvals.

Concentration is enforced inside the harness with `max_open_per_ticker = 1`,
matching `config/settings.json`.

## CSV schema

| column           | values                                                                                  |
| ---------------- | --------------------------------------------------------------------------------------- |
| `signal_id`      | free-form id, surfaces in test reports                                                  |
| `headline`       | signal text (Hades scans this for OFAC keywords)                                        |
| `category`       | `positive_news`, `earnings_surprise`, `regulatory_action`, `supplier_disruption`, etc.  |
| `severity`       | `1`..`4`                                                                                |
| `tickers`        | comma-separated, e.g. `NVDA` or `NVDA,AMD`                                              |
| `supplier`       | vendor name — Hades blocks RUSAL, Sberbank, etc.                                        |
| `vix`            | float — Artemis suppresses at extreme values                                            |
| `price`          | float — feeds Pythia sizing and Ares fill                                               |
| `expected_stage` | `hades` \| `trend` \| `pattern` \| `concentration` \| `zeus` \| empty (reach execution) |
| `note`           | human-readable rationale, shown on failure                                              |

## When to add a fixture

- Each time you change a gate (e.g. `_MIN_SAMPLES`, VIX thresholds,
  concentration caps), add a row exercising the boundary.
- Each time a real production kill surprises you, add it here so it can't
  regress silently.
- Each new compliance rule → at least one fixture that should die at Hades
  and one that should not.

## Limitations (honest)

- Apollo / KB / Supabase are out of scope — they are integration concerns and
  the harness is for *logic gates*.
- The ZEUS stand-in is a confidence floor, not the actual LLM. If you want to
  test prompt-driven behaviour, add a separate prompt-snapshot test.
- The yfinance patch returns a single flat price for everything that is not
  `^VIX`. Multi-bar logic (e.g. SPY 1-month return) gets `0.0` from this.
  Extend `_yfinance_factory` if a fixture needs a real series.
